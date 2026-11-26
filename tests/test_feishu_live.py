import asyncio
import json
import threading
import types
import unittest
from unittest import mock

from omni_service.feishu_live import FeishuListener
from omni_service.models import ChannelError


def sdk_fixture():
    class Builder:
        def __init__(self):
            self.callbacks = {}

        @classmethod
        def builder(cls, encrypt, verification):
            assert encrypt == verification == ''
            return cls()

        def register_p2_im_message_receive_v1(self, callback):
            self.callbacks['im.message.receive_v1'] = callback
            return self

        def register_p2_im_message_recalled_v1(self, callback):
            self.callbacks['im.message.recalled_v1'] = callback
            return self

        def build(self):
            return self.callbacks

    started = threading.Event()
    clients = []

    class Client:
        def __init__(self, app_id, app_secret, *, event_handler, log_level):
            self.event_handler = event_handler
            self._conn = None
            self.closed = False
            clients.append(self)

        def start(self):
            self._conn = object()
            self.on_reconnected()
            started.set()
            asyncio.get_event_loop().run_until_complete(asyncio.sleep(1000))

        async def _disconnect(self):
            self.closed = True
            self._conn = None

    return types.SimpleNamespace(
        EventDispatcherHandler=Builder,
        JSON=types.SimpleNamespace(marshal=json.dumps),
        LogLevel=types.SimpleNamespace(WARNING=30),
        ws=types.SimpleNamespace(Client=Client), started=started, clients=clients,
    )


def event(kind='im.message.receive_v1', app_id='cli_test'):
    return {'schema': '2.0', 'header': {'app_id': app_id, 'event_type': kind, 'event_id': 'test-1'},
            'event': {'chat_id': 'oc_test', 'message_id': 'om_test'}}


class FeishuListenerAcceptance(unittest.TestCase):
    def setUp(self):
        self.sdk = sdk_fixture()
        self.accepted = []
        self.listener = FeishuListener('cli_test', 'fake-secret', self.accepted.append, sdk=self.sdk)

    def test_only_registered_receive_and_recall_callbacks_deliver_payload(self):
        handlers = self.listener.build_handler()
        self.assertEqual({'im.message.receive_v1', 'im.message.recalled_v1'}, set(handlers))
        for kind, callback in handlers.items():
            callback(event(kind))
        self.assertEqual(2, len(self.accepted))
        self.assertEqual(2, self.listener.status()['events_accepted'])
        self.assertNotIn('fake-secret', repr(self.listener.status()))

    def test_wrong_application_never_reaches_durable_queue(self):
        callback = self.listener.build_handler()['im.message.receive_v1']
        with self.assertRaises(ChannelError) as caught:
            callback(event(app_id='cli_untrusted'))
        self.assertEqual('authorization_failed', caught.exception.code)
        self.assertEqual([], self.accepted)
        self.assertEqual(0, self.listener.status()['events_accepted'])

    def test_callback_storage_failure_raises_for_redelivery_without_exposing_content(self):
        def failed_accept(payload):
            raise RuntimeError('fake-secret and private message text')

        listener = FeishuListener('cli_test', 'fake-secret', failed_accept, sdk=self.sdk)
        callback = listener.build_handler()['im.message.receive_v1']
        with self.assertRaises(ChannelError) as caught:
            callback(event())
        self.assertEqual('event_not_accepted', caught.exception.code)
        self.assertNotIn('fake-secret', str(caught.exception))
        self.assertNotIn('private message', str(caught.exception))
        self.assertEqual(0, listener.status()['events_accepted'])

    def test_start_stop_owns_loop_and_cleans_up_sdk_socket(self):
        errors = []

        def run():
            try:
                self.listener.start()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.sdk.started.wait(2))
        self.assertEqual('connected', self.listener.status()['state'])
        self.sdk.clients[0].on_reconnecting()
        self.assertEqual('reconnecting', self.listener.status()['state'])
        self.sdk.clients[0].on_reconnected()
        self.listener.stop()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(self.sdk.clients[0].closed)
        self.assertEqual('stopped', self.listener.status()['state'])
        self.assertFalse(self.listener.status()['running'])

    def test_second_listener_cannot_share_sdk_loop(self):
        thread = threading.Thread(target=self.listener.start)
        thread.start()
        self.assertTrue(self.sdk.started.wait(2))
        other = FeishuListener('cli_other', 'fake-secret', self.accepted.append, sdk=sdk_fixture())
        try:
            with self.assertRaises(ChannelError) as caught:
                other.start()
            self.assertEqual('listener_conflict', caught.exception.code)
        finally:
            self.listener.stop()
            thread.join(4)

    def test_shutdown_before_start_prevents_a_new_connection(self):
        self.listener.stop()
        self.listener.start()
        self.assertEqual([], self.sdk.clients)
        self.assertEqual('stopped', self.listener.status()['state'])

    def test_optional_dependency_absence_is_explicit(self):
        listener = FeishuListener('cli_test', 'fake-secret', self.accepted.append)
        with mock.patch('omni_service.feishu_live.importlib.import_module', side_effect=ImportError):
            with self.assertRaises(ChannelError) as caught:
                listener.build_handler()
        self.assertEqual('missing_dependency', caught.exception.code)


