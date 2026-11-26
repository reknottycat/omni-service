"""Start the personal-account bridge after local configuration and authorization."""

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

from .config import ConfigError, load_config, load_env_file
from .models import ChannelError


@contextmanager
def instance_lock(directory):
    """Only one runtime may own a cursor/outbox database."""
    import fcntl  # Supported deployment targets: Linux and macOS.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ConfigError('data_dir', 'use a real local directory')
    os.chmod(directory, 0o700)
    fd = os.open(directory / 'runtime.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ChannelError('already_running', 'Another runtime owns this data directory') from None
        yield
    finally:
        os.close(fd)


class BridgeRuntime:
    """Polling, event persistence, model work and delivery have explicit lifetimes."""

    def __init__(self, config, telegram, feishu, language, *, listener_factory=None, credentials=None):
        from .live_clients import LiveTransport
        from .personal_service import PersonalService
        self.config = config
        self.telegram, self.feishu = telegram, feishu
        self.credentials = credentials
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.component_errors = {}
        self.last_poll_at = self.last_work_at = None
        self.started_at = None
        self.threads = []
        self.listener = self.admin = None
        self.listener_factory = listener_factory
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.service = PersonalService(
            config.data_dir / 'state.sqlite3', LiveTransport(telegram, feishu),
            expected_owner_id=config.telegram['expected_owner_id'],
            allowed_chat_ids=config.telegram['allowed_chat_ids'],
            feishu_account_id=config.feishu['app_id'], members=config.members,
            staff_names=config.staff_names, language=language,
            telegram_account_key=config.telegram['account_key'])

    def _error(self, component, code=None):
        with self.state_lock:
            if code:
                self.component_errors[component] = code
            else:
                self.component_errors.pop(component, None)

    def preflight(self):
        bot = self.telegram.get_me()
        if (not isinstance(bot, dict) or bot.get('is_bot') is not True or
                type(bot.get('id')) is not int or bot['id'] <= 0):
            raise ChannelError('invalid_bot_identity')
        if bot.get('can_connect_to_business') is not True:
            raise ChannelError('secretary_mode_required', 'Enable Secretary Mode for the dedicated Telegram bot')
        if self.telegram.get_webhook_info().get('url'):
            raise ChannelError('webhook_conflict', 'This bot already has a webhook; use a dedicated bot or remove it yourself')
        identity = json.dumps({'bot_id': bot['id'], 'owner_id': self.config.telegram['expected_owner_id'],
                               'feishu_app_id': self.config.feishu['app_id'],
                               'account_key': self.config.telegram['account_key']}, sort_keys=True)
        with self.service.store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runtime_identity (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
            old = db.execute('SELECT value FROM runtime_identity WHERE id=1').fetchone()
            if old and old[0] != identity:
                raise ChannelError('identity_mismatch', 'Use the original account/app configuration or a new data directory')
            db.execute('INSERT OR IGNORE INTO runtime_identity VALUES(1,?)', (identity,))
        self.service.mark_connections_unverified()
        for cid in self.service.connection_ids():
            if not self.service.hydrate_connection(self.telegram.get_connection(cid)):
                raise ChannelError('owner_mismatch', 'The connected Telegram account does not match this deployment')
        self.feishu.get_bot_info()
        self._error('telegram')
        return {'bot_id': bot['id'], 'secretary_mode': True, 'webhook': False}

    def poll_once(self):
        offset = self.service.telegram_offset()
        updates = self.telegram.get_updates(offset, timeout=self.config.telegram['poll_timeout'])
        count = 0
        for update in updates:
            if self.stop_event.is_set():
                break
            cid = self.service.connection_needed(update)
            if cid:
                self.service.hydrate_connection(self.telegram.get_connection(cid))
            self.service.accept_telegram_update(update)
            count += 1
        with self.state_lock:
            self.last_poll_at = time.time()
        self._error('telegram')
        return count

    def work_once(self):
        # The SDK callback only stores events. Models never run on its event loop.
        processed = self.service.process_inbound(limit=1, stop_event=self.stop_event)
        if self.stop_event.is_set():
            return {'inbound': processed, 'outbound': []}
        delivered = self.service.drain(limit=32, stop_event=self.stop_event)
        with self.state_lock:
            self.last_work_at = time.time()
        self._error('worker')
        return {'inbound': processed, 'outbound': delivered}

    def _poll(self):
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                count = self.poll_once()
                delay = 1.0
                if count == 0:
                    self.stop_event.wait(0.1)  # Fast fake endpoints / short-poll protection.
            except ChannelError as exc:
                self._error('telegram', exc.code)
                wait = min(max(float(exc.retry_after or delay), 0.2), 60)
                self.stop_event.wait(wait)
                delay = min(delay * 2, 30)
            except Exception:
                # An unexpected bug must be visible without dumping a payload or secret.
                self._error('telegram', 'internal_error')
                self.stop_event.wait(min(delay, 30))
                delay = min(delay * 2, 30)

    def _work(self):
        while not self.stop_event.is_set():
            try:
                self.work_once()
            except ChannelError as exc:
                self._error('worker', exc.code)
            except Exception:
                self._error('worker', 'internal_error')
            self.stop_event.wait(self.config.worker_interval)

    def status(self):
        with self.state_lock:
            errors = dict(self.component_errors)
            timestamps = {'last_poll_at': self.last_poll_at, 'last_work_at': self.last_work_at}
        with self.service.store.lock:
            db = self.service.store.db
            counts = {row[0]: row[1] for row in db.execute('SELECT status,COUNT(*) FROM outbox GROUP BY status')}
            conversation_count = db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
        state = 'stopping' if self.stop_event.is_set() else ('degraded' if errors else 'running')
        if not self.started_at and not self.stop_event.is_set():
            state = 'ready'
        listener = self.listener.status() if self.listener else {'state': 'not_started'}
        if self.started_at and listener.get('state') in ('failed', 'stopped', 'reconnecting') and not self.stop_event.is_set():
            state = 'degraded'
        return {'mode': 'personal', 'state': state, 'started_at': self.started_at,
                'errors': errors, **timestamps, 'outbox': counts, 'conversations': conversation_count,
                'inbound': self.service.queue_status(),
                'feishu_listener': listener,
                'channels': {'telegram': 'connected_account', 'feishu': 'workspace',
                             'whatsapp': 'blocked_personal_route', 'max': 'blocked_platform_permission'},
                'translation_provider': self.service.language.provider_name,
                'ai_enabled': self.config.llm is not None}

    def start_workers(self):
        self.started_at = time.time()
        for name, target in (('omni-poll', self._poll), ('omni-worker', self._work)):
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def request_stop(self):
        self.stop_event.set()
        if self.listener:
            self.listener.stop()

    def run(self):
        from .admin import create_admin_server
        from .feishu_live import FeishuListener
        if self.credentials is None:
            raise ConfigError('credentials', 'live runtime credentials are required')
        self.preflight()
        if self.stop_event.is_set():
            return
        factory = self.listener_factory or FeishuListener
        self.listener = factory(self.config.feishu['app_id'], self.credentials.feishu_secret,
                                self.service.accept_feishu_payload)
        self.admin = create_admin_server(self.service, self.credentials.admin_token,
                                         host=self.config.admin['host'], port=self.config.admin['port'],
                                         status=self.status)
        thread = threading.Thread(name='omni-admin', target=self.admin.serve_forever, daemon=True)
        thread.start()
        self.threads.append(thread)
        self.start_workers()
        print(json.dumps({'state': 'starting', 'admin_url': f'http://127.0.0.1:{self.admin.server_port}',
                          'mode': 'personal', 'no_public_listener': True}), flush=True)
        try:
            if not self.stop_event.is_set():
                self.listener.start()
            if not self.stop_event.is_set():
                self._error('feishu', 'listener_stopped')
                raise ChannelError('listener_stopped')
        finally:
            self.request_stop()

    def close(self):
        self.request_stop()
        if self.admin:
            self.admin.shutdown()
            self.admin.server_close()
        # Network/model calls are bounded; leave the DB open until worker exits.
        deadline = time.monotonic() + 65
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        alive = any(thread.is_alive() for thread in self.threads)
        if not alive:
            self.service.close()
        return not alive


def backup_database(data_dir, output):
    source = Path(data_dir) / 'state.sqlite3'
    output = Path(output).resolve()
    if not source.is_file() or output.exists():
        raise ChannelError('invalid_backup_target', 'Source must exist and destination must be new')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(output) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ChannelError('backup_integrity_failed')
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {'status': 'backup_complete', 'path': str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Original Telegram account → Feishu → local models')
    parser.add_argument('--config', default='config/personal.local.json')
    parser.add_argument('--env-file', default=None, help='Literal local KEY=VALUE file, permissions 0600')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', help='Validate local config and credentials; makes no network requests')
    sub.add_parser('run', help='Start explicitly configured live account bridge')
    backup = sub.add_parser('backup', help='Create a consistent local SQLite snapshot')
    backup.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    runtime = None
    try:
        if args.env_file:
            load_env_file(args.env_file)
        config = load_config(args.config)
        if args.command == 'backup':
            print(json.dumps(backup_database(config.data_dir, args.output), ensure_ascii=False))
            return 0
        credentials = config.credentials()
        from .model_provider import LocalModels
        language = LocalModels(config.translation, config.llm, config.knowledge_path)
        sdk_available = importlib.util.find_spec('lark_oapi') is not None
        if args.command == 'check':
            print(json.dumps({'status': 'valid' if sdk_available else 'missing_live_sdk',
                              'mode': config.mode, 'staff_count': len(config.members),
                              'approved_chat_count': len(config.telegram['allowed_chat_ids']),
                              'sdk_installed': sdk_available, 'network_requests': 0,
                              'ai_enabled': config.llm is not None,
                              'next_step': 'Set mode=live and run after test-account authorization'}, ensure_ascii=False))
            return 0 if sdk_available else 2
        if config.mode != 'live':
            raise ConfigError('mode', 'set live only after authorizing the configured test accounts')
        if not sdk_available:
            raise ChannelError('missing_live_sdk', 'Install requirements-live.txt')
        from .live_clients import TelegramClient, FeishuClient
        os.umask(0o077)
        with instance_lock(config.data_dir):
            telegram = TelegramClient(credentials.telegram_token, timeout=config.telegram['poll_timeout'] + 10)
            feishu = FeishuClient(config.feishu['app_id'], credentials.feishu_secret)
            runtime = BridgeRuntime(config, telegram, feishu, language, credentials=credentials)
            def stop(_signum, _frame):
                runtime.request_stop()
            old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                runtime.run()
            finally:
                closed = runtime.close()
                runtime = None
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                if not closed:
                    raise ChannelError('shutdown_timeout', 'A bounded request did not finish before shutdown; restart recovers persisted work')
        return 0
    except (ChannelError, OSError) as exc:
        code = getattr(exc, 'code', 'local_io_error')
        detail = exc.detail if isinstance(exc, (ConfigError, ChannelError)) else 'Local file operation failed'
        print(json.dumps({'status': 'error', 'error': code, 'detail': detail}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if runtime:
            runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
