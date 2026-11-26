"""Opt-in Telegram Business Bot API transport and acknowledged polling."""

import base64
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit

from .media import MAX_FILE_BYTES, MAX_IMAGE_BYTES, MediaBlob, normalize_blob, safe_filename
from .models import ChannelError
from .personal import Connection, valid_file_id


_MAX_WELCOME_PHOTO_BYTES = 8 * 1024 * 1024
_WELCOME_PHOTOS = {
    "image/png": ("welcome.png", b"\x89PNG\r\n\x1a\n", b"\x00\x00\x00\x00IEND\xaeB`\x82"),
    "image/jpeg": ("welcome.jpg", b"\xff\xd8\xff", b"\xff\xd9"),
}
_FILE_PATH = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")
_SETUP_NONCE = re.compile(r"[A-Za-z0-9_-]{22,64}")


def telegram_ready(api, username=None):
    """Resolve the bot's identity and ensure its long-polling setup is available."""
    if username is not None and (not isinstance(username, str) or not _BOT_USERNAME.fullmatch(username)):
        raise ChannelError("invalid_bot_username")
    bot = api.call("getMe")
    if (not isinstance(bot, dict) or bot.get("is_bot") is not True
            or type(bot.get("id")) is not int or not 0 < bot["id"] < 2**52
            or not isinstance(bot.get("username"), str) or not _BOT_USERNAME.fullmatch(bot["username"])
            or username is not None and bot["username"].casefold() != username.casefold()):
        raise ChannelError("telegram_bot_identity_mismatch")
    webhook = api.call("getWebhookInfo")
    if not isinstance(webhook, dict) or webhook.get("url") != "":
        raise ChannelError("telegram_webhook_in_use")
    return bot


def enable_secretary(api, bot):
    """Enable a dedicated bot resolved by telegram_ready and verify its new settings."""
    if bot.get("can_connect_to_business") is True:
        return bot
    if api.call("setMySettings", {"can_connect_to_business": True, "can_join_groups": False}) is not True:
        raise ChannelError("telegram_secretary_not_confirmed")
    current = telegram_ready(api, bot["username"])
    if current["id"] != bot["id"] or current.get("can_connect_to_business") is not True:
        raise ChannelError("telegram_secretary_not_confirmed")
    return current


def telegram_setup_link(username, nonce):
    """Build a Telegram /start link for a short-lived setup challenge."""
    if (not isinstance(username, str) or not _BOT_USERNAME.fullmatch(username)
            or not isinstance(nonce, str) or not _SETUP_NONCE.fullmatch(nonce)):
        raise ChannelError("invalid_telegram_setup_link")
    return f"https://t.me/{username}?start={nonce}"


