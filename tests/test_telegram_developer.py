"""Deterministic fixtures for a same-session Telegram developer enrollment."""
import json
import unittest
from omni_service.models import ChannelError
from omni_service.telegram_developer import enroll

AUTH = '<form id="my_send_form"></form>'
APPS = ('<form id="app_create_form"><input type="hidden" name="hash" value="server-form"/>'
        '<input name="app_title"/><input name="app_shortname"/><textarea name="app_desc"></textarea>'
        '<input type="radio" name="app_platform" value="desktop"/><input name="app_url"/></form>')
RECEIPT = '<label>App api_id:</label><code>1234567</code><label>App api_hash:</label><code>' + 'a'*32 + '</code>'
PHONE = "+12025550123"


class Portal:
    def __init__(self, pages, create_error=None):
        self.events = []
        self.pages = iter(pages)
        self.create_error = create_error

    def __call__(self, path, data=None):
        self.events.append((path, data))
        if path == "/auth?to=apps":
            return ("/auth", AUTH)
        if path == "/auth/send_password":
            return (path, json.dumps({"random_hash": "recognized-challenge"}))
        if path == "/auth/login":
            return (path, '{"ok":true}')
        if path == "/apps":
            return next(self.pages)
        if path == "/apps/create" and self.create_error:
            raise self.create_error
        return (path, "{}")


class DeveloperFlowTest(unittest.TestCase):
    def test_creation_uses_one_form_and_reads_back_credentials(self):
        portal = Portal([("/apps", APPS), ("/apps", RECEIPT)])
        self.assertEqual(enroll(PHONE, lambda: "LoginC0de11", fetch=portal, create=True), (1234567, "a"*32))
        self.assertEqual([path for path, _ in portal.events], [
            "/auth?to=apps", "/auth/send_password", "/auth/login", "/apps", "/apps/create", "/apps"])
        self.assertEqual(portal.events[4][1]["hash"], "server-form")
        self.assertEqual(portal.events[2][1]["random_hash"], "recognized-challenge")

    def test_existing_app_is_reused_and_not_changed(self):
        portal = Portal([("/apps", RECEIPT)])
        self.assertEqual(enroll(PHONE, lambda: "LoginC0de11", fetch=portal, create=True), (1234567, "a"*32))
        self.assertNotIn("/apps/create", [path for path, _ in portal.events])

    def test_redirect_back_to_login_prevents_registration(self):
        portal = Portal([("/auth", AUTH)])
        with self.assertRaisesRegex(ChannelError, "developer_session_not_retained"):
            enroll(PHONE, lambda: "LoginC0de11", fetch=portal, create=True)
        self.assertEqual(len(portal.events), 4)

    def test_no_create_without_explicit_opt_in(self):
        portal = Portal([("/apps", APPS)])
        with self.assertRaisesRegex(ChannelError, "developer_registration_not_enabled"):
            enroll(PHONE, lambda: "LoginC0de11", fetch=portal)
        self.assertEqual(len(portal.events), 4)

    def test_uncertain_create_only_reads_existing_state_never_replays(self):
        portal = Portal([("/apps", APPS), ("/apps", APPS)], OSError("synthetic timeout"))
        with self.assertRaisesRegex(ChannelError, "developer_create_receipt_unknown"):
            enroll(PHONE, lambda: "LoginC0de11", fetch=portal, create=True)
        self.assertEqual(sum(path == "/apps/create" for path, _ in portal.events), 1)
