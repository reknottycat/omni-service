import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from omni_service import channels
from omni_service.live_clients import FeishuClient, LiveTransport, TelegramClient
from omni_service.models import ChannelError, Request


class PlatformServer:
    """Actual HTTP fixture: records both requests and independently applied effects."""

    def __init__(self):
        self.requests = []
        self.effects = []
        self.responses = {}
        self.token_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_api()

            def do_POST(self):
                self.handle_api()

            def do_DELETE(self):
                self.handle_api()

            def handle_api(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw) if raw else None
                owner.requests.append((self.command, self.path, body, dict(self.headers)))
                operation = self.path.split('?')[0]
                scripted = owner.responses.get(operation, [])
                if scripted:
                    status, payload, headers, applied, drop = scripted.pop(0)
                elif operation == '/oauth/v3/token':
                    owner.token_count += 1
                    status, payload, headers, applied, drop = 200, {
                        'code': 0, 'access_token': 'test-tenant-' + str(owner.token_count),
                        'expires_in': 100, 'token_type': 'Bearer'}, {}, False, False
                elif operation.endswith('/getUpdates'):
                    status, payload, headers, applied, drop = 200, {'ok': True, 'result': [{'update_id': 101}]}, {}, False, False
                elif operation.endswith('/getBusinessConnection'):
                    status, payload, headers, applied, drop = 200, {'ok': True, 'result': {'id': body['business_connection_id']}}, {}, False, False
                elif operation.endswith('/getMe'):
                    status, payload, headers, applied, drop = 200, {'ok': True, 'result': {'id': 10, 'is_bot': True}}, {}, False, False
                elif operation.endswith('/getWebhookInfo'):
                    status, payload, headers, applied, drop = 200, {'ok': True, 'result': {'url': ''}}, {}, False, False
                elif operation.startswith('/bot'):
                    result = True if operation.endswith('/deleteBusinessMessages') else {'message_id': 50}
                    status, payload, headers, applied, drop = 200, {'ok': True, 'result': result}, {}, True, False
                else:
                    status, payload, headers, applied, drop = 200, {'code': 0, 'data': {'message_id': 'om_50'}}, {}, True, False
                if applied:
                    owner.effects.append((operation, body))
                if drop:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                wire = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                try:
                    self.wfile.write(wire)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def queue(self, path, payload, status=200, headers=None, applied=False, drop=False):
        self.responses.setdefault(path, []).append((status, payload, headers or {}, applied, drop))


