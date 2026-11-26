"""Feishu bot transport for private pinned or bridge-managed groups."""

import json
import os
import re
import selectors
import subprocess
import time
from contextlib import suppress
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from .approval_card import CARD_ACTION, card_decision
from .channels import feishu_text
from .models import ChannelError, InboundEvent


_EVENT = "im.message.receive_v1"
_MAX_LINE = 256 * 1024
_CLI_TIMEOUT = 15
_EVENT_LOSS_PREFIXES = (b"WARN: event seq gap", b"WARN: consume backpressure",
                        b"WARN: dropping oversized frame", b"WARN: sink write failed",
                        b"WARN: Process error", b"WARN: JQ error")


def _id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_]+", value) is not None


class FeishuLive:
    def __init__(self, chat_id, staff_ids, *, app_id, profile=None, cli="lark-cli", allowed_bot_app_ids=()):
        staff = tuple(staff_ids) if isinstance(staff_ids, (list, tuple, set, frozenset)) else ()
        if ((chat_id is not None and not _id(chat_id, "oc_")) or not _id(app_id, "cli_") or not staff
                or not all(_id(staff_id, "ou_") for staff_id in staff)
                or not isinstance(allowed_bot_app_ids, (list, tuple, set, frozenset))
                or not all(_id(bot_app_id, "cli_") for bot_app_id in allowed_bot_app_ids)
                or profile is not None and (not isinstance(profile, str) or not profile)):
            raise ChannelError("feishu_invalid_config")
        self.chat_id = chat_id
        self.app_id = app_id
        self.staff_ids = frozenset(staff)
        self.bot_app_ids = frozenset((app_id, *allowed_bot_app_ids))
        if chat_id is None and (len(self.staff_ids) > 50 or len(self.bot_app_ids) > 6):
            raise ChannelError("feishu_invalid_config")
        self._command = [cli, f"--profile={profile or app_id}"]
        self._env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                     "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}

    def _run_json(self, args, *, error, input_text=None):
        # Never include stderr or command input in errors. The CLI can echo content.
        try:
            result = subprocess.run(self._command + args, input=input_text,
                                    capture_output=True, encoding="utf-8", env=self._env,
                                    timeout=_CLI_TIMEOUT, check=False)
            if result.returncode:
                raise ChannelError(error)
            payload = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ChannelError(error) from None
        if not isinstance(payload, dict):
            raise ChannelError(error)
        return payload

    def _verify_identity(self):
        response = self._run_json(["whoami", "--as", "bot"], error="feishu_identity_unavailable")
        if (response.get("appId") != self.app_id or response.get("identity") != "bot"
                or response.get("brand") != "feishu" or response.get("available") is not True):
            raise ChannelError("feishu_identity_mismatch")

    def _bot_data(self, args, *, error, input_text=None):
        result = self._run_json(args + ["--as", "bot", "--format", "json"],
                                error=error, input_text=input_text)
        data = result.get("data")
        if result.get("ok") is not True or result.get("identity") != "bot" or not isinstance(data, dict):
            raise ChannelError(error)
        return data

    def _target(self, chat_id):
        if chat_id is None:
            return self.chat_id
        if not _id(chat_id, "oc_") or self.chat_id not in (None, chat_id):
            raise ChannelError("feishu_invalid_chat")
        return chat_id

    def _expected_staff(self, staff_ids):
        if staff_ids is None:
            return self.staff_ids
        if (not isinstance(staff_ids, (list, tuple, set, frozenset)) or not 1 <= len(staff_ids) <= 50
                or not all(_id(staff_id, "ou_") for staff_id in staff_ids)
                or len(set(staff_ids)) != len(staff_ids)):
            raise ChannelError("feishu_invalid_staff")
        return frozenset(staff_ids)

    def preflight(self, chat_id=None, *, staff_ids=None):
        """Check the configured app, group privacy, and exact expected roster."""
        chat_id = self._target(chat_id)
        expected = self._expected_staff(staff_ids)
        if chat_id is None:
            self._verify_identity()
            return  # Managed groups only exist after the first customer event.
        if self.group_members(chat_id) != expected:
            raise ChannelError("feishu_members_mismatch")

    def group_members(self, chat_id):
        """Read a complete private group roster without treating it as authorized staff."""
        chat_id = self._target(chat_id)
        if chat_id is None:
            raise ChannelError("feishu_invalid_chat")
        self._verify_identity()
        group = self._bot_data(["im", "chats", "get", "--chat-id", chat_id],
                               error="feishu_group_unavailable")
        if (group.get("chat_mode") != "group" or group.get("chat_type") != "private"
                or group.get("external") is not False or group.get("chat_status") != "normal"):
            raise ChannelError("feishu_group_not_private_internal")
        if group.get("agent_count", "0") not in (0, "0"):
            raise ChannelError("feishu_members_mismatch")
        members = self._bot_data(["im", "+chat-members-list", "--chat-id", chat_id,
                                  "--member-id-type", "open_id", "--page-size", "100"],
                                 error="feishu_members_unavailable")
        users, bots = members.get("users"), members.get("bots")
        if (members.get("chat_id") != chat_id or members.get("has_more") is not False
                or members.get("truncations") or not isinstance(users, list)
                or not isinstance(bots, list)
                or not all(isinstance(user, dict) and _id(user.get("member_id"), "ou_") for user in users)
                or len(bots) != len(self.bot_app_ids)
                or not all(isinstance(bot, dict) and _id(bot.get("app_id"), "cli_") for bot in bots)
                or {bot["app_id"] for bot in bots} != self.bot_app_ids):
            raise ChannelError("feishu_members_mismatch")
        roster = frozenset(user["member_id"] for user in users)
        if len(roster) != len(users):
            raise ChannelError("feishu_members_mismatch")
        return roster

    def create_room(self, name, operation_id, *, staff_ids=None):
        """Create an internal private group owned by the configured tracking bot."""
        if self.chat_id is not None:
            raise ChannelError("feishu_room_creation_disabled")
        if (not isinstance(name, str) or not name.strip() or len(name) > 60
                or not isinstance(operation_id, str) or not operation_id):
            raise ChannelError("feishu_invalid_payload")
        expected = self._expected_staff(staff_ids)
        self.preflight()
        request_id = str(uuid5(NAMESPACE_URL, f"urn:omni-service:feishu:{self.app_id}:{operation_id}"))
        body = {
            "name": name, "chat_mode": "group", "chat_type": "private", "external": False,
            "group_message_type": "chat", "user_id_list": sorted(expected),
        }
        other_bots = self.bot_app_ids - {self.app_id}
        if other_bots:
            body["bot_id_list"] = sorted(other_bots)
        receipt = self._bot_data(["im", "chats", "create", "--user-id-type", "open_id",
                                  "--uuid", request_id, "--data", "-"],
                                 input_text=json.dumps(body, ensure_ascii=False), error="delivery_unknown")
        chat_id = receipt.get("chat_id")
        if (not _id(chat_id, "oc_") or receipt.get("chat_mode") != "group"
                or receipt.get("chat_type") != "private" or receipt.get("external") is not False):
            raise ChannelError("delivery_unknown")
        return chat_id

    def send(self, text, operation_id, *, mention_all=False, mention_user_id=None, chat_id=None, staff_ids=None):
        """Send bot text to the scoped group and return the confirmed message ID."""
        chat_id = self._target(chat_id)
        if chat_id is None:
            raise ChannelError("feishu_invalid_chat")
        if not isinstance(text, str) or not text.strip():
            raise ChannelError("feishu_invalid_payload")
        expected = self._expected_staff(staff_ids)
        if mention_user_id is not None and (not _id(mention_user_id, "ou_") or mention_user_id not in expected):
            raise ChannelError("feishu_invalid_mention")
        content = feishu_text(text, mention_all=mention_all, mention_user_id=mention_user_id)
        return self._send_message(chat_id, "text", {"text": content}, operation_id, staff_ids=staff_ids)

    def send_card(self, card, operation_id, *, chat_id=None, staff_ids=None):
        """Send an official interactive card to the scoped service group."""
        chat_id = self._target(chat_id)
        if chat_id is None:
            raise ChannelError("feishu_invalid_chat")
        if not isinstance(card, dict) or card.get("schema") != "2.0":
            raise ChannelError("feishu_invalid_payload")
        return self._send_message(chat_id, "interactive", card, operation_id, staff_ids=staff_ids)

    def patch_card(self, message_id, card, *, chat_id=None, staff_ids=None):
        """Update an app-owned card whose receipt has been bound to this room."""
        chat_id = self._target(chat_id)
        if chat_id is None:
            raise ChannelError("feishu_invalid_chat")
        if not _id(message_id, "om_") or not isinstance(card, dict) or card.get("schema") != "2.0":
            raise ChannelError("feishu_invalid_payload")
        content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode("utf-8")) > 30 * 1024:
            raise ChannelError("feishu_invalid_payload")
        self.preflight(chat_id, staff_ids=staff_ids)
        self._bot_data(["im", "messages", "patch", "--message-id", message_id, "--data", "-"],
                       input_text=json.dumps({"content": content}, ensure_ascii=False), error="delivery_unknown")
        return message_id

    def _send_message(self, chat_id, kind, content, operation_id, *, staff_ids):
        if not isinstance(operation_id, str) or not operation_id:
            raise ChannelError("feishu_invalid_payload")
        self.preflight(chat_id, staff_ids=staff_ids)
        key = sha256(f"{self.app_id}:{chat_id}:{operation_id}".encode()).hexdigest()[:48]
        receipt = self._bot_data(["im", "+messages-send", "--chat-id", chat_id,
                                  "--content", "-", "--msg-type", kind, "--idempotency-key", key],
                                 input_text=json.dumps(content, ensure_ascii=False),
                                 error="delivery_unknown")
        if receipt.get("chat_id") != chat_id or not _id(receipt.get("message_id"), "om_"):
            raise ChannelError("delivery_unknown")
        return receipt["message_id"]

    def _event_filter(self, managed_membership):
        chat_filter = f" and .chat_id == {json.dumps(self.chat_id)}" if self.chat_id is not None else ""
        staff_filter = self._staff_filter("sender_id", managed_membership)
        return (
            f'select(.type == "{_EVENT}"{chat_filter} and .chat_type == "group" '
            f'and .sender_type == "user" and .message_type == "text"{staff_filter}) | '
            '{type, chat_id, chat_type, sender_type, message_type, sender_id, message_id, create_time, content, '
            'has_mentions: (.mentions != null and .mentions != [])}'
        )

    def _staff_filter(self, field, managed_membership):
        if managed_membership:
            return ""
        staff = json.dumps(sorted(self.staff_ids))
        return f" and (.{field} as $actor | {staff} | index($actor) != null)"

    def _parse_event(self, line, room_allowed, managed_membership):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise ChannelError("feishu_invalid_event_stream") from None
        if (not isinstance(event, dict) or event.get("type") != _EVENT or event.get("chat_type") != "group"
                or event.get("sender_type") != "user"
                or event.get("message_type") != "text" or not _id(event.get("sender_id"), "ou_")
                or (not managed_membership and event["sender_id"] not in self.staff_ids)):
            return None
        chat_id = event.get("chat_id")
        if (not _id(chat_id, "oc_") or self.chat_id not in (None, chat_id)
                or (room_allowed is not None and not room_allowed(chat_id))):
            return None
        message_id, text, created = event.get("message_id"), event.get("content"), event.get("create_time")
        if (not _id(message_id, "om_") or not isinstance(text, str) or not text.strip()
                or not isinstance(created, str) or re.fullmatch(r"[0-9]{1,15}", created) is None):
            return None
        # Feishu may assign a new event ID to re-delivery of the same message.
        return InboundEvent("feishu", self.app_id, message_id, chat_id, event["sender_id"],
                            message_id, text=text, timestamp=int(created) / 1000,
                            has_mentions=event.get("has_mentions", False) is not False)

    def _card_filter(self, managed_membership):
        chat_filter = f" and .chat_id == {json.dumps(self.chat_id)}" if self.chat_id is not None else ""
        staff_filter = self._staff_filter("operator_id", managed_membership)
        return (
            f'select(.type == "{CARD_ACTION}"{chat_filter} and .host == "im_message" '
            f'and .action_tag == "button" and (.form_value // "") == ""{staff_filter}) | '
            '{type, event_id, chat_id, operator_id, message_id, host, action_tag, action_value}'
        )

    def _parse_card_event(self, line, room_allowed, managed_membership):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise ChannelError("feishu_invalid_event_stream") from None
        if (not isinstance(event, dict) or event.get("type") != CARD_ACTION or event.get("host") != "im_message"
                or event.get("action_tag") != "button" or event.get("form_value")
                or not _id(event.get("operator_id"), "ou_")
                or (not managed_membership and event["operator_id"] not in self.staff_ids)):
            return None
        chat_id = event.get("chat_id")
        if (not _id(chat_id, "oc_") or self.chat_id not in (None, chat_id)
                or (room_allowed is not None and not room_allowed(chat_id))):
            return None
        try:
            return card_decision(app_id=self.app_id, event_id=event.get("event_id"), chat_id=chat_id,
                                 user_id=event["operator_id"], message_id=event.get("message_id"),
                                 value=json.loads(event.get("action_value", "")))
        except (ChannelError, TypeError, ValueError, RecursionError):
            return None

    def events(self, stop=None, *, duration=0, room_allowed=None, managed_membership=False):
        """Consume approved text events until stopped, or for a bounded CLI session."""
        yield from self._events(_EVENT, self._event_filter(managed_membership), self._parse_event,
                                stop, duration, room_allowed, managed_membership)

    def card_events(self, stop=None, *, room_allowed=None, duration=0, managed_membership=False):
        """Consume card clicks for which the caller applies per-room policy in managed mode."""
        yield from self._events(CARD_ACTION, self._card_filter(managed_membership), self._parse_card_event,
                                stop, duration, room_allowed, managed_membership)

    def _events(self, event_key, projection, parser, stop, duration, room_allowed, managed_membership):
        if type(duration) is not int or not 0 <= duration <= 3600:
            raise ChannelError("feishu_invalid_duration")
        if type(managed_membership) is not bool or (managed_membership and not callable(room_allowed)):
            raise ChannelError("feishu_invalid_room_filter")
        if stop is not None and stop.is_set():
            return
        if managed_membership and self.chat_id is not None:
            self.group_members(self.chat_id)
        else:
            self.preflight()
        args = ["event", "consume", event_key, "--as", "bot", "--jq", projection]
        if duration:
            args += ["--timeout", f"{duration}s"]
        try:
            process = subprocess.Popen(self._command + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=self._env)
        except OSError:
            raise ChannelError("feishu_stream_unavailable") from None
        with process:
            try:
                yield from self._read_stream(process, event_key, parser, stop, duration,
                                             room_allowed, managed_membership)
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # Neither consumer creates a server-side subscription.
                        process.kill()

    def _read_stream(self, process, event_key, parser, stop, duration, room_allowed, managed_membership):
        ready = timed_out = False
        ready_marker = f"[event] ready event_key={event_key}".encode()
        started = time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, bytearray())
            selector.register(process.stderr, selectors.EVENT_READ, bytearray())
            while selector.get_map():
                if stop is not None and stop.is_set():
                    return
                elapsed = time.monotonic() - started
                if (not ready and elapsed > _CLI_TIMEOUT) or (duration and elapsed > duration + _CLI_TIMEOUT):
                    raise ChannelError("feishu_stream_timeout")
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.fileobj == process.stdout and key.data:
                            raise ChannelError("feishu_invalid_event_stream")
                        continue
                    key.data.extend(chunk)
                    while b"\n" in key.data:
                        line, remainder = key.data.split(b"\n", 1)
                        key.data[:] = remainder
                        if len(line) > _MAX_LINE:
                            raise ChannelError("feishu_event_too_large")
                        if key.fileobj == process.stderr:
                            ready |= line.rstrip() == ready_marker
                            timed_out |= line.startswith(b"[event] exited") and line.rstrip().endswith(b"(reason: timeout)")
                            if line.startswith(_EVENT_LOSS_PREFIXES) or (line.startswith(b"WARN: event ") and b" dropped:" in line):
                                raise ChannelError("feishu_event_loss")
                        else:
                            event = parser(line, room_allowed, managed_membership)
                            if event is not None:
                                yield event
                    if len(key.data) > _MAX_LINE:
                        raise ChannelError("feishu_event_too_large")
        try:
            result = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            raise ChannelError("feishu_stream_timeout") from None
        if result or not ready or not (duration and timed_out):
            raise ChannelError("feishu_stream_interrupted")
