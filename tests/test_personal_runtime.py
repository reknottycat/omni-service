import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from omni_service.admin import create_admin_server
from omni_service.config import load_config
from omni_service.live_clients import TelegramClient, FeishuClient
from omni_service.model_provider import LocalModels
from omni_service.models import ChannelError
from omni_service.personal_simulation import PersonalPlatforms, connection_update, customer_update, staff_payload, recall_payload
from omni_service.runtime import BridgeRuntime
from test_config import configuration, ROOT


class PersonalRuntimeAcceptance(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.platform = self.enterContext(PersonalPlatforms(ROOT / 'fixtures/knowledge/logistics-zh-ru.md'))
        self.path = Path(self.directory) / 'personal.json'
        raw = configuration(self.directory)
        raw['telegram']['poll_timeout'] = 1
        raw['translation'].update(base_url=self.platform.url + '/v1', model='synthetic-translation')
        raw['llm'].update(enabled=True, base_url=self.platform.url + '/v1', model='synthetic-ai')
        self.path.write_text(json.dumps(raw))
        self.config = load_config(self.path)
        self.runtime = None
        self.addCleanup(lambda: self.runtime and self.runtime.close())
        self.restart()

    def restart(self):
        if self.runtime:
            self.runtime.close()
        tg = TelegramClient('900:synthetic-test-token', base_url=self.platform.url, allow_loopback=True, timeout=2)
        feishu = FeishuClient('cli_test', 'synthetic-test-secret', base_url=self.platform.url, allow_loopback=True, timeout=2)
        model = LocalModels(self.config.translation, self.config.llm, self.config.knowledge_path, environ={})
        self.runtime = BridgeRuntime(self.config, tg, feishu, model)
        self.runtime.preflight()
        return self.runtime

    def work(self, count=10):
        outcomes = []
        for _ in range(count):
            outcomes.append(self.runtime.work_once())
            if not self.runtime.service.queue_status().get('pending', 0):
                break
        return outcomes

    def start_customer(self):
        self.platform.push(connection_update())
        self.platform.push(customer_update(2))
        self.runtime.poll_once()
        self.work()
        return self.runtime.service.list_conversations()[0]

    def admin(self):
        server = create_admin_server(self.runtime.service, 'x' * 40, port=0, status=self.runtime.status)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(cleanup)
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None, *, authorized=True):
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['Authorization'] = 'Bearer ' + 'x' * 40
            req = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
            try:
                response = urlopen(req, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.loads(response.read())
        return request

    def test_full_original_account_seven_staff_translation_recall_and_restart(self):
        conv = self.start_customer()
        self.assertEqual('auto', conv['mode'])
        self.assertEqual(1, len(self.platform.telegram_sent))
        self.assertEqual('synthetic-connection', self.platform.telegram_sent[0]['business_connection_id'])
        self.assertEqual(20, self.platform.telegram_sent[0]['chat_id'])
        self.assertEqual(1, len(self.platform.rooms))
        self.assertEqual(7, len(self.platform.rooms[0]['user_id_list']))
        self.assertIn('您好', '\n'.join(x['content'] for x in self.platform.feishu_sent))
        staff_messages = [staff_payload(conv['room_id'], i, staff=f'ou_staff_{i}') for i in range(1, 8)]
        for message in staff_messages:
            self.runtime.service.accept_feishu_payload(message)
        # Durable callbacks have not sent or translated anything yet.
        self.assertEqual(1, len(self.platform.telegram_sent))
        self.restart()
        self.work()
        self.assertEqual(8, len(self.platform.telegram_sent))
        self.assertEqual(1, len(self.platform.rooms))
        for i, sent in enumerate(self.platform.telegram_sent[1:], 1):
            self.assertEqual(f'员工 {i}: Пожалуйста, сообщите номер заказа.', sent['text'])
            self.assertEqual(20, sent['chat_id'])
        self.runtime.service.accept_feishu_payload(recall_payload(conv['room_id'], 1))
        self.work()
        target = self.platform.telegram_sent[1]['message_id']
        self.assertEqual({'business_connection_id': 'synthetic-connection', 'message_ids': [target]}, self.platform.telegram_deleted[0])
        saved = self.runtime.service.get_conversation(conv['id'])
        message = next(m for m in saved['messages'] if m['source_id'] == 'om_staff_1')
        self.assertEqual('deleted', message['recall_status'])
        self.assertEqual('manual', saved['mode'])
        # Replay at the public receiver cannot send again.
        self.runtime.service.accept_feishu_payload(staff_messages[1])
        self.runtime.poll_once()
        self.work()
        self.assertEqual(8, len(self.platform.telegram_sent))
        raw = (self.config.data_dir / 'state.sqlite3').read_bytes()
        self.assertNotIn(b'synthetic-test-token', raw)
        self.assertNotIn(b'synthetic-test-secret', raw)

    def test_private_chats_wrong_app_and_unauthorized_staff_never_reach_model_or_store(self):
        marker = 'PRIVATE_FAMILY_MESSAGE_DO_NOT_PROCESS'
        self.platform.push(connection_update())
        self.platform.push(customer_update(2, text=marker, chat=21))
        self.runtime.poll_once()
        self.work()
        self.assertEqual([], self.platform.model_requests)
        self.assertEqual([], self.runtime.service.list_conversations())
        with self.runtime.service.store.lock:
            dump = '\n'.join(self.runtime.service.store.db.iterdump())
        self.assertNotIn(marker, dump)
        self.platform.push(customer_update(3))
        self.runtime.poll_once()
        self.work()
        conv = self.runtime.service.list_conversations()[0]
        before = len(self.platform.telegram_sent)
        ignored = self.runtime.service.accept_feishu_payload(staff_payload(conv['room_id'], 10, staff='ou_stranger', text=marker))
        self.assertEqual(0, ignored['accepted'])
        with self.assertRaises(ChannelError):
            self.runtime.service.accept_feishu_payload(staff_payload(conv['room_id'], 11, app='cli_wrong', text=marker))
        self.work()
        self.assertEqual(before, len(self.platform.telegram_sent))
        with self.runtime.service.store.lock:
            self.assertNotIn(marker, '\n'.join(self.runtime.service.store.db.iterdump()))

    def test_admin_api_requires_token_and_resolves_confirmed_unknown_without_resend(self):
        conv = self.start_customer()
        api = self.admin()
        self.assertEqual(401, api('/api/conversations', authorized=False)[0])
        self.assertEqual(200, api('/api/conversations')[0])
        self.platform.drop_next_receipt = True
        self.runtime.service.accept_feishu_payload(staff_payload(conv['room_id']))
        self.work()
        self.assertEqual(2, len(self.platform.telegram_sent))
        saved = self.runtime.service.get_conversation(conv['id'])
        unknown = next(o for o in saved['outbox'] if o['status'] == 'unknown')
        self.work()
        self.assertEqual(2, len(self.platform.telegram_sent))
        remote_id = self.platform.telegram_sent[-1]['message_id']
        code, resolved = api(f"/api/deliveries/{unknown['id']}/resolve", {
            'delivered': True, 'external_id': str(remote_id), 'evidence': 'Synthetic platform message record verified'})
        self.assertEqual(200, code, resolved)
        self.assertEqual(2, len(self.platform.telegram_sent))
        self.runtime.service.accept_feishu_payload(recall_payload(conv['room_id']))
        self.work()
        self.assertEqual([remote_id], self.platform.telegram_deleted[-1]['message_ids'])

    def test_owner_takeover_echo_filter_and_knowledge_answer(self):
        conv = self.start_customer()
        self.platform.push(customer_update(3, sender=10))
        echo = customer_update(4, sender=10)
        echo['business_message']['sender_business_bot'] = {'id': 900}
        self.platform.push(echo)
        self.runtime.poll_once()
        self.work()
        self.assertEqual('manual', self.runtime.service.get_conversation(conv['id'])['mode'])
        self.assertEqual(1, len(self.platform.telegram_sent))
        self.runtime.service.resume(conv['id'])
        self.platform.push(customer_update(5, text='Какие документы нужны для отправки груза?'))
        self.runtime.poll_once()
        self.work()
        self.assertEqual(2, len(self.platform.telegram_sent))
        self.assertIn('коммерческий инвойс', self.platform.telegram_sent[-1]['text'])
        with self.runtime.service.store.lock:
            audit = '\n'.join(row[0] for row in self.runtime.service.store.db.execute("SELECT detail FROM audit WHERE action='ai_grounding'"))
        self.assertIn('common-documents', audit)

    def test_preflight_rejects_webhook_conflict_and_identity_changes_without_messages(self):
        self.platform.webhook_url = 'https://existing.invalid/do-not-delete'
        with self.assertRaises(ChannelError) as caught:
            self.runtime.preflight()
        self.assertEqual('webhook_conflict', caught.exception.code)
        self.assertFalse(any('deleteWebhook' in r['path'] for r in self.platform.requests))
        self.assertEqual([], self.platform.telegram_sent)
        self.platform.webhook_url = ''
        with self.runtime.service.store.transaction() as db:
            db.execute("UPDATE runtime_identity SET value='different deployment'")
        with self.assertRaises(ChannelError) as caught:
            self.runtime.preflight()
        self.assertEqual('identity_mismatch', caught.exception.code)

    def test_revocation_and_shutdown_stop_before_network_send(self):
        conv = self.start_customer()
        self.runtime.service.accept_feishu_payload(staff_payload(conv['room_id']))
        self.platform.push(connection_update(3, enabled=False))
        self.runtime.poll_once()
        self.work()
        self.assertEqual(1, len(self.platform.telegram_sent))
        self.runtime.request_stop()
        self.runtime.work_once()
        self.assertEqual(1, len(self.platform.telegram_sent))
