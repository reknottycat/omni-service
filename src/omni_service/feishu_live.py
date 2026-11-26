"""Feishu bot transport for one private personal-bridge test group."""

import json
import os
import re
import selectors
import subprocess
import time
from contextlib import suppress
from hashlib import sha256

from .models import ChannelError, InboundEvent


_EVENT = "im.message.receive_v1"
_READY = f"[event] ready event_key={_EVENT}".encode()
_MAX_LINE = 256 * 1024
_CLI_TIMEOUT = 15
_EVENT_LOSS_PREFIXES = (b"WARN: event seq gap", b"WARN: consume backpressure",
                        b"WARN: dropping oversized frame", b"WARN: sink write failed",
                        b"WARN: Process error", b"WARN: JQ error")


def _id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_]+", value) is not None


class FeishuLive:
    def __init__(self, chat_id, staff_ids, *, app_id, profile=None, cli="lark-cli"):
        staff = tuple(staff_ids) if isinstance(staff_ids, (list, tuple, set, frozenset)) else ()
        if (not _id(chat_id, "oc_") or not _id(app_id, "cli_") or not staff
                or not all(_id(staff_id, "ou_") for staff_id in staff)
                or profile is not None and (not isinstance(profile, str) or not profile)):
            raise ChannelError("feishu_invalid_config")
        self.chat_id = chat_id
        self.app_id = app_id
        self.staff_ids = frozenset(staff)
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

    def preflight(self):
        """Check the configured app, group privacy, and full member roster."""
        self._verify_identity()
        group = self._bot_data(["im", "chats", "get", "--chat-id", self.chat_id],
                               error="feishu_group_unavailable")
        if (group.get("chat_mode") != "group" or group.get("chat_type") != "private"
                or group.get("external") is not False or group.get("chat_status") != "normal"):
            raise ChannelError("feishu_group_not_private_internal")
        if group.get("agent_count", "0") not in (0, "0"):
            raise ChannelError("feishu_members_mismatch")
        members = self._bot_data(["im", "+chat-members-list", "--chat-id", self.chat_id,
                                  "--member-id-type", "open_id", "--page-size", "100"],
                                 error="feishu_members_unavailable")
        users, bots = members.get("users"), members.get("bots")
        if (members.get("chat_id") != self.chat_id or members.get("has_more") is not False
                or members.get("truncations") or not isinstance(users, list)
                or not isinstance(bots, list) or len(users) != len(self.staff_ids)
                or not all(isinstance(user, dict) and _id(user.get("member_id"), "ou_") for user in users)
                or {user["member_id"] for user in users} != self.staff_ids
                or len(bots) != 1 or not isinstance(bots[0], dict) or bots[0].get("app_id") != self.app_id):
            raise ChannelError("feishu_members_mismatch")

    def send(self, text, operation_id):
        """Send bot text to the scoped group and return the confirmed message ID."""
        if not isinstance(text, str) or not text.strip() or not isinstance(operation_id, str) or not operation_id:
            raise ChannelError("feishu_invalid_payload")
        self.preflight()
        key = sha256(f"{self.app_id}:{self.chat_id}:{operation_id}".encode()).hexdigest()[:48]
        # A customer's plain text must not turn into an @mention in the service group.
        plain_text = re.sub(r"<at(?=\s|>)", "‹at", text, flags=re.IGNORECASE)
        receipt = self._bot_data(["im", "+messages-send", "--chat-id", self.chat_id,
                                  "--content", "-", "--msg-type", "text", "--idempotency-key", key],
                                 input_text=json.dumps({"text": plain_text}, ensure_ascii=False),
                                 error="delivery_unknown")
        if receipt.get("chat_id") != self.chat_id or not _id(receipt.get("message_id"), "om_"):
            raise ChannelError("delivery_unknown")
        return receipt["message_id"]

    def _event_filter(self):
        chat = json.dumps(self.chat_id)
        staff = json.dumps(sorted(self.staff_ids))
        return (
            f'select(.type == "{_EVENT}" and .chat_id == {chat} and .chat_type == "group" '
            f'and .sender_type == "user" and .message_type == "text" and '
            f'(.sender_id as $sender | {staff} | index($sender) != null)) | '
            '{type, chat_id, chat_type, sender_type, message_type, sender_id, message_id, create_time, content}'
        )

    def _parse_event(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            raise ChannelError("feishu_invalid_event_stream") from None
        if (not isinstance(event, dict) or event.get("type") != _EVENT or event.get("chat_id") != self.chat_id
                or event.get("chat_type") != "group" or event.get("sender_type") != "user"
                or event.get("message_type") != "text" or not _id(event.get("sender_id"), "ou_")
                or event["sender_id"] not in self.staff_ids):
            return None
        message_id, text, created = event.get("message_id"), event.get("content"), event.get("create_time")
        if (not _id(message_id, "om_") or not isinstance(text, str) or not text.strip()
                or not isinstance(created, str) or re.fullmatch(r"[0-9]{1,15}", created) is None):
            return None
        # Feishu may assign a new event ID to re-delivery of the same message.
        return InboundEvent("feishu", self.app_id, message_id, self.chat_id, event["sender_id"],
                            message_id, text=text, timestamp=int(created) / 1000)

    def events(self, stop=None, *, duration=0):
        """Consume approved text events until stopped, or for a bounded CLI session."""
        if type(duration) is not int or not 0 <= duration <= 3600:
            raise ChannelError("feishu_invalid_duration")
        if stop is not None and stop.is_set():
            return
        self.preflight()
        args = ["event", "consume", _EVENT, "--as", "bot", "--jq", self._event_filter()]
        if duration:
            args += ["--timeout", f"{duration}s"]
        try:
            process = subprocess.Popen(self._command + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=self._env)
        except OSError:
            raise ChannelError("feishu_stream_unavailable") from None
        with process:
            try:
                yield from self._read_stream(process, stop, duration)
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # This IM consumer has no server-side subscription to clean up.
                        process.kill()

    def _read_stream(self, process, stop, duration):
        ready = timed_out = False
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
                            ready |= line.rstrip() == _READY
                            timed_out |= line.startswith(b"[event] exited") and line.rstrip().endswith(b"(reason: timeout)")
                            if line.startswith(_EVENT_LOSS_PREFIXES) or (line.startswith(b"WARN: event ") and b" dropped:" in line):
                                raise ChannelError("feishu_event_loss")
                        else:
                            event = self._parse_event(line)
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
