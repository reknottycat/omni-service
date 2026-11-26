from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omni_service.intake import Intake
from omni_service.personal_runner import PersonalBridge
from test_personal_runner import FeishuFixture, TelegramFixture, staff


class PersonalIntakeAcceptance(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "bridge.sqlite3"
        self.telegram = TelegramFixture()
        self.feishu = FeishuFixture()

    def open_bridge(self, *, enabled):
        def extract(text, fields):
            return {"company": "Север"} if "company" in fields and "Север" in text else {}

        return PersonalBridge(self.database, self.telegram, self.feishu,
                              owner=10, peer=20, connection_id="c1", bot_id=88,
                              clock=lambda: 1000, intake=Intake(extract, simulated=True),
                              intake_enabled=enabled)

    @staticmethod
    def poll_and_drain(bridge):
        for event in bridge.telegram.poll(timeout=0):
            bridge.receive_telegram(event)
        bridge.telegram.ack()
        bridge.drain()

    def test_welcome_and_human_takeover_survive_restart(self):
        with closing(self.open_bridge(enabled=True)) as bridge:
            conversation_id = bridge.cid
            self.assertEqual(bridge.get_conversation(conversation_id)["mode"], "manual")
            self.telegram.update(1, "Unapproved customer", chat=30, sender=30)
            self.telegram.update(2, "Здравствуйте", first_name="Sandra", last_name="Perez")
            self.poll_and_drain(bridge)
            self.assertEqual(len(self.telegram.sent), 1)
            welcome = self.telegram.sent[0]
            self.assertEqual(welcome["chat_id"], 20)
            self.assertEqual(welcome["business_connection_id"], "c1")
            self.assertTrue(welcome["text"].startswith("[AI 模拟] Здравствуйте!"))
            for requested_detail in ("компанию", "груз", "количество", "Откуда", "куда"):
                self.assertIn(requested_detail, welcome["text"])
            self.assertTrue(any(message.startswith("Sandra Perez (telegram): Здравствуйте")
                                for message in self.feishu.sent))
            self.assertTrue(any(message.startswith("AI 模拟 → 客户:") for message in self.feishu.sent))

            bridge.receive_feishu(replace(staff("查看运费", mid="om_structured"), has_mentions=True))
            bridge.receive_feishu(staff("@同事 请查报价", mid="om_literal"))
            bridge.drain()
            state = bridge.get_conversation(conversation_id)
            self.assertEqual(state["mode"], "auto")
            self.assertIsNone(state["intake"]["handoff_reason"])
            self.assertEqual(len(self.telegram.sent), 1)

            self.telegram.update(3, "Компания Север")
            self.poll_and_drain(bridge)
            state = bridge.get_conversation(conversation_id)
            self.assertEqual(state["intake"]["facts"], {"company": "Север"})
            evidence = state["intake"]["source_messages"]["company"]
            self.assertTrue(any(message["id"] == evidence and message["original"] == "Компания Север"
                                for message in state["messages"]))
            self.assertEqual(len(self.telegram.sent), 2)
            self.assertNotIn("компанию", self.telegram.sent[-1]["text"])

        with closing(self.open_bridge(enabled=True)) as bridge:
            self.assertEqual(bridge.cid, conversation_id)
            self.poll_and_drain(bridge)
            self.assertEqual(len(self.telegram.sent), 2)
            state = bridge.get_conversation(conversation_id)
            self.assertEqual(state["mode"], "auto")
            self.assertEqual(state["intake"]["facts"], {"company": "Север"})

            bridge.receive_feishu(staff())
            bridge.drain()
            self.assertEqual(self.telegram.sent[-1], {
                "business_connection_id": "c1", "chat_id": 20,
                "text": "[Сотрудник] OmniService: Здравствуйте! Я помогу вам.",
            })
            self.assertEqual(len(self.telegram.sent), 3)
            state = bridge.get_conversation(conversation_id)
            self.assertEqual(state["mode"], "manual")
            self.assertEqual(state["intake"]["handoff_reason"], "staff_reply")

            self.telegram.update(4, "Спасибо")
            self.poll_and_drain(bridge)
            self.assertEqual(len(self.telegram.sent), 3)

    def test_disabled_first_contact_is_still_mirrored_without_an_ai_send(self):
        with closing(self.open_bridge(enabled=False)) as bridge:
            self.telegram.update(1, "Здравствуйте")
            self.poll_and_drain(bridge)
            self.assertEqual(self.telegram.sent, [])
            self.assertIn("客户 (telegram): Здравствуйте\n中文: 你好", self.feishu.sent)
            state = bridge.get_conversation(bridge.cid)
            self.assertEqual(state["mode"], "manual")
            self.assertNotIn("intake", state)
