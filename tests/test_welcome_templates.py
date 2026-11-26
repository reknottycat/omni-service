"""Verify one scoped Feishu Docx is compiled to a frozen welcome in document order."""

import base64
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import quote

from omni_service.models import ChannelError
from omni_service.welcome_templates import FeishuWelcomeDocument


DOC_ID = "DocFixture000"
DOC_URL = f"https://team.feishu.cn/docx/{DOC_ID}?from=recent#optional-anchor"
IMAGE_ID = "ImageFixture001"
APP_ID = "cli_test_fixture"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFf8AAAAASUVORK5CYII="
PNG = base64.b64decode(PNG_B64)


def text_block(block_id, block_type, field, text, *, parent_id=DOC_ID, children=(), style=None):
    return {"block_type": block_type, "block_id": block_id, "parent_id": parent_id,
            "children": list(children), field: {"elements": [{"text_run": {
                "content": text, "text_element_style": style or {},
            }}]}}


def root_block(*ids):
    return {"block_type": 1, "block_id": DOC_ID, "children": list(ids),
            "page": {"elements": [{"text_run": {"content": "Private admin title"}}]}}


def image_block():
    return {"block_type": 27, "block_id": "imageBlock", "parent_id": DOC_ID,
            "image": {"token": IMAGE_ID, "caption": {"content": "运输路线示意"}}}


class FeishuCLI:
    """Write the CLI's real success-envelope and media file to documented outputs."""

    def __init__(self, pages, *, revisions=(18, 18), app_id=APP_ID, image=PNG):
        self.pages = pages
        self.revisions = iter(revisions)
        self.app_id = app_id
        self.image = image
        self.calls = []

    def run(self, argv, *, stdout, cwd, **_kwargs):
        self.calls.append(list(argv))
        if argv[0:2] != ["lark-cli", "--profile=welcome-test"] or argv[argv.index("--as") + 1] != "bot":
            raise AssertionError("The compiler must use the configured Feishu bot.")
        path = f"/open-apis/docx/v1/documents/{DOC_ID}"
        data = None
        if argv[2:4] == ["whoami", "--as"]:
            result = {"identity": "bot", "appId": self.app_id, "brand": "feishu", "available": True}
        elif argv[2:5] == ["api", "GET", path]:
            data = {"document": {"document_id": DOC_ID, "title": "Private admin title",
                                 "revision_id": next(self.revisions)}}
        elif argv[2:5] == ["api", "GET", path + "/blocks"]:
            query = json.loads(argv[argv.index("--params") + 1])
            if query["document_revision_id"] != -1:
                raise AssertionError("Historical revisions would require write permission.")
            data = self.pages[query.get("page_token", "")]
        elif argv[2:4] == ["docs", "+media-download"]:
            token = argv[argv.index("--token") + 1]
            if token != IMAGE_ID or Path(argv[argv.index("--output") + 1]).is_absolute() or not cwd:
                raise AssertionError("Only the named document image may be fetched into a private temp directory.")
            output = Path(cwd) / argv[argv.index("--output") + 1]
            output.write_bytes(self.image)
            data = {"file_token": token}
        else:
            raise AssertionError("Compiler attempted an unexpected CLI operation.")
        if data is not None:
            result = {"identity": "bot", "ok": True, "data": data}
        stdout.write(json.dumps(result).encode())
        return subprocess.CompletedProcess(argv, 0)