class LiveClientAcceptance(unittest.TestCase):
    def setUp(self):
        self.platform = self.enterContext(PlatformServer())
        self.tg = TelegramClient('10:test-secret', base_url=self.platform.url, allow_loopback=True)
        self.now = 0
        self.feishu = FeishuClient('cli_test', 'synthetic-secret', base_url=self.platform.url,
                                   allow_loopback=True, clock=lambda: self.now)
        self.tg_send = Request('POST', '/sendMessage', {
            'business_connection_id': 'connection-1', 'chat_id': 123, 'text': 'Здравствуйте'})
        self.fs_send = channels.send_request('feishu', 'cli_test', 'oc_customer_1', '您好')

    def error(self, code, call):
        with self.assertRaises(ChannelError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('synthetic-secret', str(caught.exception))
        return caught.exception

    def test_telegram_original_account_wire_and_delete_receipt(self):
        receipt = self.tg.send_request(self.tg_send)
        self.assertEqual('50', channels.parse_response('telegram', 'send', receipt))
        path, body = self.platform.effects[0]
        self.assertEqual('/bot10:test-secret/sendMessage', path)
        self.assertEqual(self.tg_send.body, body)
        delete = self.tg.send_request(Request('POST', '/deleteBusinessMessages', {
            'business_connection_id': 'connection-1', 'message_ids': [50]}))
        self.assertEqual('', channels.parse_response('telegram', 'delete', delete))
        self.assertEqual('/sendMessage', self.tg_send.path)
        self.assertNotIn('test-secret', repr(self.tg))

    def test_polling_and_preflight_do_not_change_webhook(self):
        self.assertEqual([{'update_id': 101}], self.tg.get_updates(100, timeout=0))
        self.assertEqual('connection-1', self.tg.get_connection('connection-1')['id'])
        self.assertTrue(self.tg.get_me()['is_bot'])
        self.assertEqual('', self.tg.get_webhook_info()['url'])
        poll = self.platform.requests[0][2]
        self.assertEqual(100, poll['offset'])
        self.assertEqual(4, len(poll['allowed_updates']))
        self.assertEqual([], self.platform.effects)
        self.assertFalse(any('deleteWebhook' in request[1] for request in self.platform.requests))

    def test_bad_paths_and_endpoints_never_receive_credentials(self):
        for path in ('//example.com', '/bot10:other/sendMessage', '/sendMessage?token=x', '/getUpdates\r\nX:x'):
            self.error('invalid_request', lambda p=path: self.tg.send_request(Request('POST', p, {})))
        for endpoint in ('http://api.telegram.org', 'https://api.telegram.org.attacker.test',
                         'https://user:secret@api.telegram.org', 'https://api.telegram.org/x',
                         'http://localhost:1', 'http://192.168.1.2:1'):
            self.error('invalid_config', lambda u=endpoint: TelegramClient('x', base_url=u, allow_loopback=True))
        self.error('invalid_config', lambda: TelegramClient('x', base_url=self.platform.url))
        self.assertEqual([], self.platform.requests)

    def test_rate_limit_is_bounded_finite_and_not_retried_by_client(self):
        self.platform.queue('/bot10:test-secret/sendMessage',
                            {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 7}})
        error = self.error('rate_limited', lambda: self.tg.send_request(self.tg_send))
        self.assertEqual(7, error.retry_after)
        self.platform.queue('/bot10:test-secret/sendMessage',
                            {'ok': False, 'parameters': None}, status=429, headers={'Retry-After': 'NaN'})
        error = self.error('rate_limited', lambda: self.tg.send_request(self.tg_send))
        self.assertEqual(1, error.retry_after)
        self.assertEqual(2, len(self.platform.requests))

    def test_server_receipt_loss_after_effect_is_unknown_and_never_replayed(self):
        self.platform.queue('/bot10:test-secret/sendMessage', {}, applied=True, drop=True)
        self.error('delivery_unknown', lambda: self.tg.send_request(self.tg_send))
        self.assertEqual(1, len(self.platform.effects))
        self.assertEqual(1, len(self.platform.requests))

    def test_corrupt_or_oversize_receipt_never_becomes_confirmed_failure(self):
        for wire in (b'not JSON', b'[]', b'x' * 2_000_001):
            self.platform.queue('/bot10:test-secret/sendMessage', wire, applied=True)
            self.error('delivery_unknown', lambda: self.tg.send_request(self.tg_send))
        self.assertEqual(3, len(self.platform.effects))

    def test_http_redirect_server_error_and_auth_are_not_replayed(self):
        for status, expected in ((302, 'redirect_rejected'), (503, 'delivery_unknown'), (401, 'authorization_failed')):
            self.platform.queue('/bot10:test-secret/sendMessage', {'description': 'test-secret'}, status=status,
                                headers={'Location': self.platform.url + '/another'})
            self.error(expected, lambda: self.tg.send_request(self.tg_send))
        self.assertEqual(3, len(self.platform.requests))
        self.assertEqual([], self.platform.effects)

    def test_feishu_oauth_cache_and_expiry_preserve_messages(self):
        for _ in range(2):
            result = self.feishu.send_request(self.fs_send)
            self.assertEqual('om_50', channels.parse_response('feishu', 'send', result))
        self.assertEqual(1, self.platform.token_count)
        auth = self.platform.requests[0]
        self.assertEqual('/oauth/v3/token', auth[1])
        self.assertEqual('client_credentials', auth[2]['grant_type'])
        self.assertEqual('cli_test', auth[2]['client_id'])
        self.assertNotIn('Authorization', auth[3])
        self.assertEqual('Bearer test-tenant-1', self.platform.requests[1][3]['Authorization'])
        self.assertNotIn('simulation-token', str(self.platform.requests))
        self.now = 95
        self.feishu.send_request(self.fs_send)
        self.assertEqual(2, self.platform.token_count)
        self.assertEqual(3, len(self.platform.effects))

    def test_known_rejected_token_refreshes_once_with_same_uuid(self):
        request = Request('POST', self.fs_send.path, {**self.fs_send.body, 'uuid': 'stable-queue-7'})
        self.platform.queue('/open-apis/im/v1/messages', {'code': 99991663, 'msg': 'synthetic-secret'}, status=400)
        self.feishu.send_request(request)
        self.assertEqual(2, self.platform.token_count)
        self.assertEqual(1, len(self.platform.effects))
        attempts = [r for r in self.platform.requests if r[1] == request.path]
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0][2], attempts[1][2])
        self.assertNotEqual(attempts[0][3]['Authorization'], attempts[1][3]['Authorization'])

    def test_repeated_token_denial_is_terminal(self):
        for _ in range(2):
            self.platform.queue('/open-apis/im/v1/messages', {'code': 99991663})
        self.error('authorization_failed', lambda: self.feishu.send_request(self.fs_send))
        self.assertEqual(2, self.platform.token_count)
        self.assertEqual([], self.platform.effects)

    def test_feishu_generic_denial_and_unknown_do_not_trigger_auth_retry(self):
        self.platform.queue('/open-apis/im/v1/messages', {'code': 99991672, 'msg': 'synthetic-secret'})
        self.error('platform_rejected', lambda: self.feishu.send_request(self.fs_send))
        self.platform.queue('/open-apis/im/v1/messages', {}, status=502, applied=True)
        self.error('delivery_unknown', lambda: self.feishu.send_request(self.fs_send))
        self.assertEqual(1, self.platform.token_count)
        self.assertEqual(1, len(self.platform.effects))

    def test_bad_token_metadata_fails_before_business_mutation(self):
        self.platform.queue('/oauth/v3/token', {'code': 0, 'tenant_access_token': 'legacy', 'expire': 7200})
        self.error('invalid_response', lambda: self.feishu.send_request(self.fs_send))
        self.assertEqual(1, len(self.platform.requests))
        self.assertEqual([], self.platform.effects)

    def test_disabled_app_error_is_sanitized_before_business_mutation(self):
        self.platform.queue('/oauth/v3/token', {'code': 20069, 'error_description': 'synthetic-secret'}, status=400)
        self.error('authorization_failed', lambda: self.feishu.send_request(self.fs_send))
        self.assertEqual([], self.platform.effects)

    def test_live_transport_refuses_other_channels(self):
        transport = LiveTransport(self.tg, self.feishu)
        self.assertTrue(transport.send('telegram', self.tg_send)['ok'])
        self.assertEqual(0, transport.send('feishu', self.fs_send)['code'])
        self.error('unsupported_channel', lambda: transport.send('max', self.tg_send))


if __name__ == '__main__':
    unittest.main()
