"""Feishu OAuth login for the OmniService administration page."""

import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
import re
import secrets
import time
import unicodedata
from urllib.parse import urlencode, urlsplit

from .models import ChannelError


_AUTHORIZE_URL = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
_TOKEN_URL = 'https://accounts.feishu.cn/oauth/v3/token'
_USER_INFO_URL = 'https://open.feishu.cn/open-apis/authen/v1/user_info'
_CALLBACK_PATH = '/admin/auth/callback'
_STATE_TTL = 600
_SESSION_TTL = 1800
_RESPONSE_LIMIT = 65536
_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}
_BROWSER_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
_OPEN_ID = re.compile(r'ou_[A-Za-z0-9_]{1,125}')


@dataclass(frozen=True)
class AdminSession:
    user_id: str
    csrf_token: str
    display_name: str = ''


def _base64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _display_name(value):
    if not isinstance(value, str):
        return ''
    return ''.join(char for char in value[:256] if not unicodedata.category(char).startswith('C')).strip()[:80]


def _callback_security(redirect_uri):
    if (not isinstance(redirect_uri, str) or not redirect_uri.isascii() or len(redirect_uri) > 512
            or re.search(r'[\s\x00-\x1f\x7f\\?#]', redirect_uri)):
        raise ChannelError('invalid_admin_oauth_config')
    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError:
        raise ChannelError('invalid_admin_oauth_config') from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or port == 0 or parsed.path != _CALLBACK_PATH
            or (parsed.scheme != 'https' and (parsed.scheme != 'http' or parsed.hostname not in _LOOPBACK))):
        raise ChannelError('invalid_admin_oauth_config')
    return parsed.scheme == 'https'


