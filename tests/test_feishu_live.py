import json
import subprocess
import tempfile
import threading
import unittest
from collections import deque
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from omni_service.approval_card import build_approval_card, parse_feishu_card_action
from omni_service.channels import feishu_card_request
from omni_service.feishu_live import FeishuLive
from omni_service.models import ChannelError, DraftDecision


_IDENTITY = {"appId": "cli_test", "brand": "feishu", "identity": "bot", "available": True}
_GROUP = {"chat_mode": "group", "chat_type": "private", "chat_status": "normal", "external": False}
_MEMBERS = {"chat_id": "oc_test", "users": [{"member_id": "ou_staff"}],
            "bots": [{"app_id": "cli_test"}], "has_more": False}
_EVENT = {"type": "im.message.receive_v1", "chat_id": "oc_test", "chat_type": "group",
          "sender_id": "ou_staff", "sender_type": "user", "message_type": "text",
          "message_id": "om_test", "create_time": "1700000000000", "content": "收到"}
_DRAFT_ID = "draft_" + "a" * 32
_VERSION = "1234567890abcdef"
_APPROVAL = {"omni_action": "approve", "draft_id": _DRAFT_ID, "version": _VERSION}


def _data(value):
    return {"ok": True, "identity": "bot", "data": value}


def _cli_result(value):
    return subprocess.CompletedProcess([], 0, json.dumps(value), "")


