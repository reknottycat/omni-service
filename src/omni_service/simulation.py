"""Independent local platform doubles; they record what the HTTP client sends."""

import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakePlatforms:
    def __init__(self):
        self.requests = []
        self.messages = []
        self.rooms = {}
        self.failures = defaultdict(deque)
        self.lock = threading.RLock()
        self.counter = defaultdict(int)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_request(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1_048_576:
                    self.send_error(413)
                    return
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw) if raw else {}
                    status, result, headers = owner.receive(
                        self.command, self.path, dict(self.headers), payload)
                except (ValueError, TypeError, KeyError):
                    status, result, headers = 400, {"error": "invalid_mock_request"}, {}
                data = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, str(value))
                self.end_headers()
                self.wfile.write(data)

            do_POST = handle_request
            do_DELETE = handle_request
            do_PUT = handle_request
            do_PATCH = handle_request
            do_GET = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def urls(self):
        return {channel: self.base_url + "/" + channel
                for channel in ("telegram", "max", "whatsapp", "feishu")}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def fail_next(self, channel, operation, *, status=200, body=None, headers=None):
        with self.lock:
            self.failures[(channel, operation)].append((status, body or {}, headers or {}))

    def receive(self, method, path, headers, payload):
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        channel, route = parts[0], parts[1:]
        query = parse_qs(parsed.query)
        operation = "send"
        if method == "DELETE" or (route and route[-1] == "deleteMessage"):
            operation = "delete"
        elif channel == "feishu" and route[-1:] == ["chats"]:
            operation = "create_room"
        elif channel == "feishu" and route[-1:] == ["members"]:
            operation = "add_member"
        elif channel == "feishu" and method == "PATCH":
            operation = "update_card"
        with self.lock:
            # Intentionally omit credentials from the reviewable request record.
            self.requests.append({"channel": channel, "method": method,
                                  "path": parsed.path.replace("botsimulation-token", "bot<redacted>"),
                                  "query": query, "body": payload, "operation": operation})
            if self.failures[(channel, operation)]:
                return self.failures[(channel, operation)].popleft()
            if channel not in {"telegram", "max", "whatsapp", "feishu"}:
                return 404, {"error": "unknown_platform"}, {}
            lower_headers = {k.lower(): v for k, v in headers.items()}
            if channel == "telegram":
                if not route or route[0] != "botsimulation-token":
                    return 401, {"ok": False, "error_code": 401}, {}
            else:
                expected = "simulation-token" if channel == "max" else "Bearer simulation-token"
                if lower_headers.get("authorization") != expected:
                    return 401, {"error": "unauthorized"}, {}
            if operation == 'update_card':
                card = json.loads(payload.get('content', '{}'))
                original = next((row for row in self.messages if row['channel'] == 'feishu'
                                 and row['message_id'] == route[-1] and row['kind'] == 'interactive'), None)
                if route[:-1] != ['open-apis','im','v1','messages'] or not original or card.get('schema') != '2.0':
                    return 400, {'code': 1}, {}
                original['card'] = card
                return 200, {'code': 0, 'data': {}}, {}

            if operation == "create_room":
                if route != ["open-apis", "im", "v1", "chats"] or method != "POST":
                    return 400, {"code": 1, "msg": "bad room endpoint"}, {}
                if not isinstance(payload.get("user_id_list"), list):
                    return 400, {"code": 1, "msg": "missing room members"}, {}
                self.counter["room"] += 1
                room_id = f"oc_mock_{self.counter['room']}"
                self.rooms[room_id] = {"name": payload["name"], "members": list(payload["user_id_list"])}
                return 200, {"code": 0, "data": {"chat_id": room_id}}, {}
            if operation == "add_member":
                room_id = route[-2]
                if room_id not in self.rooms or not isinstance(payload.get("id_list"), list):
                    return 400, {"code": 1, "msg": "invalid room or members"}, {}
                self.rooms[room_id]["members"] = list(dict.fromkeys(
                    self.rooms[room_id]["members"] + payload["id_list"]))
                return 200, {"code": 0, "data": {}}, {}
            if operation == "delete":
                if channel == "whatsapp":
                    return 405, {"error": "message recall unsupported"}, {}
                if channel == "telegram":
                    message_id = str(payload.get("message_id", ""))
                    target = str(payload.get("chat_id", ""))
                elif channel == "max":
                    message_id = query.get("message_id", [""])[0]
                    target = None
                else:
                    message_id = route[-1]
                    target = None
                matches = [m for m in self.messages if m["channel"] == channel
                           and m["message_id"] == message_id and (target is None or m["target"] == target)]
                if not matches:
                    if channel == "max":
                        return 200, {"success": False, "code": "not.found", "message": "unknown message"}, {}
                    if channel == "telegram":
                        return 400, {"ok": False, "error_code": 400, "description": "message not found"}, {}
                    return 200, {"code": 1, "msg": "not found"}, {}
                for message in matches:
                    message["deleted"] = True
                result = {"telegram": {"ok": True, "result": True},
                          "max": {"success": True}, "feishu": {"code": 0, "data": {}}}[channel]
                return 200, result, {}

            if method != "POST":
                return 405, {"error": "wrong method"}, {}
            kind = "text"
            card = None
            if channel == "telegram":
                target = str(payload["chat_id"])
                text = payload.get("text", payload.get("caption", ""))
                if route[-1] not in {"sendMessage", "sendPhoto", "sendDocument", "sendAudio", "sendVoice"}:
                    return 404, {"ok": False}, {}
                kind = {"sendPhoto": "image", "sendDocument": "file", "sendAudio": "audio",
                        "sendVoice": "audio"}.get(route[-1], "text")
            elif channel == "max":
                if route != ["messages"]:
                    return 404, {"error": "wrong endpoint"}, {}
                target = (query.get("chat_id") or query.get("user_id") or [None])[0]
                if target is None:
                    raise ValueError("Recipient required")
                text = payload.get("text", "")
                if payload.get("attachments"):
                    kind = payload["attachments"][0]["type"]
            elif channel == "whatsapp":
                if len(route) != 3 or not route[0].startswith("v") or route[2] != "messages":
                    raise ValueError("Invalid WhatsApp endpoint")
                if payload.get("messaging_product") != "whatsapp" or bool(payload.get("to")) == bool(payload.get("recipient")):
                    raise ValueError("Exactly one destination required")
                target = payload.get("to") or payload.get("recipient")
                kind = payload["type"]
                text = payload.get("text", {}).get("body", "") if kind == "text" else payload[kind].get("caption", "")
            else:
                if route != ["open-apis", "im", "v1", "messages"]:
                    return 404, {"code": 1, "msg": "wrong endpoint"}, {}
                if query.get("receive_id_type") != ["chat_id"]:
                    raise ValueError("Feishu chat target required")
                target = payload["receive_id"]
                if target not in self.rooms:
                    return 400, {"code": 1, "msg": "unknown room"}, {}
                kind = payload["msg_type"]
                content = json.loads(payload["content"])
                if not isinstance(content, dict):
                    raise ValueError("Feishu message content must be an object")
                if kind == "interactive":
                    body = content.get("body")
                    if (content.get("schema") != "2.0" or not isinstance(body, dict)
                            or not isinstance(body.get("elements"), list)):
                        raise ValueError("Feishu Card 2.0 payload required")
                    card = content
                text = content.get("text", "")
            if kind == "text" and (not isinstance(text, str) or not text):
                raise ValueError("Nonempty text required")
            self.counter[channel] += 1
            sequence = self.counter[channel]
            message_id = {"telegram": str(sequence), "max": f"mid.mock.{sequence}",
                          "whatsapp": f"wamid.mock.{sequence}", "feishu": f"om_mock_{sequence}"}[channel]
            record = {"channel": channel, "target": str(target), "message_id": message_id,
                      "text": text, "kind": kind, "deleted": False}
            if card is not None:
                record["card"] = card
            self.messages.append(record)
            results = {"telegram": {"ok": True, "result": {"message_id": int(sequence)}},
                       "max": {"message": {"body": {"mid": message_id}}},
                       "whatsapp": {"messages": [{"id": message_id}]},
                       "feishu": {"code": 0, "data": {"message_id": message_id}}}
            return 200, results[channel], {}


