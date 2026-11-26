from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from omni_service.models import Attachment, ChannelError, InboundEvent, Request
from omni_service.service import Service
from omni_service.simulation import FakePlatforms
from omni_service.transport import HttpTransport


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.platforms = FakePlatforms().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.platforms.__exit__(None, None, None)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'state.db'
        self.now = 2_000_000_000.0
        self.transport = HttpTransport(self.platforms.urls)
        self.service = Service(self.path, self.transport, clock=lambda: self.now)
        with self.platforms.lock:
            self.platforms.messages.clear()
            self.platforms.requests.clear()
            self.platforms.failures.clear()

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def customer(self, channel='telegram', text='Здравствуйте', chat='101', event='1', **changes):
        return InboundEvent(channel, 'official-1', event, chat, 'customer-' + chat, event,
                            text=text, sender_name='Customer', timestamp=self.now,
                            recipient_type='phone' if channel == 'whatsapp' else 'chat', **changes)

    def open_room(self, channel='telegram', chat='101'):
        cid = self.service.ingest(self.customer(channel, chat=chat, event='open-' + chat))['conversation_id']
        self.service.handoff(cid)
        self.service.drain()
        return self.service.get_conversation(cid)

    def staff(self, room, text='您好，我来帮您处理。', employee='employee-1', event='staff-1'):
        return InboundEvent('feishu', 'team-1', event, room['room_id'], employee, event,
                            text=text, sender_name=employee, timestamp=self.now)

    def message(self, cid, mid):
        return next(m for m in self.service.get_conversation(cid)['messages'] if m['id'] == mid)

    def test_three_channels_automatically_reply_in_detected_language(self):
        for channel in ('telegram', 'max', 'whatsapp'):
            result = self.service.ingest(self.customer(channel))
            self.service.drain()
            state = self.service.get_conversation(result['conversation_id'])
            self.assertEqual(state['language'], 'ru')
            self.assertIsNone(state['room_id'])
            outgoing = [m for m in self.platforms.messages if m['channel'] == channel]
            self.assertEqual(len(outgoing), 1)
            self.assertEqual(outgoing[0]['text'], 'Здравствуйте! Чем я могу вам помочь?')

    def test_seven_employees_share_one_room_and_default_messages_all_forward(self):
        room = self.open_room()
        for number in range(1, 8):
            self.service.ingest(self.staff(room, employee=f'employee-{number}', event=f'staff-{number}'))
        self.service.drain()
        outgoing = [m for m in self.platforms.messages if m['channel'] == 'telegram']
        self.assertEqual(len(outgoing), 7)
        for number, message in enumerate(outgoing, 1):
            self.assertEqual(message['target'], '101')
            self.assertEqual(message['text'], f'employee-{number}: Здравствуйте! Я помогу вам.')
        self.assertEqual(len(self.service.get_conversation(room['id'])['members']), 7)

    def test_manual_takeover_cancels_ai_and_resume_reenables_it(self):
        room = self.open_room()
        self.service.resume(room['id'])
        self.service.ingest(self.customer(text='谢谢', event='2'))
        self.service.ingest(self.staff(room))
        self.service.drain()
        state = self.service.get_conversation(room['id'])
        self.assertEqual(state['mode'], 'manual')
        self.assertTrue(any(m['author_id'] == 'AI' and m['status'] == 'cancelled' for m in state['messages']))
        self.service.resume(room['id'])
        self.service.ingest(self.customer(text='Hello', event='3'))
        self.service.drain()
        self.assertEqual(self.service.get_conversation(room['id'])['mode'], 'auto')

    def test_ai_history_and_resumed_answers_appear_in_room_with_separate_delivery_ids(self):
        cid = self.service.ingest(self.customer())['conversation_id']
        self.service.drain()
        ai = next(m for m in self.service.get_conversation(cid)['messages'] if m['author_id'] == 'AI')
        customer_message_id = ai['external_id']
        self.service.handoff(cid)
        self.service.drain()
        self.assertEqual(self.message(cid, ai['id'])['external_id'], customer_message_id)
        mirrored = [m for m in self.platforms.messages if m['channel'] == 'feishu' and m['text'].startswith('AI →')]
        self.assertEqual(len(mirrored), 1)
        self.assertIn('Здравствуйте', mirrored[0]['text'])
        self.service.recall(ai['id'], cid, 'employee-1')
        self.service.drain()
        self.assertTrue(mirrored[0]['deleted'])
        self.assertTrue(next(m for m in self.platforms.messages if m['channel'] == 'telegram')['deleted'])
        self.service.resume(cid)
        self.now += 1
        self.service.ingest(self.customer(text='Hello', event='new'))
        self.service.drain()
        mirrored = [m for m in self.platforms.messages if m['channel'] == 'feishu' and m['text'].startswith('AI →')]
        self.assertEqual(len(mirrored), 2)
        self.assertIn('Hello', mirrored[-1]['text'])

    def test_one_account_many_conversations_and_accounts_are_isolated(self):
        with ThreadPoolExecutor(max_workers=7) as workers:
            results = list(workers.map(lambda n: self.service.ingest(self.customer(chat=str(n), event=str(n))), range(1, 101)))
        self.assertEqual(len({r['conversation_id'] for r in results}), 100)
        other_account = replace(self.customer(chat='1'), account_id='official-2')
        self.service.ingest(other_account)
        self.assertEqual(len(self.service.list_conversations()), 101)
        self.service.drain()
        self.assertEqual(len([m for m in self.platforms.messages if m['channel'] == 'telegram']), 101)

    def test_duplicate_and_conflicting_event_have_observable_distinct_results(self):
        event = self.customer()
        first = self.service.ingest(event)
        self.assertEqual(self.service.ingest(event)['status'], 'duplicate')
        with self.assertRaises(ChannelError) as error:
            self.service.ingest(replace(event, text='Hello'))
        self.assertEqual(error.exception.code, 'event_collision')
        self.service.drain()
        self.assertEqual(len(self.platforms.messages), 1)
        self.assertEqual(len(self.service.get_conversation(first['conversation_id'])['messages']), 2)

    def test_restart_restores_mapping_language_members_and_pending_delivery(self):
        room = self.open_room()
        reply = self.service.ingest(self.staff(room))
        self.service.close()
        self.service = Service(self.path, self.transport, clock=lambda: self.now)
        self.service.drain()
        state = self.service.get_conversation(room['id'])
        self.assertEqual(state['room_id'], room['room_id'])
        self.assertEqual(state['language'], 'ru')
        self.assertEqual(self.message(room['id'], reply['message_id'])['status'], 'sent')
        self.assertEqual(self.service.ingest(self.staff(room))['status'], 'duplicate')

    def test_stale_and_short_messages_do_not_override_latest_language(self):
        cid = self.service.ingest(self.customer())['conversation_id']
        self.service.ingest(replace(self.customer(text='Hello', event='old'), timestamp=self.now - 100))
        self.assertEqual(self.service.get_conversation(cid)['language'], 'ru')
        self.now += 1
        self.service.ingest(self.customer(text='OK', event='short', language_hint='en'))
        self.assertEqual(self.service.get_conversation(cid)['language'], 'ru')
        self.now += 1
        self.service.ingest(self.customer(text='Hello', event='new'))
        self.assertEqual(self.service.get_conversation(cid)['language'], 'en')

    def test_unknown_employee_translation_is_visible_failure_and_is_not_sent(self):
        room = self.open_room()
        result = self.service.ingest(self.staff(room, text='一个完全没有收录的任意短语'))
        self.assertEqual(result['status'], 'translation_unavailable')
        self.service.drain()
        self.assertEqual([m for m in self.platforms.messages if m['channel'] == 'telegram'], [])
        self.assertTrue(any('消息未发送' in m['text'] for m in self.platforms.messages if m['channel'] == 'feishu'))

    def test_queued_message_can_be_cancelled_on_all_channels(self):
        for channel in ('telegram', 'max', 'whatsapp'):
            room = self.open_room(channel)
            result = self.service.ingest(self.staff(room, event=channel))
            recalled = self.service.recall(result['message_id'], room['id'], 'employee-1')
            self.assertEqual(recalled['status'], 'cancelled')
            self.service.drain()
            self.assertFalse(any(m['channel'] == channel for m in self.platforms.messages))

    def test_sent_recall_supported_and_whatsapp_unsupported(self):
        for channel in ('telegram', 'max', 'whatsapp'):
            room = self.open_room(channel)
            result = self.service.ingest(self.staff(room, event=channel))
            self.service.drain()
            recalled = self.service.recall(result['message_id'], room['id'], 'employee-1')
            self.assertEqual(recalled['status'], 'unsupported' if channel == 'whatsapp' else 'pending')
            self.service.drain()
            message = self.message(room['id'], result['message_id'])
            self.assertEqual(message['status'], 'sent' if channel == 'whatsapp' else 'deleted')
            self.assertEqual(message['recall_status'], 'unsupported' if channel == 'whatsapp' else 'deleted')

    def test_max_http_200_delete_failure_does_not_falsify_message_status(self):
        room = self.open_room('max')
        result = self.service.ingest(self.staff(room))
        self.service.drain()
        self.platforms.fail_next('max', 'delete', body={'success': False, 'code': 'forbidden', 'message': 'denied'})
        self.service.recall(result['message_id'], room['id'], 'employee-1')
        self.service.drain()
        message = self.message(room['id'], result['message_id'])
        self.assertEqual((message['status'], message['recall_status']), ('sent', 'failed'))
        self.assertTrue(any('撤回未确认成功' in m['text'] for m in self.platforms.messages if m['channel'] == 'feishu'))

    def test_customer_delete_during_mirror_send_deletes_the_eventual_mirror(self):
        event = self.customer('max')
        cid = self.service.ingest(event)['conversation_id']
        self.service.handoff(cid)
        transport = self.transport
        service = self.service
        fired = []

        class DeleteDuringSend:
            def send(inner_self, channel, request):
                if (channel == 'feishu' and '/im/v1/messages?' in request.path
                        and 'Customer (max):' in request.body['content'] and not fired):
                    fired.append(True)
                    service.ingest(replace(event, event_id='delete-customer', event_type='delete', text=''))
                return transport.send(channel, request)

        self.service.transport = DeleteDuringSend()
        self.service.drain()
        state = self.service.get_conversation(cid)
        inbound = next(m for m in state['messages'] if m['direction'] == 'inbound')
        self.assertEqual((inbound['status'], inbound['recall_status']), ('deleted', 'deleted'))
        mirrors = [m for m in self.platforms.messages if m['channel'] == 'feishu'
                   and m['text'].startswith('Customer (max):')]
        self.assertEqual(len(mirrors), 1)
        self.assertTrue(mirrors[0]['deleted'])

    def test_customer_delete_before_message_survives_restart_and_is_message_scoped(self):
        event = self.customer('max', text='Hello', event='message-x')
        deletion = replace(event, event_id='delete-x', event_type='delete', text='', timestamp=self.now + 1)
        self.assertEqual(self.service.ingest(deletion)['status'], 'unknown_message')
        self.service.close()
        self.service = Service(self.path, self.transport, clock=lambda: self.now)
        self.assertEqual(self.service.ingest(event)['status'], 'suppressed_deleted')
        self.assertEqual(self.service.ingest(replace(event, event_id='edit-x', event_type='edit'))['status'], 'suppressed_deleted')
        self.service.drain()
        self.assertEqual(self.service.list_conversations(), [])
        self.assertEqual(self.platforms.messages, [])
        unrelated = (replace(event, event_id='next-message', message_id='next-message'),
                     replace(event, event_id='other-chat', chat_id='202'),
                     replace(event, event_id='other-account', account_id='official-2'),
                     replace(event, event_id='other-channel', channel='telegram'))
        for valid in unrelated:
            self.assertEqual(self.service.ingest(valid)['status'], 'accepted')
        self.service.drain()
        self.assertEqual(len(self.platforms.messages), 4)

    def test_staff_recall_before_receive_survives_restart_without_forwarding(self):
        room = self.open_room()
        event = self.staff(room, event='staff-late')
        recall = replace(event, event_id='recall-first', event_type='delete', text='', user_id='')
        self.assertEqual(self.service.ingest(recall)['status'], 'unknown_message')
        self.service.close()
        self.service = Service(self.path, self.transport, clock=lambda: self.now)
        self.assertEqual(self.service.ingest(event)['status'], 'suppressed_deleted')
        self.assertEqual(self.service.ingest(replace(event, event_id='staff-edited', event_type='edit'))['status'], 'suppressed_deleted')
        self.service.drain()
        self.assertFalse(any(m['channel'] == 'telegram' for m in self.platforms.messages))
        valid = self.staff(room, event='staff-new')
        self.assertEqual(self.service.ingest(valid)['status'], 'queued')
        self.service.drain()
        self.assertEqual(len([m for m in self.platforms.messages if m['channel'] == 'telegram']), 1)

    def test_staff_recall_during_delivery_waits_for_platform_id_then_deletes(self):
        room = self.open_room()
        result = self.service.ingest(self.staff(room))
        transport = self.transport
        service = self.service
        fired = []

        class RecallDuringSend:
            def send(inner_self, channel, request):
                if channel == 'telegram' and request.path.endswith('/sendMessage') and not fired:
                    fired.append(True)
                    service.recall(result['message_id'], room['id'], 'employee-1')
                return transport.send(channel, request)

        self.service.transport = RecallDuringSend()
        self.service.drain()
        self.assertEqual(self.message(room['id'], result['message_id'])['status'], 'deleted')
        self.assertTrue(next(m for m in self.platforms.messages if m['channel'] == 'telegram')['deleted'])

    def test_stale_message_is_recorded_without_outdated_language_auto_reply(self):
        cid = self.service.ingest(self.customer())['conversation_id']
        self.service.ingest(replace(self.customer(text='Hello', event='old'), timestamp=self.now - 100))
        self.service.drain()
        self.assertEqual(self.service.get_conversation(cid)['language'], 'ru')
        self.assertEqual(len(self.platforms.messages), 1)
        self.assertEqual(self.platforms.messages[0]['text'], 'Здравствуйте! Чем я могу вам помочь?')

    def test_recall_callback_recovers_original_author_and_cross_room_ids_are_rejected(self):
        room = self.open_room()
        other = self.open_room(chat='other')
        result = self.service.ingest(self.staff(room))
        self.service.drain()
        with self.assertRaises(ChannelError):
            self.service.recall(result['message_id'], other['id'], 'employee-1')
        callback = replace(self.staff(room), event_id='recall-1', event_type='delete', user_id='', text='')
        self.assertEqual(self.service.ingest(callback)['status'], 'pending')
        self.service.drain()
        self.assertEqual(self.message(room['id'], result['message_id'])['status'], 'deleted')

    def test_nonmember_cannot_reply_or_recall(self):
        room = self.open_room()
        with self.assertRaises(ChannelError) as error:
            self.service.ingest(self.staff(room, employee='outsider'))
        self.assertEqual(error.exception.code, 'forbidden')
        self.assertEqual(self.service.get_conversation(room['id'])['mode'], 'manual')

    def test_rate_limit_respects_retry_after_and_stops_after_three_attempts(self):
        result = self.service.ingest(self.customer())
        for _ in range(3):
            self.platforms.fail_next('telegram', 'send', status=429,
                                     body={'ok': False, 'parameters': {'retry_after': 10}})
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 1)
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 1)
        self.now += 10
        self.service.drain()
        self.now += 10
        self.service.drain()
        self.now += 100
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 3)
        state = self.service.get_conversation(result['conversation_id'])
        self.assertEqual(state['outbox'][0]['status'], 'failed')

    def test_authorization_denial_is_terminal(self):
        result = self.service.ingest(self.customer())
        self.platforms.fail_next('telegram', 'send', status=403, body={'ok': False})
        self.service.drain()
        self.now += 100
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 1)
        self.assertEqual(self.service.get_conversation(result['conversation_id'])['messages'][-1]['status'], 'failed')

    def test_unconfirmed_delivery_is_not_blindly_retried(self):
        cid = self.service.ingest(self.customer())['conversation_id']
        self.platforms.fail_next('telegram', 'send', status=500, body={'error': 'uncertain'})
        self.service.drain()
        self.now += 60
        self.service.ingest(self.customer(text='谢谢', event='2'))
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 1)
        state = self.service.get_conversation(cid)
        self.assertTrue(any(m['status'] == 'delivery_unknown' for m in state['messages']))

    def test_inflight_delivery_on_restart_is_unknown_not_resent(self):
        cid = self.service.ingest(self.customer())['conversation_id']
        with self.service.store.transaction() as db:
            db.execute("UPDATE outbox SET status='inflight'")
        self.service.close()
        self.service = Service(self.path, self.transport, clock=lambda: self.now)
        self.service.drain()
        state = self.service.get_conversation(cid)
        self.assertEqual(state['outbox'][0]['status'], 'unknown')
        self.assertEqual(state['messages'][-1]['status'], 'delivery_unknown')
        self.assertEqual(self.platforms.requests, [])

    def test_whatsapp_window_checked_both_when_queued_and_when_sent(self):
        room = self.open_room('whatsapp')
        result = self.service.ingest(self.staff(room))
        self.now += 86400
        self.service.drain()
        self.assertEqual(self.message(room['id'], result['message_id'])['status'], 'blocked_window')
        later = self.service.ingest(self.staff(room, event='staff-2'))
        self.assertEqual(later['status'], 'blocked_window')
        self.assertFalse(any(m['channel'] == 'whatsapp' for m in self.platforms.messages))

    def test_bot_echo_does_not_create_conversation_or_ai_message(self):
        self.assertEqual(self.service.ingest(replace(self.customer(), is_bot=True))['status'], 'ignored_bot')
        self.assertEqual(self.service.list_conversations(), [])

    def test_media_source_token_is_kept_pending_and_never_reused_for_destination(self):
        room = self.open_room()
        event = replace(self.staff(room), text='', attachments=(Attachment('image', 'feishu-source-key'),))
        result = self.service.ingest(event)
        self.assertEqual(result['status'], 'media_pending')
        self.service.drain()
        self.assertFalse(any(m['channel'] == 'telegram' for m in self.platforms.messages))
        message = self.message(room['id'], result['message_id'])
        self.assertEqual(message['attachments'][0]['remote_id'], 'feishu-source-key')

    def test_loopback_transport_refuses_external_hosts_redirects_and_bad_response_shape(self):
        for base in ('https://127.0.0.1', 'http://example.com', 'http://127.0.0.1.example.com', 'http://user@127.0.0.1'):
            with self.assertRaises(ChannelError):
                HttpTransport({'telegram': base})
        self.platforms.fail_next('telegram', 'send', status=302, body={'moved': True}, headers={'Location': 'https://example.com'})
        with self.assertRaises(ChannelError) as error:
            self.transport.send('telegram', Request('POST', '/botsimulation-token/sendMessage', {'chat_id': '1', 'text': 'test'}))
        self.assertEqual(error.exception.code, 'redirect_rejected')


if __name__ == '__main__':
    unittest.main()