class FeishuLiveTest(unittest.TestCase):
    def setup_adapter(self, cli="lark-cli"):
        return FeishuLive("oc_test", ["ou_staff"], app_id="cli_test", cli=cli)

    def test_send_checks_bot_and_audience_and_passes_content_via_stdin(self):
        adapter = self.setup_adapter()
        receipt = _data({"chat_id": "oc_test", "message_id": "om_sent"})
        results = deque([_IDENTITY, _data(_GROUP), _data(_MEMBERS), receipt] * 3)
        with patch("omni_service.feishu_live.subprocess.run", side_effect=lambda *_a, **_k: _cli_result(results.popleft())) as run:
            self.assertEqual(adapter.send("<at user_id=\"all\">客户</at>", "op1"), "om_sent")
            self.assertEqual(adapter.send("<at user_id=\"all\">客户</at>", "op1"), "om_sent")
            self.assertEqual(adapter.send('<at user_id="all">客户</at>', "rules", mention_all=True), "om_sent")
        first, second = run.call_args_list[3], run.call_args_list[7]
        self.assertEqual(first, second)
        args = first.args[0]
        self.assertIn("--profile=cli_test", args)
        self.assertIn("oc_test", args)
        self.assertIn("bot", args)
        self.assertNotIn("客户", " ".join(args))
        self.assertIn("--idempotency-key", args)
        self.assertEqual(json.loads(first.kwargs["input"])["text"], '‹at user_id="all">客户</at>')
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["text"],
                         '<at user_id="all"></at> ‹at user_id="all">客户</at>')

    def test_targeted_send_limits_mention_to_the_allowed_staff(self):
        adapter = self.setup_adapter()
        with patch("omni_service.feishu_live.subprocess.run") as run:
            for invalid in ("all", "ou_unlisted", ["ou_staff"]):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ChannelError, "feishu_invalid_mention"):
                    adapter.send("交接", "handoff", mention_user_id=invalid)
            with self.assertRaisesRegex(ChannelError, "one specific user"):
                adapter.send("交接", "handoff", mention_all=True, mention_user_id="ou_staff")
            run.assert_not_called()

        receipt = _data({"chat_id": "oc_test", "message_id": "om_handoff"})
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, [_IDENTITY, _data(_GROUP), _data(_MEMBERS), receipt]
        )) as run:
            self.assertEqual(adapter.send("交接给客服", "handoff", mention_user_id="ou_staff"), "om_handoff")
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["text"],
                         '<at user_id="ou_staff"></at> 交接给客服')

    def test_approval_card_preserves_preview_and_sends_interactive_message(self):
        text = '[AI] Exact **plain preview**\nSecond line'
        reference = '群内参考'
        card = build_approval_card(draft_id=_DRAFT_ID, version=_VERSION, customer_text=text, reference=reference)
        preview, context, *buttons = card["body"]["elements"]
        self.assertEqual(preview["text"], {"tag": "plain_text", "content": text})
        self.assertEqual(context["text"]["content"], reference)
        self.assertEqual([button["text"]["content"] for button in buttons], ["可以发送", "不可以发送"])
        self.assertEqual([button["behaviors"] for button in buttons], [
            [{"type": "callback", "value": _APPROVAL}],
            [{"type": "callback", "value": {**_APPROVAL, "omni_action": "reject"}}],
        ])
        request = feishu_card_request("oc_test", card)
        self.assertEqual(request.body["msg_type"], "interactive")
        self.assertEqual(json.loads(request.body["content"]), card)

        receipt = _data({"chat_id": "oc_test", "message_id": "om_card"})
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, [_IDENTITY, _data(_GROUP), _data(_MEMBERS), receipt]
        )) as run:
            self.assertEqual(self.setup_adapter().send_card(card, "review:first"), "om_card")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--msg-type") + 1], "interactive")
        self.assertEqual(args[args.index("--content") + 1], "-")
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), card)

    def test_patch_card_requires_exact_room_roster_and_uses_nested_content(self):
        adapter = FeishuLive(None, ["ou_staff"], app_id="cli_test")
        card = {"schema": "2.0", "body": {"elements": []}}
        members = {**_MEMBERS, "users": [{"member_id": "ou_second"}]}
        preflight = [_IDENTITY, _data(_GROUP), _data(members)]
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, preflight
        )) as run:
            self.assertEqual(adapter.group_members("oc_test"), frozenset({"ou_second"}))
            self.assertEqual(run.call_count, 3)
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, preflight
        )) as run:
            with self.assertRaisesRegex(ChannelError, "^feishu_members_mismatch$"):
                adapter.patch_card("om_card", card, chat_id="oc_test")
            self.assertEqual(run.call_count, 3)
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, [*preflight, _data({})]
        )) as run:
            self.assertEqual(adapter.patch_card("om_card", card, chat_id="oc_test", staff_ids=["ou_second"]),
                             "om_card")
        self.assertEqual(run.call_args.args[0][2:], ["im", "messages", "patch", "--message-id", "om_card",
                                                   "--data", "-", "--as", "bot", "--format", "json"])
        self.assertEqual(json.loads(json.loads(run.call_args.kwargs["input"])["content"]), card)
        self.assertEqual(adapter.staff_ids, frozenset({"ou_staff"}))

    def test_staff_override_validates_before_any_request_and_controls_create_or_send(self):
        adapter = FeishuLive(None, ["ou_staff"], app_id="cli_test")
        with patch("omni_service.feishu_live.subprocess.run") as run:
            for invalid in ([], ["oc_staff"], ["ou_staff", "ou_staff"], [f"ou_staff_{i}" for i in range(51)]):
                with self.assertRaisesRegex(ChannelError, "^feishu_invalid_staff$"):
                    adapter.create_room("Synthetic room", "room:first", staff_ids=invalid)
            with self.assertRaisesRegex(ChannelError, "^feishu_invalid_mention$"):
                adapter.send("handoff", "note:first", chat_id="oc_test", staff_ids=["ou_second"],
                             mention_user_id="ou_staff")
            run.assert_not_called()
        members = {**_MEMBERS, "users": [{"member_id": "ou_second"}]}
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(_cli_result, [
            _IDENTITY, _data({**_GROUP, "chat_id": "oc_test"}),
            _IDENTITY, _data(_GROUP), _data(members), _data({"chat_id": "oc_test", "message_id": "om_sent"}),
        ])) as run:
            self.assertEqual(adapter.create_room("Synthetic room", "room:first", staff_ids=["ou_second"]), "oc_test")
            self.assertEqual(adapter.send("handoff", "note:first", chat_id="oc_test", staff_ids=["ou_second"],
                                          mention_user_id="ou_second"), "om_sent")
        self.assertEqual(json.loads(run.call_args_list[1].kwargs["input"])["user_id_list"], ["ou_second"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["text"], '<at user_id="ou_second"></at> handoff')

    def test_raw_card_callback_requires_matching_app_and_button_payload(self):
        header = {"event_type": "card.action.trigger", "event_id": "ev_test", "app_id": "demo-feishu"}
        event = {"host": "im_message", "operator": {"open_id": "employee-1"},
                 "context": {"open_message_id": "om_card", "open_chat_id": "oc_test"},
                 "action": {"tag": "button", "value": _APPROVAL}}
        payload = {"schema": "2.0", "header": header, "event": event}
        self.assertEqual(parse_feishu_card_action(payload, app_id="demo-feishu"),
                         DraftDecision("demo-feishu", "ev_test", "oc_test", "employee-1", "om_card",
                                       _DRAFT_ID, _VERSION, "approve"))
        with self.assertRaisesRegex(ChannelError, "^invalid_payload$"):
            parse_feishu_card_action(payload, app_id="demo-other")
        for action in ({"tag": "select_static", "value": _APPROVAL},
                       {"tag": "button", "value": {**_APPROVAL, "customer_text": "swap"}}):
            with self.subTest(action=action), self.assertRaisesRegex(ChannelError, "^invalid_payload$"):
                parse_feishu_card_action({**payload, "event": {**event, "action": action}}, app_id="demo-feishu")

    def test_unapproved_members_block_the_send_before_issuing_the_command(self):
        adapter = self.setup_adapter()
        scenarios = [
            {**_MEMBERS, "users": [{"member_id": "ou_staff"}, {"member_id": "ou_other"}]},
            {**_MEMBERS, "bots": [{"app_id": "cli_other"}]},
            {**_MEMBERS, "has_more": True},
        ]
        for membership in scenarios:
            with self.subTest(membership=membership), patch(
                "omni_service.feishu_live.subprocess.run",
                side_effect=map(_cli_result, [_IDENTITY, _data(_GROUP), _data(membership)]),
            ) as run:
                with self.assertRaisesRegex(ChannelError, "feishu_members_mismatch"):
                    adapter.send("sample", "op1")
                self.assertEqual(run.call_count, 3)

    def test_additional_bot_roster_must_match_explicitly_allowed_app_ids(self):
        adapter = FeishuLive("oc_test", ["ou_staff"], app_id="cli_test", allowed_bot_app_ids=["cli_assistant"])
        allowed = {**_MEMBERS, "bots": [*_MEMBERS["bots"], {"app_id": "cli_assistant"}]}
        with patch("omni_service.feishu_live.subprocess.run",
                   side_effect=map(_cli_result, [_IDENTITY, _data(_GROUP), _data(allowed)])):
            adapter.preflight()
        for membership in (_MEMBERS, {**allowed, "bots": [*allowed["bots"], {"app_id": "cli_other"}]}):
            with self.subTest(membership=membership), patch(
                "omni_service.feishu_live.subprocess.run",
                side_effect=map(_cli_result, [_IDENTITY, _data(_GROUP), _data(membership)]),
            ):
                with self.assertRaisesRegex(ChannelError, "feishu_members_mismatch"):
                    adapter.preflight()
        for invalid in (None, "cli_assistant", ["ou_staff"], [None]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ChannelError, "feishu_invalid_config"):
                FeishuLive("oc_test", ["ou_staff"], app_id="cli_test", allowed_bot_app_ids=invalid)

    def test_receipt_must_confirm_group_and_message(self):
        adapter = self.setup_adapter()
        with patch(
            "omni_service.feishu_live.subprocess.run",
            side_effect=map(_cli_result, [_IDENTITY, _data(_GROUP), _data(_MEMBERS),
                                         _data({"chat_id": "oc_other", "message_id": "om_sent"})]),
        ):
            with self.assertRaisesRegex(ChannelError, "delivery_unknown"):
                adapter.send("sample", "op1")

    def test_managed_private_group_creation_is_scoped_and_repeatable(self):
        adapter = FeishuLive(None, ["ou_second", "ou_staff"], app_id="cli_test",
                             allowed_bot_app_ids=["cli_companion"])
        result = _data({**_GROUP, "chat_id": "oc_created"})
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, [_IDENTITY, result] * 3
        )) as run:
            self.assertEqual(adapter.create_room("Synthetic group", "room:first"), "oc_created")
            self.assertEqual(adapter.create_room("Synthetic group", "room:first"), "oc_created")
            self.assertEqual(adapter.create_room("Synthetic group", "room:second"), "oc_created")

        first, repeated, different = run.call_args_list[1::2]
        self.assertEqual(first, repeated)
        args = first.args[0]
        self.assertEqual(args[2:5], ["im", "chats", "create"])
        self.assertEqual(args[args.index("--user-id-type") + 1], "open_id")
        request_uuid = args[args.index("--uuid") + 1]
        self.assertEqual(str(UUID(request_uuid)), request_uuid)
        self.assertNotIn(request_uuid, different.args[0])
        self.assertEqual(json.loads(first.kwargs["input"]), {
            "name": "Synthetic group", "chat_mode": "group", "chat_type": "private", "external": False,
            "group_message_type": "chat", "user_id_list": ["ou_second", "ou_staff"],
            "bot_id_list": ["cli_companion"],
        })

    def test_managed_room_requires_a_private_internal_creation_receipt(self):
        adapter = FeishuLive(None, ["ou_staff"], app_id="cli_test")
        for mutation in ({"chat_id": "room_incorrect"}, {"chat_type": "public"}, {"external": True}):
            receipt = _data({**_GROUP, "chat_id": "oc_created", **mutation})
            with self.subTest(mutation=mutation), patch("omni_service.feishu_live.subprocess.run",
                side_effect=map(_cli_result, [_IDENTITY, receipt])
            ) as run:
                with self.assertRaisesRegex(ChannelError, "^delivery_unknown$"):
                    adapter.create_room("Synthetic group", "room:uncertain")
                self.assertEqual(run.call_count, 2)

    def test_managed_send_requires_explicit_chat_and_checks_its_roster(self):
        adapter, pinned = FeishuLive(None, ["ou_staff"], app_id="cli_test"), self.setup_adapter()
        with patch("omni_service.feishu_live.subprocess.run") as run:
            with self.assertRaisesRegex(ChannelError, "feishu_invalid_chat"):
                adapter.send("sample", "op1")
            with self.assertRaisesRegex(ChannelError, "feishu_invalid_chat"):
                pinned.send("sample", "op1", chat_id="oc_other")
            with self.assertRaisesRegex(ChannelError, "feishu_room_creation_disabled"):
                pinned.create_room("Cannot create", "op1")
            run.assert_not_called()

        members = _data({**_MEMBERS, "chat_id": "oc_created"})
        receipt = _data({"chat_id": "oc_created", "message_id": "om_sent"})
        with patch("omni_service.feishu_live.subprocess.run", side_effect=map(
            _cli_result, [_IDENTITY, _data(_GROUP), members, receipt]
        )) as run:
            self.assertEqual(adapter.send("room rule", "room:rule", chat_id="oc_created", mention_all=True), "om_sent")
        self.assertTrue(all("oc_created" in step.args[0] for step in run.call_args_list[1:]))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["text"], '<at user_id="all"></at> room rule')

    def _stub_cli(self, directory, trace, *, managed=False):
        script = Path(directory) / "lark-cli"
        responses = {
            ("whoami", "--as"): _IDENTITY,
            ("im", "chats"): _data(_GROUP),
            ("im", "+chat-members-list"): _data(_MEMBERS),
        }
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            f"response = {responses!r}.get(tuple(sys.argv[2:4]))\n"
            "if response is not None:\n"
            "    print(json.dumps(response))\n"
            "    sys.exit(0)\n"
            "if sys.argv[2:4] != ['event', 'consume']:\n"
            "    sys.exit(2)\n"
            + trace,
            encoding="utf-8",
        )
        script.chmod(0o700)
        if managed:
            return FeishuLive(None, ["ou_staff"], app_id="cli_test", cli=str(script))
        return self.setup_adapter(str(script))

    def test_stream_filters_chat_sender_bot_and_type_before_returning_message(self):
        events = [{**_EVENT, **mutation} for mutation in (
            {"chat_id": "oc_outside"}, {"sender_id": "ou_other"}, {"sender_type": "bot"},
            {"message_type": "post"}, {"chat_type": "p2p"}, {"create_time": "bad"},
            {"event_id": "ev_first"}, {"event_id": "ev_retry"},
            {"message_id": "om_mention", "content": "员工内部协调", "has_mentions": True},
        )]
        lines = "\n".join(json.dumps(event) for event in events)
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, (
                "arg = sys.argv[sys.argv.index('--jq') + 1]\n"
                "assert 'oc_test' in arg and 'ou_staff' in arg and 'text' in arg\n"
                "assert 'has_mentions:' in arg and '.mentions' in arg\n"
                "assert '--timeout' in sys.argv\n"
                "print('[event] ready event_key=im.message.receive_v1', file=sys.stderr)\n"
                f"print({lines!r})\n"
                "print('[event] exited (reason: timeout)', file=sys.stderr)\n"
            ))
            accepted = list(adapter.events(duration=1))
        self.assertEqual(len(accepted), 3)
        self.assertEqual(accepted[0], accepted[1])
        self.assertEqual((accepted[0].text, accepted[0].event_id, accepted[0].timestamp),
                         ("收到", "om_test", 1700000000))
        self.assertFalse(accepted[0].has_mentions)
        self.assertTrue(accepted[2].has_mentions)

    def test_managed_stream_only_returns_rooms_currently_mapped(self):
        events = [{**_EVENT, **mutation} for mutation in (
            {"chat_id": "oc_outside", "content": "unrelated"},
            {"chat_id": "oc_first"},
            {"chat_id": "oc_second", "has_mentions": True},
            {"chat_id": "oc_first", "sender_type": "bot"},
        )]
        lines = "\n".join(json.dumps(event) for event in events)
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, (
                "arg = sys.argv[sys.argv.index('--jq') + 1]\n"
                "assert '.chat_id ==' not in arg and 'ou_staff' in arg\n"
                "print('[event] ready event_key=im.message.receive_v1', file=sys.stderr)\n"
                f"print({lines!r})\n"
                "print('[event] exited (reason: timeout)', file=sys.stderr)\n"
            ), managed=True)
            mapped = {"oc_first"}
            with closing(adapter.events(duration=1, room_allowed=mapped.__contains__)) as stream:
                self.assertEqual(next(stream).chat_id, "oc_first")
                mapped.add("oc_second")
                second, = list(stream)
                self.assertEqual(second.chat_id, "oc_second")
                self.assertTrue(second.has_mentions)

    def test_card_stream_limits_clicks_to_mapped_rooms_and_authorized_staff(self):
        event = {"type": "card.action.trigger", "host": "im_message", "action_tag": "button",
                 "event_id": "ev_test", "chat_id": "oc_test", "operator_id": "ou_staff",
                 "message_id": "om_card", "action_value": json.dumps(_APPROVAL),
                 "token": "omitted-token", "card_content": "omitted-card"}
        events = [{**event, **mutation} for mutation in (
            {"chat_id": "oc_unmapped"}, {"operator_id": "ou_other"}, {"operator_id": "employee-1"},
            {"host": "im_top_notice"}, {"action_tag": "select_static"}, {"form_value": '{"field":"text"}'},
            {"action_value": json.dumps({**_APPROVAL, "version": "bad"})}, {},
            {"event_id": "ev_second", "action_value": json.dumps({**_APPROVAL, "omni_action": "reject"})},
        )]
        lines = "\n".join(json.dumps(row) for row in events)
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, (
                "assert sys.argv[4] == 'card.action.trigger'\n"
                "arg = sys.argv[sys.argv.index('--jq') + 1]\n"
                "assert 'ou_staff' in arg and 'action_value' in arg and 'im_message' in arg\n"
                "assert 'token' not in arg and 'card_content' not in arg\n"
                "print('[event] ready event_key=card.action.trigger', file=sys.stderr)\n"
                f"print({lines!r})\n"
                "print('[event] exited (reason: timeout)', file=sys.stderr)\n"
            ), managed=True)
            accepted = list(adapter.card_events(duration=1, room_allowed={"oc_test"}.__contains__))
        self.assertEqual(accepted, [
            DraftDecision("cli_test", "ev_test", "oc_test", "ou_staff", "om_card", _DRAFT_ID, _VERSION, "approve"),
            DraftDecision("cli_test", "ev_second", "oc_test", "ou_staff", "om_card", _DRAFT_ID, _VERSION, "reject"),
        ])

    def test_per_room_membership_stream_keeps_identity_checks_and_requires_room_mapping(self):
        card = {"type": "card.action.trigger", "host": "im_message", "action_tag": "button",
                "event_id": "ev_test", "chat_id": "oc_test", "operator_id": "ou_new",
                "message_id": "om_card", "action_value": json.dumps(_APPROVAL)}
        with tempfile.TemporaryDirectory() as directory:
            for record, method, actor in (
                ({**_EVENT, "sender_id": "ou_new"}, "events", "sender_id"),
                (card, "card_events", "operator_id"),
            ):
                with self.subTest(method=method):
                    lines = "\n".join(json.dumps({**record, **change}) for change in (
                        {"chat_id": "oc_unmapped"}, {actor: "employee-untrusted"}, {},
                    ))
                    adapter = self._stub_cli(directory, (
                        "arg = sys.argv[sys.argv.index('--jq') + 1]\n"
                        "assert 'ou_staff' not in arg and 'index(' not in arg\n"
                        "print(f'[event] ready event_key={sys.argv[4]}', file=sys.stderr)\n"
                        f"print({lines!r})\n"
                        "print('[event] exited (reason: timeout)', file=sys.stderr)\n"
                    ), managed=True)
                    consume = getattr(adapter, method)
                    with self.assertRaisesRegex(ChannelError, "^feishu_invalid_room_filter$"):
                        list(consume(duration=1, managed_membership=True))
                    accepted, = list(consume(duration=1, managed_membership=True,
                                             room_allowed={"oc_test"}.__contains__))
                    self.assertEqual(accepted.chat_id, "oc_test")

    def test_persistent_stream_stops_or_reports_unexpected_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, "")
            with self.assertRaisesRegex(ChannelError, "feishu_stream_interrupted"):
                list(adapter.events())
            event = json.dumps(_EVENT)
            adapter = self._stub_cli(directory, (
                "print('[event] ready event_key=im.message.receive_v1', file=sys.stderr, flush=True)\n"
                f"print({event!r}, flush=True)\n"
                "time.sleep(20)\n"
            ))
            stop = threading.Event()
            stream = adapter.events(stop)
            try:
                self.assertEqual(next(stream).message_id, "om_test")
                stop.set()
                self.assertEqual(list(stream), [])
            finally:
                stream.close()

    def test_stream_reports_cli_event_loss_without_exposing_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, (
                "print('[event] ready event_key=im.message.receive_v1', file=sys.stderr)\n"
                "print('WARN: JQ error: synthetic confidential contents', file=sys.stderr)\n"
            ))
            with self.assertRaisesRegex(ChannelError, "^feishu_event_loss$"):
                list(adapter.events())


if __name__ == "__main__":
    unittest.main()
