"""A successful remote effect plus a lost receipt must remain unknown."""

from pathlib import Path
import tempfile
import unittest

from omni_service.models import InboundEvent
from omni_service.service import Service
from omni_service.simulation import FakePlatforms
from omni_service.transport import HttpTransport


class ReceiptLoss:
    def __init__(self, delegate):
        self.delegate = delegate
        self.target = None

    def send(self, channel, request):
        result = self.delegate.send(channel, request)
        if self.target == (channel, request.method):
            self.target = None
            return {}
        return result


class ReceiptAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platforms = self.enterContext(FakePlatforms())
        self.transport = ReceiptLoss(HttpTransport(self.platforms.urls))
        self.now = 2_000_000_000
        self.service = Service(Path(self.temp.name) / "state.sqlite3", self.transport, clock=lambda: self.now)
        self.addCleanup(self.service.close)

    def customer(self, channel):
        event = InboundEvent(channel, "official", "in-1", "10001", "10001", "in-1",
                             text="Hello", timestamp=self.now)
        return self.service.ingest(event)["conversation_id"]

    def test_platform_accepted_send_but_empty_receipt_is_unknown_and_not_retried(self):
        for channel in ("telegram", "max", "whatsapp"):
            with self.subTest(channel=channel):
                cid = self.customer(channel)
                self.transport.target = (channel, "POST")
                self.service.drain()
                actual = [m for m in self.platforms.messages if m["channel"] == channel]
                self.assertEqual(len(actual), 1)
                detail = self.service.get_conversation(cid)
                outgoing = next(m for m in detail["messages"] if m["direction"] == "outbound")
                self.assertEqual(outgoing["status"], "delivery_unknown")
                self.assertEqual(detail["outbox"][0]["status"], "unknown")
                self.service.drain()
                self.assertEqual(len([m for m in self.platforms.messages if m["channel"] == channel]), 1)

    def test_platform_deleted_message_but_empty_receipt_is_unknown(self):
        cid = self.customer("max")
        self.service.drain()
        self.service.handoff(cid)
        self.service.drain()
        detail = self.service.get_conversation(cid)
        outgoing = next(m for m in detail["messages"] if m["direction"] == "outbound")
        self.transport.target = ("max", "DELETE")
        self.service.recall(outgoing["id"], cid, "employee-1")
        self.service.drain()
        self.assertTrue(next(m for m in self.platforms.messages if m["channel"] == "max")["deleted"])
        latest = next(m for m in self.service.get_conversation(cid)["messages"] if m["id"] == outgoing["id"])
        self.assertEqual(latest["status"], "sent")
        self.assertEqual(latest["recall_status"], "delivery_unknown")

    def test_room_created_with_missing_receipt_is_unknown_and_not_created_again(self):
        cid = self.customer("telegram")
        self.service.handoff(cid)
        self.transport.target = ("feishu", "POST")
        self.service.drain()
        self.assertEqual(len(self.platforms.rooms), 1)
        self.assertEqual(self.service.get_conversation(cid)["room_status"], "unknown")
        self.service.drain()
        self.assertEqual(len(self.platforms.rooms), 1)
