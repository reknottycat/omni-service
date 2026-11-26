"""Authenticated settings surface for an existing OmniService instance."""

import hmac
import json
import secrets
import socket
import threading
from contextlib import suppress
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .models import ChannelError


_BASE = "/admin/"
_MAX_BODY = 128 * 1024
_ERROR_STATUS = {
    "forbidden": 403,
    "unauthorized": 401,
    "not_found": 404,
    "payload_too_large": 413,
}


def create_admin_server(service, identity, templates, *, host="127.0.0.1", port=0):
    """Bind an admin listener. Feishu identity and the welcome compiler are injected."""
    callback = urlsplit(identity.redirect_uri)
    if (callback.scheme not in ("http", "https") or not callback.hostname
            or callback.path != _BASE + "auth/callback"):
        raise ValueError("Expected the Feishu /admin/auth/callback URL")
    hostname = f"[{callback.hostname}]" if ":" in callback.hostname else callback.hostname
    default_port = 443 if callback.scheme == "https" else 80
    port_suffix = f":{callback.port}" if callback.port not in (None, default_port) else ""
    origin = f"{callback.scheme}://{hostname}{port_suffix}"
    page = Path(__file__).with_name("admin_page.html").read_text(encoding="utf-8")
    clients = set()
    clients_lock = threading.Lock()

    class AdminServer(ThreadingHTTPServer):
        daemon_threads = False

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(8)
            with clients_lock:
                clients.add(connection)
            return connection, address

        def close_request(self, connection):
            with clients_lock:
                clients.discard(connection)
            super().close_request(connection)

        def server_close(self):
            # Wake incomplete HTTP reads before waiting for in-flight handlers.
            with clients_lock:
                for connection in clients:
                    with suppress(OSError):
                        connection.shutdown(socket.SHUT_RD)
            super().server_close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # OAuth codes and state may appear in the request URL.
            pass

        def respond(self, status, body=b"", *, media_type=None, headers=()):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if media_type:
                self.send_header("Content-Type", media_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def json(self, status, value):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.respond(status, body, media_type="application/json; charset=utf-8")

        def redirect(self, location, *cookies):
            self.respond(303, headers=[("Location", location), *[("Set-Cookie", value) for value in cookies]])

        def cookie(self, name):
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                return jar[name].value if name in jar else None
            except CookieError:
                return None

        def session(self):
            session = identity.read_session(self.cookie(identity.session_cookie_name))
            if session is None:
                raise ChannelError("unauthorized")
            return session

        def body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > _MAX_BODY:
                    raise ChannelError("payload_too_large")
                if length < 0 or self.headers.get_content_type() != "application/json":
                    raise ChannelError("invalid_payload")
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                body = json.loads(raw) if len(raw) == length else None
            except (ValueError, UnicodeError, OSError):
                raise ChannelError("invalid_payload") from None
            if not isinstance(body, dict):
                raise ChannelError("invalid_payload")
            return body

        def fail(self, exc):
            code = getattr(exc, "code", "invalid_payload")
            self.json(_ERROR_STATUS.get(code, 400), {"error": code})

        def do_GET(self):
            route = urlsplit(self.path)
            if route.path in ("/", "/admin"):
                return self.redirect(_BASE)
            if route.path == _BASE:
                nonce = secrets.token_urlsafe(18)
                policy = ("default-src 'none'; connect-src 'self'; img-src data:; "
                          f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                          "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
                return self.respond(200, page.replace("__OMNI_NONCE__", nonce).encode("utf-8"),
                                    media_type="text/html; charset=utf-8",
                                    headers=[("Content-Security-Policy", policy)])
            if route.path == _BASE + "auth/login":
                try:
                    url, cookie = identity.begin()
                    return self.redirect(url, cookie)
                except ChannelError:
                    return self.redirect(_BASE + "?login=failed")
            if route.path == _BASE + "auth/callback":
                try:
                    query = parse_qs(route.query, max_num_fields=8)
                    code, = query.get("code", ())
                    state, = query.get("state", ())
                    session, state_clear = identity.finish(
                        code=code, state=state, state_cookie=self.cookie(identity.state_cookie_name))
                    return self.redirect(_BASE, session, state_clear)
                except (ChannelError, ValueError):
                    return self.redirect(_BASE + "?login=failed", identity.clear_state_cookie())
            if route.path == _BASE + "api/state":
                try:
                    session = self.session()
                    state = service.admin_state(session.user_id)
                    return self.json(200, {**state, "user_id": session.user_id, "csrf_token": session.csrf_token})
                except ChannelError as exc:
                    return self.fail(exc)
            return self.json(404, {"error": "not_found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith(_BASE + "api/"):
                return self.json(404, {"error": "not_found"})
            action = path.removeprefix(_BASE + "api/")
            try:
                session = self.session()
                received = self.headers.get("X-Omni-CSRF", "")
                if self.headers.get("Origin") != origin or not hmac.compare_digest(
                        received.encode(), session.csrf_token.encode()):
                    raise ChannelError("invalid_csrf")
                if action == "logout":
                    return self.respond(204, headers=[("Set-Cookie", identity.clear_session_cookie())])
                value = self.dispatch(session.user_id, action, self.body())
                return self.json(200, value)
            except (ChannelError, KeyError, ValueError) as exc:
                if getattr(exc, "code", None) == "invalid_csrf":
                    return self.json(403, {"error": "invalid_csrf"})
                return self.fail(exc)

        def dispatch(self, user, action, body):
            if action == "defaults":
                return service.admin_update_defaults(user, body)
            if action == "command-policy":
                return service.admin_set_command_policy(user, body["command_id"], body["policy"])
            if action.startswith("conversations/"):
                cid = unquote(action.removeprefix("conversations/"))
                return service.admin_update_conversation(user, cid, body)
            if action == "welcome/prepare":
                return service.admin_prepare_welcome(user)
            if action == "welcome/preview":
                # Resolve the approved document through admin permissions, never a client URL.
                document = service.admin_prepare_welcome(user)
                return service.admin_preview_welcome(user, templates.read(document["document_url"]))
            if action == "welcome/publish":
                return service.admin_publish_welcome(user, body["version"])
            if action == "welcome/rollback":
                return service.admin_rollback_welcome(user, body["version"])
            if action == "welcome/send-preview":
                return service.admin_send_welcome_preview(user, body["version"], body["conversation_id"])
            raise ChannelError("not_found")

    return AdminServer((host, port), Handler)