def _request_json(method, url, body, headers):
    # Never redirect or use an origin supplied by the browser for these credentials.
    if (method, url) not in {('POST', _TOKEN_URL), ('GET', _USER_INFO_URL)}:
        raise ChannelError('admin_oauth_unavailable')
    target = urlsplit(url)
    try:
        with closing(http.client.HTTPSConnection(target.hostname, timeout=8)) as connection:
            connection.request(method, target.path, body=None if body is None else urlencode(body).encode('ascii'),
                               headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ChannelError('admin_oauth_unavailable')
            content = response.read(_RESPONSE_LIMIT + 1)
            if len(content) > _RESPONSE_LIMIT:
                raise ChannelError('admin_oauth_unavailable')
            return json.loads(content)
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise ChannelError('admin_oauth_unavailable') from None


class FeishuAdminAuth:
    def __init__(self, *, app_id, app_secret, tenant_key, callback_url, signing_key,
                 request_json=None, clock=time.time):
        if (not _matches(r'cli_[A-Za-z0-9_]{1,125}', app_id)
                or not _matches(r'[!-~]{1,512}', app_secret)
                or not _matches(r'[A-Za-z0-9_-]{1,128}', tenant_key)
                or not isinstance(signing_key, str) or len(signing_key.encode('utf-8')) < 32
                or (request_json is not None and not callable(request_json))):
            raise ChannelError('invalid_admin_oauth_config')
        self.redirect_uri = callback_url
        self._secure = _callback_security(callback_url)
        prefix = '__Host-' if self._secure else ''
        self.state_cookie_name = prefix + 'omni_admin_state'
        self.session_cookie_name = prefix + 'omni_admin_session'
        self._app_id, self._app_secret, self._tenant_key = app_id, app_secret, tenant_key
        context = ('omni.admin.v1\0' + json.dumps([app_id, tenant_key, callback_url])).encode('utf-8')
        self._key = hmac.digest(signing_key.encode('utf-8'), context, 'sha256')
        self._request = request_json or _request_json
        self._clock = clock

    def _cookie(self, name, value, ttl):
        path = '/' if self._secure else '/admin'
        secure = '; Secure' if self._secure else ''
        return f'{name}={value}; Path={path}; Max-Age={ttl}; HttpOnly; SameSite=Lax{secure}'

    def _signature(self, purpose, encoded):
        return _base64(hmac.digest(self._key, f'{purpose}.{encoded}'.encode('ascii'), 'sha256'))

    def _sign(self, purpose, claims, ttl):
        encoded = _base64(json.dumps({**claims, 'exp': int(self._clock()) + ttl},
                                     separators=(',', ':')).encode('utf-8'))
        return f'{encoded}.{self._signature(purpose, encoded)}'

    def _verify(self, purpose, value):
        if not _matches(r'[A-Za-z0-9_-]{1,1536}\.[A-Za-z0-9_-]{43}', value):
            return None
        encoded, signature = value.split('.')
        if not hmac.compare_digest(signature, self._signature(purpose, encoded)):
            return None
        try:
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        except (ValueError, RecursionError):
            return None
        if (not isinstance(claims, dict) or type(claims.get('exp')) is not int
                or claims['exp'] <= self._clock()):
            return None
        return claims

    def begin(self):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        challenge = _base64(hashlib.sha256(verifier.encode('ascii')).digest())
        query = urlencode({
            'client_id': self._app_id, 'response_type': 'code', 'redirect_uri': self.redirect_uri,
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        })
        signed_state = self._sign('state', {'state': state, 'verifier': verifier}, _STATE_TTL)
        return f'{_AUTHORIZE_URL}?{query}', self._cookie(self.state_cookie_name, signed_state, _STATE_TTL)

    def finish(self, *, code, state, state_cookie):
        claims = self._verify('state', state_cookie)
        if (not claims or not _matches(_BROWSER_TOKEN, state) or not _matches(_BROWSER_TOKEN, claims.get('state'))
                or not hmac.compare_digest(state, claims['state'])
                or not _matches(_BROWSER_TOKEN, claims.get('verifier'))
                or not _matches(r'[A-Za-z0-9_-]{1,1024}', code)):
            raise ChannelError('admin_oauth_state')
        tokens = self._request('POST', _TOKEN_URL, {
            'grant_type': 'authorization_code', 'client_id': self._app_id, 'client_secret': self._app_secret,
            'redirect_uri': self.redirect_uri, 'code': code, 'code_verifier': claims['verifier'],
        }, {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
        access_token = tokens.get('access_token') if isinstance(tokens, dict) and type(tokens.get('code')) is int and tokens['code'] == 0 else None
        if not _matches(r'[!-~]{1,8192}', access_token):
            raise ChannelError('admin_oauth_unavailable')
        profile = self._request('GET', _USER_INFO_URL, None, {
            'Authorization': 'Bearer ' + access_token, 'Accept': 'application/json',
        })
        user = profile.get('data') if isinstance(profile, dict) and type(profile.get('code')) is int and profile['code'] == 0 else None
        if not isinstance(user, dict):
            raise ChannelError('admin_oauth_unavailable')
        if user.get('tenant_key') != self._tenant_key or not _matches(_OPEN_ID, user.get('open_id')):
            raise ChannelError('admin_oauth_forbidden')
        session_claims = {'open_id': user['open_id'], 'csrf': secrets.token_urlsafe(32)}
        name = _display_name(user.get('name'))
        if name:
            session_claims['name'] = name
        session = self._sign('session', session_claims, _SESSION_TTL)
        return self._cookie(self.session_cookie_name, session, _SESSION_TTL), self.clear_state_cookie()

    def read_session(self, value):
        claims = self._verify('session', value)
        if not claims or not _matches(_OPEN_ID, claims.get('open_id')) or not _matches(_BROWSER_TOKEN, claims.get('csrf')):
            return None
        return AdminSession(claims['open_id'], claims['csrf'], _display_name(claims.get('name')))

    def clear_state_cookie(self):
        return self._cookie(self.state_cookie_name, '', 0)

    def clear_session_cookie(self):
        return self._cookie(self.session_cookie_name, '', 0)
