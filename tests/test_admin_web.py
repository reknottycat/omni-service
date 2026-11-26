"""Admin HTTP identity, authorization and publication boundaries."""

from contextlib import closing
import http.client
import json
import threading
import unittest
from types import SimpleNamespace

from omni_service.admin_web import create_admin_server
from omni_service.models import ChannelError


class FakeIdentity:
    redirect_uri = "https://ops.example:443/admin/auth/callback"
    state_cookie_name = "mock-state"
    session_cookie_name = "mock-session"

    def begin(self):
        return "https://auth.example/consent", "mock-state=nonce; Path=/; HttpOnly"

    def finish(self, *, code, state, state_cookie):
        if (code, state, state_cookie) != ("code", "nonce", "nonce"):
            raise ChannelError("admin_oauth_state")
        return "mock-session=admin; Path=/; HttpOnly", self.clear_state_cookie()

    def read_session(self, value):
        if value in ("admin", "staff"):
            return SimpleNamespace(user_id=value, csrf_token="csrf-" + value)
        return None

    def clear_state_cookie(self):
        return "mock-state=; Path=/; Max-Age=0"

    def clear_session_cookie(self):
        return "mock-session=; Path=/; Max-Age=0"


class SettingsProbe:
    def __init__(self):
        self.calls = []

    def admin_state(self, user):
        return {"role": user, "conversations": []}

    def admin_update_defaults(self, user, changes):
        self.calls.append(("defaults", user, changes))
        return {"status": "saved"}

    def admin_update_conversation(self, user, cid, changes):
        self.calls.append(("conversation", user, cid, changes))
        return {"status": "saved"}

    def admin_set_command_policy(self, user, command, policy):
        self.calls.append(("command", user, command, policy))
        return {"status": "saved"}

    def admin_prepare_welcome(self, user):
        if user != "admin":
            raise ChannelError("forbidden")
        return {"document_url": "https://example.feishu.cn/docx/allowed"}

    def admin_preview_welcome(self, user, compiled):
        self.calls.append(("preview", user, compiled))
        return {"version": "fixture-version", **compiled}

    def admin_publish_welcome(self, user, version):
        self.calls.append(("publish", user, version))
        return {"status": "published"}

    def admin_rollback_welcome(self, user, version):
        self.calls.append(("rollback", user, version))
        return {"status": "published"}


class TemplateProbe:
    def __init__(self):
        self.calls = []

    def read(self, url):
        self.calls.append(url)
        return {"items": [{"type": "text", "text": "您好"}]}


class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service, self.templates = SettingsProbe(), TemplateProbe()
        self.server = create_admin_server(self.service, FakeIdentity(), self.templates)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, *, body=None, user=None, origin="https://ops.example",
                csrf=None, cookie=None):
        headers = {}
        if cookie or user:
            headers["Cookie"] = cookie or f"mock-session={user}"
        if body is not None:
            headers.update({"Origin": origin, "Content-Type": "application/json"})
            if csrf or user:
                headers["X-Omni-CSRF"] = csrf or f"csrf-{user}"
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        with closing(conn):
            conn.request("GET" if body is None else "POST", path,
                         body=None if body is None else json.dumps(body), headers=headers)
            with conn.getresponse() as result:
                data = result.read()
                if result.headers.get_content_type() == "application/json":
                    data = json.loads(data)
                return result.status, result.headers, data

    def test_login_redirect_cookies_and_state_are_separated(self):
        status, headers, html = self.request("/admin/")
        self.assertEqual(status, 200)
        self.assertIn(b"OmniService", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"csrf-admin", html)
        self.assertEqual(self.request("/admin/api/state")[0], 401)
        status, headers, _ = self.request("/admin/auth/login")
        self.assertEqual((status, headers["Location"]), (303, "https://auth.example/consent"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        status, headers, _ = self.request("/admin/auth/callback?code=code&state=nonce")
        self.assertEqual((status, headers["Location"]), (303, "/admin/?login=failed"))
        status, headers, _ = self.request("/admin/auth/callback?code=code&state=nonce", cookie="mock-state=nonce")
        self.assertEqual((status, headers["Location"]), (303, "/admin/"))
        self.assertEqual(len(headers.get_all("Set-Cookie")), 2)
        status, _, state = self.request("/admin/api/state", user="admin")
        self.assertEqual((status, state["user_id"], state["csrf_token"]), (200, "admin", "csrf-admin"))

    def test_mutations_require_both_csrf_checks_and_call_service_with_session_user(self):
        defaults = {"reply_assist": True}
        route = "/admin/api/defaults"
        self.assertEqual(self.request(route, body=defaults, user="admin", origin="https://other.example")[0], 403)
        self.assertEqual(self.request(route, body=defaults, user="admin", csrf="wrong")[0], 403)
        self.assertEqual(self.request(route, body=defaults)[0], 401)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.request(route, body={**defaults, "user_id": "spoof"}, user="admin")[0], 200)
        self.assertEqual(self.service.calls[0][:2], ("defaults", "admin"))
        self.request("/admin/api/conversations/conv_1", body={"show_labels": True}, user="staff")
        self.request("/admin/api/command-policy", body={"command_id": "/标签", "policy": "staff"}, user="admin")
        self.assertEqual(self.service.calls[-2:], [
            ("conversation", "staff", "conv_1", {"show_labels": True}),
            ("command", "admin", "/标签", "staff"),
        ])
        status, headers, _ = self.request("/admin/api/logout", body={}, user="admin")
        self.assertEqual(status, 204)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_welcome_preview_uses_only_permission_checked_document_and_separate_publish(self):
        route = "/admin/api/welcome/preview"
        self.assertEqual(self.request(route, body={}, user="staff")[0], 403)
        self.assertEqual(self.templates.calls, [])
        status, _, result = self.request(route, body={"document_url": "https://untrusted.example"}, user="admin")
        self.assertEqual((status, result["items"][0]["text"]), (200, "您好"))
        self.assertEqual(self.templates.calls, ["https://example.feishu.cn/docx/allowed"])
        self.assertEqual([call[0] for call in self.service.calls], ["preview"])
        self.request("/admin/api/welcome/publish", body={"version": result["version"]}, user="admin")
        self.request("/admin/api/welcome/rollback", body={"version": "old"}, user="admin")
        self.assertEqual(self.service.calls[-2:], [("publish", "admin", "fixture-version"), ("rollback", "admin", "old")])
