import json
import subprocess
import sys
import threading
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from omni_service.feishu_live import FeishuLive
from omni_service.feishu_sdk import RECALL_EVENT, SDK_STREAM, _dispatcher
from omni_service.models import Attachment, ChannelError
from omni_service.personal_runner import run


def envelope(event_type, event):
    return {
        "schema": "2.0",
        "header": {"event_id": "event-test", "event_type": event_type, "app_id": "cli_test",
                   "tenant_key": "tenant_test", "token": "fixture-verification-token"},
        "event": event,
    }


RECALL = envelope(RECALL_EVENT, {"message_id": "om_test", "chat_id": "oc_test",
                                "recall_time": "1700000000000", "recall_type": "message_owner"})
MESSAGE = envelope("im.message.receive_v1", {
    "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_staff"}},
    "message": {"message_id": "om_test", "chat_id": "oc_test", "chat_type": "group",
                "create_time": "1700000000000", "message_type": "text", "content": '{"text":"收到"}'},
})
CARD = envelope("card.action.trigger", {
    "operator": {"open_id": "ou_staff"}, "host": "im_message", "token": "fixture-card-token",
    "context": {"open_chat_id": "oc_test", "open_message_id": "om_card", "preview_token": "fixture-preview"},
    "action": {"tag": "button", "value": {"omni_action": "approve", "draft_id": "draft_" + "a" * 32,
                                         "version": "a" * 16}},
})


