"""Read one Feishu Docx and compile a frozen Telegram welcome."""

import base64
from html import escape
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .models import ChannelError


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}")
_APP_ID = re.compile(r"cli_[A-Za-z0-9_]+")
_BLOCK_FAMILIES = {
    2: "text", **{level + 2: f"heading{level}" for level in range(1, 10)},
    12: "bullet", 13: "ordered",
}
_MAX_BLOCKS = 256
_MAX_IMAGES = 6
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_TOTAL = 16 * 1024 * 1024
_MAX_ITEMS = 32
_MAX_TEXT_UNITS = 3800
_MAX_TOTAL_TEXT_UNITS = 16000
_MAX_JSON_BYTES = 2 * 1024 * 1024


def _document_id(reference):
    if not isinstance(reference, str) or not reference or len(reference) > 2048:
        raise ChannelError("welcome_document_invalid")
    if _TOKEN.fullmatch(reference):
        return reference
    try:
        parsed = urlsplit(reference)
        host = parsed.hostname or ""
        valid = (parsed.scheme == "https" and (host == "feishu.cn" or host.endswith(".feishu.cn"))
                 and parsed.username is None and parsed.password is None and parsed.port in (None, 443)
                 and not any(char.isspace() or ord(char) < 32 for char in reference))
    except ValueError:
        valid = False
    match = re.fullmatch(r"/docx/([A-Za-z0-9][A-Za-z0-9_-]{3,127})/?", parsed.path) if valid else None
    if match is None:
        raise ChannelError("welcome_document_invalid", "Use a Feishu Docx URL or document ID.")
    return match[1]


def _text_units(value):
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise ChannelError("welcome_document_invalid") from None


