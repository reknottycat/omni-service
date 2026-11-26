import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from omni_service.config import ConfigError, load_config, load_env_file
from omni_service.runtime import backup_database, instance_lock


ROOT = Path(__file__).resolve().parents[1]


def configuration(directory):
    value = json.loads((ROOT / 'config/personal.example.json').read_text())
    value['data_dir'] = str(Path(directory) / 'data')
    value['knowledge_path'] = str(ROOT / 'fixtures/knowledge/logistics-zh-ru.md')
    value['telegram'].update(expected_owner_id=10, allowed_chat_ids=[20])
    value['feishu'].update(app_id='cli_test', members=[{'open_id': f'ou_staff_{i}', 'name': f'员工 {i}'} for i in range(1, 8)])
    value['translation'].update(model='test-translation')
    value['admin']['port'] = 0
    return value


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'personal.json'
        self.raw = configuration(self.temp)

    def config(self):
        self.path.write_text(json.dumps(self.raw))
        return load_config(self.path)

    def test_runtime_configuration_selects_original_account_and_seven_staff(self):
        config = self.config()
        self.assertEqual(config.members, tuple(f'ou_staff_{i}' for i in range(1, 8)))
        self.assertEqual(config.staff_names['ou_staff_2'], '员工 2')
        self.assertEqual(config.telegram['allowed_chat_ids'], [20])
        self.assertIsNone(config.llm)
        self.assertEqual(config.mode, 'disabled')
        self.assertFalse(config.data_dir.exists())  # Checking does not start a service.

    def test_secrets_are_local_and_redacted(self):
        config = self.config()
        with self.assertRaises(ConfigError):
            config.credentials({})
        env = {'TELEGRAM_BOT_TOKEN': 'TEST_PRIVATE_TOKEN', 'FEISHU_APP_SECRET': 'TEST_SECRET',
               'OMNI_ADMIN_TOKEN': 'a' * 40}
        creds = config.credentials(env)
        self.assertNotIn(env['TELEGRAM_BOT_TOKEN'], repr(creds))
        self.assertNotIn(env['FEISHU_APP_SECRET'], repr(creds))
        env['OMNI_ADMIN_TOKEN'] = 'short'
        with self.assertRaises(ConfigError):
            config.credentials(env)

    def test_deny_unsafe_or_ambiguous_routing_and_secret_fields(self):
        mutations = [
            lambda r: r['telegram'].update(allowed_chat_ids=[]),
            lambda r: r['telegram'].update(allowed_chat_ids=[10]),
            lambda r: r['telegram'].update(allowed_chat_ids=[True]),
            lambda r: r['telegram'].update(allowed_chat_ids=[20, 20]),
            lambda r: r['telegram'].update(bot_token='never-print-this'),
            lambda r: r['feishu'].update(members=[{'open_id': 'user_id', 'name': 'X'}]),
            lambda r: r['admin'].update(host='0.0.0.0'),
            lambda r: r['whatsapp'].update(mode='personal'),
            lambda r: r['max'].update(mode='personal'),
            lambda r: r.update(worker_interval=float('nan')),
            lambda r: r['translation'].update(timeout_seconds=float('inf')),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.raw = configuration(self.temp)
                mutate(self.raw)
                with self.assertRaises(ConfigError) as caught:
                    self.config()
                self.assertNotIn('never-print-this', str(caught.exception))

    def test_literal_env_loader_permissions_and_no_shell_execution(self):
        path = Path(self.temp) / '.env.local'
        marker = Path(self.temp) / 'must-not-exist'
        path.write_text(f'TEST_TOKEN=$(touch {marker})\nSECOND="literal value"\n')
        os.chmod(path, 0o644)
        with self.assertRaises(ConfigError):
            load_env_file(path, environ={})
        os.chmod(path, 0o600)
        env = {'SECOND': 'existing'}
        load_env_file(path, environ=env)
        self.assertTrue(env['TEST_TOKEN'].startswith('$(touch'))
        self.assertEqual(env['SECOND'], 'existing')
        self.assertFalse(marker.exists())
        path.write_text('VALID=value\nnot-an-assignment\n')
        env = {}
        with self.assertRaises(ConfigError):
            load_env_file(path, environ=env)
        self.assertEqual(env, {})

    def test_single_instance_lock_releases_and_backup_is_consistent(self):
        data = Path(self.temp) / 'data'
        with instance_lock(data):
            with self.assertRaises(Exception) as caught:
                with instance_lock(data):
                    self.fail('second instance acquired the same database')
            self.assertEqual(caught.exception.code, 'already_running')
        with instance_lock(data):
            pass
        db = sqlite3.connect(data / 'state.sqlite3')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE example (body TEXT)')
        db.execute('INSERT INTO example VALUES (?)', ('synthetic data',))
        db.commit()
        output = Path(self.temp) / 'backup.sqlite3'
        self.assertEqual(backup_database(data, output)['status'], 'backup_complete')
        with sqlite3.connect(output) as saved:
            self.assertEqual(saved.execute('SELECT body FROM example').fetchone()[0], 'synthetic data')
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Exception):
            backup_database(data, output)
