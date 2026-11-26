import copy
from dataclasses import replace
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from omni_service.company_accounts import (CompanyConfigurationError, configuration_conflicts,
                                           list_companies, serve_companies, validate_company)
from omni_service.intake import Intake
from omni_service.personal_runner import PersonalBridge
from test_personal_managed import ManagedFeishu, ManagedTelegram
from test_personal_runner import staff


class CompanyRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary) / 'omni-service'
        self.root.mkdir(mode=0o700)

    def company(self, name, index):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        config = {
            'mode': 'personal-managed',
            'telegram': {'bot_username': f'Company{index}Bot', 'connection_id': f'connection-{index}',
                         'expected_owner_id': 100 + index},
            'feishu': {'app_id': f'cli_company{index}'},
            'management': {'enabled': True, 'port': 8080 + index,
                           'callback_url': f'https://company{index}.example/admin/auth/callback'},
        }
        self.write(name, config)
        return config

    def write(self, name, value):
        target = self.root / name / 'personal.json'
        target.write_text(json.dumps(value))
        target.chmod(0o600)

    def test_three_companies_list_without_credentials_and_detect_each_collision(self):
        first = self.company('company-a', 1)
        self.company('company-b', 2)
        third = self.company('company-c', 3)
        with patch('omni_service.company_accounts.subprocess.run') as command:
            command.return_value.stdout = 'active\n'
            result = list_companies(self.root)
        self.assertEqual([item['company'] for item in result], ['company-a', 'company-b', 'company-c'])
        self.assertEqual([item['service'] for item in result], ['active'] * 3)
        self.assertNotIn('connection-', json.dumps(result))
        validate_company(self.root, 'company-c')
        for field, section, key in [('telegram_bot', 'telegram', 'bot_username'),
                                    ('connection', 'telegram', 'connection_id'),
                                    ('owner', 'telegram', 'expected_owner_id'),
                                    ('feishu_app', 'feishu', 'app_id'),
                                    ('management_port', 'management', 'port'),
                                    ('management_callback', 'management', 'callback_url')]:
            modified = copy.deepcopy(third)
            modified[section][key] = first[section][key]
            self.write('company-c', modified)
            self.assertIn(field, [conflict['field'] for conflict in configuration_conflicts(self.root)])
            with self.assertRaises(CompanyConfigurationError):
                validate_company(self.root, 'company-c')
        self.write('company-c', third)

    def test_disabled_and_unsafe_files(self):
        self.company('company-a', 1)
        target = self.root / 'company-b'
        target.mkdir(mode=0o700)
        self.write('company-b', {'mode': 'disabled'})
        validate_company(self.root, 'company-a')
        (target / 'personal.json').chmod(0o644)
        with self.assertRaises(CompanyConfigurationError):
            validate_company(self.root, 'company-a')

    def test_dashboard_requires_secret_url_and_loopback_host(self):
        self.company('company-a', 1)
        from omni_service import company_accounts
        actual_server = company_accounts.ThreadingHTTPServer
        ready = threading.Event()
        holder = {}

        class StoppableServer(actual_server):
            def __init__(self, *args):
                super().__init__(*args)
                holder['server'] = self
                ready.set()

        with patch.object(company_accounts, 'ThreadingHTTPServer', StoppableServer), \
                patch.object(company_accounts.secrets, 'token_urlsafe', return_value='synthetic-token'), \
                patch.object(company_accounts.subprocess, 'run') as status, \
                patch('builtins.print'):
            status.return_value.stdout = 'active\n'
            # An ephemeral port is fixed for the Host header passed to the handler.
            import socket
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            thread = threading.Thread(target=serve_companies, args=(self.root, port))
            thread.start()
            self.assertTrue(ready.wait(3))
            try:
                connection = HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('GET', '/synthetic-token', headers={'Host': f'127.0.0.1:{port}'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn('company-a', response.read().decode())
                connection.request('GET', '/synthetic-token', headers={'Host': 'attacker.example'})
                self.assertEqual(connection.getresponse().status, 404)
                connection.close()
            finally:
                holder['server'].shutdown()
                thread.join(3)


class SeparateCompaniesTest(unittest.TestCase):
    def test_same_customer_in_three_companies_stays_in_its_own_database_and_room(self):
        with tempfile.TemporaryDirectory() as directory:
            bridges, clients, rooms, apis = [], [], [], []
            try:
                for index in range(3):
                    api, feishu = ManagedTelegram(), ManagedFeishu()
                    feishu.app_id = f'cli_company{index}'
                    feishu.bot_app_ids = {feishu.app_id}
                    feishu.create_room = lambda name, key, *, index=index, feishu=feishu: (
                        feishu.groups.setdefault(f'oc_company{index}', {'users': feishu.staff_ids,
                                                                      'bots': feishu.bot_app_ids})
                        and f'oc_company{index}')
                    bridge = PersonalBridge(Path(directory) / f'{index}.sqlite', api, feishu,
                        owner=10, peer_ids=[20], connection_id='c1', bot_id=88, clock=lambda: 1000,
                        intake=Intake(lambda _text, _fields: {}, simulated=True),
                        intake_enabled=True, approval_intake=True, company_name=f'Company {index}')
                    bridges.append(bridge)
                    apis.append(api)
                    api.update(1, 'Здравствуйте', chat=20, sender=20, first_name=f'Customer {index}')
                    for event in bridge.telegram.poll(timeout=0):
                        bridge.receive_telegram(event)
                    bridge.telegram.ack()
                    bridge.drain()
                    client = bridge.get_conversation(bridge.list_conversations()[0]['id'])
                    clients.append(client)
                    rooms.append(client['room_id'])
                    self.assertEqual(len(api.sent), 1)
                    self.assertTrue(any(f'Customer {index}' in row[1] for row in feishu.sent))
                self.assertEqual(len(set(rooms)), 3)
                for bridge, room in zip(bridges, rooms):
                    self.assertTrue(bridge.room_allowed(room))
                    self.assertTrue(all(not bridge.room_allowed(other) for other in rooms if other != room))
                for index, (bridge, room) in enumerate(zip(bridges, rooms)):
                    reply = replace(staff('您好，我来帮您处理。', mid=f'om_reply_{index}', room=room),
                                    account_id=f'cli_company{index}')
                    self.assertEqual(bridge.receive_feishu(reply)['status'], 'queued')
                    bridge.drain()
                for api in apis:
                    self.assertEqual(len(api.sent), 2)
                    self.assertEqual(api.sent[-1]['chat_id'], 20)
                    self.assertEqual(api.sent[-1]['business_connection_id'], 'c1')
            finally:
                for bridge in bridges:
                    bridge.close()