def run_official_sdk_smoke():
    """Explicit optional check: actual generated models, frames and lifecycle.

    Only HTTP/WebSocket network boundaries are replaced. Not part of the
    dependency-free unittest gate, and it never contacts Feishu.
    """
    import importlib.metadata
    import sqlite3
    import tempfile
    from pathlib import Path
    import lark_oapi as lark
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    from omni_service.live_clients import _HttpJson
    from test_live_clients import PlatformServer

    assert importlib.metadata.version('lark-oapi') == '1.9.0'
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / 'events.sqlite3')
        db.execute('CREATE TABLE accepted (event_id TEXT PRIMARY KEY, body TEXT)')

        def accept(payload):
            if payload['header']['event_id'] == 'storage-failure':
                raise RuntimeError('synthetic private error, must not be logged')
            with db:
                db.execute('INSERT INTO accepted VALUES (?,?)',
                           (payload['header']['event_id'], json.dumps(payload)))

        listener = FeishuListener('cli_test', 'synthetic-secret', accept)
        replies = []

        async def capture(data):
            parsed = Frame()
            parsed.ParseFromString(data)
            replies.append(json.loads(parsed.payload))

        async def dispatch():
            client = lark.ws.Client('cli_test', 'synthetic-secret', event_handler=listener.build_handler(),
                                    log_level=lark.LogLevel.WARNING)
            client._write_message = capture
            for kind, eid in (('im.message.receive_v1', 'received'),
                              ('im.message.recalled_v1', 'recalled'),
                              ('im.message.receive_v1', 'storage-failure')):
                payload = event(kind)
                payload['header']['event_id'] = eid
                if kind == 'im.message.receive_v1':
                    payload['event'] = {
                        'sender': {'sender_id': {'open_id': 'ou_test'}, 'sender_type': 'user'},
                        'message': {'message_id': 'om_test', 'chat_id': 'oc_test',
                                    'message_type': 'text', 'chat_type': 'group',
                                    'create_time': '1000000', 'content': '{"text":"合成测试"}'}}
                else:
                    payload['event'].update(recall_time='1000000', recall_type='message_recalled')
                frame = Frame()
                frame.SeqID, frame.LogID, frame.service, frame.method = 0, 0, 1, 1
                for key, value in {'type': 'event', 'message_id': eid, 'trace_id': 'synthetic',
                                   'sum': '1', 'seq': '0'}.items():
                    header = frame.headers.add()
                    header.key, header.value = key, value
                frame.payload = json.dumps(payload).encode()
                await client._handle_data_frame(frame)

        # Wrapper produces a sanitized exception before the SDK sees failure.
        with mock.patch('lark_oapi.ws.client.logger.error') as errors:
            asyncio.run(dispatch())
        assert [r['code'] for r in replies] == [200, 200, 500]
        rows = db.execute('SELECT event_id,body FROM accepted ORDER BY event_id').fetchall()
        assert [row[0] for row in rows] == ['recalled', 'received']
        assert all(json.loads(row[1])['header']['app_id'] == 'cli_test' for row in rows)
        assert 'synthetic private error' not in str(errors.call_args_list)
        db.close()

    started = threading.Event()
    closed = threading.Event()
    failures = []

    class OfflineSocket:
        async def close(self):
            closed.set()

        async def send(self, data):
            started.set()

        async def recv(self):
            await asyncio.sleep(1000)

    async def connect_without_network(url, **kwargs):
        assert url.startswith('wss://msg-frontier.feishu.cn/ws/v2?')
        return OfflineSocket()

    owner = FeishuListener('cli_test', 'synthetic-secret', lambda payload: None)

    def run():
        try:
            owner.start()
        except Exception as error:
            failures.append(type(error).__name__)

    with PlatformServer() as platform:
        platform.queue('/callback/ws/endpoint', {'code': 0, 'data': {
            'URL': 'wss://msg-frontier.feishu.cn/ws/v2?device_id=offline&service_id=1',
            'ClientConfig': {'ReconnectCount': -1, 'ReconnectInterval': 120,
                             'ReconnectNonce': 0, 'PingInterval': 120}}})
        owner._discovery_http = _HttpJson(platform.url, official_host='open.feishu.cn',
                                         timeout=2, allow_loopback=True)
        with mock.patch('lark_oapi.ws.client.websockets.connect', connect_without_network):
            thread = threading.Thread(target=run)
            thread.start()
            assert started.wait(3), 'SDK listener did not start'
            assert owner.status()['state'] == 'connected'
            owner.stop()
            thread.join(5)
            assert not thread.is_alive() and not failures
            assert closed.is_set() and owner.status()['state'] == 'stopped'
        assert len(platform.requests) == 1
        assert platform.requests[0][1] == '/callback/ws/endpoint'
        assert platform.requests[0][2]['AppID'] == 'cli_test'
    return {'sdk_version': '1.9.0', 'actual_sdk_dispatch': True,
            'durable_events': 2, 'ack_codes': [200, 200, 500],
            'actual_sdk_start_stop': True, 'loopback_discovery_requests': 1,
            'external_network_calls': 0}


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--official-sdk-smoke']:
        print(json.dumps(run_official_sdk_smoke(), sort_keys=True))
    else:
        unittest.main()
