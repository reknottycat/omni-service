"""Exercise real HTTP-shaped Feishu card decisions against simulated platforms."""

import json
from pathlib import Path
import tempfile
import threading
import unittest

from omni_service.approval_card import build_approval_card, finalize_approval_card
from omni_service.intake import Intake
from omni_service.server import DEFAULT_ACCOUNTS, create_server
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload, signed_headers
from omni_service.transport import HttpTransport
from test_approval_flow import COMPANY, COMPLETE, extract
from test_http import http


STAFF = "ou_card_reviewer"
UNAUTHORIZED = "ou_card_outsider"


def card_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from card_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from card_objects(child)


class CardApprovalAcceptance(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.platforms = self.enterContext(FakePlatforms())
        self.now = 2_000_000_000
        self.service = Service(
            Path(directory) / "cards.sqlite3",
            HttpTransport(self.platforms.urls),
            clock=lambda: self.now,
            members=(STAFF,),
            intake=Intake(extract, simulated=True),
            approval_intake=True,
            approval_cards=True,
            agent_advisor=lambda _transcript: "您好，我来帮您处理。",
        )
        self.addCleanup(self.service.close)
        server = create_server(self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def admin(self, path, body=None):
        code, data = http(self.base, path, body, headers={"Authorization": "Bearer simulation-admin"})
        self.assertEqual(code, 200, data)
        return data

    def webhook(self, channel, body, *, signed=True):
        account = "demo-" + channel
        raw = json.dumps(body, ensure_ascii=False).encode()
        headers = signed_headers(channel, raw, DEFAULT_ACCOUNTS[(channel, account)]) if signed else {}
        return http(self.base, f"/webhooks/{channel}/{account}", body, headers=headers)

    def customer(self, text, *, peer="101"):
        self.now += 1
        body = customer_payload("telegram", text, customer=peer, sequence=self.now,
                                timestamp=self.now, language_hint="ru")
        code, data = self.webhook("telegram", body)
        self.assertEqual(code, 200, data)
        return data["results"][0]["conversation_id"]

    def staff(self, room, text):
        self.now += 1
        body = employee_payload(room, text, employee=STAFF, sequence=self.now, timestamp=self.now)
        code, data = self.webhook("feishu", body)
        self.assertEqual(code, 200, data)
        return data["results"][0]

    def action(self, room, message_id, value, *, actor=STAFF):
        self.now += 1
        return {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger", "event_id": f"card_click_{self.now}",
                       "app_id": "demo-feishu", "create_time": str(self.now * 1000),
                       "token": DEFAULT_ACCOUNTS[("feishu", "demo-feishu")]},
            "event": {"operator": {"open_id": actor}, "host": "im_message",
                      "context": {"open_chat_id": room, "open_message_id": message_id},
                      "action": {"tag": "button", "value": value}},
        }

    def drain(self):
        return self.admin("/api/drain", {})

    def conversation(self, cid):
        return self.admin(f"/api/conversations/{cid}")

    def sent_to(self, peer):
        return [item["text"] for item in self.platforms.messages
                if item["channel"] == "telegram" and item["target"] == peer]

    def card_for(self, cid):
        state = self.conversation(cid)
        draft = state["drafts"][-1]
        preview = next(item for item in state["outbox"]
                       if item["operation_key"] == "notice:draft:" + draft["id"])
        self.assertEqual(preview["status"], "sent")
        self.assertTrue(preview["external_id"])
        bodies = [request["body"] for request in self.platforms.requests if request["channel"] == "feishu"
                  and request["body"].get("receive_id") == state["room_id"]
                  and request["body"].get("msg_type") == "interactive"]
        card = json.loads(bodies[-1]["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])
        objects = tuple(card_objects(card))
        self.assertTrue(any(part.get("content") == draft["customer_text"] for part in objects))
        buttons = {part["text"]["content"]: part["behaviors"][0]["value"]
                   for part in objects if part.get("tag") == "button"}
        expected = {label: {"omni_action": decision, "draft_id": draft["id"], "version": draft["version"]}
                    for label, decision in (("可以发送", "approve"), ("不可以发送", "reject"))}
        self.assertEqual(buttons, expected)
        return state["room_id"], preview["external_id"], draft, buttons

    def assert_final_card(self, room, receipt, draft, buttons, title):
        messages = [item for item in self.platforms.messages
                    if item["channel"] == "feishu" and item["target"] == room and item["message_id"] == receipt]
        self.assertEqual(len(messages), 1)
        card = messages[0]["card"]
        self.assertEqual(card["header"]["title"]["content"], title)
        self.assertTrue(card["config"]["update_multi"])
        parts = tuple(card_objects(card))
        self.assertTrue(any(part.get("content") == draft["customer_text"] for part in parts))
        final_buttons = [part for part in parts if part.get("tag") == "button"]
        self.assertEqual({part["text"]["content"]: part["behaviors"][0]["value"]
                          for part in final_buttons}, buttons)
        self.assertTrue(all(part["disabled"] is True for part in final_buttons))
        patches = [request for request in self.platforms.requests
                   if request["channel"] == "feishu" and request["method"] == "PATCH"
                   and request["path"].endswith("/" + receipt)]
        self.assertEqual(len(patches), 1)

    def test_only_signed_same_room_card_and_version_can_send_exactly_once(self):
        cid_a = self.customer(COMPANY)
        cid_b = self.customer(COMPANY, peer="102")
        self.drain()
        room_a, receipt_a, draft_a, buttons_a = self.card_for(cid_a)
        room_b, receipt_b, _draft_b, _buttons_b = self.card_for(cid_b)
        welcome_a, welcome_b = self.sent_to("101"), self.sent_to("102")
        self.assertEqual((len(welcome_a), len(welcome_b)), (1, 1))
        approve = buttons_a["可以发送"]
        valid = self.action(room_a, receipt_a, approve)
        unauthenticated = {**valid, "header": {**valid["header"], "token": "incorrect"}}
        self.assertEqual(self.webhook("feishu", unauthenticated, signed=False)[0], 403)
        self.assertNotEqual(self.staff(room_a, f"/approve {draft_a['id']} {draft_a['version']}")["status"], "approved")
        wrong_version = ("0" if approve["version"][0] != "0" else "1") + approve["version"][1:]
        for name, attempt in (
            ("different room", self.action(room_b, receipt_a, approve)),
            ("different receipt", self.action(room_a, receipt_b, approve)),
            ("wrong version", self.action(room_a, receipt_a, {**approve, "version": wrong_version})),
            ("unlisted staff", self.action(room_a, receipt_a, approve, actor=UNAUTHORIZED)),
        ):
            with self.subTest(name=name):
                http_status, result = self.webhook("feishu", attempt)
                self.assertIn(http_status, (200, 403), result)
                self.assertEqual(self.conversation(cid_a)["drafts"][-1]["status"], "pending")
                self.drain()
                self.assertEqual(self.sent_to("101"), welcome_a)
                self.assertEqual(self.sent_to("102"), welcome_b)

        self.assertEqual(self.webhook("feishu", valid)[0], 200)
        self.drain()
        self.assert_final_card(room_a, receipt_a, draft_a, buttons_a, "已批准发送")
        self.assertEqual(self.webhook("feishu", valid)[0], 200)
        self.assertEqual(self.webhook("feishu", self.action(room_a, receipt_a, approve))[0], 200)
        self.drain()
        self.assert_final_card(room_a, receipt_a, draft_a, buttons_a, "已批准发送")
        self.assertEqual(self.conversation(cid_a)["drafts"][-1]["status"], "approved")
        self.assertEqual(self.sent_to("101"), [*welcome_a, draft_a["customer_text"]])
        self.assertEqual(self.sent_to("102"), welcome_b)

    def test_reject_and_replaced_card_never_send(self):
        cid = self.customer(COMPANY)
        self.drain()
        room, old_receipt, old_draft, old_buttons = self.card_for(cid)
        self.customer("Нужна кукуруза.")
        self.drain()
        _, receipt, draft, buttons = self.card_for(cid)
        self.assertNotEqual(old_receipt, receipt)
        self.assert_final_card(room, old_receipt, old_draft, old_buttons, "草稿已取消")
        self.assertEqual(self.webhook("feishu", self.action(room, old_receipt, old_buttons["可以发送"]))[0], 200)
        self.assertEqual(self.webhook("feishu", self.action(room, receipt, buttons["不可以发送"]))[0], 200)
        self.drain()
        self.assert_final_card(room, receipt, draft, buttons, "已拒绝发送")
        self.assertEqual(self.webhook("feishu", self.action(room, receipt, buttons["可以发送"]))[0], 200)
        self.drain()
        self.assert_final_card(room, receipt, draft, buttons, "已拒绝发送")
        self.assertEqual(self.conversation(cid)["drafts"][-1]["status"], "rejected")
        self.assertNotIn(draft["customer_text"], self.sent_to("101"))
        self.assertEqual(len(self.sent_to("101")), 1)

    def test_followup_auto_send_requires_both_switches_in_that_room(self):
        cid_a = self.customer(COMPLETE)
        cid_b = self.customer(COMPLETE, peer="102")
        self.drain()
        room_a, room_b = (self.conversation(cid)["room_id"] for cid in (cid_a, cid_b))
        for cid in (cid_a, cid_b):
            settings = self.conversation(cid)["settings"]
            self.assertFalse(settings["reply_assist"])
            self.assertFalse(settings["auto_reply"])
            self.assertTrue(settings["approval_required"])

        self.staff(room_a, "/回复辅助 开")
        self.staff(room_a, "/自动回复 开")
        self.customer("Какова стоимость?")
        self.drain()
        self.card_for(cid_a)
        self.assertEqual(len(self.sent_to("101")), 1)

        self.staff(room_a, "/审批 关")
        self.staff(room_a, "/自动回复 关")
        self.customer("Когда заберёте груз?")
        self.drain()
        _, receipt, draft, buttons = self.card_for(cid_a)
        self.assertEqual(len(self.sent_to("101")), 1)
        self.assertEqual(self.webhook("feishu", self.action(room_a, receipt, buttons["可以发送"]))[0], 200)
        self.drain()
        self.assertEqual(self.sent_to("101")[-1], draft["customer_text"])

        self.staff(room_a, "/自动回复 开")
        self.customer("Как подготовить документы?")
        self.customer("Как подготовить документы?", peer="102")
        self.drain()
        self.assertEqual(len(self.sent_to("101")), 3)
        self.assertEqual(self.sent_to("101")[-1], self.conversation(cid_a)["drafts"][-1]["customer_text"])
        self.assertEqual(len(self.sent_to("102")), 1)
        self.assertEqual(self.conversation(cid_b)["drafts"], [])
        self.assertEqual(self.conversation(cid_b)["settings"], {
            "reply_assist": False, "auto_reply": False, "approval_required": True, "show_labels": False,
        })
        self.assertNotEqual(room_a, room_b)

    def test_basic_intake_keeps_approval_and_auto_authorization_can_be_revoked(self):
        cid = self.customer(COMPANY)
        self.drain()
        room = self.conversation(cid)["room_id"]
        for command in ("/回复辅助 开", "/自动回复 开", "/审批 关"):
            self.staff(room, command)
        self.customer("Нужна кукуруза.")
        self.drain()
        _room, _receipt, intake_draft, _buttons = self.card_for(cid)
        self.assertEqual(intake_draft["kind"], "intake_question")
        self.assertEqual(len(self.sent_to("101")), 1)

        self.customer(COMPLETE)
        self.drain()
        self.assertEqual(self.conversation(cid)["mode"], "manual")
        self.platforms.fail_next("telegram", "send", status=429, headers={"Retry-After": "60"})
        self.customer("Когда можно забрать груз?")
        self.drain()
        self.assertTrue(any(item["channel"] == "telegram" and item["status"] == "pending"
                            for item in self.conversation(cid)["outbox"]))
        self.staff(room, "/审批 开")
        self.now += 61
        self.drain()
        self.assertEqual(len(self.sent_to("101")), 1)
        self.assertFalse(any(item["channel"] == "telegram" and item["status"] == "pending"
                             for item in self.conversation(cid)["outbox"]))

    def test_old_events_and_manual_draft_requests_cannot_gain_auto_authorization(self):
        cid = self.customer(COMPLETE)
        self.drain()
        room = self.conversation(cid)["room_id"]
        self.staff(room, "/回复辅助 开")

        self.customer("Как подготовить документы?")
        self.staff(room, "/自动回复 开")
        self.staff(room, "/审批 关")
        self.drain()
        _, _, old_draft, _ = self.card_for(cid)
        self.assertEqual(len(self.sent_to("101")), 1)

        self.assertEqual(self.staff(room, "/draft")["status"], "draft_queued")
        self.drain()
        _, _, manual_draft, _ = self.card_for(cid)
        self.assertNotEqual(old_draft["id"], manual_draft["id"])
        self.assertEqual(len(self.sent_to("101")), 1)

        self.customer("Как подготовить груз?")
        self.staff(room, "/审批 开")
        self.staff(room, "/审批 关")
        self.drain()
        self.card_for(cid)
        self.assertEqual(len(self.sent_to("101")), 1)

        self.customer("Как оформить следующий заказ?")
        self.drain()
        self.assertEqual(len(self.sent_to("101")), 2)


class FinalizedApprovalCardTest(unittest.TestCase):
    def test_cancelled_card_keeps_exact_preview_and_callback_version(self):
        text = "[AI] Точная версия, <текст> и & символы."
        version = "1234567890abcdef"
        card = build_approval_card(draft_id="draft_" + "f" * 32, version=version,
                                   customer_text=text, reference="Internal source")
        before = json.dumps(card, ensure_ascii=False)
        final = finalize_approval_card(card, "cancelled")
        self.assertEqual(json.dumps(card, ensure_ascii=False), before)
        self.assertEqual(final["header"]["title"]["content"], "草稿已取消")
        self.assertEqual(final["header"]["template"], "grey")
        self.assertTrue(final["config"]["update_multi"])
        self.assertEqual(final["body"]["elements"][:2], card["body"]["elements"][:2])
        for old, new in zip(card["body"]["elements"][2:], final["body"]["elements"][2:], strict=True):
            self.assertEqual(new, {**old, "disabled": True})
