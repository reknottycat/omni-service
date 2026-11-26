"""Behavioral channel contracts, with synthetic fixtures and no external network."""

import copy
import hashlib
import hmac
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from omni_service.channels import (
    API_BASE_URLS, CAPABILITIES, delete_request, feishu_add_members_request,
    feishu_create_room_request, parse_events, parse_response, send_request,
    verify_webhook,
)
from omni_service.models import Attachment, ChannelError


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "channels"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ChannelContracts(unittest.TestCase):
    def assert_error(self, code, func, *args, **kwargs):
        with self.assertRaises(ChannelError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_three_external_identities_and_seconds(self):
        for channel, account, file, chat, user in (
            ("telegram", "tg-official", "telegram_text.json", "1001", "1001"),
            ("max", "max-official", "max_text.json", "2201", "2001"),
            ("whatsapp", "phone-1", "whatsapp_text.json", "15550001111", "15550001111"),
        ):
            with self.subTest(channel=channel):
                event, = parse_events(channel, account, fixture(file))
                self.assertEqual((event.chat_id, event.user_id), (chat, user))
                self.assertEqual(event.timestamp, 1795651200)
                for value in (event.account_id, event.chat_id, event.user_id, event.event_id, event.message_id):
                    self.assertIsInstance(value, str)

    def test_bot_echoes_are_ignored(self):
        tg = fixture("telegram_text.json")
        tg["message"]["from"]["is_bot"] = True
        mx = fixture("max_text.json")
        mx["message"]["sender"]["is_bot"] = True
        fs = fixture("feishu_text.json")
        fs["event"]["sender"]["sender_type"] = "app"
        for channel, payload in (("telegram", tg), ("max", mx), ("feishu", fs)):
            self.assertEqual(parse_events(channel, "account", payload), [])

    def test_telegram_edit_and_nonmessage_event(self):
        payload = fixture("telegram_text.json")
        payload["edited_message"] = payload.pop("message")
        payload["edited_message"]["edit_date"] = 1795651230
        event, = parse_events("telegram", "official", payload)
        self.assertEqual((event.event_type, event.timestamp), ("edit", 1795651230))
        self.assertEqual(parse_events("telegram", "official", {"update_id": 1, "callback_query": {}}), [])

    def test_max_edit_removal_and_stable_create_id(self):
        payload = fixture("max_text.json")
        original, = parse_events("max", "official", payload)
        payload["timestamp"] += 500
        repeated, = parse_events("max", "official", payload)
        self.assertEqual(original.event_id, repeated.event_id)
        payload["update_type"] = "message_edited"
        edited, = parse_events("max", "official", payload)
        self.assertEqual(edited.event_type, "edit")
        self.assertNotEqual(original.event_id, edited.event_id)
        deleted, = parse_events("max", "official", fixture("max_removed.json"))
        self.assertEqual((deleted.event_type, deleted.chat_id, deleted.message_id),
                         ("delete", "2201", "mid.source.1"))

    def test_max_missing_chat_uses_sender_not_recipient(self):
        payload = fixture("max_text.json")
        payload["message"]["recipient"]["chat_id"] = None
        event, = parse_events("max", "official", payload)
        self.assertEqual((event.recipient_type, event.chat_id), ("user", "2001"))
        request = send_request("max", "official", event.chat_id, "test", recipient_type=event.recipient_type)
        self.assertEqual(parse_qs(urlsplit(request.path).query), {"user_id": ["2001"]})

    def test_whatsapp_full_batch_across_entries_and_changes(self):
        payload = fixture("whatsapp_text.json")
        change = payload["entry"][0]["changes"][0]
        change["value"]["messages"].append(dict(change["value"]["messages"][0], id="wamid.2"))
        extra_change = copy.deepcopy(change)
        extra_change["value"]["messages"] = [dict(extra_change["value"]["messages"][0], id="wamid.3")]
        payload["entry"][0]["changes"].append(extra_change)
        extra_entry = copy.deepcopy(payload["entry"][0])
        extra_entry["changes"] = [copy.deepcopy(extra_change)]
        extra_entry["changes"][0]["value"]["messages"][0]["id"] = "wamid.4"
        payload["entry"].append(extra_entry)
        events = parse_events("whatsapp", "phone-1", payload)
        self.assertEqual([e.message_id for e in events], ["wamid.source.1", "wamid.2", "wamid.3", "wamid.4"])

    def test_whatsapp_status_and_account_mismatch(self):
        self.assertEqual(parse_events("whatsapp", "phone-1", fixture("whatsapp_status.json")), [])
        self.assert_error("invalid_payload", parse_events, "whatsapp", "wrong-phone", fixture("whatsapp_text.json"))

    def test_bsuid_without_phone_routes_with_recipient(self):
        event, = parse_events("whatsapp", "phone-1", fixture("whatsapp_bsuid.json"))
        self.assertEqual((event.recipient_type, event.user_id, event.sender_name),
                         ("bsuid", "US.123456789", "Customer"))
        req = send_request("whatsapp", "phone-1", event.chat_id, "Ответ", recipient_type=event.recipient_type)
        self.assertEqual(req.body["recipient"], "US.123456789")
        self.assertNotIn("to", req.body)

    def test_feishu_employee_and_recall_without_invented_actor(self):
        event, = parse_events("feishu", "app-1", fixture("feishu_text.json"))
        self.assertEqual((event.user_id, event.text, event.timestamp),
                         ("ou_employee_1", "您好，我来帮您确认。", 1795651200))
        self.assertFalse(event.has_mentions)
        recalled, = parse_events("feishu", "app-1", fixture("feishu_recalled.json"))
        self.assertEqual((recalled.event_type, recalled.user_id, recalled.message_id),
                         ("delete", "", "om.source.1"))

    def test_feishu_preserves_mention_metadata_for_staff_routing(self):
        for mention_id in ("all", "ou_bot", "ou_staff"):
            with self.subTest(mention_id=mention_id):
                payload = fixture("feishu_text.json")
                payload["event"]["message"]["mentions"] = [{"key": "@_user_1", "id": {"open_id": mention_id}}]
                event, = parse_events("feishu", "app-1", payload)
                self.assertTrue(event.has_mentions)

    def test_feishu_text_only_adds_a_trusted_group_mention(self):
        untrusted = '<at user_id="all"></at> 原文'
        plain = send_request("feishu", "app-1", "oc_test", untrusted)
        group_notice = send_request("feishu", "app-1", "oc_test", untrusted, mention_all=True)
        escaped = '‹at user_id="all"></at> 原文'
        self.assertEqual(json.loads(plain.body["content"]), {"text": escaped})
        self.assertEqual(json.loads(group_notice.body["content"]),
                         {"text": '<at user_id="all"></at> ' + escaped})

    def test_malformed_payloads_have_typed_error(self):
        for channel, payload in (("telegram", {}), ("max", {}), ("whatsapp", {}), ("feishu", {}),
                                 ("telegram", {"update_id": 1, "message": []}),
                                 ("whatsapp", {"object": "whatsapp_business_account", "entry": None})):
            with self.subTest(channel=channel, payload=payload):
                self.assert_error("invalid_payload", parse_events, channel, "account", payload)
        for bad_time in (None, True, "NaN", "inf", "bad", -1):
            payload = fixture("telegram_text.json")
            payload["message"]["date"] = bad_time
            self.assert_error("invalid_payload", parse_events, "telegram", "account", payload)

    def test_unsupported_media_never_becomes_normal_text(self):
        tg = fixture("telegram_text.json")
        tg["message"]["video"] = {"file_id": "source-video"}
        mx = fixture("max_text.json")
        mx["message"]["body"]["attachments"] = [{"type": "sticker", "payload": {"code": "x"}}]
        wa = fixture("whatsapp_text.json")
        wa["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "location"
        fs = fixture("feishu_text.json")
        fs["event"]["message"]["message_type"] = "sticker"
        for channel, account, payload in (("telegram", "account", tg), ("max", "account", mx),
                                           ("whatsapp", "phone-1", wa), ("feishu", "account", fs)):
            self.assert_error("unsupported_media", parse_events, channel, account, payload)

    def test_media_normalization_all_channels(self):
        tg, = parse_events("telegram", "account", fixture("telegram_media.json"))
        self.assertEqual((tg.attachments[0].kind, tg.attachments[0].name), ("file", "report.pdf"))
        mx, = parse_events("max", "account", fixture("max_media.json"))
        self.assertEqual(mx.attachments[0].kind, "image")
        wa = fixture("whatsapp_text.json")
        msg = wa["entry"][0]["changes"][0]["value"]["messages"][0]
        msg["type"], msg["audio"] = "audio", {"id": "wa.source.voice", "mime_type": "audio/ogg", "voice": True}
        ev, = parse_events("whatsapp", "phone-1", wa)
        self.assertEqual(ev.attachments[0].kind, "audio")
        fs = fixture("feishu_text.json")
        fs["event"]["message"].update(message_type="image", content='{"image_key":"img.source"}')
        ev, = parse_events("feishu", "account", fs)
        self.assertEqual(ev.attachments[0].remote_id, "img.source")

    def test_telegram_photo_selects_largest_and_voice_normalizes(self):
        payload = fixture("telegram_text.json")
        del payload["message"]["text"]
        payload["message"]["photo"] = [{"file_id": "small"}, {"file_id": "large"}]
        ev, = parse_events("telegram", "account", payload)
        self.assertEqual(ev.attachments[0].remote_id, "large")
        del payload["message"]["photo"]
        payload["message"]["voice"] = {"file_id": "voice-id"}
        ev, = parse_events("telegram", "account", payload)
        self.assertEqual(ev.attachments[0].kind, "audio")

    def test_outgoing_text_paths_auth_and_json_content(self):
        tg = send_request("telegram", "official", "123", "Hello", token="123:test")
        self.assertEqual((tg.method, tg.path, tg.body), ("POST", "/bot123:test/sendMessage", {"chat_id": "123", "text": "Hello"}))
        mx = send_request("max", "official", "234", "Привет", token="test")
        self.assertEqual((mx.path, mx.headers["Authorization"]), ("/messages?chat_id=234", "test"))
        wa = send_request("whatsapp", "phone-1", "15550001111", "Hello", token="test")
        self.assertEqual((wa.path, wa.headers["Authorization"]), ("/v27.0/phone-1/messages", "Bearer test"))
        self.assertEqual(wa.body["text"]["body"], "Hello")
        fs = send_request("feishu", "app", "oc.room", "你好", token="test")
        self.assertEqual(fs.path, "/open-apis/im/v1/messages?receive_id_type=chat_id")
        self.assertEqual(json.loads(fs.body["content"]), {"text": "你好"})
        self.assertEqual(fs.headers["Authorization"], "Bearer test")

    def test_outgoing_media_uses_destination_upload_id(self):
        for channel in CAPABILITIES:
            for kind in ("image", "file", "audio"):
                with self.subTest(channel=channel, kind=kind):
                    req = send_request(channel, "account", "123", "", attachment=Attachment(kind, "destination-upload-id", "report.pdf"))
                    serialized = json.dumps(req.body)
                    self.assertIn("destination-upload-id", serialized)
                    self.assertNotIn("source-id", serialized)
        mx = send_request("max", "account", "123", "", attachment=Attachment("file", "dest"))
        self.assertEqual(mx.body["attachments"], [{"type": "file", "payload": {"token": "dest"}}])
        fs = send_request("feishu", "account", "123", "", attachment=Attachment("image", "dest"))
        self.assertEqual(json.loads(fs.body["content"]), {"image_key": "dest"})

    def test_media_caption_is_not_silently_lost(self):
        self.assert_error("unsupported", send_request, "feishu", "app", "chat", "说明", attachment=Attachment("file", "id"))
        self.assert_error("unsupported", send_request, "whatsapp", "phone", "chat", "caption", attachment=Attachment("audio", "id"))
        self.assert_error("invalid_payload", send_request, "telegram", "app", "chat", "x" * 1025, attachment=Attachment("image", "id"))

    def test_text_limits_and_empty_messages(self):
        for channel, limit in (("telegram", 4096), ("max", 4000), ("whatsapp", 4096)):
            self.assert_error("invalid_payload", send_request, channel, "account", "chat", "x" * (limit + 1))
            self.assert_error("invalid_payload", send_request, channel, "account", "chat", "")

    def test_delete_shapes_and_whatsapp_unsupported(self):
        tg = delete_request("telegram", "official", "123", "42")
        self.assertEqual(tg.body, {"chat_id": "123", "message_id": 42})
        mx = delete_request("max", "official", "123", "mid.x&y")
        self.assertEqual(parse_qs(urlsplit(mx.path).query), {"message_id": ["mid.x&y"]})
        self.assertEqual(mx.method, "DELETE")
        fs = delete_request("feishu", "official", "chat", "om_123")
        self.assertEqual(fs.path, "/open-apis/im/v1/messages/om_123")
        self.assert_error("unsupported", delete_request, "whatsapp", "phone", "chat", "wamid.1")
        self.assertFalse(CAPABILITIES["whatsapp"]["delete"])
        self.assertEqual(API_BASE_URLS["max"], "https://platform-api2.max.ru")

    def test_success_response_message_ids(self):
        for channel, payload, expected in (
            ("telegram", {"ok": True, "result": {"message_id": 42}}, "42"),
            ("max", {"message": {"body": {"mid": "mid.42"}}}, "mid.42"),
            ("whatsapp", {"messages": [{"id": "wamid.42"}]}, "wamid.42"),
            ("feishu", {"code": 0, "data": {"message_id": "om_42"}}, "om_42"),
        ):
            self.assertEqual(parse_response(channel, "send", payload), expected)

    def test_semantic_delete_success_and_failure(self):
        for channel, payload in (("telegram", {"ok": True, "result": True}),
                                 ("max", {"success": True}), ("feishu", {"code": 0})):
            self.assertEqual(parse_response(channel, "delete", payload), "")
        self.assert_error("provider_error", parse_response, "max", "delete", {"success": False, "message": "Forbidden"})
        self.assert_error("invalid_response", parse_response, "telegram", "delete", {"ok": True, "result": False})
        self.assert_error("provider_error", parse_response, "feishu", "delete", {"code": 230001, "msg": "No permission"})

    def test_provider_errors_and_rate_limit_delay(self):
        err = self.assert_error("rate_limited", parse_response, "telegram", "send",
                                {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}})
        self.assertEqual(err.retry_after, 30)
        self.assert_error("unauthorized", parse_response, "whatsapp", "send", {"error": {"code": 401, "message": "Invalid token"}})
        self.assert_error("invalid_response", parse_response, "whatsapp", "send", {"messages": []})
        self.assert_error("invalid_response", parse_response, "max", "send", {"message": {"body": {}}})

    def test_empty_or_missing_receipts_preserve_unknown_outcome(self):
        # A platform may have performed the mutation before returning a bad
        # HTTP-200 body. These must never be classified as rejected/failed.
        for channel, operations in (("telegram", ("send", "delete")),
                                    ("max", ("send", "delete")),
                                    ("whatsapp", ("send",)),
                                    ("feishu", ("send", "delete", "create_room", "add_members"))):
            for operation in operations:
                for payload in ({}, None, [], ""):
                    with self.subTest(channel=channel, operation=operation, payload=payload):
                        self.assert_error("invalid_response", parse_response, channel, operation, payload)

    def test_missing_or_wrong_success_flags_are_not_rejections(self):
        for bad_flag in (None, 0, 1, "true", "false", {}, []):
            for operation in ("send", "delete"):
                self.assert_error("invalid_response", parse_response, "telegram", operation,
                                  {"ok": bad_flag, "result": {"message_id": 42}})
            self.assert_error("invalid_response", parse_response, "max", "delete", {"success": bad_flag})
        for bad_code in (None, True, False, "0", "230001", 0.0, {}, []):
            for operation in ("send", "delete", "create_room", "add_members"):
                self.assert_error("invalid_response", parse_response, "feishu", operation, {"code": bad_code})
        for incomplete in ({"ok": True}, {"ok": True, "result": None}, {"ok": True, "result": 1}):
            self.assert_error("invalid_response", parse_response, "telegram", "delete", incomplete)

    def test_malformed_ids_in_success_receipts_are_unknown(self):
        for bad_id in (None, True, False, {}, [], 0, -1, "", " ", "bad id"):
            for channel, payload in (
                ("max", {"message": {"body": {"mid": bad_id}}}),
                ("whatsapp", {"messages": [{"id": bad_id}]}),
                ("feishu", {"code": 0, "data": {"message_id": bad_id}}),
            ):
                with self.subTest(channel=channel, id=bad_id):
                    self.assert_error("invalid_response", parse_response, channel, "send", payload)
            self.assert_error("invalid_response", parse_response, "feishu", "create_room",
                              {"code": 0, "data": {"chat_id": bad_id}})
        for bad_id in (None, True, 0, -1, "42", "not-a-number", 1.5, {}, []):
            self.assert_error("invalid_response", parse_response, "telegram", "send",
                              {"ok": True, "result": {"message_id": bad_id}})

    def test_missing_message_fields_and_broken_error_envelopes_are_unknown(self):
        for channel, payload in (
            ("telegram", {"ok": True, "result": {}}),
            ("max", {"message": {}}),
            ("max", {"success": True}),
            ("whatsapp", {"messages": [{}]}),
            ("whatsapp", {"messages": [{"id": "wamid.1"}, {"id": "wamid.2"}]}),
            ("whatsapp", {"error": {}}),
            ("whatsapp", {"error": {"code": []}}),
            ("feishu", {"code": 0, "data": {}}),
        ):
            self.assert_error("invalid_response", parse_response, channel, "send", payload)
        self.assert_error("invalid_response", parse_response, "max", "delete", {"code": {}})
        self.assert_error("invalid_response", parse_response, "max", "delete", {"code": "", "message": ""})

    def test_only_explicit_rejections_are_provider_failures(self):
        for channel, operation, payload in (
            ("telegram", "send", {"ok": False}),
            ("telegram", "delete", {"ok": False}),
            ("max", "send", {"success": False}),
            ("max", "delete", {"success": False}),
            ("max", "delete", {"code": "denied", "message": "not allowed"}),
            ("feishu", "delete", {"code": 230001}),
            ("whatsapp", "send", {"error": {"code": 100, "message": "invalid parameter"}}),
        ):
            self.assert_error("provider_error", parse_response, channel, operation, payload)

    def test_telegram_and_max_secrets_case_insensitive_headers(self):
        for channel, header in (("telegram", "X-Telegram-Bot-Api-Secret-Token"), ("max", "X-Max-Bot-Api-Secret")):
            self.assertTrue(verify_webhook(channel, b"{}", {header.lower(): "secret"}, "secret"))
            self.assertFalse(verify_webhook(channel, b"{}", {header: "wrong"}, "secret"))
            self.assertFalse(verify_webhook(channel, b"{}", {}, "secret"))
            self.assertFalse(verify_webhook(channel, b"{}", {}, ""))

    def test_whatsapp_hmac_on_original_bytes(self):
        raw = b'{ "value": 1 }'
        signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature}
        self.assertTrue(verify_webhook("whatsapp", raw, headers, "secret"))
        self.assertFalse(verify_webhook("whatsapp", b'{"value":1}', headers, "secret"))
        self.assertFalse(verify_webhook("whatsapp", raw, {}, "secret"))

    def test_feishu_token_signature_and_no_downgrade(self):
        raw = json.dumps(fixture("feishu_text.json"), ensure_ascii=False).encode()
        self.assertTrue(verify_webhook("feishu", raw, {}, "fixture-secret"))
        self.assertFalse(verify_webhook("feishu", raw, {}, "wrong"))
        timestamp, nonce, key = "1795651200", "nonce-1", "encrypt-key"
        signature = hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest()
        headers = {"X-Lark-Request-Timestamp": timestamp, "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": signature}
        self.assertTrue(verify_webhook("feishu", raw, headers, key))
        self.assertFalse(verify_webhook("feishu", raw + b" ", headers, key))
        # A bad signature must not fall back to the valid plaintext token.
        self.assertFalse(verify_webhook("feishu", raw, {"X-Lark-Signature": "bad"}, "fixture-secret"))
        self.assertFalse(verify_webhook("feishu", b"not json", {}, "fixture-secret"))

    def test_feishu_encryption_is_not_falsely_supported(self):
        self.assert_error("unsupported", parse_events, "feishu", "app", {"encrypt": "ciphertext"})

    def test_feishu_room_creation_members_and_partial_failure(self):
        req = feishu_create_room_request("Customer room", ["ou_1", "ou_2", "ou_1"])
        self.assertEqual(req.path, "/open-apis/im/v1/chats?user_id_type=open_id")
        self.assertEqual(req.body["user_id_list"], ["ou_1", "ou_2"])
        self.assertEqual(parse_response("feishu", "create_room", {"code": 0, "data": {"chat_id": "oc_1"}}), "oc_1")
        add = feishu_add_members_request("oc_1", ["ou_3"])
        self.assertEqual(add.path, "/open-apis/im/v1/chats/oc_1/members?member_id_type=open_id")
        self.assertEqual(add.body, {"id_list": ["ou_3"]})
        self.assertEqual(parse_response("feishu", "add_members", {"code": 0, "data": {}}), "")
        for key in ("invalid_id_list", "not_existed_id_list", "pending_approval_id_list"):
            self.assert_error("partial_failure", parse_response, "feishu", "add_members", {"code": 0, "data": {key: ["ou_3"]}})


if __name__ == "__main__":
    unittest.main()
