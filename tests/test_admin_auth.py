import base64
import hashlib
from http.cookies import SimpleCookie
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from omni_service.admin_auth import FeishuAdminAuth
from omni_service.models import ChannelError


CALLBACK = 'https://omni.example.test/admin/auth/callback'
TEST_SECRET = 'fixture-app-secret'
TEST_ACCESS_TOKEN = 'fixture-user-access-token'


def cookie_value(header, name):
    jar = SimpleCookie()
    jar.load(header)
    return jar[name].value


class AdminAuthTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_000
        self.calls = []
        self.profile = {'code': 0, 'data': {'open_id': 'ou_staff', 'tenant_key': 'fixture-tenant'}}
        self.tokens = {'code': 0, 'access_token': TEST_ACCESS_TOKEN, 'refresh_token': 'not-used'}

    def request(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        return self.tokens if method == 'POST' else self.profile

    def auth(self, **overrides):
        options = {
            'app_id': 'cli_fixture', 'app_secret': TEST_SECRET, 'tenant_key': 'fixture-tenant',
            'callback_url': CALLBACK, 'signing_key': 'example-signing-key-for-a-fixture-only',
            'request_json': self.request, 'clock': lambda: self.now,
        }
        return FeishuAdminAuth(**{**options, **overrides})

    def start(self, auth):
        redirect, cookie = auth.begin()
        params = {key: values[0] for key, values in parse_qs(urlsplit(redirect).query).items()}
        return params, cookie_value(cookie, auth.state_cookie_name)

    def login(self, auth):
        params, cookie = self.start(auth)
        session, clear = auth.finish(code='sample-code', state=params['state'], state_cookie=cookie)
        return params, session, clear

    def test_oidc_code_exchange_checks_exact_redirect_and_retains_only_identity(self):
        auth = self.auth()
        authorize_url, state_cookie = auth.begin()
        authorize = urlsplit(authorize_url)
        self.assertEqual((authorize.scheme, authorize.netloc, authorize.path),
                         ('https', 'accounts.feishu.cn', '/open-apis/authen/v1/authorize'))
        self.assertTrue(state_cookie.startswith('__Host-'))
        self.assertIn('HttpOnly; SameSite=Lax; Secure', state_cookie)
        params, session, clear = self.login(auth)
        self.assertEqual(params['redirect_uri'], CALLBACK)
        self.assertEqual(params['client_id'], 'cli_fixture')
        self.assertEqual(params['response_type'], 'code')
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertNotIn('offline_access', str(params))

        token, user_info = self.calls
        self.assertEqual(token[:2], ('POST', 'https://accounts.feishu.cn/oauth/v3/token'))
        self.assertEqual(token[2]['grant_type'], 'authorization_code')
        self.assertEqual(token[2]['client_secret'], TEST_SECRET)
        self.assertEqual(token[2]['redirect_uri'], CALLBACK)
        digest = hashlib.sha256(token[2]['code_verifier'].encode('ascii')).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).decode().rstrip('='), params['code_challenge'])
        self.assertEqual(user_info, ('GET', 'https://open.feishu.cn/open-apis/authen/v1/user_info', None,
                                    {'Authorization': 'Bearer ' + TEST_ACCESS_TOKEN, 'Accept': 'application/json'}))

        value = cookie_value(session, auth.session_cookie_name)
        self.assertEqual(auth.read_session(value).user_id, 'ou_staff')
        self.assertEqual(len(auth.read_session(value).csrf_token), 43)
        encoded = value.split('.')[0]
        stored = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        self.assertEqual(set(stored), {'open_id', 'csrf', 'exp'})
        self.assertIn('Max-Age=0', clear)
        self.assertEqual(cookie_value(clear, auth.state_cookie_name), '')
        self.assertEqual(cookie_value(auth.clear_session_cookie(), auth.session_cookie_name), '')

    def test_mismatched_tampered_expired_state_fails_before_network(self):
        auth = self.auth()
        one, cookie_one = self.start(auth)
        two, cookie_two = self.start(auth)
        tampered = ('b' if cookie_one[0] == 'a' else 'a') + cookie_one[1:]
        for state, cookie, code in ((one['state'], cookie_two, 'sample-code'),
                                    (two['state'], cookie_one, 'sample-code'),
                                    (one['state'], tampered, 'sample-code'),
                                    (one['state'], cookie_one, ''),
                                    (one['state'], None, 'sample-code')):
            with self.subTest(state_present=bool(state), cookie_present=bool(cookie)), self.assertRaisesRegex(
                    ChannelError, '^admin_oauth_state$'):
                auth.finish(code=code, state=state, state_cookie=cookie)
        self.now += 601
        with self.assertRaisesRegex(ChannelError, '^admin_oauth_state$'):
            auth.finish(code='sample-code', state=one['state'], state_cookie=cookie_one)
        self.assertEqual(self.calls, [])

    def test_wrong_organization_missing_identity_and_failed_provider_do_not_sign_in(self):
        auth = self.auth()
        for profile in (
            {'code': 0, 'data': {'open_id': 'ou_staff', 'tenant_key': 'different-tenant'}},
            {'code': 0, 'data': {'open_id': 'ou_staff'}},
            {'code': 0, 'data': {'user_id': 'employee-1', 'tenant_key': 'fixture-tenant'}},
        ):
            self.profile = profile
            with self.assertRaisesRegex(ChannelError, '^admin_oauth_forbidden$'):
                self.login(auth)
        self.tokens = {'code': 20000, 'msg': TEST_SECRET, 'access_token': TEST_ACCESS_TOKEN}
        before = len(self.calls)
        with self.assertRaisesRegex(ChannelError, '^admin_oauth_unavailable$'):
            self.login(auth)
        self.assertEqual(len(self.calls), before + 1)

    def test_sessions_expire_and_cannot_cross_auth_realms(self):
        auth = self.auth()
        _params, header, _clear = self.login(auth)
        value = cookie_value(header, auth.session_cookie_name)
        self.assertIsNotNone(auth.read_session(value))
        for overrides in ({'tenant_key': 'other-tenant'}, {'app_id': 'cli_other'},
                          {'callback_url': 'https://other.example.test/admin/auth/callback'}):
            self.assertIsNone(self.auth(**overrides).read_session(value))
        self.assertIsNone(auth.read_session(self.start(auth)[1]))
        self.assertIsNone(auth.read_session(value + '.extra'))
        self.now += 1801
        self.assertIsNone(auth.read_session(value))

    def test_callback_must_be_fixed_https_or_explicit_loopback(self):
        for origin in ('http://example.test', 'https://evil.test@omni.example.test', 'file://test'):
            with self.assertRaisesRegex(ChannelError, '^invalid_admin_oauth_config$'):
                self.auth(callback_url=origin + '/admin/auth/callback')
        for suffix in ('?return=https://evil.test', '#fragment', '/extra'):
            with self.assertRaisesRegex(ChannelError, '^invalid_admin_oauth_config$'):
                self.auth(callback_url=CALLBACK + suffix)
        auth = self.auth(callback_url='http://127.0.0.1:8080/admin/auth/callback')
        _params, header, _clear = self.login(auth)
        self.assertNotIn('Secure', header)
        self.assertIn('Path=/admin', header)

    def test_default_transport_does_not_follow_redirects(self):
        auth = self.auth(request_json=None)
        params, cookie = self.start(auth)
        with patch('omni_service.admin_auth.http.client.HTTPSConnection') as tls:
            connection = tls.return_value
            connection.getresponse.return_value.status = 302
            with self.assertRaisesRegex(ChannelError, '^admin_oauth_unavailable$'):
                auth.finish(code='sample-code', state=params['state'], state_cookie=cookie)
            tls.assert_called_once_with('accounts.feishu.cn', timeout=8)
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
