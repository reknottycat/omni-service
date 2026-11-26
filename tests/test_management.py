"""Exercise management policies and frozen welcomes through the service queue."""

from pathlib import Path
import tempfile
import unittest

from omni_service.channels import parse_events
from omni_service.intake import Intake
from omni_service.models import ChannelError
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload
from omni_service.transport import HttpTransport


ADMIN = "employee-1"
STAFF = "employee-2"
NEW_STAFF = "employee-3"
MEMBER = "ou_joined_member"
OUTSIDER = "ou_unlinked_member"
DOCUMENT = "https://team.feishu.cn/docx/WelcomeFixture"
NEXT_DOCUMENT = "https://team.feishu.cn/docx/NextWelcomeFixture"
PHOTO = {"type": "image", "mime": "image/png", "alt": "Маршрут", "data": (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFf8AAAAASUVORK5CYII="
)}
FIRST_TEXT = {"type": "text", "text": "Здравствуйте, партнёр!",
              "html": "<b>Здравствуйте, партнёр!</b>"}
SECOND_TEXT = {"type": "text", "text": "Новая версия", "html": "<i>Новая версия</i>"}


class ManagementAcceptance(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "management.sqlite3"
        self.platforms = self.enterContext(FakePlatforms())
        self.transport = HttpTransport(self.platforms.urls)
        self.now = 2_000_000_000
        self.service = self.open_service()
        self.addCleanup(lambda: self.service.close())

    def open_service(self):
        service = Service(self.path, self.transport, clock=lambda: self.now,
                          members=(ADMIN, STAFF), intake=Intake(lambda _text, _fields: {}, simulated=True),
                          approval_intake=True, approval_cards=True, agent_advisor=lambda _transcript: "明白。")
        service.configure_management([ADMIN], welcome_document=DOCUMENT, preview_peers=["101"])
        return service

    def customer(self, peer):
        self.now += 1
        event, = parse_events("telegram", "official", customer_payload(
            "telegram", "Здравствуйте!", customer=peer, sequence=self.now,
            timestamp=self.now, language_hint="ru"))
        return self.service.ingest(event)["conversation_id"]

    def command(self, room, actor, text):
        self.now += 1
        event, = parse_events("feishu", "team", employee_payload(
            room, text, employee=actor, sequence=self.now, timestamp=self.now))
        return self.service.ingest(event)["status"]

    def conversation_row(self, cid):
        return next(row for row in self.service.admin_state(ADMIN)["conversations"] if row["id"] == cid)

    def sends(self, peer):
        return [request["body"] for request in self.platforms.requests
                if request["channel"] == "telegram" and request["body"].get("chat_id") == peer]

    def assert_channel_error(self, code, operation, *args):
        with self.assertRaises(ChannelError) as error:
            operation(*args)
        self.assertEqual(error.exception.code, code)

    def test_new_defaults_apply_only_to_new_conversations_and_survive_restart(self):
        old = self.customer("101")
        self.service.drain()
        old_row = self.conversation_row(old)
        self.assertEqual((old_row["reply_assist"], old_row["show_labels"], old_row["handoff_staff_id"]),
                         (False, False, ADMIN))
        self.assertEqual(self.platforms.rooms[old_row["room_id"]]["members"], [ADMIN, STAFF])
        defaults = {"staff_ids": [STAFF, NEW_STAFF], "handoff_staff_id": NEW_STAFF,
                    "reply_assist": True, "show_labels": True, "welcome_document": NEXT_DOCUMENT}
        self.assert_channel_error("forbidden", self.service.admin_update_defaults, STAFF, defaults)
        self.service.admin_update_defaults(ADMIN, defaults)
        self.service.close()
        self.service = self.open_service()

        self.assertEqual(self.service.admin_state(ADMIN)["defaults"], defaults)
        self.assertEqual(self.customer("101"), old)
        new = self.customer("102")
        self.service.drain()
        new_row = self.conversation_row(new)
        self.assertEqual((new_row["reply_assist"], new_row["show_labels"], new_row["handoff_staff_id"]),
                         (True, True, NEW_STAFF))
        self.assertEqual(self.platforms.rooms[new_row["room_id"]]["members"], [STAFF, NEW_STAFF])
        self.assertEqual(self.conversation_row(old), old_row)
        self.assertEqual(self.platforms.rooms[old_row["room_id"]]["members"], [ADMIN, STAFF])

    def test_web_and_slash_use_the_same_permission_and_room_membership(self):
        cid_a, cid_b = self.customer("101"), self.customer("102")
        self.service.drain()
        room_a = self.conversation_row(cid_a)["room_id"]
        room_b = self.conversation_row(cid_b)["room_id"]
        self.platforms.rooms[room_a]["members"].append(MEMBER)
        self.service.sync_room_members(room_a, self.platforms.rooms[room_a]["members"])
        self.assertEqual(self.service.admin_state(MEMBER)["role"], "member")
        self.assertEqual(self.service.admin_state(STAFF)["role"], "staff")
        self.assertEqual([r["id"] for r in self.service.admin_state(MEMBER)["conversations"]], [cid_a])
        self.assert_channel_error("forbidden", self.service.admin_state, OUTSIDER)

        self.assertEqual(self.command(room_a, MEMBER, "/标签 开"), "settings_updated")
        self.assertTrue(self.conversation_row(cid_a)["show_labels"])
        self.service.admin_set_command_policy(ADMIN, "show_labels", "admin")
        for actor in (MEMBER, STAFF):
            with self.subTest(actor=actor):
                self.assert_channel_error("forbidden", self.command, room_a, actor, "/标签 关")
                self.assert_channel_error("forbidden", self.service.admin_update_conversation,
                                          actor, cid_a, {"show_labels": False})
        self.assertTrue(self.conversation_row(cid_a)["show_labels"])
        self.assertFalse(self.service.admin_update_conversation(ADMIN, cid_a, {"show_labels": False})["show_labels"])
        self.assertEqual(self.command(room_a, ADMIN, "/标签 开"), "settings_updated")
        self.assertTrue(self.conversation_row(cid_a)["show_labels"])

        self.assertEqual(self.command(room_a, MEMBER, "/回复辅助 开"), "settings_updated")
        self.assertTrue(self.conversation_row(cid_a)["reply_assist"])
        self.assertFalse(self.service.admin_update_conversation(MEMBER, cid_a, {"reply_assist": False})["reply_assist"])
        self.assert_channel_error("forbidden", self.command, room_b, MEMBER, "/回复辅助 开")
        self.assert_channel_error("forbidden", self.service.admin_update_conversation,
                                  MEMBER, cid_b, {"reply_assist": True})
        self.assertFalse(self.conversation_row(cid_b)["reply_assist"])
        self.service.admin_set_command_policy(ADMIN, "reply_assist", "staff")
        self.assert_channel_error("forbidden", self.command, room_a, MEMBER, "/回复辅助 开")
        self.assertTrue(self.service.admin_update_conversation(STAFF, cid_a, {"reply_assist": True})["reply_assist"])

        self.assert_channel_error("forbidden", self.command, room_a, MEMBER, "请提供您的邮箱")
        self.platforms.rooms[room_a]["members"].remove(MEMBER)
        self.service.sync_room_members(room_a, self.platforms.rooms[room_a]["members"])
        self.service.admin_set_command_policy(ADMIN, "reply_assist", "room_member")
        self.assert_channel_error("forbidden", self.service.admin_update_conversation,
                                  MEMBER, cid_a, {"reply_assist": False})
        self.assert_channel_error("forbidden", self.command, room_a, MEMBER, "/回复辅助 关")
        self.service.drain()
        self.assertEqual((len(self.sends("101")), len(self.sends("102"))), (1, 1))

    def test_welcome_preview_publish_and_rollback_freeze_the_exact_items(self):
        cid_test, cid_real = self.customer("101"), self.customer("102")
        self.service.drain()
        test_baseline, real_baseline = self.sends("101"), self.sends("102")
        self.assertEqual(self.service.admin_prepare_welcome(ADMIN), {"document_url": DOCUMENT})
        self.assert_channel_error("forbidden", self.service.admin_prepare_welcome, STAFF)
        compiled = {"items": [dict(FIRST_TEXT), dict(PHOTO)]}
        preview = self.service.admin_preview_welcome(ADMIN, compiled)
        v1 = preview["version"]
        self.assertEqual(preview["items"], [FIRST_TEXT, PHOTO])
        self.assertIsNone(self.service.admin_state(ADMIN)["published_welcome_version"])
        self.assert_channel_error("not_found", self.service.admin_rollback_welcome, ADMIN, v1)
        self.assert_channel_error("forbidden", self.service.admin_send_welcome_preview, ADMIN, v1, cid_real)
        self.assert_channel_error("forbidden", self.service.admin_send_welcome_preview, STAFF, v1, cid_test)
        self.service.drain()
        self.assertEqual(self.sends("101"), test_baseline)
        self.assertEqual(self.sends("102"), real_baseline)

        compiled["items"][0]["html"] = "<b>Unpublished edit</b>"
        self.assertEqual(self.service.admin_send_welcome_preview(ADMIN, v1, cid_test)["status"], "queued")
        self.service.drain()
        expected_v1 = [{"chat_id": "101", "text": FIRST_TEXT["html"], "parse_mode": "HTML"},
                       {"chat_id": "101", "photo": PHOTO, "caption": PHOTO["alt"]}]
        self.assertEqual(self.sends("101"), test_baseline + expected_v1)
        self.assertEqual(self.sends("102"), real_baseline)

        self.assertEqual(self.service.admin_publish_welcome(ADMIN, v1)["version"], v1)
        self.service.admin_update_defaults(ADMIN, {"welcome_document": NEXT_DOCUMENT})
        self.assertEqual(self.service.admin_prepare_welcome(ADMIN), {"document_url": NEXT_DOCUMENT})
        v2 = self.service.admin_preview_welcome(ADMIN, {"items": [SECOND_TEXT]})["version"]
        self.assertNotEqual(v2, v1)
        first = self.customer("103")
        self.assertEqual(self.service.admin_state(ADMIN)["published_welcome_version"], v1)
        self.service.admin_publish_welcome(ADMIN, v2)
        second = self.customer("104")
        self.service.admin_rollback_welcome(ADMIN, v1)
        third = self.customer("105")
        self.service.drain()
        for cid, peer, version in ((first, "103", v1), (second, "104", v2), (third, "105", v1)):
            with self.subTest(peer=peer):
                items = [row for row in self.service.get_conversation(cid)["outbox"]
                         if row["channel"] == "telegram" and row["operation"] == "send"]
                self.assertTrue(items)
                self.assertTrue(all(item["status"] == "sent" and item["payload"]["welcome_version"] == version
                                    for item in items))
        for peer in ("103", "105"):
            self.assertEqual(self.sends(peer), [{**body, "chat_id": peer} for body in expected_v1])
        self.assertEqual(self.sends("104"), [{"chat_id": "104", "text": SECOND_TEXT["html"], "parse_mode": "HTML"}])
        self.assertEqual(self.sends("102"), real_baseline)
