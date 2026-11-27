"""Managed Telegram acceptance through the business poller and Feishu adapter."""

from pathlib import Path
import tempfile
import unittest

from omni_service.intake import Intake
from omni_service.models import ChannelError
from omni_service.personal_runner import PersonalBridge
from test_personal_runner import TelegramFixture, recall, staff


class ManagedTelegram(TelegramFixture):
    wrong_chat_receipt = None

    def call(self, method, payload):
        receipt = super().call(method, payload)
        if method == "sendMessage" and payload["chat_id"] == self.wrong_chat_receipt:
            receipt["chat"]["id"] = 999
        return receipt


class ManagedFeishu:
    chat_id = None
    app_id = "cli_pilot"
    staff_ids = {"ou_allowed"}
    bot_app_ids = {"cli_pilot"}

    def __init__(self):
        self.groups = {}
        self.create_requests = []
        self.sent = []
        self.mentioned_staff = []
        self.unknown_create = False

    def create_room(self, name, key):
        self.create_requests.append((name, key))
        if self.unknown_create:
            raise ChannelError("delivery_unknown")
        room = "oc_room_" + str(len(self.create_requests))
        self.groups[room] = {"users": self.staff_ids, "bots": self.bot_app_ids}
        return room

    def preflight(self, chat_id):
        if self.groups.get(chat_id) != {"users": self.staff_ids, "bots": self.bot_app_ids}:
            raise ChannelError("feishu_members_mismatch")

    def send(self, text, key, *, chat_id, mention_all=False, mention_user_id=None):
        self.preflight(chat_id)
        self.sent.append((chat_id, text, key, mention_all))
        if mention_user_id is not None:
            self.mentioned_staff.append((chat_id, mention_user_id))
        return "om_" + str(len(self.sent))


class ManagedBridgeAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "managed.sqlite3"
        self.telegram = ManagedTelegram()
        self.feishu = ManagedFeishu()
        self.now = 1000
        self.bridge = self.open_bridge([20, 30])
        self.addCleanup(lambda: self.bridge.close())

    def open_bridge(self, peers):
        return PersonalBridge(self.path, self.telegram, self.feishu, owner=10, peer_ids=peers,
                              connection_id="c1", bot_id=88, clock=lambda: self.now,
                              intake=Intake(lambda _text, _fields: {}, simulated=True),
                              intake_enabled=True, approval_intake=True)

    def intake(self):
        for event in self.bridge.telegram.poll(timeout=0):
            self.bridge.receive_telegram(event)
        self.bridge.telegram.ack()
        self.bridge.drain()

    def conversations(self):
        return {row["chat_id"]: self.bridge.get_conversation(row["id"])
                for row in self.bridge.list_conversations()}

    def test_two_isolated_groups_require_same_room_and_exact_version_approval(self):
        self.telegram.update(1, "Owner before customer", sender=10)
        self.telegram.update(2, "Unapproved customer", chat=40, sender=40)
        self.telegram.update(3, "Old customer event")
        self.telegram.updates[-1]["business_message"]["date"] = 999
        self.telegram.update(4, "Здравствуйте", first_name="Alpha")
        self.telegram.update(5, "Hello", chat=30, sender=30, first_name="Beta")
        self.intake()
        clients = self.conversations()
        self.assertEqual(set(clients), {"20", "30"})
        first, second = clients["20"], clients["30"]
        room_a, room_b = first["room_id"], second["room_id"]
        self.assertNotEqual(room_a, room_b)
        self.assertEqual(len(self.feishu.create_requests), 2)
        self.assertEqual([out["chat_id"] for out in self.telegram.sent], [20, 30])
        self.assertTrue(all("[AI 模拟]" in out["text"] for out in self.telegram.sent))
        for room, name, other, state in ((room_a, "Alpha", "Beta", first), (room_b, "Beta", "Alpha", second)):
            texts = [text for target, text, key, _ in self.feishu.sent if target == room]
            self.assertTrue(any(name in text for text in texts))
            self.assertFalse(any(other in text or "Owner before customer" in text for text in texts))
            notices = [ping for target, text, key, ping in self.feishu.sent
                       if target == room and key.startswith("room-rules:")]
            self.assertEqual(notices, [True])
            self.assertEqual([draft["status"] for draft in state["drafts"]], ["pending"])
        draft = first["drafts"][0]
        command = f"/approve {draft['id']} {draft['version']}"
        self.assertIsNone(self.bridge.receive_feishu(staff(command, mid="om_unmapped", room="oc_other")))
        self.assertIsNone(self.bridge.receive_feishu(staff(command, mid="om_intruder", who="ou_other", room=room_a)))
        self.assertEqual(self.bridge.receive_feishu(staff(command, mid="om_wrong_room", room=room_b))["status"],
                         "approval_stale")
        self.assertEqual(self.bridge.receive_feishu(staff(f"/approve {draft['id']} 000", mid="om_stale", room=room_a))["status"],
                         "approval_stale")
        self.bridge.drain()
        self.assertEqual(len(self.telegram.sent), 2)

        self.assertEqual(self.bridge.receive_feishu(staff(command, mid="om_approved", room=room_a))["status"],
                         "approved")
        self.assertEqual(self.bridge.receive_feishu(staff(command, mid="om_repeat", room=room_a))["status"],
                         "duplicate_approval")
        self.bridge.receive_feishu(staff(mid="om_staff_b", room=room_b))
        self.bridge.drain()
        self.assertEqual([(out["chat_id"], out["text"]) for out in self.telegram.sent[-2:]], [
            (20, draft["customer_text"]), (30, "[Agent] OmniService: Hello! I will help you."),
        ])
        self.assertEqual(self.conversations()["30"]["drafts"][0]["status"], "cancelled")
        self.assertEqual(self.feishu.mentioned_staff, [(room_b, "ou_allowed")])

        self.bridge.close()
        self.now = 1002
        self.bridge = self.open_bridge([30, 20])
        self.assertEqual(self.bridge.started_at, 1000)
        self.assertTrue(self.bridge.room_allowed(room_a))
        self.assertTrue(self.bridge.room_allowed(room_b))
        self.assertFalse(self.bridge.room_allowed("oc_other"))
        self.intake()
        self.assertEqual(len(self.telegram.sent), 4)
        self.assertEqual(len(self.feishu.create_requests), 2)
        self.assertEqual(self.conversations()["20"]["room_id"], room_a)

    def test_mismatched_telegram_receipt_stays_unknown_and_other_chat_can_send(self):
        self.telegram.wrong_chat_receipt = 20
        self.telegram.update(1, "Здравствуйте")
        self.telegram.update(2, "Hello", chat=30, sender=30)
        self.intake()
        self.bridge.drain()
        self.assertEqual([out["chat_id"] for out in self.telegram.sent], [20, 30])
        clients = self.conversations()
        for chat, status in (("20", "delivery_unknown"), ("30", "sent")):
            welcome = next(message for message in clients[chat]["messages"] if message["source_channel"] == "ai_welcome")
            self.assertEqual(welcome["status"], status)

    def test_unknown_room_creation_does_not_suppress_fixed_welcome(self):
        self.feishu.unknown_create = True
        self.telegram.update(1, "Здравствуйте")
        self.intake()
        self.assertEqual(self.conversations()["20"]["room_status"], "unknown")
        self.assertEqual(self.feishu.sent, [])
        self.assertEqual([out["chat_id"] for out in self.telegram.sent], [20])
        self.bridge.drain()
        self.assertEqual(len(self.feishu.create_requests), 1)
        self.assertEqual(len(self.telegram.sent), 1)

    def test_native_recall_in_another_linked_room_cannot_delete_staff_or_customer_mirror(self):
        self.telegram.delete_right = True
        self.telegram.update(1, "Здравствуйте")
        self.telegram.update(2, "Hello", chat=30, sender=30)
        self.intake()
        clients = self.conversations()
        first, second = clients["20"], clients["30"]
        customer_mirror = next(item["external_id"] for item in first["outbox"]
                               if item["operation_key"].startswith("mirror:"))
        event = staff(room=first["room_id"])
        self.bridge.receive_feishu(event)
        self.bridge.drain()
        for unrelated in (recall(event, event_id="wrong-room", chat_id=second["room_id"]),
                          recall(staff(mid=customer_mirror, room=first["room_id"]), event_id="mirror")):
            self.assertEqual(self.bridge.receive_feishu(unrelated)["status"], "unknown_message")
        self.bridge.drain()
        self.assertEqual(self.telegram.deleted, [])
        self.assertEqual(self.bridge.receive_feishu(recall(event))["status"], "pending")
        self.bridge.drain()
        self.assertEqual(self.telegram.deleted, [{"business_connection_id": "c1", "message_ids": [92]}])

    def test_approved_agent_recall_uses_its_sent_receipt_and_keeps_the_welcome(self):
        self.telegram.delete_right = True
        self.telegram.update(1, "Здравствуйте")
        self.intake()
        first = self.conversations()["20"]
        cid, room = first["id"], first["room_id"]
        self.bridge.handoff(cid)
        self.bridge.agent_advisor = lambda _transcript: "您好，我来帮您处理。"
        self.bridge.set_agent_enabled(True)
        self.bridge.receive_feishu(staff("/draft", mid="om_draft", room=room))
        self.bridge.drain()
        draft = self.conversations()["20"]["drafts"][-1]
        self.assertEqual(draft["kind"], "agent_draft")
        approved = self.bridge.receive_feishu(
            staff(f"/approve {draft['id']} {draft['version']}", mid="om_approve", room=room))
        self.assertEqual(approved["status"], "approved")
        self.bridge.drain()
        message_id = self.conversations()["20"]["drafts"][-1]["message_id"]
        self.assertEqual(self.bridge.recall(message_id, cid, "ou_allowed")["status"], "pending")
        self.bridge.drain()
        self.assertEqual(self.telegram.deleted, [{"business_connection_id": "c1", "message_ids": [91]}])
        state = self.conversations()["20"]
        messages = state["messages"]
        self.assertEqual(next(item for item in messages if item["id"] == message_id)["status"], "deleted")
        self.assertEqual(next(item for item in messages if item["source_channel"] == "ai_welcome")["status"], "sent")
        history = next(item for item in state["outbox"] if item["operation_key"] == "history-ai:" + message_id)
        self.assertEqual(history["status"], "sent")
        self.assertFalse(any(item["channel"] == "feishu" and item["operation"] == "delete" for item in state["outbox"]))

    def test_reopening_with_different_authorized_peers_fails_closed(self):
        with self.assertRaisesRegex(ChannelError, "personal_db_binding_mismatch"):
            self.open_bridge([20, 40])


if __name__ == "__main__":
    unittest.main()