def _styled_text(content, style, *, heading=False):
    if (not isinstance(content, str) or not isinstance(style, dict)
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", content)):
        raise ChannelError("welcome_document_invalid")
    html = escape(content, quote=False)
    if style.get("inline_code") is True:
        if style.get("link") is not None:
            raise ChannelError("welcome_document_unsupported", "Linked inline code is unsupported.")
        return content, f"<code>{html}</code>" if html else ""
    for key, tag in (("italic", "i"), ("underline", "u"),
                     ("strikethrough", "s"), ("bold", "b")):
        if html and (style.get(key) is True or (heading and tag == "b")):
            html = f"<{tag}>{html}</{tag}>"
    link = style.get("link")
    if link is not None:
        url = link.get("url") if isinstance(link, dict) else None
        try:
            # Docx rich-text hyperlinks may encode the entire URL. Decode that
            # wrapper once, leaving any escapes in the actual URL intact.
            if isinstance(url, str) and re.match(r"https?%3a%2f%2f", url, re.I):
                url = unquote(url, errors="strict")
            parsed = urlsplit(url) if isinstance(url, str) else None
            allowed = (parsed is not None and parsed.scheme in ("http", "https") and parsed.hostname
                       and parsed.username is None and parsed.password is None
                       and (parsed.port is None or 0 < parsed.port <= 65535)
                       and len(url) <= 2048 and not re.search(r"[\s\x00-\x1f\x7f<>\\]", url))
        except (ValueError, UnicodeError):
            allowed = False
        if not allowed:
            raise ChannelError("welcome_document_unsupported", "Only HTTP and HTTPS links are supported.")
        html = f'<a href="{escape(url)}">{html or escape(url, quote=False)}</a>'
        content = f"{content} ({url})" if content and content != url else url
    return content, html


def _render_block(block, kind):
    value = block.get(kind)
    if not isinstance(value, dict) or not isinstance(value.get("elements", []), list):
        raise ChannelError("welcome_document_invalid")
    plain, rich = [], []
    for element in value.get("elements", []):
        if not isinstance(element, dict):
            raise ChannelError("welcome_document_invalid")
        variants = [key for key, val in element.items() if val is not None]
        run = element.get("text_run")
        preview = element.get("inline_link_preview")
        if variants == ["text_run"] and isinstance(run, dict):
            text, html = _styled_text(run.get("content"), run.get("text_element_style") or {},
                                      heading=kind.startswith("heading"))
        elif variants == ["inline_link_preview"] and isinstance(preview, dict):
            url, title = preview.get("url"), preview.get("title") or ""
            text, html = _styled_text(title, {"link": {"url": url}}, heading=kind.startswith("heading"))
        else:
            raise ChannelError("welcome_document_unsupported", "Unsupported inline element in welcome document.")
        plain.append(text)
        rich.append(html)
    return "".join(plain), "".join(rich)


def _jpeg_dimensions(data):
    start = 2
    size_markers = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    while start + 4 <= len(data) and data[start] == 0xff:
        while start < len(data) and data[start] == 0xff:
            start += 1
        if start + 3 > len(data):
            break
        marker = data[start]
        length = int.from_bytes(data[start + 1:start + 3], "big")
        end = start + length + 1
        if length < 2 or end > len(data) or marker in (0xd9, 0xda):
            break
        if marker in size_markers and length >= 8:
            height = int.from_bytes(data[start + 4:start + 6], "big")
            width = int.from_bytes(data[start + 6:start + 8], "big")
            return width, height
        start = end
    return 0, 0


def _image_mime(data):
    if (len(data) >= 45 and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        width, height = _jpeg_dimensions(data)
        mime = "image/jpeg"
    else:
        raise ChannelError("welcome_image_invalid", "The welcome image must be PNG or JPEG.")
    if not width or not height or width + height > 10000 or max(width, height) > 20 * min(width, height):
        raise ChannelError("welcome_image_invalid", "The welcome image dimensions are unsupported.")
    return mime


class FeishuWelcomeDocument:
    """Read only the specified bot-visible document and its inline image tokens."""

    def __init__(self, app_id, *, profile=None, cli="lark-cli"):
        if (not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id)
                or (profile is not None and (not isinstance(profile, str) or not profile))):
            raise ChannelError("welcome_document_invalid_config")
        self.app_id = app_id
        self._command = [cli, f"--profile={profile or app_id}"]
        self._env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                     "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}

    def _run(self, args, *, deadline, cwd=None, error="welcome_document_unavailable"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChannelError(error)
        # Do not absorb arbitrary CLI output in memory or log provider diagnostics.
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(self._command + args, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=subprocess.DEVNULL, cwd=cwd, env=self._env,
                                        timeout=min(15, remaining), check=False)
                output.seek(0)
                raw = output.read(_MAX_JSON_BYTES + 1)
        except (OSError, subprocess.TimeoutExpired):
            raise ChannelError(error) from None
        if result.returncode:
            raise ChannelError(error)
        if len(raw) > _MAX_JSON_BYTES:
            raise ChannelError("welcome_document_too_large")
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError, RecursionError):
            raise ChannelError(error) from None
        if not isinstance(result, dict):
            raise ChannelError(error)
        return result

    def _get(self, path, deadline, **params):
        args = ["api", "GET", path, "--as", "bot", "--format", "json"]
        if params:
            args += ["--params", json.dumps(params)]
        result = self._run(args, deadline=deadline)
        data = result.get("data")
        if result.get("ok") is not True or result.get("identity") != "bot" or not isinstance(data, dict):
            raise ChannelError("welcome_document_unavailable")
        return data

    def _revision(self, path, document_id, deadline):
        document = self._get(path, deadline).get("document")
        revision = document.get("revision_id") if isinstance(document, dict) else None
        if type(revision) is not int or revision < 1 or document.get("document_id") != document_id:
            raise ChannelError("welcome_document_invalid")
        return revision

    def _blocks(self, path, deadline):
        blocks, tokens = {}, set()
        params = {"document_revision_id": -1, "page_size": 100}
        for _ in range(5):
            page = self._get(path + "/blocks", deadline, **params)
            items, has_more = page.get("items"), page.get("has_more")
            if not isinstance(items, list) or type(has_more) is not bool:
                raise ChannelError("welcome_document_invalid")
            if len(blocks) + len(items) > _MAX_BLOCKS:
                raise ChannelError("welcome_document_too_large")
            for block in items:
                ident = block.get("block_id") if isinstance(block, dict) else None
                if not isinstance(ident, str) or not _TOKEN.fullmatch(ident) or ident in blocks:
                    raise ChannelError("welcome_document_invalid")
                blocks[ident] = block
            if not has_more:
                return blocks
            token = page.get("page_token")
            if not isinstance(token, str) or not 0 < len(token) <= 1024 or token in tokens:
                raise ChannelError("welcome_document_invalid")
            tokens.add(token)
            params["page_token"] = token
        raise ChannelError("welcome_document_too_large")

    def _download(self, token, deadline):
        with tempfile.TemporaryDirectory(prefix="omni-welcome-") as directory:
            filename = "image.bin"
            result = self._run(["docs", "+media-download", "--as", "bot", "--format", "json",
                                "--type", "media", "--token", token, "--output", filename],
                               deadline=deadline, cwd=directory, error="welcome_image_unavailable")
            if result.get("ok") is not True or result.get("identity") != "bot":
                raise ChannelError("welcome_image_unavailable")
            try:
                with (Path(directory) / filename).open("rb") as image:
                    data = image.read(_MAX_IMAGE_BYTES + 1)
            except OSError:
                raise ChannelError("welcome_image_unavailable") from None
        if len(data) > _MAX_IMAGE_BYTES:
            raise ChannelError("welcome_document_too_large")
        return data

    def _compile(self, document_id, blocks, deadline):
        items, seen, assets = [], {document_id}, {}
        plaintext, richtext = [], []
        count_images, media_total, text_total = 0, 0, 0

        def add_item(item):
            if len(items) >= _MAX_ITEMS:
                raise ChannelError("welcome_document_too_large")
            items.append(item)

        def flush_text():
            text = "\n".join(plaintext).strip("\n")
            if text.strip():
                add_item({"type": "text", "text": text, "html": "\n".join(richtext).strip("\n")})
            plaintext.clear()
            richtext.clear()

        def append_text(text, html):
            nonlocal text_total
            units = _text_units(text)
            text_total += units + 1
            if units > _MAX_TEXT_UNITS or text_total > _MAX_TOTAL_TEXT_UNITS or len(html) > 24000:
                raise ChannelError("welcome_document_too_large")
            if (plaintext and (_text_units("\n".join(plaintext)) + units + 1 > _MAX_TEXT_UNITS
                              or sum(map(len, richtext)) + len(html) > 24000)):
                flush_text()
            plaintext.append(text)
            richtext.append(html)

        def append_image(block):
            nonlocal count_images, media_total
            image = block.get("image")
            token = image.get("token") if isinstance(image, dict) else None
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ChannelError("welcome_image_invalid")
            caption = image.get("caption")
            alt = caption.get("content", "") if isinstance(caption, dict) else ""
            if not isinstance(alt, str) or _text_units(alt) > 1000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", alt):
                raise ChannelError("welcome_image_invalid")
            count_images += 1
            if count_images > _MAX_IMAGES:
                raise ChannelError("welcome_document_too_large")
            if token not in assets:
                data = self._download(token, deadline)
                assets[token] = (_image_mime(data), base64.b64encode(data).decode("ascii"), len(data))
            mime, encoded, size = assets[token]
            media_total += size
            if media_total > _MAX_IMAGE_TOTAL:
                raise ChannelError("welcome_document_too_large")
            flush_text()
            add_item({"type": "image", "mime": mime, "data": encoded, "alt": alt})

        def walk(parent_id, depth=0):
            children = blocks[parent_id].get("children") or []
            if not isinstance(children, list) or depth > 12:
                raise ChannelError("welcome_document_invalid")
            index = 0
            for ident in children:
                if not isinstance(ident, str) or ident in seen or ident not in blocks:
                    raise ChannelError("welcome_document_invalid")
                seen.add(ident)
                block = blocks[ident]
                if block.get("parent_id") not in (None, parent_id):
                    raise ChannelError("welcome_document_invalid")
                block_type = block.get("block_type")
                if type(block_type) is not int:
                    raise ChannelError("welcome_document_invalid")
                index = index + 1 if block_type == 13 else 0
                if block_type == 27:
                    if block.get("children"):
                        raise ChannelError("welcome_document_unsupported")
                    append_image(block)
                elif block_type in _BLOCK_FAMILIES:
                    text, html = _render_block(block, _BLOCK_FAMILIES[block_type])
                    prefix = "  " * depth + (f"{index}. " if index else "• ") if block_type in (12, 13) else ""
                    append_text(prefix + text, prefix + html)
                    walk(ident, depth + 1)
                else:
                    raise ChannelError("welcome_document_unsupported", f"Unsupported Docx block type {block_type}.")

        root = blocks.get(document_id)
        if not root or root.get("block_type") != 1:
            raise ChannelError("welcome_document_invalid")
        walk(document_id)
        flush_text()
        if not items or seen != set(blocks):
            raise ChannelError("welcome_document_invalid")
        return {"items": items}

    def read(self, document_url):
        document_id = _document_id(document_url)
        path = f"/open-apis/docx/v1/documents/{document_id}"
        deadline = time.monotonic() + 70
        bot = self._run(["whoami", "--as", "bot"], deadline=deadline)
        if (bot.get("appId") != self.app_id or bot.get("identity") != "bot"
                or bot.get("brand") != "feishu" or bot.get("available") is not True):
            raise ChannelError("welcome_document_identity_mismatch")
        # Reading a historical revision would require edit rights. Instead check
        # the latest revision again after both the block and image passes.
        revision = self._revision(path, document_id, deadline)
        template = self._compile(document_id, self._blocks(path, deadline), deadline)
        if self._revision(path, document_id, deadline) != revision:
            raise ChannelError("welcome_document_changed", "The welcome document changed while reading it.")
        return template