def customer_payload(channel, text, *, customer="10001", sequence=1, timestamp=None,
                     account=None, language_hint="", bsuid=False):
    timestamp = int(time.time() if timestamp is None else timestamp)
    if channel == "telegram":
        return {"update_id": sequence, "message": {"message_id": sequence, "date": timestamp,
                "from": {"id": int(customer), "first_name": "Customer", "is_bot": False,
                         "language_code": language_hint or "en"},
                "chat": {"id": int(customer), "type": "private"}, "text": text}}
    if channel == "max":
        return {"update_type": "message_created", "timestamp": timestamp * 1000,
                "message": {"sender": {"user_id": int(customer), "first_name": "Customer", "is_bot": False},
                            "recipient": {"chat_id": int(customer), "chat_type": "dialog", "user_id": 999},
                            "timestamp": timestamp * 1000, "body": {"mid": f"max.in.{customer}.{sequence}",
                            "seq": sequence, "text": text, "attachments": []}}, "user_locale": language_hint or "ru"}
    if channel == "whatsapp":
        identity = {"from_user_id" if bsuid else "from": customer}
        return {"object": "whatsapp_business_account", "entry": [{"id": "mock-business", "changes": [
            {"field": "messages", "value": {"messaging_product": "whatsapp", "metadata": {
                "phone_number_id": account or "demo-whatsapp", "display_phone_number": "15550000000"},
                "contacts": [{"profile": {"name": "Customer"}, "user_id" if bsuid else "wa_id": customer}],
                "messages": [{**identity, "id": f"wamid.in.{customer}.{sequence}", "timestamp": str(timestamp),
                              "type": "text", "text": {"body": text}}]}}]}]}
    raise ValueError(channel)


def employee_payload(room_id, text, *, employee="employee-1", sequence=1, timestamp=None):
    timestamp = int((time.time() if timestamp is None else timestamp) * 1000)
    return {"schema": "2.0", "header": {"event_id": f"event.staff.{room_id}.{employee}.{sequence}",
            "event_type": "im.message.receive_v1", "create_time": str(timestamp),
            "token": "feishu-verification-token", "app_id": "demo-feishu"},
            "event": {"sender": {"sender_id": {"open_id": employee}, "sender_type": "user"},
                      "message": {"message_id": f"om.staff.{room_id}.{employee}.{sequence}",
                                  "chat_id": room_id, "chat_type": "group", "message_type": "text",
                                  "create_time": str(timestamp),
                                  "content": json.dumps({"text": text}, ensure_ascii=False)}}}


def signed_headers(channel, raw, secret):
    if channel == "telegram":
        return {"X-Telegram-Bot-Api-Secret-Token": secret}
    if channel == "max":
        return {"X-Max-Bot-Api-Secret": secret}
    if channel == "whatsapp":
        return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
    if channel == "feishu":
        timestamp = str(int(time.time()))
        nonce = "simulation-nonce"
        digest = hashlib.sha256((timestamp + nonce + secret).encode() + raw).hexdigest()
        return {"X-Lark-Request-Timestamp": timestamp, "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": digest}
    raise ValueError(channel)
