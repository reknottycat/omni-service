from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from omni_service.channels import parse_events
from omni_service.intake import Intake
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload
from omni_service.transport import HttpTransport


FACTS = {
    "company": "ООО Амур", "cargo": "кукуруза", "quantity": "18 тонн",
    "origin": "Хабаровск", "destination": "Тунцзян",
}
COMPANY_MESSAGE = "Наша компания — ООО Амур."
SHIPMENT_MESSAGE = "кукуруза, 18 тонн, из Хабаровск в Тунцзян."


def extract(text, fields):
    return {field: FACTS[field] for field in fields if FACTS[field] in text}


class IntakeServiceAcceptance(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.platforms = self.enterContext(FakePlatforms())
        self.path = Path(self.directory) / "state.sqlite3"
        self.transport = HttpTransport(self.platforms.urls)
        self.now = 2_000_000_000
        self.service = self.open_service()
        self.addCleanup(lambda: self.service.close())

    def open_service(self, *, max_replies=5):
        return Service(self.path, self.transport, clock=lambda: self.now,
                       intake=Intake(extract, max_replies=max_replies, simulated=True), intake_enabled=True)

    def customer_event(self, text="Здравствуйте", *, chat="101", account="official"):
        self.now += 1
        payload = customer_payload("telegram", text, customer=chat, sequence=self.now,
                                   timestamp=self.now, language_hint="ru")
        return parse_events("telegram", account, payload)[0]

    def customer(self, text="Здравствуйте", **scope):
        return self.service.ingest(self.customer_event(text, **scope))

    def staff_payload(self, room, text):
        self.now += 1
        return employee_payload(room, text, timestamp=self.now, sequence=self.now)

    def staff(self, payload):
        event, = parse_events("feishu", "team", payload)
        return self.service.ingest(event)

    def texts(self, channel, target):
        return [m["text"] for m in self.platforms.messages
                if m["channel"] == channel and m["target"] == target]

    def test_new_sessions_get_separate_rooms_and_provenanced_handoff(self):
        customer_a = self.customer(COMPANY_MESSAGE)
        customer_b = self.customer(chat="102", account="another-official")
        cid_a, cid_b = customer_a["conversation_id"], customer_b["conversation_id"]
        self.service.drain()
        room_a = self.service.get_conversation(cid_a)["room_id"]
        room_b = self.service.get_conversation(cid_b)["room_id"]
        self.assertTrue(room_a and room_b and room_a != room_b)
        self.assertEqual(self.service.get_conversation(cid_a)["intake"]["facts"], {"company": FACTS["company"]})
        self.assertEqual(self.service.get_conversation(cid_b)["intake"]["facts"], {})
        self.assertEqual(len(self.texts("telegram", "101")), 1)
        self.assertTrue(self.texts("telegram", "101")[0].startswith("[AI 模拟] Здравствуйте!"))
        self.assertNotIn("Какую компанию", self.texts("telegram", "101")[0])
        self.assertIn("Какую компанию", self.texts("telegram", "102")[0])

        shipment = self.customer(SHIPMENT_MESSAGE)
        self.service.drain()
        state = self.service.get_conversation(cid_a)
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["intake"]["facts"], FACTS)
        self.assertEqual(state["intake"]["handoff_reason"], "complete")
        self.assertEqual(state["intake"]["source_messages"], {
            field: customer_a["message_id"] if field == "company" else shipment["message_id"]
            for field in FACTS
        })
        self.assertEqual(len(self.texts("telegram", "101")), 2)
        self.assertIn("сотруднику", self.texts("telegram", "101")[-1])
        summaries = [text for text in self.texts("feishu", room_a) if "自动接待已转人工" in text]
        self.assertEqual(len(summaries), 1)
        for value in FACTS.values():
            self.assertIn(value, summaries[0])
            self.assertNotIn(value, "\n".join(self.texts("feishu", room_b)))
        self.assertEqual(self.service.get_conversation(cid_b)["mode"], "auto")

    def test_restart_replay_and_hard_reply_limit_send_one_room_broadcast(self):
        self.service.close()
        self.service = self.open_service(max_replies=3)
        first = self.customer_event()
        cid = self.service.ingest(first)["conversation_id"]
        old_event_shape = asdict(first)
        del old_event_shape["has_mentions"]
        old_digest = hashlib.sha256(json.dumps(old_event_shape, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
        with self.service.store.lock:
            stored_digest = self.service.store.db.execute(
                "SELECT digest FROM inbox WHERE event_id=?", (first.event_id,),
            ).fetchone()[0]
        self.assertEqual(stored_digest, old_digest)
        self.service.drain()
        room = self.service.get_conversation(cid)["room_id"]
        self.service.close()
        self.service = self.open_service(max_replies=3)
        self.assertEqual(self.service.ingest(first)["status"], "duplicate")

        for text in ("Спасибо", "Не знаю", "Здравствуйте"):
            self.customer(text)
            self.service.drain()
        customer_texts = self.texts("telegram", "101")
        self.assertEqual(len(customer_texts), 3)
        self.assertTrue(all(text.startswith("[AI 模拟] ") for text in customer_texts))
        self.assertIn("компанию", customer_texts[1])
        self.assertIn("сотруднику", customer_texts[2])
        self.assertNotIn("?", customer_texts[2])
        self.assertEqual(self.service.get_conversation(cid)["intake"]["handoff_reason"], "reply_limit")
        room_texts = self.texts("feishu", room)
        rules = [text for text in room_texts if "本群的普通文字消息会翻译后发给客户" in text]
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0].startswith('<at user_id="all"></at>'))
        self.assertNotIn("本群的普通文字消息会翻译后发给客户", "\n".join(customer_texts))
        self.assertEqual(len(self.platforms.rooms), 1)

    def test_mentions_stay_internal_and_plain_staff_reply_stops_ai(self):
        cid = self.customer()["conversation_id"]
        self.service.drain()
        room = self.service.get_conversation(cid)["room_id"]
        self.customer("Спасибо")  # Keep the next AI reply pending during staff coordination.
        mention = self.staff_payload(room, "_user_ref 请看看这条客户需求")
        mention["event"]["message"]["mentions"] = [{
            "key": "_user_ref", "id": {"open_id": "employee-2"}, "name": "同事",
        }]
        for payload in (mention, self.staff_payload(room, "@同事 请看看"),
                        self.staff_payload(room, "＠同事 请看看")):
            self.assertEqual(self.staff(payload)["status"], "internal_only")
        self.assertEqual(self.service.get_conversation(cid)["mode"], "auto")
        bot = self.staff_payload(room, "新会话规则")
        bot["event"]["sender"]["sender_type"] = "app"
        self.assertEqual(parse_events("feishu", "team", bot), [])
        self.service.drain()
        self.assertEqual(len(self.texts("telegram", "101")), 2)
        self.assertTrue(self.texts("telegram", "101")[-1].startswith("[AI 模拟] "))

        self.customer("Не знаю")  # Leaves the next AI question pending until staff respond.
        self.staff(self.staff_payload(room, "您好，我来帮您处理。"))
        self.service.drain()
        self.customer("Здравствуйте")
        self.service.drain()
        result = self.texts("telegram", "101")
        self.assertEqual(len(result), 3)
        self.assertTrue(result[-1].startswith("[Сотрудник] "))
        self.assertNotIn("看看", "\n".join(result))
        state = self.service.get_conversation(cid)
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["intake"]["handoff_reason"], "staff_reply")
        self.assertTrue(any(m["author_id"] == "AI" and m["status"] == "cancelled"
                            for m in state["messages"]))

    def test_disable_cancels_queued_and_enable_only_affects_new_sessions(self):
        cid_before = self.customer()["conversation_id"]
        self.service.set_intake_enabled(False)
        cid_off = self.customer(chat="102")["conversation_id"]
        self.service.drain()
        before = self.service.get_conversation(cid_before)
        self.assertEqual(before["intake"]["handoff_reason"], "server_disabled")
        self.assertEqual(before["mode"], "manual")
        self.assertTrue(any(m["author_id"] == "AI" and m["status"] == "cancelled"
                            for m in before["messages"]))
        self.assertEqual(self.service.get_conversation(cid_off)["mode"], "manual")
        self.assertEqual(self.texts("telegram", "101"), [])
        self.assertEqual(self.texts("telegram", "102"), [])

        self.service.set_intake_enabled(True)
        self.customer(chat="101")
        self.customer(chat="102")
        self.customer(chat="103")
        self.service.drain()
        self.assertEqual(self.texts("telegram", "101"), [])
        self.assertEqual(self.texts("telegram", "102"), [])
        self.assertEqual(len(self.texts("telegram", "103")), 1)
        self.assertTrue(self.texts("telegram", "103")[0].startswith("[AI 模拟] "))
