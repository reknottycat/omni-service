"""Behavioral regressions for the three externally reported P0 review findings."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from omni_service.models import ChannelError, InboundEvent
from omni_service.service import Service
from omni_service.simulation import FakePlatforms
from omni_service.transport import HttpTransport


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platforms = self.enterContext(FakePlatforms())
        self.service = Service(Path(self.temp.name) / 'state.db', HttpTransport(self.platforms.urls),
                               clock=lambda: 2_000_000_000, feishu_account_id='selected-team')
        self.addCleanup(self.service.close)

    def incoming(self, channel='telegram', event='source'):
        return InboundEvent(channel, 'original-account', event, '20', '20', event,
                            text='Здравствуйте', timestamp=2_000_000_000)

    def room(self, channel='telegram'):
        cid = self.service.ingest(self.incoming(channel))['conversation_id']
        self.service.handoff(cid)
        self.service.drain()
        return self.service.get_conversation(cid)

    def test_staff_identity_from_different_feishu_account_cannot_use_same_room(self):
        room = self.room()
        forged = InboundEvent('feishu', 'other-team', 'wrong-app', room['room_id'], 'employee-1', 'wrong-app',
                              text='您好，我来帮您处理。')
        with self.assertRaises(ChannelError) as error:
            self.service.ingest(forged)
        self.assertEqual(error.exception.code, 'forbidden')
        self.service.drain()
        self.assertFalse(any(m['channel'] == 'telegram' for m in self.platforms.messages))
        valid = replace(forged, account_id='selected-team', event_id='valid', message_id='valid')
        self.service.ingest(valid)
        self.service.drain()
        self.assertEqual(len([m for m in self.platforms.messages if m['channel'] == 'telegram']), 1)
        with self.assertRaises(ChannelError):
            self.service.ingest(replace(valid, account_id='other-team', event_id='bad-recall', event_type='delete', text=''))

    def test_unknown_customer_mirror_is_not_claimed_cancelled_when_customer_deletes(self):
        incoming = self.incoming('max')
        cid = self.service.ingest(incoming)['conversation_id']
        self.service.handoff(cid)
        self.platforms.fail_next('feishu', 'send', status=500, body={'error': 'uncertain'})
        self.service.drain()
        self.service.ingest(replace(incoming, event_id='deleted', event_type='delete', text=''))
        state = self.service.get_conversation(cid)
        source = next(m for m in state['messages'] if m['direction'] == 'inbound')
        self.assertEqual(source['status'], 'deleted')
        self.assertEqual(source['recall_status'], 'delivery_unknown')
        self.assertTrue(any(o['status'] == 'unknown' for o in state['outbox']))

    def test_recall_of_preempted_ai_returns_known_cancelled_state(self):
        cid = self.service.ingest(self.incoming())['conversation_id']
        self.service.handoff(cid)
        ai = next(m for m in self.service.get_conversation(cid)['messages'] if m['author_id'] == 'AI')
        self.assertEqual(ai['status'], 'cancelled')
        result = self.service.recall(ai['id'], cid, 'employee-1')
        self.assertEqual(result['status'], 'cancelled')
        self.service.drain()
        self.assertFalse(any(m['channel'] == 'telegram' for m in self.platforms.messages))

    def test_operator_confirmed_no_effect_unblocks_later_messages_without_resending_unknown(self):
        cid = self.service.ingest(self.incoming())['conversation_id']
        self.platforms.fail_next('telegram', 'send', status=500, body={'error': 'uncertain'})
        self.service.drain()
        unknown = next(o for o in self.service.get_conversation(cid)['outbox'] if o['status'] == 'unknown')
        self.service.ingest(replace(self.incoming(), event_id='second', message_id='second', text='Спасибо'))
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 1)
        self.service.resolve_delivery(unknown['id'], delivered=False, evidence='Test platform checked: no message')
        self.service.drain()
        self.assertEqual(len(self.platforms.requests), 2)
        self.assertEqual(len(self.platforms.messages), 1)
        self.assertIn('Пожалуйста', self.platforms.messages[0]['text'])

    def test_drain_limit_and_stop_do_not_leave_unstarted_requests_inflight(self):
        for n in range(3):
            self.service.ingest(replace(self.incoming(event=str(n)), chat_id=str(n + 20)))
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.service.drain(stop_event=stop), [])
        self.assertEqual(len(self.platforms.requests), 0)
        self.service.drain(limit=1)
        self.assertEqual(len(self.platforms.requests), 1)
        pending = [o for c in self.service.list_conversations() for o in self.service.get_conversation(c['id'])['outbox']]
        self.assertEqual(sum(o['status'] == 'pending' for o in pending), 2)
        self.assertFalse(any(o['status'] == 'inflight' for o in pending))

    def test_unknown_deleted_mirror_survives_restart_and_deletes_after_receipt_resolution(self):
        incoming = self.incoming('max')
        cid = self.service.ingest(incoming)['conversation_id']
        self.service.handoff(cid)
        transport = self.service.transport
        class LoseMirrorReceipt:
            def send(inner, channel, request):
                result = transport.send(channel, request)
                if channel == 'feishu' and '/messages?' in request.path:
                    return {}
                return result
        self.service.transport = LoseMirrorReceipt()
        self.service.drain()
        actual = next(m for m in self.platforms.messages if m['channel'] == 'feishu')
        self.service.ingest(replace(incoming, event_id='deleted', event_type='delete', text=''))
        unknown = next(o for o in self.service.get_conversation(cid)['outbox'] if o['status'] == 'unknown')
        self.service.close()
        self.service = Service(Path(self.temp.name) / 'state.db', transport,
                               clock=lambda: 2_000_000_000, feishu_account_id='selected-team')
        self.addCleanup(self.service.close)
        source = next(m for m in self.service.get_conversation(cid)['messages'] if m['direction'] == 'inbound')
        self.assertEqual((source['status'], source['recall_status']), ('deleted', 'delivery_unknown'))
        self.service.resolve_delivery(unknown['id'], delivered=True, external_id=actual['message_id'],
                                      evidence='Checked source mirror in test room')
        self.service.drain()
        self.assertTrue(actual['deleted'])
        source = next(m for m in self.service.get_conversation(cid)['messages'] if m['direction'] == 'inbound')
        self.assertEqual((source['status'], source['recall_status']), ('deleted', 'deleted'))
