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

    def call(self, method, payload):
        if method == "getBusinessConnection":
            return {"id": "c1", "user": {"id": 10}, "is_enabled": self.enabled,
                    "rights": {"can_reply": True}}
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= payload["offset"]]
        if method == "sendMessage":
            self.sent.append(payload)
            return {"chat": {"id": payload["chat_id"]}, "business_connection_id":
                    "c1" if self.known_receipt else "wrong", "message_id": 90}
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
        self.assertIn("Telegram 本人已回复：Owner answer", self.feishu.sent)
        self.bridge.receive_feishu(staff(who="ou_unapproved"))
        self.bridge.receive_feishu(staff(room="oc_wrong"))
        self.bridge.receive_feishu(staff())
        self.bridge.receive_feishu(staff())
        self.bridge.drain()
        self.assertEqual(self.telegram.sent, [{
            "business_connection_id": "c1", "chat_id": 20,
            "text": "OmniService: Здравствуйте! Я помогу вам.",
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