class TelegramAPI:
    _METHODS = frozenset({
        "getMe", "getWebhookInfo", "getBusinessConnection", "getUpdates",
        "getFile", "sendMessage", "deleteBusinessMessages", "setMySettings",
    })

    def __init__(self, token, *, proxy_url=None, tunnel_port=None):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ChannelError("invalid_telegram_token")
        self._token = token
        if tunnel_port is not None and (type(tunnel_port) is not int or not 1 <= tunnel_port <= 65535
                                        or proxy_url is not None):
            raise ChannelError("invalid_telegram_tunnel")
        self._tunnel_port = tunnel_port
        self._proxy = None
        if proxy_url is not None:
            try:
                proxy = urlsplit(proxy_url)
                host = "127.0.0.1" if proxy.hostname == "localhost" else proxy.hostname
                address, port = ipaddress.ip_address(host), proxy.port
                private = (address.is_loopback or address.is_private
                           or address in ipaddress.ip_network("100.64.0.0/10"))
                if (proxy.scheme != "http" or not private or not port or proxy.username is not None
                        or proxy.password is not None or proxy.path not in ("", "/")
                        or proxy.query or proxy.fragment):
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise ChannelError("invalid_telegram_proxy") from None
            self._proxy = str(address), port

    def call(self, method, payload=None):
        if method not in self._METHODS:
            raise ChannelError("invalid_telegram_method")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ChannelError("invalid_payload")
        poll_timeout = payload.get("timeout", 0) if method == "getUpdates" else 0
        if type(poll_timeout) is not int or not 0 <= poll_timeout <= 50:
            raise ChannelError("invalid_poll_timeout")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeError):
            raise ChannelError("invalid_payload") from None
        return self._request(method, body, content_type="application/json", timeout=poll_timeout + 10)

    def send_welcome_photo(self, connection_id, chat_id, photo, *, caption=''):
        """Upload a validated image from a frozen welcome snapshot."""
        if (not isinstance(photo, dict) or set(photo) != {"type", "data", "mime", "alt"}
                or photo["type"] != "image" or not isinstance(photo["alt"], str)):
            raise ChannelError("invalid_payload")
        mime, encoded = photo["mime"], photo["data"]
        encoded_limit = 4 * ((_MAX_WELCOME_PHOTO_BYTES + 2) // 3)
        if (not isinstance(mime, str) or mime not in _WELCOME_PHOTOS or not isinstance(encoded, str)
                or not 0 < len(encoded) <= encoded_limit):
            raise ChannelError("invalid_payload")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ChannelError("invalid_payload") from None
        filename, start, end = _WELCOME_PHOTOS[mime]
        if (not 0 < len(data) <= _MAX_WELCOME_PHOTO_BYTES
                or not data.startswith(start) or not data.endswith(end)):
            raise ChannelError("invalid_payload")
        return self._send_media("sendPhoto", "photo", connection_id, chat_id,
                                MediaBlob(data, filename, mime), caption, timeout=10)

    def download_attachment(self, remote_id, *, kind, filename=""):
        """Resolve a Telegram file ID and download it through the fixed TLS endpoint."""
        if not valid_file_id(remote_id) or kind not in ("image", "file", "audio"):
            raise ChannelError("invalid_payload")
        try:
            resource = self.call("getFile", {"file_id": remote_id})
        except ChannelError as exc:
            if exc.code in ("authorization_failed", "rate_limited"):
                raise
            raise ChannelError("media_source_unavailable") from None
        path = resource.get("file_path") if isinstance(resource, dict) else None
        if (not isinstance(path, str) or len(path) > 1024 or not _FILE_PATH.fullmatch(path)
                or any(segment in (".", "..") for segment in path.split("/"))):
            raise ChannelError("media_source_unavailable")
        limit = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
        size = resource.get("file_size")
        if type(size) is int and size > limit:
            raise ChannelError("media_too_large")
        data, content_type = self._download(path, limit)
        if type(size) is int and size != len(data):
            raise ChannelError("media_source_unavailable")
        name = safe_filename(filename, fallback=path.rsplit("/", 1)[-1])
        return normalize_blob(MediaBlob(data, name, content_type), kind)

    def send_attachment(self, connection_id, chat_id, kind, blob, *, caption=""):
        """Send a relayed attachment as the connected account."""
        blob = normalize_blob(blob, kind)
        if kind == "image" and blob.mime in _WELCOME_PHOTOS:
            return self._send_media("sendPhoto", "photo", connection_id, chat_id, blob, caption, timeout=60)
        return self._send_media("sendDocument", "document", connection_id, chat_id, blob, caption, timeout=60)

    def _send_media(self, method, field, connection_id, chat_id, blob, caption, *, timeout):
        if (not isinstance(connection_id, str) or not connection_id.strip() or len(connection_id) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in connection_id)
                or type(chat_id) is not int or not 0 < chat_id < 2**63
                or not isinstance(caption, str) or len(caption) > 1024 or "\x00" in caption):
            raise ChannelError("invalid_payload")
        try:
            connection_bytes = connection_id.encode()
            caption_bytes = caption.encode()
            caption_units = len(caption.encode("utf-16-le")) // 2
        except UnicodeError:
            raise ChannelError("invalid_payload") from None
        if caption_units > 1024:
            raise ChannelError("invalid_payload")

        boundary = "omni-" + secrets.token_hex(24)
        delimiter = ("--" + boundary).encode("ascii")
        fields = [("business_connection_id", connection_bytes), ("chat_id", str(chat_id).encode("ascii"))]
        if caption_bytes:
            fields.append(("caption", caption_bytes))
        if field == "document":
            fields.append(("disable_content_type_detection", b"true"))
        parts = []
        for name, value in fields:
            parts.extend((delimiter, f'Content-Disposition: form-data; name="{name}"'.encode("ascii"),
                          b"", value))
        disposition = f'Content-Disposition: form-data; name="{field}"; filename="{blob.name}"'.encode()
        parts.extend((delimiter, disposition, f"Content-Type: {blob.mime}".encode("ascii"),
                      b"", blob.data, delimiter + b"--", b""))
        return self._request(method, b"\r\n".join(parts),
                             content_type="multipart/form-data; boundary=" + boundary, timeout=timeout)

    def _connection(self, timeout):
        connection = http.client.HTTPSConnection(*(self._proxy or ("api.telegram.org",)), timeout=timeout)
        try:
            if self._proxy:
                connection.set_tunnel("api.telegram.org", 443)
            if self._tunnel_port is not None:
                # The SSH forward carries the original Telegram TLS stream.
                # Verify Telegram's certificate and send its hostname as SNI.
                raw_socket = socket.create_connection(("127.0.0.1", self._tunnel_port), timeout)
                try:
                    connection.sock = ssl.create_default_context().wrap_socket(
                        raw_socket, server_hostname="api.telegram.org")
                except BaseException:
                    raw_socket.close()
                    raise
            return connection
        except BaseException:
            connection.close()
            raise

    def _download(self, path, limit):
        connection = None
        try:
            connection = self._connection(60)
            connection.request("GET", f"/file/bot{self._token}/{path}")
            response = connection.getresponse()
            if response.status in (401, 403):
                raise ChannelError("authorization_failed")
            if response.status == 429:
                raise ChannelError("rate_limited", retry_after=self._retry_after(response))
            if response.status != 200:
                raise ChannelError("media_source_unavailable")
            advertised = response.getheader("Content-Length")
            length = int(advertised) if advertised is not None else None
            if length is not None and length > limit:
                raise ChannelError("media_too_large")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ChannelError("media_too_large")
            if not data or (length is not None and length != len(data)):
                raise ChannelError("media_source_unavailable")
            return data, response.getheader("Content-Type") or "application/octet-stream"
        except (OSError, http.client.HTTPException, ValueError):
            raise ChannelError("media_source_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _retry_after(response):
        try:
            delay = int(response.getheader("Retry-After") or 1)
        except (TypeError, ValueError):
            delay = 1
        return max(1, min(delay, 3600))

    def _request(self, method, body, *, content_type, timeout):
        connection = None
        try:
            connection = self._connection(timeout)
            connection.request("POST", f"/bot{self._token}/{method}", body=body,
                               headers={"Content-Type": content_type})
            response = connection.getresponse()
            raw = response.read(8_000_001)
            status = response.status
            if len(raw) > 8_000_000 or status >= 500 or 300 <= status < 400:
                raise ChannelError("delivery_unknown")
            if status in (401, 403):
                raise ChannelError("authorization_failed")
            if status == 409 and method == "getUpdates":
                raise ChannelError("telegram_poll_conflict")
            if status == 429:
                delay = self._retry_after(response)
                try:
                    data = json.loads(raw)
                    delay = int(data.get("parameters", {}).get("retry_after", delay))
                except (AttributeError, TypeError, ValueError):
                    pass
                raise ChannelError("rate_limited", retry_after=max(1, min(delay, 3600)))
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ChannelError("delivery_unknown")
            if data.get("ok") is False and 400 <= status < 500:
                raise ChannelError("platform_rejected")
            if status != 200 or data.get("ok") is not True or "result" not in data:
                raise ChannelError("delivery_unknown")
            return data["result"]
        except (OSError, http.client.HTTPException, ValueError):
            # Exception strings can contain the path, which includes the token.
            raise ChannelError("delivery_unknown") from None
        finally:
            if connection is not None:
                connection.close()


class TelegramPoller:
    """One consumer per dedicated bot. Commit service writes before ack()."""

    # After a week of inactivity Telegram can issue a lower update ID.
    # Retained updates expire after 24 hours, so stop using an old offset early.
    _CURSOR_MAX_AGE = 5 * 86400

    def __init__(self, api, policy, store, connection_id, *, clock=time.time):
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ChannelError("invalid_connection")
        if not policy.connection_scope and not policy.allowed.difference({policy.owner_id}):
            raise ChannelError("empty_telegram_allowlist")
        self.api = api
        self.policy = policy
        self.store = store
        self.connection_id = connection_id
        self.clock = clock
        self._pending = None
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_business_cursor (
                connection_id TEXT PRIMARY KEY,
                next_offset INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )""")

    def refresh(self):
        """Fetch current Telegram authorization. Failed fetch clears local rights."""
        self.policy.connections.pop(self.connection_id, None)
        data = self.api.call("getBusinessConnection", {"business_connection_id": self.connection_id})
        connection = Connection.from_update(data)
        if connection.connection_id != self.connection_id or connection.owner_id != self.policy.owner_id:
            raise ChannelError("invalid_connection")
        self.policy.connections[self.connection_id] = connection
        return connection

    def poll(self, *, timeout=30):
        self._pending = None
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise ChannelError("invalid_poll_timeout")
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT next_offset, updated_at FROM telegram_business_cursor WHERE connection_id=?",
                (self.connection_id,),
            ).fetchone()
        age = self.clock() - row["updated_at"] if row else None
        offset = row["next_offset"] if age is not None and 0 <= age < self._CURSOR_MAX_AGE else 0
        method, payload = self.policy.poll_payload(offset)
        payload.update(timeout=timeout, limit=25)
        updates = self.api.call(method, payload)
        if not isinstance(updates, list) or any(
            not isinstance(update, dict) or type(update.get("update_id")) is not int
            or not 0 < update["update_id"] < 2**63 - 1 for update in updates
        ):
            raise ChannelError("invalid_telegram_updates")
        updates = sorted((update for update in updates if update["update_id"] >= offset),
                         key=lambda update: update["update_id"])
        ids = [update["update_id"] for update in updates]
        if len(set(ids)) != len(ids):
            raise ChannelError("invalid_telegram_updates")
        if not updates:
            return []
        current = self.refresh()
        events = []
        if current.enabled:
            for update in updates:
                # Telegram can return queued updates that precede allowed_updates.
                # Route only the configured business connection, regardless of queue.
                if "business_connection" in update:
                    continue
                for key in self.policy.allowed_updates[1:]:
                    data = update.get(key)
                    if isinstance(data, dict) and data.get("business_connection_id") == self.connection_id:
                        events.extend(self.policy.parse({"update_id": update["update_id"], key: data}))
                        break
        self._pending = ids[-1] + 1, self.clock()
        return events

    def ack(self):
        """Advance the durable offset after the caller has persisted the batch."""
        if self._pending is None:
            return
        with self.store.transaction() as db:
            db.execute("""INSERT INTO telegram_business_cursor
                (connection_id, next_offset, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                next_offset = excluded.next_offset, updated_at = excluded.updated_at""",
                       (self.connection_id, *self._pending))
        self._pending = None
