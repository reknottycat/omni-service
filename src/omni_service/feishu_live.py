"""Optional official Feishu WebSocket listener with durable callback handoff.

``start`` blocks its calling thread. It owns a fresh asyncio loop, so callers
may use a dedicated thread. The pinned SDK 1.9.0 uses a module-global loop and
has no public stop method; this wrapper serializes SDK ownership and cleans up
its tasks/socket on stop. No model or outbound API work belongs in callbacks.
"""

import asyncio
import importlib
import json
import logging
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from .live_clients import MAX_JSON_BYTES, _HttpJson, _secret
from .models import ChannelError


_SDK_OWNER = threading.Lock()


class _SafeSdkLog(logging.Filter):
    def filter(self, record):
        # SDK DEBUG includes message bodies; errors can contain server echoes.
        if record.levelno < logging.WARNING:
            return False
        record.msg = 'Feishu SDK transport reported an event; inspect listener status.'
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class FeishuListener:
    def __init__(self, app_id, app_secret, accept_payload, *, sdk=None):
        self.app_id = _secret(app_id)
        self._secret = _secret(app_secret)
        if not callable(accept_payload):
            raise ChannelError('invalid_config', 'A durable Feishu event callback is required.')
        self._accept = accept_payload
        self._sdk = sdk
        self._injected = sdk is not None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._loop = None
        self._client = None
        self._running = False
        self._state = 'stopped'
        self._last_error = ''
        self._last_event_at = None
        self._received = 0
        # SDK 1.9.0 endpoint discovery has no request timeout. Replace only
        # that HTTP boundary; frame processing/reconnect remain in the SDK.
        self._discovery_http = _HttpJson('https://open.feishu.cn', official_host='open.feishu.cn',
                                         timeout=10, allow_loopback=False)

    def _load_sdk(self):
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module('lark_oapi')
            except ImportError:
                raise ChannelError('missing_dependency', 'Install requirements-live.txt for Feishu long connections.') from None
        return self._sdk

    def _callback(self, data):
        try:
            wire = self._load_sdk().JSON.marshal(data)
            if not isinstance(wire, str) or len(wire.encode('utf-8')) > MAX_JSON_BYTES:
                raise ChannelError('invalid_payload', 'Feishu event exceeds the size limit.')
            payload = json.loads(wire)
            if not isinstance(payload, dict) or payload.get('schema') != '2.0':
                raise ChannelError('invalid_payload', 'A Feishu schema 2.0 event is required.')
            header = payload.get('header')
            if not isinstance(header, dict) or header.get('app_id') != self.app_id:
                raise ChannelError('authorization_failed', 'Feishu event application does not match.')
            if header.get('event_type') not in ('im.message.receive_v1', 'im.message.recalled_v1'):
                raise ChannelError('invalid_payload', 'This Feishu event type is not subscribed.')
            # The callback must commit before returning. Raising causes SDK to
            # send a failure acknowledgement, allowing platform redelivery.
            self._accept(payload)
        except ChannelError as error:
            with self._lock:
                self._last_error = error.code
            raise ChannelError(error.code, 'Feishu event was not durably accepted.') from None
        except Exception:
            with self._lock:
                self._last_error = 'event_not_accepted'
            raise ChannelError('event_not_accepted', 'Feishu event was not durably accepted.') from None
        with self._lock:
            self._received += 1
            self._last_event_at = time.time()
            self._last_error = ''
            if self._running:
                self._state = 'connected'

    def build_handler(self):
        sdk = self._load_sdk()
        return (sdk.EventDispatcherHandler.builder('', '')
                .register_p2_im_message_receive_v1(self._callback)
                .register_p2_im_message_recalled_v1(self._callback)
                .build())

    def _discover_endpoint(self, client):
        errors = importlib.import_module('lark_oapi.ws.exception')
        try:
            reply = self._discovery_http.request('POST', '/callback/ws/endpoint',
                                                 {'AppID': self.app_id, 'AppSecret': self._secret})
        except ChannelError as error:
            if error.code in ('authorization_failed', 'redirect_rejected', 'invalid_config'):
                raise errors.ClientException(403, 'Feishu connection discovery authorization failed.') from None
            raise errors.ServerException(1, 'Feishu connection discovery unavailable.') from None
        payload, code = reply.payload, reply.payload.get('code')
        if type(code) is not int:
            raise errors.ServerException(1, 'Feishu discovery returned an invalid receipt.')
        if code in (1, 1000040343):
            raise errors.ServerException(code, 'Feishu connection discovery unavailable.')
        if code != 0 or reply.status >= 400:
            raise errors.ClientException(code or reply.status, 'Feishu rejected connection authorization.')
        data = payload.get('data')
        url = data.get('URL') if isinstance(data, dict) else None
        if not isinstance(url, str) or not url or len(url) > 8192:
            raise errors.ServerException(1, 'Feishu discovery returned no connection endpoint.')
        try:
            endpoint = urlsplit(url)
            valid = (endpoint.scheme == 'wss' and endpoint.hostname and not endpoint.username
                     and not endpoint.password and not endpoint.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise errors.ClientException(1, 'Feishu discovery returned an insecure connection endpoint.')
        config = data.get('ClientConfig')
        if config is not None:
            fields = ('ReconnectCount', 'ReconnectInterval', 'ReconnectNonce', 'PingInterval')
            if (not isinstance(config, dict) or any(type(config.get(key)) is not int for key in fields)
                    or config['ReconnectCount'] < -1 or config['ReconnectInterval'] <= 0
                    or config['ReconnectNonce'] < 0 or config['PingInterval'] <= 0):
                raise errors.ServerException(1, 'Feishu discovery returned invalid connection settings.')
            client._configure(SimpleNamespace(**{key: config[key] for key in fields}))
        return url

    def _new_client(self, sdk):
        client_type = sdk.ws.Client
        if not self._injected:
            discover = self._discover_endpoint

            class BoundedDiscoveryClient(client_type):
                def _get_conn_url(self):
                    return discover(self)

            client_type = BoundedDiscoveryClient
        return client_type(self.app_id, self._secret, event_handler=self.build_handler(),
                           log_level=sdk.LogLevel.WARNING)

    def _reconnecting(self):
        with self._lock:
            self._state = 'reconnecting'

    def _reconnected(self):
        with self._lock:
            self._state = 'connected'
            self._last_error = ''

    def status(self):
        with self._lock:
            state = self._state
            if self._running and state == 'connecting' and getattr(self._client, '_conn', None) is not None:
                state = 'connected'
            return {'state': state, 'running': self._running,
                    'stop_requested': self._stop.is_set(),
                    'events_accepted': self._received, 'last_event_at': self._last_event_at,
                    'last_error': self._last_error}

    async def _shutdown(self):
        # The pinned SDK has no public shutdown hook. Bound its private socket
        # cleanup, then stop our loop; startup/reconnect sleeps are cancelled.
        client = self._client
        if client is not None and callable(getattr(client, '_disconnect', None)):
            try:
                await asyncio.wait_for(client._disconnect(), timeout=2)
            except Exception:
                pass
        self._loop.stop()

    def stop(self):
        self._stop.set()
        with self._lock:
            loop = self._loop
            if self._running:
                self._state = 'stopping'
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: loop.create_task(self._shutdown()))
            except RuntimeError:
                pass  # The owning thread completed between the two checks.

    def start(self):
        # A shutdown request can arrive between runtime construction and this
        # thread entering start. Never erase it and open a new connection.
        if self._stop.is_set():
            return
        if not _SDK_OWNER.acquire(blocking=False):
            raise ChannelError('listener_conflict', 'Only one Feishu listener may own the SDK loop in this process.')
        loop = asyncio.new_event_loop()
        module = previous_sdk_loop = None
        sdk_logger = logging.getLogger('Lark')
        safe_filter = _SafeSdkLog()
        try:
            asyncio.set_event_loop(loop)
            with self._lock:
                self._running = True
                self._state = 'connecting'
                self._last_error = ''
                self._loop = loop
            sdk = self._load_sdk()
            if not self._injected:
                module = importlib.import_module('lark_oapi.ws.client')
                previous_sdk_loop = module.loop
                module.loop = loop
            sdk_logger.addFilter(safe_filter)
            self._client = self._new_client(sdk)
            self._client.on_reconnecting = self._reconnecting
            self._client.on_reconnected = self._reconnected
            if not self._stop.is_set():
                self._client.start()
        except Exception as error:
            if not self._stop.is_set():
                code = error.code if isinstance(error, ChannelError) else 'listener_failed'
                with self._lock:
                    self._state, self._last_error = 'failed', code
                raise ChannelError(code, 'Feishu listener could not continue.') from None
        finally:
            # Cancel the SDK's heartbeat, receive and infinite-select tasks.
            pending = list(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except RuntimeError:
                    pass
            loop.close()
            if module is not None:
                module.loop = previous_sdk_loop
            sdk_logger.removeFilter(safe_filter)
            asyncio.set_event_loop(None)
            with self._lock:
                self._running = False
                if self._state != 'failed':
                    self._state = 'stopped'
                self._loop = None
                self._client = None
            _SDK_OWNER.release()
