import json
import subprocess
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

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
        results = deque([_IDENTITY, _data(_GROUP), _data(_MEMBERS), receipt] * 2)
        with patch("omni_service.feishu_live.subprocess.run", side_effect=lambda *_a, **_k: _cli_result(results.popleft())) as run:
            self.assertEqual(adapter.send("<at user_id=\"all\">客户</at>", "op1"), "om_sent")
            self.assertEqual(adapter.send("<at user_id=\"all\">客户</at>", "op1"), "om_sent")
        first, second = run.call_args_list[3], run.call_args_list[7]
        self.assertEqual(first, second)
        args = first.args[0]
        self.assertIn("--profile=cli_test", args)
        self.assertIn("oc_test", args)
        self.assertIn("bot", args)
        self.assertNotIn("客户", " ".join(args))
        self.assertIn("--idempotency-key", args)
        self.assertEqual(json.loads(first.kwargs["input"])["text"], '‹at user_id="all">客户</at>')

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

    def test_receipt_must_confirm_group_and_message(self):
        adapter = self.setup_adapter()
        with patch(
            "omni_service.feishu_live.subprocess.run",
            side_effect=map(_cli_result, [_IDENTITY, _data(_GROUP), _data(_MEMBERS),
                                         _data({"chat_id": "oc_other", "message_id": "om_sent"})]),
        ):
            with self.assertRaisesRegex(ChannelError, "delivery_unknown"):
                adapter.send("sample", "op1")

    def _stub_cli(self, directory, trace):
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
        return self.setup_adapter(str(script))

    def test_stream_filters_chat_sender_bot_and_type_before_returning_message(self):
        events = [{**_EVENT, **mutation} for mutation in (
            {"chat_id": "oc_outside"}, {"sender_id": "ou_other"}, {"sender_type": "bot"},
            {"message_type": "post"}, {"chat_type": "p2p"}, {"create_time": "bad"},
            {"event_id": "ev_first"}, {"event_id": "ev_retry"},
        )]
        lines = "\n".join(json.dumps(event) for event in events)
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._stub_cli(directory, (
                "arg = sys.argv[sys.argv.index('--jq') + 1]\n"
                "assert 'oc_test' in arg and 'ou_staff' in arg and 'text' in arg\n"
                "assert '--timeout' in sys.argv\n"
                "print('[event] ready event_key=im.message.receive_v1', file=sys.stderr)\n"
                f"print({lines!r})\n"
                "print('[event] exited (reason: timeout)', file=sys.stderr)\n"
            ))
            accepted = list(adapter.events(duration=1))
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0], accepted[1])
        self.assertEqual((accepted[0].text, accepted[0].event_id, accepted[0].timestamp),
                         ("收到", "om_test", 1700000000))

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
