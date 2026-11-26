import base64
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from omni_service.media import MAX_FILE_BYTES, MAX_IMAGE_BYTES, MediaBlob
from omni_service.models import ChannelError
from omni_service.personal import TelegramPersonal
from omni_service.store import Store
from omni_service.telegram_live import TelegramAPI, TelegramPoller


PNG_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFf8AAAAASUVORK5CYII="


def message(update_id, *, chat=20, sender=20, connection_id="conn-1"):
    return {"update_id": update_id, "business_message": {
        "business_connection_id": connection_id,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": sender, "is_bot": False},
        "date": 1000, "message_id": update_id, "text": "Sample",
    }}


class FakeTelegram:
    def __init__(self):
        self.updates = []
        self.requests = []
        self.connection = {"id": "conn-1", "user": {"id": 10}, "is_enabled": True,
                           "rights": {"can_reply": True}}

    def call(self, method, payload):
        self.requests.append((method, payload))
        if method == "getBusinessConnection":
            return self.connection
        if method == "getUpdates":
            return [update for update in self.updates if update["update_id"] >= payload["offset"]]
        raise AssertionError(method)


class TelegramPolling(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "service.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.api = FakeTelegram()
        self.policy = TelegramPersonal(expected_owner_id=10, allowed_chat_ids=[20])
        self.now = 1000

    def poller(self):
        return TelegramPoller(self.api, self.policy, self.store, "conn-1", clock=lambda: self.now)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        return self.poller()

    def test_prerequisite_and_durable_offset(self):
        self.api.updates = [message(100)]
        self.assertEqual(len(self.poller().poll(timeout=1)), 1)
        method, payload = self.api.requests[0]
        self.assertEqual(method, "getUpdates")
        self.assertEqual((payload["offset"], payload["timeout"]), (0, 1))
        self.assertNotIn("message", payload["allowed_updates"])
        restarted = self.restart()
        self.assertEqual(len(restarted.poll(timeout=0)), 1)
        restarted.ack()
        self.assertEqual(self.restart().poll(timeout=0), [])
        self.assertEqual(self.api.requests[-1][1]["offset"], 101)

    def test_only_configured_chat_and_connection_are_emitted_in_order(self):
        self.api.updates = [
            message(108, sender=10), message(104, chat=21, sender=21),
            message(107, connection_id="other"), {"update_id": 105, "message": {"text": "Private"}},
            message(106),
        ]
        poller = self.poller()
        events = poller.poll()
        self.assertEqual([(event.role, event.event.event_id) for event in events],
                         [("customer", "106"), ("owner", "108")])
        poller.ack()
        self.assertEqual(self.restart().poll(), [])
        self.assertEqual(self.api.requests[-1][1]["offset"], 109)

    def test_queued_enabled_connection_cannot_override_current_revocation(self):
        self.api.updates = [
            {"update_id": 99, "business_connection": self.api.connection.copy()}, message(100),
        ]
        self.api.connection["is_enabled"] = False
        poller = self.poller()
        self.assertEqual(poller.poll(), [])
        with self.assertRaisesRegex(ChannelError, "forbidden"):
            self.policy.send_payload("conn-1", 20, "Reply", last_incoming_at=1000, now=1001)

    def test_failed_connection_refresh_clears_permissions_and_does_not_ack(self):
        self.api.updates = [message(100)]
        poller = self.poller()
        poller.refresh()
        self.api.connection["user"] = {"id": 11}
        with self.assertRaisesRegex(ChannelError, "invalid_connection"):
            poller.poll()
        self.assertNotIn("conn-1", self.policy.connections)
        poller.ack()
        self.api.connection["user"] = {"id": 10}
        self.assertEqual(len(self.restart().poll()), 1)

    def test_cursor_expires_before_telegram_update_id_resets(self):
        self.api.updates = [message(9000)]
        poller = self.poller()
        self.assertEqual(len(poller.poll()), 1)
        poller.ack()
        self.now += 7 * 86400
        self.api.updates = [message(100)]
        self.assertEqual(self.restart().poll()[0].event.event_id, "100")


class TelegramTransport(unittest.TestCase):
    @patch("omni_service.telegram_live.ssl.create_default_context")
    @patch("omni_service.telegram_live.socket.create_connection")
    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_ssh_forward_keeps_telegram_tls_name(self, https, connect, ssl_context):
        conn = https.return_value
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"ok":true,"result":{"id":123}}'
        api = TelegramAPI("123:synthetic", tunnel_port=37892)
        self.assertEqual(api.call("getMe"), {"id": 123})
        connect.assert_called_once_with(("127.0.0.1", 37892), 10)
        ssl_context.return_value.wrap_socket.assert_called_once_with(
            connect.return_value, server_hostname="api.telegram.org")
        self.assertIs(conn.sock, ssl_context.return_value.wrap_socket.return_value)

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_explicit_proxy_tunnels_only_to_telegram(self, tls):
        conn = tls.return_value
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"ok":true,"result":{"id":123}}'
        api = TelegramAPI("123:synthetic", proxy_url="http://127.0.0.1:17890")
        self.assertEqual(api.call("getMe"), {"id": 123})
        tls.assert_called_once_with("127.0.0.1", 17890, timeout=10)
        conn.set_tunnel.assert_called_once_with("api.telegram.org", 443)
        with self.assertRaisesRegex(ChannelError, "invalid_telegram_proxy"):
            TelegramAPI("123:synthetic", proxy_url="http://user:secret@127.0.0.1:17890")

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_fixed_tls_endpoint_and_server_receipt(self, tls):
        conn = tls.return_value
        response = conn.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{"message_id":7}}'
        api = TelegramAPI("123:synthetic")
        payload = {"business_connection_id": "conn-1", "chat_id": 20, "text": "Sample"}
        self.assertEqual(api.call("sendMessage", payload), {"message_id": 7})
        tls.assert_called_once_with("api.telegram.org", timeout=10)
        self.assertEqual(conn.request.call_args.args, ("POST", "/bot123:synthetic/sendMessage"))
        self.assertEqual(json.loads(conn.request.call_args.kwargs["body"]), payload)
        conn.close.assert_called_once()

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_transport_failure_does_not_expose_token(self, tls):
        tls.return_value.request.side_effect = OSError("request /bot123:synthetic/sendMessage")
        with self.assertRaisesRegex(ChannelError, "^delivery_unknown$"):
            TelegramAPI("123:synthetic").call("sendMessage", {"chat_id": 20})

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_media_download_resolves_file_id_on_fixed_proxy_endpoint(self, tls):
        image = base64.b64decode(PNG_DATA)
        conn = tls.return_value
        metadata, resource = Mock(status=200), Mock(status=200)
        metadata.read.return_value = json.dumps({"ok": True, "result": {
            "file_id": "image12", "file_path": "photos/file_12.png", "file_size": len(image),
        }}).encode()
        resource.getheader.side_effect = {"Content-Length": str(len(image)), "Content-Type": "image/png"}.get
        resource.read.return_value = image
        conn.getresponse.side_effect = (metadata, resource)
        api = TelegramAPI("123:synthetic", proxy_url="http://127.0.0.1:17890")

        self.assertEqual(api.download_attachment("image12", kind="image"),
                         MediaBlob(image, "file_12.png", "image/png"))
        self.assertEqual(tls.call_args_list, [call("127.0.0.1", 17890, timeout=10),
                                              call("127.0.0.1", 17890, timeout=60)])
        self.assertEqual(conn.set_tunnel.call_args_list, [call("api.telegram.org", 443)] * 2)
        post, get = conn.request.call_args_list
        self.assertEqual(post.args, ("POST", "/bot123:synthetic/getFile"))
        self.assertEqual(json.loads(post.kwargs["body"]), {"file_id": "image12"})
        self.assertEqual(get, call("GET", "/file/bot123:synthetic/photos/file_12.png"))
        resource.read.assert_called_once_with(MAX_IMAGE_BYTES + 1)

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_media_download_rejects_invalid_paths_oversize_and_redirects(self, tls):
        api = TelegramAPI("123:synthetic")
        for path in ("https://example.invalid/secret", "../secret", "docs/%2fsecret", "docs//secret"):
            with self.subTest(path=path), patch.object(api, "call", return_value={"file_path": path}):
                with self.assertRaisesRegex(ChannelError, "^media_source_unavailable$"):
                    api.download_attachment("doc12", kind="file")
        with patch.object(api, "call", return_value={"file_path": "docs/file.pdf", "file_size": MAX_FILE_BYTES + 1}):
            with self.assertRaisesRegex(ChannelError, "^media_too_large$"):
                api.download_attachment("doc12", kind="file")
        tls.assert_not_called()

        conn = tls.return_value
        response = conn.getresponse.return_value
        response.status = 302
        with patch.object(api, "call", return_value={"file_path": "docs/file.pdf"}):
            with self.assertRaisesRegex(ChannelError, "^media_source_unavailable$"):
                api.download_attachment("doc12", kind="file")
        conn.request.assert_called_once_with("GET", "/file/bot123:synthetic/docs/file.pdf")
        response.read.assert_not_called()

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_media_upload_multipart_preserves_file_bytes_and_business_routing(self, tls):
        conn = tls.return_value
        response = conn.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{"message_id":8,"chat":{"id":20}}}'
        file = MediaBlob(b"%PDF-1.7\nfixture\n%%EOF\n", "sample.pdf", "application/pdf")
        api = TelegramAPI("123:synthetic")
        self.assertEqual(api.send_attachment("conn-1", 20, "file", file, caption="Документ"),
                         {"message_id": 8, "chat": {"id": 20}})
        request = conn.request.call_args
        self.assertEqual(request.args, ("POST", "/bot123:synthetic/sendDocument"))
        content_type = request.kwargs["headers"]["Content-Type"]
        envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        multipart = BytesParser(policy=default).parsebytes(envelope + request.kwargs["body"])
        fields = {part.get_param("name", header="Content-Disposition"): part for part in multipart.iter_parts()}
        self.assertEqual(set(fields), {"business_connection_id", "chat_id", "caption", "document",
                                      "disable_content_type_detection"})
        self.assertEqual(fields["business_connection_id"].get_payload(decode=True), b"conn-1")
        self.assertEqual(fields["chat_id"].get_payload(decode=True), b"20")
        self.assertEqual(fields["disable_content_type_detection"].get_payload(decode=True), b"true")
        self.assertEqual(fields["document"].get_filename(), file.name)
        self.assertEqual(fields["document"].get_payload(decode=True), file.data)
        self.assertEqual(fields["caption"].get_payload(decode=True), "Документ".encode())

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_media_upload_uses_native_photo_only_for_compatible_mime(self, tls):
        api = TelegramAPI("123:synthetic")
        conn = tls.return_value
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"ok":true,"result":{"message_id":8}}'
        api.send_attachment("conn-1", 20, "image", MediaBlob(base64.b64decode(PNG_DATA), "image.png", ""))
        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        api.send_attachment("conn-1", 20, "image", MediaBlob(gif, "image.gif", ""))
        self.assertEqual([request.args for request in conn.request.call_args_list], [
            ("POST", "/bot123:synthetic/sendPhoto"),
            ("POST", "/bot123:synthetic/sendDocument"),
        ])

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_welcome_photo_upload_preserves_proxy_and_exact_multipart(self, tls):
        conn = tls.return_value
        response = conn.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{"message_id":7,"chat":{"id":20}}}'
        api = TelegramAPI("123:synthetic", proxy_url="http://127.0.0.1:17890")
        photo = {"type": "image", "mime": "image/png", "data": PNG_DATA, "alt": "Ignored alt"}
        caption = "路线\nДобро пожаловать"
        self.assertEqual(api.send_welcome_photo("conn-1", 20, photo, caption=caption),
                         {"message_id": 7, "chat": {"id": 20}})
        tls.assert_called_once_with("127.0.0.1", 17890, timeout=10)
        conn.set_tunnel.assert_called_once_with("api.telegram.org", 443)
        conn.close.assert_called_once()
        request = conn.request.call_args
        self.assertEqual(request.args, ("POST", "/bot123:synthetic/sendPhoto"))
        content_type = request.kwargs["headers"]["Content-Type"]
        envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        multipart = BytesParser(policy=default).parsebytes(envelope + request.kwargs["body"])
        self.assertEqual(multipart.get_content_type(), "multipart/form-data")
        fields = {part.get_param("name", header="Content-Disposition"): part for part in multipart.iter_parts()}
        self.assertEqual(set(fields), {"business_connection_id", "chat_id", "caption", "photo"})
        self.assertEqual(fields["business_connection_id"].get_payload(decode=True), b"conn-1")
        self.assertEqual(fields["chat_id"].get_payload(decode=True), b"20")
        self.assertEqual(fields["caption"].get_payload(decode=True), caption.encode())
        self.assertEqual(fields["photo"].get_content_type(), "image/png")
        self.assertEqual(fields["photo"].get_filename(), "welcome.png")
        self.assertEqual(fields["photo"].get_payload(decode=True), base64.b64decode(PNG_DATA))

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_welcome_photo_rejects_invalid_media_before_transport(self, tls):
        api = TelegramAPI("123:synthetic")
        photo = {"type": "image", "mime": "image/png", "data": PNG_DATA, "alt": ""}
        invalid_uploads = (
            (20, {**photo, "mime": "image/svg+xml"}, ""),
            (20, {**photo, "mime": "image/jpeg"}, ""),
            (20, {**photo, "data": "not base64!"}, ""),
            (20, {**photo, "data": PNG_DATA[:-12]}, ""),
            (20, photo, "🌐" * 513),
            (True, photo, ""),
        )
        for chat_id, item, caption in invalid_uploads:
            with self.subTest(item=item["mime"], chat_id=chat_id, caption_length=len(caption)):
                with self.assertRaisesRegex(ChannelError, "^invalid_payload$"):
                    api.send_welcome_photo("conn-1", chat_id, item, caption=caption)
        with patch("omni_service.telegram_live._MAX_WELCOME_PHOTO_BYTES", len(base64.b64decode(PNG_DATA)) - 1):
            with self.assertRaisesRegex(ChannelError, "^invalid_payload$"):
                api.send_welcome_photo("conn-1", 20, photo)
        tls.assert_not_called()

    @patch("omni_service.telegram_live.http.client.HTTPSConnection")
    def test_welcome_photo_keeps_rate_limit_and_sanitizes_unknown_delivery(self, tls):
        api = TelegramAPI("123:synthetic")
        photo = {"type": "image", "mime": "image/png", "data": PNG_DATA, "alt": ""}
        response = tls.return_value.getresponse.return_value
        response.status = 429
        response.getheader.return_value = "1"
        response.read.return_value = b'{"ok":false,"parameters":{"retry_after":32}}'
        with self.assertRaisesRegex(ChannelError, "^rate_limited$") as limited:
            api.send_welcome_photo("conn-1", 20, photo)
        self.assertEqual(limited.exception.retry_after, 32)

        tls.return_value.request.side_effect = OSError("request /bot123:synthetic/sendPhoto Private caption")
        with self.assertRaisesRegex(ChannelError, "^delivery_unknown$") as unknown:
            api.send_welcome_photo("conn-1", 20, photo, caption="Private caption")
        self.assertTrue(unknown.exception.__suppress_context__)
