"""Loopback HTTP surface for the explicitly simulated P0 runtime."""

import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .channels import parse_events, verify_webhook
from .models import ChannelError


DEFAULT_ACCOUNTS = {
    ("telegram", "demo-telegram"): "telegram-webhook-secret",
    ("max", "demo-max"): "max-webhook-secret",
    ("whatsapp", "demo-whatsapp"): "whatsapp-app-secret",
    ("feishu", "demo-feishu"): "feishu-verification-token",
}
MAX_BODY_BYTES = 1_048_576


def create_server(service, *, host="127.0.0.1", port=0,
                  accounts=None, admin_token="simulation-admin"):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The simulation server only binds to loopback")
    if not admin_token:
        raise ValueError("An admin token is required")
    account_secrets = dict(DEFAULT_ACCOUNTS if accounts is None else accounts)

    class Handler(BaseHTTPRequestHandler):
        server_version = "OmniServiceSimulation/0.1"

        def log_message(self, *_args):
            pass

        def respond(self, status, value, *, plain=False):
            data = str(value).encode() if plain else json.dumps(
                value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8" if plain
                             else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def route(self):
            parsed = urlsplit(self.path)
            return [unquote(x) for x in parsed.path.strip("/").split("/")], parse_qs(parsed.query)

        def authenticated(self):
            value = self.headers.get("Authorization", "")
            return hmac.compare_digest(value.encode(), ("Bearer " + admin_token).encode())

        def read_body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ChannelError("invalid_payload", "Invalid Content-Length") from None
            if not 0 <= length <= MAX_BODY_BYTES:
                raise ChannelError("payload_too_large")
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ChannelError("invalid_payload", "Incomplete request")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise ChannelError("invalid_payload", "Expected JSON") from None
            if not isinstance(data, dict):
                raise ChannelError("invalid_payload", "Expected a JSON object")
            return raw, data

        def do_GET(self):
            parts, query = self.route()
            if parts == ["health"]:
                return self.respond(200, {"status": "ok", "mode": "simulation"})
            if len(parts) == 3 and parts[:2] == ["webhooks", "whatsapp"]:
                secret = account_secrets.get(("whatsapp", parts[2]))
                supplied = query.get("hub.verify_token", [""])[0]
                if (secret and query.get("hub.mode") == ["subscribe"]
                        and hmac.compare_digest(supplied.encode(), secret.encode())
                        and "hub.challenge" in query):
                    return self.respond(200, query["hub.challenge"][0], plain=True)
                return self.respond(403, {"error": "invalid_verification"})
            if not self.authenticated():
                return self.respond(401, {"error": "unauthorized"})
            try:
                if parts == ["api", "conversations"]:
                    return self.respond(200, {"conversations": service.list_conversations()})
                if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                    return self.respond(200, service.get_conversation(parts[2]))
                self.respond(404, {"error": "not_found"})
            except (ChannelError, KeyError, ValueError) as exc:
                self.handle_error(exc)

        def handle_error(self, exc):
            code = getattr(exc, "code", "invalid_request")
            status = {"unauthorized": 403, "forbidden": 403, "not_found": 404,
                      "payload_too_large": 413, "duplicate_conflict": 409,
                      "event_collision": 409, "invalid_signature": 403}.get(code, 400)
            self.respond(status, {"error": code})

        def do_POST(self):
            parts, _query = self.route()
            try:
                if len(parts) == 3 and parts[0] == "webhooks":
                    channel, account = parts[1:]
                    secret = account_secrets.get((channel, account))
                    if secret is None:
                        return self.respond(404, {"error": "account_not_configured"})
                    raw, payload = self.read_body()
                    if not verify_webhook(channel, raw, dict(self.headers), secret):
                        return self.respond(403, {"error": "invalid_signature"})
                    if channel == "feishu" and payload.get("type") == "url_verification":
                        challenge = payload.get("challenge")
                        if not isinstance(challenge, str):
                            raise ChannelError("invalid_payload")
                        return self.respond(200, {"challenge": challenge})
                    events = parse_events(channel, account, payload)
                    if any(e.channel != channel or e.account_id != account for e in events):
                        return self.respond(403, {"error": "account_mismatch"})
                    results = [service.ingest(event) for event in events]
                    return self.respond(200, {"accepted": len(results), "results": results})
                if not self.authenticated():
                    return self.respond(401, {"error": "unauthorized"})
                _raw, body = self.read_body()
                if parts == ["api", "drain"]:
                    return self.respond(200, {"results": service.drain()})
                if parts == ["api", "intake"]:
                    return self.respond(200, service.set_intake_enabled(body.get("enabled")))
                if parts == ["api", "agent"]:
                    return self.respond(200, service.set_agent_enabled(body.get("enabled")))
                if len(parts) == 4 and parts[:2] == ["api", "conversations"]:
                    conversation, action = parts[2:]
                    if action == "handoff":
                        members = body.get("members")
                        if members is not None and (not isinstance(members, list) or
                                not all(isinstance(m, str) and m for m in members)):
                            raise ChannelError("invalid_payload")
                        return self.respond(200, service.handoff(conversation, members))
                    if action == "members":
                        staff_id = body.get("staff_id")
                        if not isinstance(staff_id, str) or not staff_id:
                            raise ChannelError("invalid_payload")
                        return self.respond(200, service.add_member(conversation, staff_id))
                    if action == "resume":
                        return self.respond(200, service.resume(conversation))
                if (len(parts) == 6 and parts[:2] == ["api", "conversations"]
                        and parts[3] == "messages" and parts[5] == "recall"):
                    actor = body.get("actor_id")
                    if not isinstance(actor, str) or not actor:
                        raise ChannelError("invalid_payload")
                    return self.respond(200, service.recall(parts[4], parts[2], actor))
                self.respond(404, {"error": "not_found"})
            except (ChannelError, KeyError, ValueError) as exc:
                self.handle_error(exc)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Local simulation only; see scripts/demo.py")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="omni-simulation.sqlite3")
    parser.add_argument("--fake-platform-url", required=True)
    parser.add_argument("--intake-max-replies", type=int, choices=range(3, 6),
                        help="Enable simulated new-conversation intake with this reply limit.")
    args = parser.parse_args()
    from .service import Service
    from .intake import Intake
    from .transport import HttpTransport
    transport = HttpTransport({c: args.fake_platform_url.rstrip("/") + "/" + c
                               for c in ("telegram", "max", "whatsapp", "feishu")})
    intake = None
    if args.intake_max_replies is not None:
        intake = Intake(lambda _text, _fields: {}, max_replies=args.intake_max_replies, simulated=True)
    service = Service(args.db, transport, intake=intake, approval_intake=intake is not None)
    server = create_server(service, port=args.port)
    print(f"Simulation API: http://127.0.0.1:{server.server_port}; POST /api/drain to process queue", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
