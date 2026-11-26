"""Acceptance through public HTTP, with independently observed platform effects."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from omni_service.server import create_server, DEFAULT_ACCOUNTS
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload, employee_payload, signed_headers
from omni_service.transport import HttpTransport


class Clock:
    def __init__(self):
        self.value = int(time.time())

    def __call__(self):
        return self.value


def http(base, path, body=None, *, method=None, headers=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = Request(base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                      method=method or ("GET" if body is None else "POST"))
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read().decode()
        try:
            result = json.loads(raw)
        except ValueError:
            result = raw
        return response.status, result


class HTTPAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platforms = self.enterContext(FakePlatforms())
        self.clock = Clock()
        self.db = str(Path(self.temp.name) / "state.sqlite3")
        self.service = Service(self.db, HttpTransport(self.platforms.urls), clock=self.clock)
        self.addCleanup(self.service.close)
        self.accounts = {**DEFAULT_ACCOUNTS, ("telegram", "second-telegram"): "second-secret"}
        self.server = create_server(self.service, accounts=self.accounts)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.sequence = 0

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def admin(self, path, body=None):
        status, result = http(self.base, path, body, headers={"Authorization": "Bearer simulation-admin"})
        self.assertEqual(status, 200, result)
        return result

    def webhook(self, channel, payload, *, account=None, expected=200, secret=None):
        account = account or "demo-" + channel
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = signed_headers(channel, raw, secret or self.accounts[(channel, account)])
        status, result = http(self.base, f"/webhooks/{channel}/{account}", payload, headers=headers)
        self.assertEqual(status, expected, result)
        return result

    def customer(self, channel, text="Здравствуйте", *, customer="10001", **kwargs):
        self.sequence += 1
        payload = customer_payload(channel, text, customer=customer, sequence=self.sequence,
                                   timestamp=self.clock(), **kwargs)
        return self.webhook(channel, payload)["results"][0], payload

    def handoff(self, conversation):
        self.admin(f"/api/conversations/{conversation}/handoff", {})
        self.admin("/api/drain", {})
        return self.admin(f"/api/conversations/{conversation}")["room_id"]

    def staff(self, room, text="您好，我来帮您处理。", employee="employee-1", expected=200):
        self.sequence += 1
        payload = employee_payload(room, text, employee=employee, sequence=self.sequence,
                                   timestamp=self.clock())
        return self.webhook("feishu", payload, expected=expected)

    def test_three_channels_auto_reply_then_seven_staff_translate_without_send_modes(self):
        for channel in ("telegram", "max", "whatsapp"):
            with self.subTest(channel=channel):
                incoming, _ = self.customer(channel)
                cid = incoming["conversation_id"]
                self.admin("/api/drain", {})
                actual = [m for m in self.platforms.messages if m["channel"] == channel]
                self.assertEqual(actual[-1]["text"], "Здравствуйте! Чем я могу вам помочь?")
                room = self.handoff(cid)
                for employee in range(1, 8):
                    self.staff(room, employee=f"employee-{employee}")
                self.admin("/api/drain", {})
                actual = [m for m in self.platforms.messages if m["channel"] == channel]
                self.assertEqual([m["text"] for m in actual[-7:]],
                                 [f"employee-{n}: Здравствуйте! Я помогу вам." for n in range(1, 8)])
                self.assertTrue(all(m["target"] == "10001" for m in actual))
                detail = self.admin(f"/api/conversations/{cid}")
                self.assertEqual(len(detail["members"]), 7)
                self.assertEqual(detail["mode"], "manual")

    def test_1000_simulated_customers_are_isolated_under_one_account_per_channel(self):
        channels = ("telegram", "max", "whatsapp")
        def submit(index):
            channel = channels[index % 3]
            customer = str(100000 + index)
            payload = customer_payload(channel, "Hello", customer=customer,
                                       sequence=10000 + index, timestamp=self.clock())
            result = self.webhook(channel, payload)
            return channel, customer, result["results"][0]["conversation_id"]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(submit, range(1000)))
        self.assertEqual(len({r[2] for r in results}), 1000)
        self.admin("/api/drain", {})
        received = self.platforms.messages
        self.assertEqual(len(received), 1000)
        self.assertEqual({(m["channel"], m["target"]) for m in received},
                         {(channel, customer) for channel, customer, _ in results})
        self.assertTrue(all(m["text"] == "Hello! How can I help you?" for m in received))
        self.assertEqual(len(self.admin("/api/conversations")["conversations"]), 1000)

    def test_account_scope_and_unmapped_or_unapproved_staff_have_no_side_effect(self):
        incoming, payload = self.customer("telegram", "Hello")
        second = self.webhook("telegram", payload, account="second-telegram")["results"][0]
        self.assertNotEqual(incoming["conversation_id"], second["conversation_id"])
        room = self.handoff(incoming["conversation_id"])
        before = len(self.platforms.messages)
        self.staff(room, employee="intruder", expected=403)
        self.admin("/api/drain", {})
        self.assertEqual(len(self.platforms.messages), before)
        self.staff("oc_unknown", expected=400)

    def test_replay_batch_status_only_and_bsuid(self):
        incoming, payload = self.customer("whatsapp", "Hello", customer="US.123456", bsuid=True)
        duplicate = self.webhook("whatsapp", payload)
        self.assertEqual(duplicate["results"][0]["status"], "duplicate")
        second = customer_payload("whatsapp", "Hello", customer="US.8888", sequence=2,
                                  timestamp=self.clock(), bsuid=True)
        payload["entry"][0]["changes"][0]["value"]["messages"].extend(
            second["entry"][0]["changes"][0]["value"]["messages"])
        result = self.webhook("whatsapp", payload)
        self.assertEqual(result["accepted"], 2)
        self.admin("/api/drain", {})
        self.assertEqual(len(self.platforms.messages), 2)
        requests = [r for r in self.platforms.requests if r["channel"] == "whatsapp"]
        self.assertEqual({r["body"]["recipient"] for r in requests}, {"US.123456", "US.8888"})
        self.assertTrue(all("to" not in r["body"] for r in requests))
        value = payload["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [{"id": "wamid.mock.1", "status": "delivered"}]
        self.assertEqual(self.webhook("whatsapp", payload)["accepted"], 0)

    def test_automatic_handoff_creates_one_room_and_manual_mode_suppresses_ai(self):
        incoming, payload = self.customer("max", "Мне нужна помощь сотрудника")
        cid = incoming["conversation_id"]
        self.webhook("max", payload)
        self.admin("/api/drain", {})
        self.assertEqual(len(self.platforms.rooms), 1)
        room = self.admin(f"/api/conversations/{cid}")["room_id"]
        self.staff(room)
        self.admin("/api/drain", {})
        external_before = len([m for m in self.platforms.messages if m["channel"] == "max"])
        self.customer("max", "Hello")
        self.admin("/api/drain", {})
        self.assertEqual(len([m for m in self.platforms.messages if m["channel"] == "max"]), external_before)
        self.admin(f"/api/conversations/{cid}/resume", {})
        self.customer("max", "Hello")
        self.admin("/api/drain", {})
        self.assertEqual([m for m in self.platforms.messages if m["channel"] == "max"][-1]["text"],
                         "Hello! How can I help you?")

    def test_language_is_from_customer_text_and_short_numbers_do_not_change_it(self):
        incoming, _ = self.customer("telegram", "Здравствуйте", language_hint="en")
        cid = incoming["conversation_id"]
        room = self.handoff(cid)
        self.clock.value += 1
        self.customer("telegram", "12345", language_hint="en")
        self.staff(room)
        self.admin("/api/drain", {})
        self.assertEqual(self.admin(f"/api/conversations/{cid}")["language"], "ru")
        self.assertEqual([m for m in self.platforms.messages if m["channel"] == "telegram"][-1]["text"],
                         "employee-1: Здравствуйте! Я помогу вам.")
        self.clock.value += 1
        self.customer("telegram", "Hello", language_hint="ru")
        self.staff(room)
        self.admin("/api/drain", {})
        self.assertEqual([m for m in self.platforms.messages if m["channel"] == "telegram"][-1]["text"],
                         "employee-1: Hello! I will help you.")

    def test_cancel_before_dispatch_and_native_feishu_recall_after_dispatch(self):
        incoming, _ = self.customer("telegram")
        cid = incoming["conversation_id"]
        room = self.handoff(cid)
        result = self.staff(room)["results"][0]
        self.admin(f"/api/conversations/{cid}/messages/{result['message_id']}/recall", {"actor_id": "employee-1"})
        self.admin("/api/drain", {})
        self.assertFalse(any(m["channel"] == "telegram" for m in self.platforms.messages))
        result = self.staff(room)["results"][0]
        self.admin("/api/drain", {})
        detail = self.admin(f"/api/conversations/{cid}")
        msg = next(m for m in detail["messages"] if m["id"] == result["message_id"])
        recall = {"schema": "2.0", "header": {"event_id": "recall-1", "event_type": "im.message.recalled_v1",
                  "token": "feishu-verification-token"}, "event": {"message_id": msg["source_id"], "chat_id": room,
                  "recall_time": str(self.clock() * 1000), "recall_type": "message_owner"}}
        self.webhook("feishu", recall)
        self.admin("/api/drain", {})
        self.assertTrue([m for m in self.platforms.messages if m["channel"] == "telegram"][-1]["deleted"])
        detail = self.admin(f"/api/conversations/{cid}")
        self.assertEqual(next(m for m in detail["messages"] if m["id"] == msg["id"])["recall_status"], "deleted")

    def test_max_semantic_delete_failure_and_whatsapp_unsupported_are_not_success(self):
        for channel in ("max", "whatsapp"):
            incoming, _ = self.customer(channel)
            cid = incoming["conversation_id"]
            room = self.handoff(cid)
            result = self.staff(room)["results"][0]
            self.admin("/api/drain", {})
            if channel == "max":
                self.platforms.fail_next("max", "delete", body={"success": False, "code": "denied", "message": "not allowed"})
            self.admin(f"/api/conversations/{cid}/messages/{result['message_id']}/recall", {"actor_id": "employee-1"})
            self.admin("/api/drain", {})
            detail = self.admin(f"/api/conversations/{cid}")
            msg = next(m for m in detail["messages"] if m["id"] == result["message_id"])
            self.assertEqual(msg["status"], "sent")
            self.assertEqual(msg["recall_status"], "failed" if channel == "max" else "unsupported")
            self.assertFalse([m for m in self.platforms.messages if m["channel"] == channel][-1]["deleted"])
        self.assertFalse(any(r["operation"] == "delete" and r["channel"] == "whatsapp" for r in self.platforms.requests))

    def test_whatsapp_window_is_checked_again_at_dispatch(self):
        incoming, _ = self.customer("whatsapp", "Hello")
        cid = incoming["conversation_id"]
        self.clock.value += 86401
        self.admin("/api/drain", {})
        self.assertFalse(any(m["channel"] == "whatsapp" for m in self.platforms.messages))
        self.assertTrue(any(m["status"] == "blocked_window" for m in self.admin(f"/api/conversations/{cid}")["messages"]))

    def test_authentication_challenges_and_account_mismatch(self):
        for channel in ("telegram", "max", "whatsapp"):
            payload = customer_payload(channel, "Hello", timestamp=self.clock())
            self.webhook(channel, payload, secret="wrong-secret", expected=403)
        self.assertEqual(self.admin("/api/conversations")["conversations"], [])
        status, result = http(self.base, "/webhooks/whatsapp/demo-whatsapp?hub.mode=subscribe&hub.verify_token=whatsapp-app-secret&hub.challenge=012345")
        self.assertEqual((status, result), (200, "012345"))
        self.webhook("feishu", {"type": "url_verification", "token": "feishu-verification-token", "challenge": "abc"})
        wrong = customer_payload("whatsapp", "Hello", account="other-phone")
        self.webhook("whatsapp", wrong, expected=400)
        self.assertEqual(self.admin("/api/conversations")["conversations"], [])
        status, _ = http(self.base, "/api/drain", {})
        self.assertEqual(status, 401)


class ProcessRestartAcceptance(unittest.TestCase):
    def test_queued_message_survives_actual_linux_process_restart_and_replay(self):
        with tempfile.TemporaryDirectory() as folder, FakePlatforms() as platforms:
            root = Path(__file__).resolve().parents[1]
            db = str(Path(folder) / "restart.sqlite3")
            def start():
                process = subprocess.Popen([sys.executable, "-m", "omni_service.server", "--port", "0",
                    "--db", db, "--fake-platform-url", platforms.base_url], cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                ready = selector.select(10)
                selector.close()
                if not ready:
                    process.kill()
                    process.communicate()
                    self.fail("Simulation process failed to become ready")
                line = process.stdout.readline()
                if "Simulation API: " not in line:
                    process.kill()
                    stdout, stderr = process.communicate()
                    self.fail(f"Bad startup: {line} {stdout} {stderr}")
                return process, line.split("Simulation API: ", 1)[1].split(";", 1)[0]
            def stop(process):
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
            process, base = start()
            try:
                payload = customer_payload("telegram", "Hello", sequence=77)
                headers = signed_headers("telegram", json.dumps(payload, ensure_ascii=False).encode(), "telegram-webhook-secret")
                status, first = http(base, "/webhooks/telegram/demo-telegram", payload, headers=headers)
                self.assertEqual(status, 200)
                cid = first["results"][0]["conversation_id"]
            finally:
                stop(process)
            self.assertEqual(platforms.messages, [])
            process, base = start()
            try:
                auth = {"Authorization": "Bearer simulation-admin"}
                status, detail = http(base, f"/api/conversations/{cid}", headers=auth)
                self.assertEqual(status, 200)
                self.assertEqual(detail["language"], "en")
                self.assertTrue(any(m["status"] == "queued" for m in detail["messages"]))
                self.assertEqual(http(base, "/api/drain", {}, headers=auth)[0], 200)
                status, replay = http(base, "/webhooks/telegram/demo-telegram", payload, headers=headers)
                self.assertEqual(replay["results"][0]["status"], "duplicate")
                http(base, "/api/drain", {}, headers=auth)
                self.assertEqual(len(platforms.messages), 1)
                self.assertEqual(platforms.messages[0]["text"], "Hello! How can I help you?")
            finally:
                stop(process)


if __name__ == "__main__":
    unittest.main()
