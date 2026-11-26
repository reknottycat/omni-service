#!/usr/bin/env python3
"""Exercise personal-account HTTP clients and local models with synthetic data."""

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from omni_service.config import load_config
from omni_service.live_clients import TelegramClient, FeishuClient
from omni_service.model_provider import LocalModels
from omni_service.personal_simulation import PersonalPlatforms, connection_update, customer_update, staff_payload, recall_payload
from omni_service.runtime import BridgeRuntime


def run():
    knowledge = ROOT / 'fixtures/knowledge/logistics-zh-ru.md'
    with tempfile.TemporaryDirectory(prefix='omni-personal-demo-') as temp, PersonalPlatforms(knowledge) as platforms:
        value = json.loads((ROOT / 'config/personal.example.json').read_text())
        value['data_dir'] = str(Path(temp) / 'data')
        value['knowledge_path'] = str(knowledge)
        value['telegram'].update(expected_owner_id=10, allowed_chat_ids=[20], poll_timeout=1)
        value['feishu'].update(app_id='cli_test', members=[
            {'open_id': f'ou_staff_{i}', 'name': f'员工 {i}'} for i in range(1, 8)])
        value['translation'].update(base_url=platforms.url + '/v1', model='synthetic-translation')
        value['llm'].update(enabled=True, base_url=platforms.url + '/v1', model='synthetic-ai')
        path = Path(temp) / 'personal.json'
        path.write_text(json.dumps(value))
        config = load_config(path)
        def runtime():
            result = BridgeRuntime(config,
                TelegramClient('900:synthetic-test-token', base_url=platforms.url, timeout=2, allow_loopback=True),
                FeishuClient('cli_test', 'synthetic-test-secret', base_url=platforms.url, timeout=2, allow_loopback=True),
                LocalModels(config.translation, config.llm, knowledge, environ={}))
            result.preflight()
            return result
        bridge = runtime()
        try:
            platforms.push(connection_update())
            platforms.push(customer_update(2))
            bridge.poll_once()
            bridge.work_once()
            conversation, = bridge.service.list_conversations()
            assert conversation['room_id'] and conversation['mode'] == 'auto'
            assert len(platforms.telegram_sent) == 1
            staff_events = [staff_payload(conversation['room_id'], i, staff=f'ou_staff_{i}') for i in range(1, 8)]
            for event in staff_events:
                bridge.service.accept_feishu_payload(event)
            queued_before_restart = bridge.service.queue_status()['pending']
            bridge.close()
            bridge = runtime()
            for _ in range(7):
                bridge.work_once()
            assert queued_before_restart == 7 and len(platforms.telegram_sent) == 8
            assert len(platforms.rooms) == 1
            for i, message in enumerate(platforms.telegram_sent[1:], 1):
                assert message['text'] == f'员工 {i}: Пожалуйста, сообщите номер заказа.'
                assert message['chat_id'] == 20
                assert message['business_connection_id'] == 'synthetic-connection'
            bridge.service.accept_feishu_payload(recall_payload(conversation['room_id'], 1))
            bridge.work_once()
            assert platforms.telegram_deleted[0]['message_ids'] == [platforms.telegram_sent[1]['message_id']]
            bridge.service.accept_feishu_payload(staff_events[1])
            bridge.work_once()
            assert len(platforms.telegram_sent) == 8
            platforms.push(customer_update(3, text='SYNTHETIC_PRIVATE_CHAT', chat=99))
            bridge.poll_once()
            bridge.work_once()
            with bridge.service.store.lock:
                dump = '\n'.join(bridge.service.store.db.iterdump())
            assert 'SYNTHETIC_PRIVATE_CHAT' not in dump
            bridge.service.resume(conversation['id'])
            platforms.push(customer_update(4, text='Какие документы нужны для отправки груза?'))
            bridge.poll_once()
            bridge.work_once()
            assert 'коммерческий инвойс' in platforms.telegram_sent[-1]['text']
            result = {
                'scope': 'P1-personal-account-loopback', 'passed': True,
                'mode': 'simulation', 'external_network_calls': 0,
                'translation_provider': 'real HTTP client / synthetic JSON model server',
                'sdk': 'Feishu event payload injected; actual SDK verified separately',
                'checks': {'original_account_reply': True, 'seven_staff_replies': True,
                           'translated_to_customer_language': True, 'durable_restart': True,
                           'single_feishu_room': True, 'recall_confirmed': True,
                           'duplicate_not_resent': True, 'unapproved_chat_not_stored': True,
                           'logistics_knowledge_answer': True},
                'observed': {'telegram_sent': len(platforms.telegram_sent),
                             'telegram_deleted': len(platforms.telegram_deleted),
                             'feishu_rooms': len(platforms.rooms),
                             'feishu_messages': len(platforms.feishu_sent),
                             'model_http_requests': len(platforms.model_requests),
                             'pending_staff_survived_restart': queued_before_restart},
                'sample': {'customer': 'Здравствуйте',
                           'employee_chinese': '请提供订单号。',
                           'customer_received': platforms.telegram_sent[1]['text'],
                           'knowledge_reply': platforms.telegram_sent[-1]['text']},
                'limits': ['No personal account login or real model quality validation',
                           'WhatsApp/MAX private account routes are not connected',
                           'Media binary relay is not part of this text acceptance']}
        finally:
            bridge.close()
        return result


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