class WelcomeDocumentTest(unittest.TestCase):
    def setUp(self):
        self.reader = FeishuWelcomeDocument(APP_ID, profile="welcome-test")

    def test_compiles_ordered_rich_text_and_image_from_revision_checked_pages(self):
        url = "https://example.test/terms?q=%2F&actual=2"
        header = text_block("heading1", 3, "heading1", "Добро пожаловать <partner>")
        linked = text_block("linked01", 2, "text", "Условия", style={"link": {"url": quote(url, safe="")}, "bold": True})
        bullet = text_block("bullet01", 12, "bullet", "Расскажите о грузе", children=("nested01",))
        nested = text_block("nested01", 12, "bullet", "Вес", parent_id="bullet01", style={"italic": True})
        first = text_block("ordered1", 13, "ordered", "Откуда?")
        second = text_block("ordered2", 13, "ordered", "Куда?")
        closing = text_block("closing1", 2, "text", "Спасибо & до связи", style={"underline": True})
        root = root_block("heading1", "linked01", "bullet01", "ordered1", "ordered2", "imageBlock", "closing1")
        cli = FeishuCLI({
            "": {"items": [root, linked, second, header, nested], "has_more": True, "page_token": "next-page"},
            "next-page": {"items": [closing, bullet, image_block(), first], "has_more": False},
        })
        with patch("omni_service.welcome_templates.subprocess.run", side_effect=cli.run):
            result = self.reader.read(DOC_URL)
        self.assertEqual(result, {"items": [
            {"type": "text",
             "text": ("Добро пожаловать <partner>\nУсловия (https://example.test/terms?q=%2F&actual=2)\n"
                      "• Расскажите о грузе\n  • Вес\n1. Откуда?\n2. Куда?"),
             "html": ('<b>Добро пожаловать &lt;partner&gt;</b>\n<a href="https://example.test/terms?q=%2F&amp;actual=2">'
                      "<b>Условия</b></a>\n• Расскажите о грузе\n  • <i>Вес</i>\n1. Откуда?\n2. Куда?")},
            {"type": "image", "mime": "image/png", "data": PNG_B64, "alt": "运输路线示意"},
            {"type": "text", "text": "Спасибо & до связи", "html": "<u>Спасибо &amp; до связи</u>"},
        ]})
        self.assertEqual([argv[2:4] for argv in cli.calls], [
            ["whoami", "--as"], ["api", "GET"], ["api", "GET"],
            ["api", "GET"], ["docs", "+media-download"], ["api", "GET"],
        ])

    def test_rejects_revision_changed_during_capture(self):
        cli = FeishuCLI({"": {"items": [root_block("imageBlock"), image_block()], "has_more": False}},
                         revisions=(18, 19))
        with patch("omni_service.welcome_templates.subprocess.run", side_effect=cli.run):
            with self.assertRaisesRegex(ChannelError, "changed") as error:
                self.reader.read(DOC_ID)
        self.assertEqual(error.exception.code, "welcome_document_changed")
        self.assertTrue(any("+media-download" in call for call in cli.calls))

    def test_rejects_unsupported_blocks_invalid_links_and_invalid_images(self):
        cases = (
            ({"block_type": 31, "block_id": "unknown0", "parent_id": DOC_ID}, PNG, "welcome_document_unsupported"),
            (text_block("badLink01", 2, "text", "link", style={"link": {"url": "javascript:alert(1)"}}),
             PNG, "welcome_document_unsupported"),
            (image_block(), b"this is not a PNG", "welcome_image_invalid"),
        )
        for block, data, expected in cases:
            with self.subTest(kind=block["block_type"]):
                cli = FeishuCLI({"": {"items": [root_block(block["block_id"]), block], "has_more": False}}, image=data)
                with patch("omni_service.welcome_templates.subprocess.run", side_effect=cli.run):
                    with self.assertRaises(ChannelError) as error:
                        self.reader.read(DOC_URL)
                self.assertEqual(error.exception.code, expected)

    def test_no_cli_access_for_unscoped_urls_or_matching_document_for_wrong_profile(self):
        with patch("omni_service.welcome_templates.subprocess.run") as runner:
            for url in (f"https://feishu.cn.example/docx/{DOC_ID}", f"http://localhost/docx/{DOC_ID}",
                        f"https://user@team.feishu.cn/docx/{DOC_ID}", f"https://team.feishu.cn/wiki/{DOC_ID}"):
                with self.subTest(url=url):
                    with self.assertRaises(ChannelError) as error:
                        self.reader.read(url)
                    self.assertEqual(error.exception.code, "welcome_document_invalid")
            runner.assert_not_called()
        cli = FeishuCLI({}, app_id="cli_unexpected")
        with patch("omni_service.welcome_templates.subprocess.run", side_effect=cli.run):
            with self.assertRaises(ChannelError) as error:
                self.reader.read(DOC_URL)
        self.assertEqual(error.exception.code, "welcome_document_identity_mismatch")
        self.assertEqual(len(cli.calls), 1)