class FeishuSDKTest(unittest.TestCase):
    def setUp(self):
        self.adapter = FeishuLive("oc_test", ["ou_staff"], app_id="cli_test")

    def parse(self, wire, cards=False):
        return self.adapter._parse_sdk_event(json.dumps(wire), {"oc_test"}.__contains__, False,
                                             tenant_key="tenant_test", approval_cards=cards)

    def test_recall_uses_exact_trusted_room_and_owner_recall_without_invented_actor(self):
        source, event = self.parse(RECALL)
        self.assertEqual((source, event.event_type, event.message_id, event.chat_id, event.user_id),
                         ("recall", "delete", "om_test", "oc_test", ""))
        self.assertEqual(event.timestamp, 1700000000)
        for section, field, value in (
            ("header", "app_id", "cli_other"), ("header", "tenant_key", "tenant_other"),
            ("header", "event_id", None), ("event", "chat_id", "oc_other"),
            ("event", "message_id", "wrong"), ("event", "recall_time", "late"),
            ("event", "recall_type", "group_owner"),
        ):
            with self.subTest(field=field):
                wire = deepcopy(RECALL)
                wire[section][field] = value
                self.assertIsNone(self.parse(wire))

    def test_message_media_mention_and_card_match_live_semantics(self):
        source, event = self.parse(MESSAGE)
        self.assertEqual((source, event.event_id, event.text, event.has_mentions), ("message", "om_test", "收到", False))
        wire = deepcopy(MESSAGE)
        wire["event"]["message"]["content"] = json.dumps({"text": '<at user_id="all">Everyone</at>'})
        self.assertTrue(self.parse(wire)[1].has_mentions)
        wire["event"]["message"].update(message_type="media", content='{"file_key":"file_movie"}')
        self.assertEqual(self.parse(wire)[1].attachments, (Attachment("file", "file_movie", "video.mp4"),))
        wire["event"]["sender"]["sender_id"]["open_id"] = "ou_outsider"
        self.assertIsNone(self.parse(wire))
        self.assertIsNone(self.parse(CARD))
        source, decision = self.parse(CARD, cards=True)
        self.assertEqual((source, decision.user_id, decision.message_id, decision.decision),
                         ("card", "ou_staff", "om_card", "approve"))

    def test_dispatcher_registers_single_source_and_strips_wire_tokens(self):
        sdk = SimpleNamespace(EventDispatcherHandler=Mock())
        builder = sdk.EventDispatcherHandler.builder.return_value
        for method in ("register_p2_im_message_receive_v1", "register_p2_im_message_recalled_v1",
                       "register_p2_card_action_trigger"):
            getattr(builder, method).return_value = builder
        settings = {"app_id": "cli_test", "tenant_key": "tenant_test", "approval_cards": False}
        events = []
        _dispatcher(sdk, json.dumps, settings, events.append)
        callback = builder.register_p2_im_message_recalled_v1.call_args.args[0]
        callback(RECALL)
        builder.register_p2_card_action_trigger.assert_not_called()
        self.assertNotIn("token", events[0]["header"])
        _dispatcher(sdk, json.dumps, {**settings, "approval_cards": True}, events.append)
        callback = builder.register_p2_card_action_trigger.call_args.args[0]
        callback(CARD)
        self.assertNotIn("token", events[1]["event"])
        self.assertNotIn("preview_token", events[1]["event"]["context"])
        wrong = deepcopy(CARD)
        wrong["header"]["tenant_key"] = "tenant_other"
        callback(wrong)
        self.assertEqual(len(events), 2)

    def test_subprocess_unifies_sources_and_is_closed_when_consumer_stops(self):
        launch, workers = subprocess.Popen, []

        def start(args, **kwargs):
            self.assertEqual(args[-1], "omni_service.feishu_sdk")
            self.assertNotIn("fixture-secret", args)
            script = f"""import json,sys
settings = json.loads(sys.stdin.readline())
assert settings['app_secret'] == 'fixture-secret'
print('[event] ready event_key={SDK_STREAM}', file=sys.stderr, flush=True)
for event in {repr([MESSAGE, RECALL, CARD])}:
    print(json.dumps(event), flush=True)
sys.stdin.read(1)
"""
            proc = launch([sys.executable, "-c", script], **kwargs)
            workers.append(proc)
            return proc

        stop = threading.Event()
        with patch.object(self.adapter, "preflight"), patch("omni_service.feishu_live.subprocess.Popen", side_effect=start):
            stream = self.adapter.sdk_events(stop, app_secret="fixture-secret", tenant_key="tenant_test",
                                             approval_cards=True)
            self.assertEqual([next(stream)[0] for _ in range(3)], ["message", "recall", "card"])
            stop.set()
            self.assertEqual(list(stream), [])
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].poll())

    def test_unmapped_managed_room_is_rejected_at_start(self):
        adapter = FeishuLive(None, ["ou_staff"], app_id="cli_test")
        with self.assertRaisesRegex(ChannelError, "feishu_invalid_room_filter"):
            list(adapter.sdk_events(app_secret="fixture-secret", tenant_key="tenant_test"))

    def test_bridge_dispatches_all_sdk_events_without_starting_cli_consumers(self):
        message, recall, card = (self.parse(wire, cards=True)[1] for wire in (MESSAGE, RECALL, CARD))
        queued = threading.Event()
        bridge = Mock(managed=True, management_enabled=True, approval_cards=True)

        def stream(stop, **options):
            self.assertEqual(options, {
                "app_secret": "fixture-secret", "tenant_key": "tenant_test", "approval_cards": True,
                "room_allowed": bridge.room_allowed, "managed_membership": True,
            })
            yield "message", message
            yield "recall", recall
            yield "card", card
            queued.set()
            stop.wait(5)

        def poll(**_options):
            self.assertTrue(queued.wait(3))
            return []

        bridge.feishu.sdk_events.side_effect = stream
        bridge.telegram.poll.side_effect = poll
        bridge.drain.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run(bridge, feishu_sdk={"app_secret": "fixture-secret", "tenant_key": "tenant_test"})
        bridge.feishu.sdk_events.assert_called_once()
        bridge.feishu.events.assert_not_called()
        bridge.feishu.card_events.assert_not_called()
        self.assertEqual([call.args[0] for call in bridge.receive_feishu.call_args_list], [message, recall])
        bridge.receive_card_action.assert_called_once_with(card)
