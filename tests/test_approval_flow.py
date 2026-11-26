"""Verify that only the exact Feishu-approved translation can leave its conversation."""

import json
from pathlib import Path
import tempfile
import threading
import unittest

from omni_service.intake import Intake
from omni_service.server import DEFAULT_ACCOUNTS, create_server
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload, signed_headers
from omni_service.transport import HttpTransport
from test_http import http


FACTS = {"company": "ООО Амур", "cargo": "кукуруза", "quantity": "18 тонн",
         "origin": "Хабаровск", "destination": "Тунцзян"}
COMPANY = "Наша компания — ООО Амур."
SHIPMENT = "Нужно перевезти кукуруза, 18 тонн, из Хабаровск в Тунцзян."
COMPLETE = COMPANY + " " + SHIPMENT


def extract(text, fields):
    return {name: FACTS[name] for name in fields if FACTS[name] in text}


def approve(draft):
    return f"/approve {draft['id']} {draft['version']}"


class ApprovalFlowAcceptance(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.platforms = self.enterContext(FakePlatforms())
        self.now = 2_000_000_000

    def start(self, *, advisor=None):
        self.service = Service(Path(self.directory) / "approval.sqlite3", HttpTransport(self.platforms.urls),
                               clock=lambda: self.now, intake=Intake(extract, simulated=True),
                               approval_intake=True, agent_advisor=advisor)
        self.addCleanup(self.service.close)
        server = create_server(self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def admin(self, path, body=None):
        status, result = http(self.base, path, body, headers={"Authorization": "Bearer simulation-admin"})
        self.assertEqual(status, 200, result)
        return result

    def webhook(self, channel, payload, *, expected=200):
        account = "demo-" + channel
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = signed_headers(channel, raw, DEFAULT_ACCOUNTS[(channel, account)])
        status, result = http(self.base, f"/webhooks/{channel}/{account}", payload, headers=headers)
        self.assertEqual(status, expected, result)
        return result

    def customer(self, text="Здравствуйте", *, chat="101"):
        self.now += 1
        payload = customer_payload("telegram", text, customer=chat, sequence=self.now,
                                   timestamp=self.now, language_hint="ru")
        return self.webhook("telegram", payload)["results"][0]["conversation_id"]

    def staff(self, room, text, *, employee="employee-1", bot=False, expected=200):
        self.now += 1
        payload = employee_payload(room, text, employee=employee, timestamp=self.now, sequence=self.now)
        if bot:
            payload["event"]["sender"]["sender_type"] = "app"
        result = self.webhook("feishu", payload, expected=expected)
        return result["results"][0] if result.get("results") else result

    def drain(self):
        return self.admin("/api/drain", {})

    def conversation(self, cid):
        return self.admin(f"/api/conversations/{cid}")

    def texts(self, channel, target):
        return [message["text"] for message in self.platforms.messages
                if message["channel"] == channel and message["target"] == target]

    def test_only_authorized_staff_in_the_same_room_can_send_the_previewed_translation(self):
        self.start()
        cid = self.customer(COMPANY)
        other_cid = self.customer(chat="102")
        self.drain()
        state, other_state = self.conversation(cid), self.conversation(other_cid)
        room, other_room = state["room_id"], other_state["room_id"]
        self.assertNotEqual(room, other_room)
        self.assertEqual(len(self.platforms.rooms), 2)
        target_texts = self.texts("telegram", "101")
        self.assertEqual(len(target_texts), 1)
        self.assertEqual(len(self.texts("telegram", "102")), 1)
        self.assertIn("виртуальный помощник", target_texts[0])
        self.assertNotIn("Какой груз", target_texts[0])

        draft = state["drafts"][-1]
        command = approve(draft)
        previews = [message for message in self.texts("feishu", room) if command in message]
        self.assertEqual(len(previews), 1)
        self.assertIn("\n" + draft["customer_text"] + "\n", previews[0])
        self.assertIn("Какой груз", draft["customer_text"])
        self.assertNotIn("Какую компанию", draft["customer_text"])
        self.assertNotIn(command, "\n".join(self.texts("feishu", other_room)))
        self.assertEqual(self.staff(other_room, command)["status"], "approval_stale")
        self.assertEqual(self.staff(room, command, employee="outside", expected=403)["error"], "forbidden")
        self.assertEqual(self.staff(room, command, bot=True)["accepted"], 0)
        self.assertEqual(self.staff(room, "@employee-2 " + command)["status"], "internal_only")
        version = draft["version"]
        wrong_version = ("0" if version[0] != "0" else "1") + version[1:]
        self.assertEqual(self.staff(room, f"/approve {draft['id']} {wrong_version}")["status"], "approval_stale")
        self.drain()
        self.assertEqual(self.texts("telegram", "101"), target_texts)
        self.assertEqual(self.staff(room, command)["status"], "approved")
        self.assertEqual(self.staff(room, command)["status"], "duplicate_approval")
        self.drain()
        self.assertEqual(self.texts("telegram", "101"), [*target_texts, draft["customer_text"]])
        self.assertEqual(len(self.texts("telegram", "102")), 1)

    def test_new_customer_context_supersedes_drafts_and_complete_handoff_is_feishu_only(self):
        self.start()
        cid = self.customer(COMPANY)
        self.drain()
        first = self.conversation(cid)
        room, original = first["room_id"], first["drafts"][-1]
        self.assertEqual(self.staff(room, "/draft")["status"], "agent_disabled")
        self.customer("Нужна кукуруза.")
        self.drain()
        revised = self.conversation(cid)["drafts"][-1]
        self.assertNotEqual(original["version"], revised["version"])
        self.assertEqual(self.staff(room, approve(original))["status"], "approval_stale")
        self.customer(SHIPMENT)
        self.drain()
        state = self.conversation(cid)
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["intake"]["facts"], FACTS)
        self.assertEqual(state["intake"]["handoff_reason"], "complete")
        self.assertEqual(self.staff(room, approve(revised))["status"], "approval_stale")
        rules = [text for text in self.texts("feishu", room) if "本群的普通文字消息" in text]
        handoffs = [text for text in self.texts("feishu", room) if "AI 接待结束，请转人工" in text]
        self.assertEqual(len(rules), 1)
        self.assertEqual(len(handoffs), 1)
        self.assertTrue(rules[0].startswith('<at user_id="all"></at>'))
        self.assertTrue(handoffs[0].startswith('<at user_id="all"></at>'))
        self.assertIn("客户原话：" + COMPANY, handoffs[0])
        self.assertIn("客户原话：" + SHIPMENT, handoffs[0])
        for fact in FACTS.values():
            self.assertIn(fact, handoffs[0])
        self.assertEqual(len(self.texts("telegram", "101")), 1)

    def test_first_message_that_already_needs_handoff_still_gets_one_welcome(self):
        self.start()
        examples = [("101", COMPLETE, "complete"), ("102", "Мне нужен оператор.", "requested_human")]
        for chat, text, reason in examples:
            with self.subTest(reason=reason):
                cid = self.customer(text, chat=chat)
                self.drain()
                state = self.conversation(cid)
                self.assertEqual(state["mode"], "manual")
                self.assertEqual(state["intake"]["handoff_reason"], reason)
                self.assertEqual(state["drafts"], [])
                self.assertEqual(len(self.texts("telegram", chat)), 1)
                self.assertIn("виртуальный помощник", self.texts("telegram", chat)[0])
                self.assertTrue(any("AI 接待结束，请转人工" in text
                                    for text in self.texts("feishu", state["room_id"])))

    def test_preview_confirmation_and_five_delivered_questions_gate_handoff(self):
        self.start()
        platform, transport = self.platforms, self.service.transport

        class DelayPreviewOnce:
            delayed = False

            def send(self, channel, request):
                content = (request.body or {}).get("content", "")
                if channel == "feishu" and "/approve draft_" in content and not self.delayed:
                    self.delayed = True
                    platform.fail_next("feishu", "send", status=429, headers={"Retry-After": "5"})
                return transport.send(channel, request)

        self.service.transport = DelayPreviewOnce()
        cid = self.customer()
        self.drain()
        state = self.conversation(cid)
        room, original = state["room_id"], state["drafts"][-1]
        self.assertEqual(self.staff(room, approve(original))["status"], "approval_preview_not_delivered")
        self.now += 5
        self.drain()
        self.customer("Извините, пока не знаю.")
        self.drain()
        self.assertEqual(self.staff(room, approve(original))["status"], "approval_stale")
        self.assertEqual(len(self.texts("telegram", "101")), 1)

        for index in range(5):
            if index:
                self.customer("Пока не знаю.")
                self.drain()
            state = self.conversation(cid)
            self.assertEqual(state["mode"], "auto")
            if index == 4:
                self.platforms.fail_next("telegram", "send", status=429, headers={"Retry-After": "5"})
            approval = self.staff(room, approve(state["drafts"][-1]))
            self.assertEqual(approval["status"], "approved")
            self.drain()
            if index == 1:
                message_id = approval["message_id"]
                recall = self.admin(f"/api/conversations/{cid}/messages/{message_id}/recall",
                                    {"actor_id": "employee-1"})
                self.assertEqual(recall["status"], "pending")
                self.drain()
                message = next(m for m in self.conversation(cid)["messages"] if m["id"] == message_id)
                self.assertEqual(message["recall_status"], "deleted")
                self.assertIsNotNone(message["external_id"])

        state = self.conversation(cid)
        sent_questions = [m for m in state["messages"] if m["source_channel"] == "intake_question" and m["external_id"]]
        self.assertEqual(len(sent_questions), 4)
        self.assertEqual(state["mode"], "auto")
        self.now += 5
        self.drain()
        state = self.conversation(cid)
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["intake"]["handoff_reason"], "reply_limit")
        self.assertEqual(len(self.texts("telegram", "101")), 6)
        self.assertEqual(len([m for m in state["messages"]
                              if m["source_channel"] == "intake_question" and m["external_id"]]), 5)
        self.assertTrue(any("AI 接待结束，请转人工" in text and "待补" in text
                            for text in self.texts("feishu", room)))
        self.customer("Спасибо")
        self.drain()
        self.assertEqual(len(self.texts("telegram", "101")), 6)

    def test_agent_switch_leaves_intake_alone_and_followup_still_requires_approval(self):
        calls = []

        def advisor(transcript):
            calls.append(transcript)
            return "您好，我来帮您处理。"

        self.start(advisor=advisor)
        self.service.set_agent_enabled(False)
        cid = self.customer(COMPANY)
        self.drain()
        state = self.conversation(cid)
        room = state["room_id"]
        self.assertEqual(len(self.texts("telegram", "101")), 1)
        self.assertEqual(state["drafts"][-1]["kind"], "intake_question")
        self.assertEqual(self.staff(room, "/draft")["status"], "agent_disabled")
        self.assertFalse(calls)
        self.customer(SHIPMENT)
        self.drain()
        self.assertEqual(self.conversation(cid)["mode"], "manual")
        private = "@employee-2 internal-only note"
        self.staff(room, private)
        other_cid = self.customer("SEPARATE CUSTOMER", chat="102")
        self.drain()
        self.service.set_agent_enabled(True)
        self.assertEqual(self.staff(room, "/draft")["status"], "drafted")
        self.drain()
        advice = self.conversation(cid)["drafts"][-1]
        self.assertEqual(advice["kind"], "agent_draft")
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(turn["actor"] == "customer" and turn["text"] == COMPANY for turn in calls[0]))
        self.assertNotIn(private, str(calls[0]))
        self.assertNotIn("SEPARATE CUSTOMER", str(calls[0]))
        self.assertEqual(len(self.texts("telegram", "101")), 1)
        self.assertNotIn(approve(advice), "\n".join(self.texts("feishu", self.conversation(other_cid)["room_id"])))

        self.service.set_agent_enabled(False)
        self.assertEqual(self.staff(room, approve(advice))["status"], "approval_stale")
        self.service.set_agent_enabled(True)
        self.assertEqual(self.staff(room, "/draft")["status"], "drafted")
        self.drain()
        advice = self.conversation(cid)["drafts"][-1]
        self.assertEqual(self.staff(room, approve(advice))["status"], "approved")
        self.drain()
        self.assertEqual(self.texts("telegram", "101")[-1], advice["customer_text"])
        self.assertEqual(len(self.texts("telegram", "101")), 2)
        self.assertEqual(len(self.texts("telegram", "102")), 1)
