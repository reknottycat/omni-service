"""Bounded Telegram/Feishu HTTP clients; credentials exist only in memory.

Production destinations are the official HTTPS origins. Explicit loopback
endpoints exercise the exact wire clients in offline acceptance. No request is
automatically replayed after an uncertain delivery. Feishu's one auth refresh
is limited to a provider response explicitly rejecting the tenant token.
"""

import http.client
import ipaddress
import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import ChannelError, Request


MAX_JSON_BYTES = 2_000_000


def _secret(value):
    if (not isinstance(value, str) or not value or len(value) > 65_536
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ChannelError('invalid_config', 'A nonempty credential without whitespace is required.')
    return value


def _delay(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return max(0.1, min(number, 3600.0)) if math.isfinite(number) else 1.0


def _relative_path(path):
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or any(ord(char) < 33 or ord(char) > 126 for char in path)
            or '\\' in path or '#' in path):
        raise ChannelError('invalid_request', 'A relative API path is required.')
    return path


@dataclass(frozen=True)
class _Reply:
    status: int
    payload: dict
    retry_after: float


class _HttpJson:
    def __init__(self, base_url, *, official_host, timeout, allow_loopback):
        try:
            base = urlsplit(base_url)
            port = base.port
            host = base.hostname or ''
        except (TypeError, ValueError):
            raise ChannelError('invalid_config', 'Invalid platform endpoint.') from None
        if (base.username or base.password or base.query or base.fragment
                or '\\' in base_url or any(ord(c) < 33 for c in base_url)):
            raise ChannelError('invalid_config', 'Endpoint credentials or URL parameters are forbidden.')
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        official = (base.scheme == 'https' and host == official_host
                    and port in (None, 443) and base.path in ('', '/'))
        local = (allow_loopback is True and base.scheme == 'http' and loopback
                 and not any(p in ('.', '..') for p in base.path.split('/'))
                 and '%' not in base.path)
        if not (official or local):
            raise ChannelError('invalid_config', 'Use the official HTTPS origin or an explicit loopback test endpoint.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ChannelError('invalid_config', 'HTTP timeout must be between zero and 120 seconds.')
        self.host = host
        self.port = port or (443 if base.scheme == 'https' else 80)
        self.prefix = base.path.rstrip('/')
        self.https = base.scheme == 'https'
        self.loopback = local
        self.timeout = float(timeout)

    def request(self, method, path, body=None, headers=None):
        _relative_path(path)
        if method not in ('GET', 'POST', 'DELETE'):
            raise ChannelError('invalid_request', 'Unsupported HTTP method.')
        if body is not None and not isinstance(body, dict):
            raise ChannelError('invalid_request', 'The API request body must be an object.')
        try:
            wire = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            raise ChannelError('invalid_request', 'The API request is not valid JSON.') from None
        if wire is not None and len(wire) > MAX_JSON_BYTES:
            raise ChannelError('invalid_request', 'The API request exceeds the size limit.')
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        connection = cls(self.host, self.port, timeout=self.timeout)
        try:
            connection.connect()
            if self.loopback and not ipaddress.ip_address(connection.sock.getpeername()[0]).is_loopback:
                raise ChannelError('invalid_config', 'The test endpoint peer is not loopback.')
            connection.request(method, self.prefix + path, body=wire,
                               headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                        **(headers or {})})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ChannelError('redirect_rejected', 'Platform redirects are never followed.')
            if response.status >= 500:
                raise ChannelError('delivery_unknown', 'The platform outcome could not be confirmed.')
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ChannelError('delivery_unknown', 'The platform receipt exceeds the size limit.')
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                if response.status in (401, 403):
                    raise ChannelError('authorization_failed', 'The platform rejected authorization.') from None
                if response.status == 429:
                    raise ChannelError('rate_limited', 'The platform rate limit was reached.',
                                       _delay(response.getheader('Retry-After'))) from None
                raise ChannelError('delivery_unknown', 'The platform returned an unreadable receipt.') from None
            if not isinstance(payload, dict):
                raise ChannelError('delivery_unknown', 'The platform receipt must be an object.')
            params = payload.get('parameters')
            retry = params.get('retry_after') if isinstance(params, dict) else None
            delay = _delay(retry if retry is not None else response.getheader('Retry-After'))
            if response.status == 429:
                raise ChannelError('rate_limited', 'The platform rate limit was reached.', delay)
            return _Reply(response.status, payload, delay)
        except ChannelError:
            raise
        except (socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            # Never expose exception text: Telegram puts its token in the URL.
            raise ChannelError('delivery_unknown', 'The platform outcome could not be confirmed.') from None
        finally:
            connection.close()


def _http_error(reply):
    if reply.status in (401, 403):
        raise ChannelError('authorization_failed', 'The platform rejected authorization.')
    if reply.status >= 400:
        raise ChannelError('platform_rejected', 'The platform rejected the request.')


class TelegramClient:
    """Original-account requests use a credential-free path such as /sendMessage."""

    METHODS = frozenset(('getUpdates', 'getBusinessConnection', 'getMe', 'getWebhookInfo',
                         'sendMessage', 'deleteBusinessMessages'))

    def __init__(self, bot_token, *, base_url='https://api.telegram.org', timeout=35.0,
                 allow_loopback=False):
        self._token = _secret(bot_token)
        if not re.fullmatch(r'[A-Za-z0-9_:\-]+', self._token):
            raise ChannelError('invalid_config', 'The Telegram bot token has invalid characters.')
        self._http = _HttpJson(base_url, official_host='api.telegram.org', timeout=timeout,
                               allow_loopback=allow_loopback)

    def send_request(self, request):
        if (not isinstance(request, Request) or request.method != 'POST'
                or request.path not in {'/' + method for method in self.METHODS}):
            raise ChannelError('invalid_request', 'Unsupported credential-free Telegram method.')
        reply = self._http.request('POST', '/bot' + self._token + request.path, request.body)
        payload = reply.payload
        if payload.get('ok') is False:
            code = payload.get('error_code')
            if code == 429:
                raise ChannelError('rate_limited', 'Telegram rate limit reached.', reply.retry_after)
            if code in (401, 403):
                raise ChannelError('authorization_failed', 'Telegram rejected authorization.')
            if type(code) is int and code >= 500:
                raise ChannelError('delivery_unknown', 'Telegram could not confirm the result.')
            raise ChannelError('platform_rejected', 'Telegram rejected the request.')
        _http_error(reply)
        if payload.get('ok') is not True or 'result' not in payload:
            raise ChannelError('invalid_response', 'Telegram did not provide a valid receipt.')
        return payload

    def _read(self, method, body, expected):
        result = self.send_request(Request('POST', '/' + method, body))['result']
        if not isinstance(result, expected):
            raise ChannelError('invalid_response', 'Telegram returned an unexpected result type.')
        return result

    def get_updates(self, offset, timeout=30):
        if type(offset) is not int or offset < 0:
            raise ChannelError('invalid_request', 'Polling offset must be a nonnegative integer.')
        if type(timeout) is not int or not 0 <= timeout <= 50 or timeout >= self._http.timeout:
            raise ChannelError('invalid_request', 'Polling timeout must be shorter than the HTTP timeout.')
        result = self._read('getUpdates', {
            'offset': offset, 'timeout': timeout, 'limit': 100,
            'allowed_updates': ['business_connection', 'business_message',
                                'edited_business_message', 'deleted_business_messages'],
        }, list)
        if any(not isinstance(item, dict) for item in result):
            raise ChannelError('invalid_response', 'Telegram returned a malformed update batch.')
        return result

    def get_connection(self, connection_id):
        if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 512:
            raise ChannelError('invalid_request', 'A business connection identifier is required.')
        return self._read('getBusinessConnection', {'business_connection_id': connection_id}, dict)

    def get_me(self):
        result = self._read('getMe', {}, dict)
        if result.get('is_bot') is not True or type(result.get('id')) is not int or result['id'] <= 0:
            raise ChannelError('invalid_response', 'Telegram returned an invalid bot identity.')
        return result

    def get_webhook_info(self):
        result = self._read('getWebhookInfo', {}, dict)
        if not isinstance(result.get('url'), str):
            raise ChannelError('invalid_response', 'Telegram returned invalid webhook information.')
        return result


class FeishuClient:
    """Current self-built-app OAuth token flow, with a memory-only token cache."""

    def __init__(self, app_id, app_secret, *, base_url='https://open.feishu.cn',
                 timeout=15.0, allow_loopback=False, clock=time.monotonic, auth_base_url=None):
        self.app_id = _secret(app_id)
        self._secret = _secret(app_secret)
        self._http = _HttpJson(base_url, official_host='open.feishu.cn', timeout=timeout,
                               allow_loopback=allow_loopback)
        auth_url = auth_base_url or (base_url if self._http.loopback else 'https://accounts.feishu.cn')
        self._auth = _HttpJson(auth_url, official_host='accounts.feishu.cn', timeout=timeout,
                               allow_loopback=allow_loopback)
        self._clock = clock
        self._lock = threading.RLock()
        self._token = ''
        self._expires = 0.0

    def _access_token(self):
        with self._lock:
            if self._token and self._clock() < self._expires:
                return self._token
            reply = self._auth.request('POST', '/oauth/v3/token', {
                'grant_type': 'client_credentials', 'client_id': self.app_id,
                'client_secret': self._secret,
            })
            payload = reply.payload
            if reply.status >= 400 or type(payload.get('code')) is not int or payload.get('code') != 0:
                raise ChannelError('authorization_failed', 'Feishu did not issue an application access token.')
            token, expires = payload.get('access_token'), payload.get('expires_in')
            if (not isinstance(token, str) or not token or len(token) > 65_536
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)
                    or type(expires) is not int or expires <= 0
                    or payload.get('token_type') != 'Bearer'):
                raise ChannelError('invalid_response', 'Feishu returned invalid token metadata.')
            self._token = token
            self._expires = self._clock() + expires - min(60, expires * 0.1)
            return token

    def _invalidate(self, token):
        with self._lock:
            if self._token == token:
                self._token, self._expires = '', 0.0

    def send_request(self, request):
        if not isinstance(request, Request):
            raise ChannelError('invalid_request', 'A platform request is required.')
        _relative_path(request.path)
        path = urlsplit(request.path).path
        allowed = ((request.method == 'POST' and path in ('/open-apis/im/v1/messages', '/open-apis/im/v1/chats'))
                   or (request.method == 'POST' and re.fullmatch(r'/open-apis/im/v1/chats/[A-Za-z0-9_-]+/members', path))
                   or (request.method == 'DELETE' and re.fullmatch(r'/open-apis/im/v1/messages/[A-Za-z0-9_-]+', path))
                   or (request.method == 'GET' and path == '/open-apis/bot/v3/info/'))
        if not allowed:
            raise ChannelError('invalid_request', 'Unsupported Feishu API path.')
        for attempt in range(2):
            token = self._access_token()
            # Ignore caller Authorization, including simulation-token headers.
            reply = self._http.request(request.method, request.path, request.body,
                                       {'Authorization': 'Bearer ' + token})
            payload = reply.payload
            if type(payload.get('code')) is int and payload['code'] == 99991663:
                self._invalidate(token)
                if attempt == 0:
                    continue  # Explicit authorization rejection proves no operation took place.
                raise ChannelError('authorization_failed', 'Feishu rejected the refreshed access token.')
            if payload.get('code') == 99991400:
                raise ChannelError('rate_limited', 'Feishu rate limit reached.', reply.retry_after)
            if reply.status == 401:
                self._invalidate(token)
            _http_error(reply)
            if type(payload.get('code')) is not int:
                raise ChannelError('invalid_response', 'Feishu did not provide a valid result code.')
            if payload['code'] != 0:
                raise ChannelError('platform_rejected', 'Feishu rejected the operation.')
            return payload
        raise AssertionError('unreachable')

    def get_bot_info(self):
        return self.send_request(Request('GET', '/open-apis/bot/v3/info/'))


class LiveTransport:
    def __init__(self, telegram_client, feishu_client):
        self.telegram = telegram_client
        self.feishu = feishu_client

    def send(self, channel, request):
        if channel == 'telegram':
            return self.telegram.send_request(request)
        if channel == 'feishu':
            return self.feishu.send_request(request)
        raise ChannelError('unsupported_channel', 'This live runtime supports Telegram and Feishu.')
