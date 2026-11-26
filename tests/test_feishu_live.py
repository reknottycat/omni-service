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

from omni_service.feishu_live import FeishuLive
from omni_service.models import ChannelError


_IDENTITY = {"appId": "cli_test", "brand": "feishu", "identity": "bot", "available": True}
_GROUP = {"chat_mode": "group", "chat_type": "private", "chat_status": "normal", "external": False}
_MEMBERS = {"chat_id": "oc_test", "users": [{"member_id": "ou_staff"}],
            "bots": [{"app_id": "cli_test"}], "has_more": False}
_EVENT = {"type": "im.message.receive_v1", "chat_id": "oc_test", "chat_type": "group",
          "sender_id": "ou_staff", "sender_type": "user", "message_type": "text",
          "message_id": "om_test", "create_time": "1700000000000", "content": "收到"}


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
