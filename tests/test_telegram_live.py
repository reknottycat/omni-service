import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omni_service.models import ChannelError
from omni_service.personal import TelegramPersonal
from omni_service.store import Store
from omni_service.telegram_live import TelegramAPI, TelegramPoller


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
