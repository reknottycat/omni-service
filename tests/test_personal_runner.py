from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from omni_service.models import InboundEvent
from omni_service.personal_runner import PersonalBridge, discover_connections


class TelegramFixture:
    def __init__(self):
        self.enabled = True
        self.updates = []
        self.sent = []
        self.known_receipt = True
        self.delete_right = False
        self.delete_result = True
        self.deleted = []

    def call(self, method, payload):
        if method == "getBusinessConnection":
            return {"id": "c1", "user": {"id": 10}, "is_enabled": self.enabled,
                    "rights": {"can_reply": True, "can_delete_sent_messages": self.delete_right}}
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= payload["offset"]]
        if method == "sendMessage":
            self.sent.append(payload)
            return {"chat": {"id": payload["chat_id"]}, "business_connection_id":
                    "c1" if self.known_receipt else "wrong", "message_id": 89 + len(self.sent)}
        if method == "deleteBusinessMessages":
            self.deleted.append(payload)
            return self.delete_result
        raise AssertionError(method)

    def update(self, uid, text, *, chat=20, sender=20, echo=False, first_name=None, last_name=None):
        message = {"business_connection_id": "c1", "message_id": uid, "date": 1000,
                   "chat": {"id": chat, "type": "private"}, "from": {"id": sender, "is_bot": False},
                   "text": text}
        if first_name is not None:
            message["from"]["first_name"] = first_name
        if last_name is not None:
            message["from"]["last_name"] = last_name
        if echo:
            message["sender_business_bot"] = {"id": 88}
        self.updates.append({"update_id": uid, "business_message": message})


class FeishuFixture:
    chat_id = "oc_group"
    app_id = "cli_pilot"
    staff_ids = {"ou_allowed"}

    def __init__(self):
        self.sent = []

    def preflight(self):
        pass

    def send(self, text, key):
        self.sent.append(text)
        return "om_" + str(len(self.sent))


def staff(text="您好，我来帮您处理。", *, mid="om_staff", who="ou_allowed", room="oc_group"):
    return InboundEvent("feishu", "cli_pilot", mid, room, who, mid, text=text, timestamp=1001)


def recall(event, *, event_id="recall", **metadata):
    return replace(event, event_id=event_id, event_type="delete", text="", user_id="", **metadata)


class BridgeAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.telegram, self.feishu = TelegramFixture(), FeishuFixture()
        self.bridge = self.open_bridge()
        self.addCleanup(lambda: self.bridge.close())

    def open_bridge(self):
        return PersonalBridge(Path(self.temp.name) / "test.sqlite3", self.telegram, self.feishu,
                              owner=10, peer=20, connection_id="c1", bot_id=88, clock=lambda: 1000)

    def poll_and_drain(self):
        for event in self.bridge.telegram.poll(timeout=0):
            self.bridge.receive_telegram(event)
        self.bridge.telegram.ack()
        self.bridge.drain()

    def test_two_way_text_owner_echo_and_replay(self):
        self.telegram.update(1, "PRIVATE", chat=30, sender=30)
        self.telegram.update(2, "Здравствуйте", first_name="Sandra", last_name="Perez")
        self.telegram.update(3, "BOT ECHO", sender=10, echo=True)
        self.telegram.update(4, "Owner answer", sender=10)
        self.poll_and_drain()
        self.assertEqual(len(self.feishu.sent), 2)
        self.assertEqual(self.feishu.sent[0], "Sandra Perez (telegram): Здравствуйте\n中文: 你好")
        self.assertIn("[人工客服] Telegram 本人已回复：Owner answer", self.feishu.sent)
        self.bridge.receive_feishu(staff(who="ou_unapproved"))
        self.bridge.receive_feishu(staff(room="oc_wrong"))
        self.bridge.receive_feishu(staff())
        self.bridge.receive_feishu(staff())
        self.bridge.drain()
        self.assertEqual(self.telegram.sent, [{
            "business_connection_id": "c1", "chat_id": 20,
            "text": "[Сотрудник] OmniService: Здравствуйте! Я помогу вам.",
        }])
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.poll_and_drain()
        self.bridge.receive_feishu(staff())
        self.bridge.drain()
        self.assertEqual(len(self.feishu.sent), 2)
        self.assertEqual(len(self.telegram.sent), 1)
        data = self.bridge.get_conversation(self.bridge.cid)
        self.assertNotIn("PRIVATE", str(data))
        self.assertNotIn("BOT ECHO", str(data))
        self.assertEqual(data["mode"], "manual")

    def test_discovery_leaves_first_customer_update_for_the_runner(self):
        class StickySubscription(TelegramFixture):
            def call(self, method, payload):
                updates = super().call(method, {"offset": 0, **payload})
                if method == "getUpdates":
                    if "business_message" not in payload["allowed_updates"]:
                        self.updates = [item for item in self.updates if "business_message" not in item]
                    return [item for item in updates if any(key in item for key in payload["allowed_updates"])]
                return updates

        api = StickySubscription()
        api.updates.append({"update_id": 1, "business_connection": api.call("getBusinessConnection", {})})
        api.update(2, "Здравствуйте")
        self.assertEqual(discover_connections(api), [{"connection_id": "c1", "owner_id": 10,
                                                      "enabled": True, "can_reply": True}])
        self.bridge.telegram.api = api
        self.poll_and_drain()
        self.assertEqual(self.feishu.sent[0], "客户 (telegram): Здравствуйте\n中文: 你好")

    def test_revocation_blocks_pending_reply(self):
        self.telegram.update(2, "Здравствуйте")
        self.poll_and_drain()
        self.bridge.receive_feishu(staff())
        self.telegram.enabled = False
        self.bridge.drain()
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.bridge.get_conversation(self.bridge.cid)["messages"][-1]["status"], "failed")

    def test_wrong_receipt_is_unknown_and_never_resent(self):
        self.telegram.update(2, "Здравствуйте")
        self.poll_and_drain()
        self.telegram.known_receipt = False
        self.bridge.receive_feishu(staff())
        self.bridge.drain()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.bridge.drain()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.bridge.get_conversation(self.bridge.cid)["messages"][-1]["status"], "delivery_unknown")

    def test_native_feishu_recall_deletes_the_linked_staff_reply_once(self):
        self.telegram.update(1, "Здравствуйте")
        self.poll_and_drain()
        self.telegram.delete_right = True
        event = staff()
        reply = self.bridge.receive_feishu(event)
        self.bridge.drain()
        for invalid in (recall(event, chat_id="oc_other"), recall(event, account_id="cli_other"),
                        recall(event, timestamp=999)):
            self.assertIsNone(self.bridge.receive_feishu(invalid))
        self.assertEqual(self.bridge.receive_feishu(recall(event))["status"], "pending")
        self.assertEqual(self.bridge.receive_feishu(recall(event))["status"], "duplicate")
        self.bridge.drain()
        self.assertEqual(self.bridge.receive_feishu(recall(event, event_id="same_message"))["status"], "deleted")
        self.bridge.drain()
        self.assertEqual(self.telegram.deleted, [{"business_connection_id": "c1", "message_ids": [90]}])
        state = self.bridge.get_conversation(self.bridge.cid)
        message = next(message for message in state["messages"] if message["id"] == reply["message_id"])
        self.assertEqual((message["status"], message["recall_status"]), ("deleted", "deleted"))
        self.assertTrue(any("平台已确认删除" in notice for notice in self.feishu.sent))

    def test_native_recall_cancels_queued_reply_and_suppresses_late_event_after_restart(self):
        self.telegram.update(1, "Здравствуйте")
        self.poll_and_drain()
        self.telegram.delete_right = True
        early, late = staff(), staff(mid="om_late")
        self.bridge.receive_feishu(early)
        self.assertEqual(self.bridge.receive_feishu(recall(early))["status"], "cancelled")
        self.assertEqual(self.bridge.receive_feishu(recall(late, event_id="late"))["status"], "unknown_message")
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.bridge.receive_feishu(late)["status"], "suppressed_deleted")
        self.bridge.drain()
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.deleted, [])

    def test_native_recall_without_telegram_right_preserves_sent_message(self):
        self.telegram.update(1, "Здравствуйте")
        self.poll_and_drain()
        event = staff()
        reply = self.bridge.receive_feishu(event)
        self.bridge.drain()
        self.bridge.receive_feishu(recall(event))
        self.bridge.drain()
        self.assertEqual(self.telegram.deleted, [])
        state = self.bridge.get_conversation(self.bridge.cid)
        message = next(item for item in state["messages"] if item["id"] == reply["message_id"])
        self.assertEqual((message["status"], message["recall_status"]), ("sent", "unsupported"))

    def test_unknown_native_delete_keeps_message_sent_and_is_never_retried(self):
        self.telegram.update(1, "Здравствуйте")
        self.poll_and_drain()
        self.telegram.delete_right = True
        self.telegram.delete_result = {"ok": True}
        event = staff()
        reply = self.bridge.receive_feishu(event)
        self.bridge.drain()
        self.bridge.receive_feishu(recall(event))
        self.bridge.drain()
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.bridge.receive_feishu(recall(event, event_id="retry"))["status"], "delivery_unknown")
        self.bridge.drain()
        self.assertEqual(len(self.telegram.deleted), 1)
        state = self.bridge.get_conversation(self.bridge.cid)
        message = next(item for item in state["messages"] if item["id"] == reply["message_id"])
        deletion = next(item for item in state["outbox"] if item["operation"] == "delete")
        self.assertEqual((message["status"], message["recall_status"], deletion["status"]),
                         ("sent", "delivery_unknown", "unknown"))
