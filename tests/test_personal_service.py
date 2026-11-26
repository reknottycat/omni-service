"""Original-account behavior observed through actual local HTTP effects."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from omni_service.language import SimulationLanguage
from omni_service.models import ChannelError, Request
from omni_service.personal_service import PersonalService
from omni_service.simulation import FakePlatforms
from omni_service.transport import HttpTransport


def connection(cid='c1', enabled=True, owner=10, delete=True, reply=True):
    return {'id': cid, 'user': {'id': owner}, 'is_enabled': enabled,
            'rights': {'can_reply': reply, 'can_delete_sent_messages': delete}}


def telegram(uid, *, chat=20, sender=None, mid=None, text='Здравствуйте', cid='c1', date=2_000_000_000, **extra):
    return {'update_id': uid, 'business_message': {
        'business_connection_id': cid, 'chat': {'id': chat, 'type': 'private'},
        'from': {'id': chat if sender is None else sender, 'is_bot': False},
        'message_id': uid if mid is None else mid, 'date': date, 'text': text, **extra}}


def feishu(room, mid='staff-1', text='您好，我来帮您处理。', employee='ou_1', *, app='cli_test', event_id=None):
    return {'schema': '2.0', 'header': {'app_id': app, 'event_id': event_id or mid, 'event_type': 'im.message.receive_v1'},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': employee}},
                      'message': {'message_id': mid, 'chat_id': room, 'message_type': 'text',
                                  'content': json.dumps({'text': text}), 'create_time': '2000000000000'}}}


class BusinessPlatforms(FakePlatforms):
    def receive(self, method, path, headers, payload):
        if path.startswith('/telegram/') and path.endswith('/deleteBusinessMessages'):
            with self.lock:
                self.requests.append({'channel': 'telegram', 'method': method,
                                      'path': '/telegram/deleteBusinessMessages', 'body': payload, 'operation': 'delete'})
                if self.failures[('telegram', 'delete')]:
                    return self.failures[('telegram', 'delete')].popleft()
                found = [m for m in self.messages if m['channel'] == 'telegram'
                         and m['connection_id'] == payload.get('business_connection_id')
                         and int(m['message_id']) in payload.get('message_ids', [])]
                if not found:
                    return 400, {'ok': False, 'error_code': 400}, {}
                for message in found:
                    message['deleted'] = True
                return 200, {'ok': True, 'result': True}, {}
        response = super().receive(method, path, headers, payload)
        if path.startswith('/telegram/') and path.endswith('/sendMessage'):
            status, data, response_headers = response
            if status == 200 and data.get('ok') is True and isinstance(data.get('result'), dict):
                data['result'].update({'chat': {'id': payload['chat_id']},
                                       'business_connection_id': payload['business_connection_id']})
                with self.lock:
                    self.messages[-1]['connection_id'] = payload['business_connection_id']
            return status, data, response_headers
        return response


class SimulationCredentials:
    """Test-only injection at transport boundary, mirroring a credential owner."""
    def __init__(self, urls):
        self.http = HttpTransport(urls)
        self.requests = []
        self.wrong_receipt = False

    def send(self, channel, request):
        self.requests.append((channel, request))
        if channel == 'telegram':
            request = replace(request, path='/botsimulation-token' + request.path)
        else:
            request = replace(request, headers={**request.headers, 'Authorization': 'Bearer simulation-token'})
        result = self.http.send(channel, request)
        if self.wrong_receipt and channel == 'telegram' and request.path.endswith('/sendMessage'):
            self.wrong_receipt = False
            result['result']['chat']['id'] = 999
        return result


class PersonalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platforms = self.enterContext(BusinessPlatforms())
        self.transport = SimulationCredentials(self.platforms.urls)
        self.now = 2_000_000_000.0
        self.path = Path(self.temp.name) / 'state.db'
        self.service = self.build()
        self.addCleanup(lambda: self.service.close())
        self.service.accept_telegram_update({'update_id': 1, 'business_connection': connection()})

    def build(self, **changes):
        values = dict(expected_owner_id=10, allowed_chat_ids=[20, 21], feishu_account_id='cli_test',
                      members=tuple(f'ou_{n}' for n in range(1, 8)), clock=lambda: self.now,
                      staff_names={f'ou_{n}': f'员工{n}' for n in range(1, 8)})
        values.update(changes)
        return PersonalService(self.path, self.transport, **values)

    def open_room(self):
        self.service.accept_telegram_update(telegram(2))
        self.service.process_inbound()
        self.service.drain()
        return self.service.list_conversations()[0]

    def outgoing(self):
        return [m for m in self.platforms.messages if m['channel'] == 'telegram']

    def test_customer_auto_reply_and_seven_staff_return_via_original_account(self):
        room = self.open_room()
        self.assertEqual(room['mode'], 'auto')
        self.assertEqual(room['feishu_account_id'], 'cli_test')
        self.assertEqual(self.outgoing()[0]['connection_id'], 'c1')
        for n in range(1, 8):
            self.service.accept_feishu_payload(feishu(room['room_id'], mid=f'staff-{n}', employee=f'ou_{n}'))
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual([m['text'] for m in self.outgoing()[1:]],
                         [f'员工{n}: Здравствуйте! Я помогу вам.' for n in range(1, 8)])
        self.assertEqual(self.service.get_conversation(room['id'])['mode'], 'manual')
        for channel, request in self.transport.requests:
            if channel == 'telegram':
                self.assertEqual(request.path, '/sendMessage')
                self.assertEqual(request.body['business_connection_id'], 'c1')
                self.assertEqual(request.body['chat_id'], 20)
                self.assertNotIn('Authorization', request.headers)
        self.assertTrue(any(m['text'].startswith('AI → 客户:') for m in self.platforms.messages if m['channel'] == 'feishu'))

    def test_denied_private_chats_are_not_stored_or_sent_to_models(self):
        secret = 'PRIVATE FAMILY MESSAGE NEVER STORE'
        self.service.accept_telegram_update(telegram(2, chat=99, text=secret))
        self.service.accept_telegram_update(telegram(3, sender=99, text=secret))
        ordinary = {'update_id': 4, 'message': {'text': secret}}
        self.service.accept_telegram_update(ordinary)
        self.service.process_inbound()
        self.service.drain()
        with self.service.store.lock:
            dump = '\n'.join(self.service.store.db.iterdump())
        self.assertNotIn(secret, dump)
        self.assertEqual(self.service.list_conversations(), [])
        self.assertEqual(self.platforms.messages, [])
        self.assertEqual(self.service.telegram_offset(), 5)

    def test_durable_queue_cursor_restart_and_replay_are_at_most_once(self):
        update = telegram(2)
        self.service.accept_telegram_update(update)
        self.assertEqual(self.service.telegram_offset(), 3)
        self.service.close()
        self.service = self.build()
        self.assertEqual(self.service.telegram_offset(), 3)
        self.assertEqual(self.service.accept_telegram_update(update)['status'], 'duplicate')
        self.service.hydrate_connection(connection())
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)
        self.service.accept_telegram_update(update)
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)

    def test_revocation_cancels_before_models_and_rechecks_already_queued_send(self):
        self.service.accept_telegram_update(telegram(2))
        self.service.accept_telegram_update({'update_id': 3, 'business_connection': connection(enabled=False)})
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(self.service.queue_status(), {'cancelled': 1})
        self.assertEqual(self.service.list_conversations(), [])
        self.service.hydrate_connection(connection())
        self.service.accept_telegram_update(telegram(4))
        self.service.process_inbound()
        self.service.accept_telegram_update({'update_id': 5, 'business_connection': connection(enabled=False)})
        self.service.drain()
        self.assertEqual(self.outgoing(), [])

    def test_connection_hydration_is_scoped_and_wrong_owner_cannot_bind(self):
        self.assertIsNone(self.service.connection_needed(telegram(2, chat=99, cid='other')))
        self.assertEqual(self.service.connection_needed(telegram(2, cid='other')), 'other')
        self.assertFalse(self.service.hydrate_connection(connection('other', owner=11)))
        self.assertEqual(self.service.connection_ids(), ('c1',))
        self.assertTrue(self.service.hydrate_connection(connection('other')))
        self.service.accept_telegram_update(telegram(2, cid='other'))
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(self.outgoing()[0]['connection_id'], 'other')

    def test_owner_phone_reply_pauses_ai_mirrors_and_never_resends(self):
        self.service.accept_telegram_update(telegram(2))
        self.service.accept_telegram_update(telegram(3, sender=10, text='您好，我来帮您处理。'))
        self.service.process_inbound()
        self.service.drain()
        room = self.service.list_conversations()[0]
        self.assertEqual(room['mode'], 'manual')
        self.assertEqual(self.outgoing(), [])
        self.assertTrue(any('账号本人' in m['text'] for m in self.platforms.messages))
        self.service.accept_telegram_update(telegram(4, sender=10, sender_business_bot={'id': 7}))
        self.service.process_inbound()
        self.assertEqual(len(self.service.get_conversation(room['id'])['messages']), 2)

    def test_wrong_feishu_app_unknown_room_and_nonmembers_never_relay(self):
        room = self.open_room()
        with self.assertRaises(ChannelError):
            self.service.accept_feishu_payload(feishu(room['room_id'], app='other-app'))
        self.assertEqual(self.service.accept_feishu_payload(feishu('oc_unknown'))['accepted'], 0)
        self.assertEqual(self.service.accept_feishu_payload(feishu(room['room_id'], employee='outsider'))['accepted'], 0)
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)

    def test_feishu_recall_uses_business_delete_with_exact_receipt(self):
        room = self.open_room()
        self.service.accept_feishu_payload(feishu(room['room_id']))
        self.service.process_inbound()
        self.service.drain()
        source = self.outgoing()[-1]
        callback = {'schema': '2.0', 'header': {'app_id': 'cli_test', 'event_id': 'recall-1', 'event_type': 'im.message.recalled_v1'},
                    'event': {'chat_id': room['room_id'], 'message_id': 'staff-1', 'recall_time': str(int(self.now * 1000))}}
        self.service.accept_feishu_payload(callback)
        self.service.process_inbound()
        self.service.drain()
        self.assertTrue(source['deleted'])
        request = next(r for c, r in self.transport.requests if c == 'telegram' and r.path == '/deleteBusinessMessages')
        self.assertEqual(request.body, {'business_connection_id': 'c1', 'message_ids': [int(source['message_id'])]})
        self.assertNotIn('chat_id', request.body)

    def test_customer_side_delete_of_sent_ai_updates_receipt_and_mirror(self):
        room = self.open_room()
        external_id = int(self.outgoing()[0]['message_id'])
        self.service.accept_telegram_update({'update_id': 3, 'deleted_business_messages': {
            'business_connection_id': 'c1', 'chat': {'id': 20, 'type': 'private'}, 'message_ids': [external_id]}})
        self.service.process_inbound()
        self.service.drain()
        outgoing = next(m for m in self.service.get_conversation(room['id'])['messages'] if m['author_id'] == 'AI')
        self.assertEqual(outgoing['recall_status'], 'deleted')
        mirror = next(m for m in self.platforms.messages if m['channel'] == 'feishu' and m['text'].startswith('AI →'))
        self.assertTrue(mirror['deleted'])

    def test_window_rechecked_before_dispatch_and_recall_right_not_faked(self):
        room = self.open_room()
        self.service.accept_feishu_payload(feishu(room['room_id']))
        self.service.process_inbound()
        self.now += 86400
        self.service.drain()
        state = self.service.get_conversation(room['id'])
        staff = next(m for m in state['messages'] if m['author_id'] == 'ou_1')
        self.assertEqual(staff['status'], 'blocked_window')
        self.service.hydrate_connection(connection(delete=False))
        ai = next(m for m in state['messages'] if m['author_id'] == 'AI')
        self.service.recall(ai['id'], room['id'], 'ou_1')
        self.service.drain()
        ai = next(m for m in self.service.get_conversation(room['id'])['messages'] if m['author_id'] == 'AI')
        self.assertEqual(ai['recall_status'], 'unsupported')

    def test_missing_or_cross_chat_receipt_is_unknown_and_manual_resolution_never_resends(self):
        self.transport.wrong_receipt = True
        room = self.open_room()
        state = self.service.get_conversation(room['id'])
        unknown = next(o for o in state['outbox'] if o['status'] == 'unknown')
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)
        self.service.resolve_delivery(unknown['id'], delivered=True,
                                      external_id=self.outgoing()[0]['message_id'], evidence='Checked original test chat')
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)
        with self.assertRaises(ChannelError):
            self.service.resolve_delivery(unknown['id'], delivered=False, evidence='Cannot resolve twice')

    def test_outbound_waits_for_fresh_connection_after_restart(self):
        self.service.accept_telegram_update(telegram(2))
        self.service.process_inbound()
        self.service.close()
        self.service = self.build()
        self.service.drain()
        self.assertEqual(self.platforms.messages, [])
        self.service.hydrate_connection(connection())
        self.now += 5
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)

    def test_cursor_can_recover_telegram_random_reset_after_week_idle(self):
        self.service.accept_telegram_update(telegram(400000))
        self.now += 7 * 86400
        self.assertEqual(self.service.telegram_offset(), 0)
        self.service.accept_telegram_update(telegram(5, date=int(self.now)))
        self.assertEqual(self.service.telegram_offset(), 6)
        with self.service.store.lock:
            ids = [r['event_id'] for r in self.service.store.db.execute('SELECT event_id FROM personal_queue')]
        self.assertEqual(ids, ['0:400000', '1:5'])

    def test_slow_model_does_not_block_callback_and_owner_preempts_commit(self):
        started, release = threading.Event(), threading.Event()
        class Slow(SimulationLanguage):
            def reply(inner, text, language):
                started.set()
                if not release.wait(5):
                    raise RuntimeError('Test release timeout')
                return super().reply(text, language)
        self.service.close()
        self.service = self.build(language=Slow())
        self.service.hydrate_connection(connection())
        self.service.accept_telegram_update(telegram(2))
        worker = threading.Thread(target=self.service.process_inbound)
        worker.start()
        self.assertTrue(started.wait(3))
        before = time.monotonic()
        self.service.accept_telegram_update(telegram(3, sender=10, text='您好，我来帮您处理。'))
        self.assertLess(time.monotonic() - before, 0.5)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.service.drain()
        self.assertEqual(self.outgoing(), [])
        self.assertEqual(self.service.list_conversations()[0]['mode'], 'manual')

    def test_shutdown_during_model_request_keeps_queue_pending(self):
        stop = threading.Event()
        class Stops(SimulationLanguage):
            def detect(inner, text, previous='und', hint=''):
                stop.set()
                return super().detect(text, previous, hint)
        self.service.close()
        self.service = self.build(language=Stops())
        self.service.hydrate_connection(connection())
        self.service.accept_telegram_update(telegram(2))
        self.service.process_inbound(stop_event=stop)
        self.assertEqual(self.service.queue_status(), {'pending': 1})
        self.assertEqual(self.service.list_conversations(), [])

    def test_failed_translation_can_retry_after_model_recovers(self):
        class Intermittent(SimulationLanguage):
            fail = True
            def translate(inner, text, source, target):
                if inner.fail and source == 'zh' and target == 'ru':
                    raise ChannelError('translation_unavailable')
                return super().translate(text, source, target)
        model = Intermittent()
        self.service.close()
        self.service = self.build(language=model)
        self.service.hydrate_connection(connection())
        room = self.open_room()
        self.service.accept_feishu_payload(feishu(room['room_id']))
        self.service.process_inbound()
        state = self.service.get_conversation(room['id'])
        staff = next(m for m in state['messages'] if m['author_id'] == 'ou_1')
        self.assertEqual(staff['status'], 'translation_unavailable')
        model.fail = False
        self.service.retry_translation(staff['id'], room['id'], 'ou_1')
        self.service.drain()
        self.assertEqual(self.outgoing()[-1]['text'], '员工1: Здравствуйте! Я помогу вам.')

    def test_negative_detection_and_handoff_cache_recover_on_identical_text(self):
        class Intermittent(SimulationLanguage):
            fail = True
            def detect(inner, text, previous='und', hint=''):
                return 'und' if inner.fail else super().detect(text, previous, hint)
            def reply(inner, text, language):
                return {'action': 'handoff', 'reason': 'model_unavailable'} if inner.fail else super().reply(text, language)
        model = Intermittent()
        self.service.close()
        self.service = self.build(language=model)
        self.service.hydrate_connection(connection())
        room = self.open_room()
        self.assertEqual(room['language'], 'und')
        model.fail = False
        self.service.resume(room['id'])
        self.service.accept_telegram_update(telegram(3))
        self.service.process_inbound()
        self.service.drain()
        self.assertEqual(self.service.list_conversations()[0]['language'], 'ru')
        self.assertEqual(len(self.outgoing()), 1)

    def test_delete_before_message_and_all_edited_mirrors_are_recalled(self):
        room = self.open_room()
        update = telegram(3, text='Спасибо', mid=90)
        self.service.accept_telegram_update(update)
        edit = telegram(4, text='Hello', mid=90)
        edit['edited_business_message'] = edit.pop('business_message')
        self.service.accept_telegram_update(edit)
        self.service.process_inbound()
        self.service.drain()
        self.service.accept_telegram_update({'update_id': 5, 'deleted_business_messages': {
            'business_connection_id': 'c1', 'chat': {'id': 20, 'type': 'private'}, 'message_ids': [90, 91]}})
        self.service.process_inbound()
        self.service.drain()
        self.service.accept_telegram_update(telegram(6, mid=91))
        self.service.process_inbound()
        state = self.service.get_conversation(room['id'])
        versions = [m for m in state['messages'] if m['source_id'] == '90' or m['source_id'].startswith('90:edit:')]
        self.assertEqual(len(versions), 2)
        self.assertTrue(all(m['recall_status'] == 'deleted' for m in versions))
        self.assertFalse(any(m['source_id'] == '91' for m in state['messages']))

    def test_removed_staff_is_blocked_at_send_and_admin_cannot_add_unknown_member(self):
        room = self.open_room()
        self.service.accept_feishu_payload(feishu(room['room_id']))
        self.service.process_inbound()
        self.service.default_members = ('ou_2',)
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)
        with self.assertRaises(ChannelError):
            self.service.add_member(room['id'], 'outsider')

    def test_recall_accepted_before_worker_prevents_send_even_without_delete_right(self):
        room = self.open_room()
        self.service.hydrate_connection(connection(delete=False))
        self.service.accept_feishu_payload(feishu(room['room_id']))
        callback = {'schema': '2.0', 'header': {'app_id': 'cli_test', 'event_id': 'recall-early', 'event_type': 'im.message.recalled_v1'},
                    'event': {'chat_id': room['room_id'], 'message_id': 'staff-1', 'recall_time': str(int(self.now * 1000))}}
        self.service.accept_feishu_payload(callback)
        before = len(self.outgoing())
        self.service.process_inbound(limit=1)
        self.service.drain()
        self.assertEqual(len(self.outgoing()), before)
        self.service.process_inbound(limit=1)
        self.service.drain()
        self.assertEqual(len(self.outgoing()), before)
        self.assertFalse(any(m['source_id'] == 'staff-1' for m in self.service.get_conversation(room['id'])['messages']))

    def test_customer_delete_accepted_before_worker_prevents_mirror_and_ai(self):
        self.service.accept_telegram_update(telegram(2, mid=90))
        self.service.accept_telegram_update({'update_id': 3, 'deleted_business_messages': {
            'business_connection_id': 'c1', 'chat': {'id': 20, 'type': 'private'}, 'message_ids': [90]}})
        self.service.process_inbound(limit=1)
        self.service.drain()
        self.assertEqual(self.platforms.messages, [])
        self.assertEqual(self.service.list_conversations(), [])
        self.service.process_inbound(limit=1)
        self.service.drain()
        self.assertEqual(self.platforms.messages, [])

    def test_accepted_recall_immediately_cancels_already_prepared_unsent_staff(self):
        room = self.open_room()
        self.service.hydrate_connection(connection(delete=False))
        self.service.accept_feishu_payload(feishu(room['room_id']))
        self.service.process_inbound(limit=1)
        callback = {'schema': '2.0', 'header': {'app_id': 'cli_test', 'event_id': 'recall-prepared', 'event_type': 'im.message.recalled_v1'},
                    'event': {'chat_id': room['room_id'], 'message_id': 'staff-1', 'recall_time': str(int(self.now * 1000))}}
        self.service.accept_feishu_payload(callback)
        self.service.drain()
        self.assertEqual(len(self.outgoing()), 1)
        staff = next(m for m in self.service.get_conversation(room['id'])['messages'] if m['source_id'] == 'staff-1')
        self.assertEqual(staff['recall_status'], 'cancelled')

    def test_definitely_failed_room_creation_can_be_explicitly_retried_after_fix(self):
        self.platforms.fail_next('feishu', 'create_room', status=403, body={'code': 99991672})
        self.service.accept_telegram_update(telegram(2))
        self.service.process_inbound()
        self.service.drain()
        room = self.service.list_conversations()[0]
        self.assertEqual(room['room_status'], 'failed')
        self.assertIsNone(room['room_id'])
        self.service.retry_room(room['id'])
        self.service.drain()
        current = self.service.get_conversation(room['id'])
        self.assertEqual(current['room_status'], 'ready')
        self.assertTrue(any(m['channel'] == 'feishu' and 'Здравствуйте' in m['text'] for m in self.platforms.messages))
        create_requests = [r for r in self.platforms.requests if r['operation'] == 'create_room']
        self.assertEqual(len(create_requests), 2)
        self.assertEqual(create_requests[0]['query']['uuid'], create_requests[1]['query']['uuid'])
        with self.assertRaises(ChannelError):
            self.service.retry_room(room['id'])

    def test_unknown_room_creation_is_not_retried_by_recovery_control(self):
        self.platforms.fail_next('feishu', 'create_room', status=500, body={'code': 500})
        self.service.accept_telegram_update(telegram(2))
        self.service.process_inbound()
        self.service.drain()
        room = self.service.list_conversations()[0]
        self.assertEqual(room['room_status'], 'unknown')
        with self.assertRaises(ChannelError):
            self.service.retry_room(room['id'])
        self.service.drain()
        self.assertEqual(len([r for r in self.platforms.requests if r['operation'] == 'create_room']), 1)

    def test_known_message_recall_bypasses_unrelated_unknown_send(self):
        room = self.open_room()
        self.service.accept_feishu_payload(feishu(room['room_id'], mid='staff-known'))
        self.service.process_inbound()
        self.service.drain()
        known = self.outgoing()[-1]
        self.service.accept_feishu_payload(feishu(room['room_id'], mid='staff-uncertain'))
        self.service.process_inbound()
        self.platforms.fail_next('telegram', 'send', status=503, body={'error': 'uncertain'})
        self.service.drain()
        state = self.service.get_conversation(room['id'])
        message = next(m for m in state['messages'] if m['source_id'] == 'staff-known')
        self.assertTrue(any(o['status'] == 'unknown' for o in state['outbox']))
        self.service.recall(message['id'], room['id'], 'ou_1')
        self.service.drain()
        self.assertTrue(known['deleted'])
        self.assertTrue(any(o['status'] == 'unknown' for o in self.service.get_conversation(room['id'])['outbox']))
