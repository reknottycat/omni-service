"""Explicit deployment configuration; credentials stay in the environment."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat

from .models import ChannelError


class ConfigError(ChannelError):
    def __init__(self, field_name, reason):
        # Never interpolate a supplied value: it may accidentally contain a secret.
        self.field_name = field_name
        super().__init__('invalid_config', f'{field_name}: {reason}')


def _string(value, name, *, optional=False):
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ConfigError(name, 'a non-empty string is required')
    if any(c in value for c in ('\x00', '\r', '\n')):
        raise ConfigError(name, 'control characters are not allowed')
    return value.strip()


def _env_name(value, name):
    value = _string(value, name)
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', value):
        raise ConfigError(name, 'use an environment variable name')
    return value


def _mapping(value, name):
    if not isinstance(value, dict):
        raise ConfigError(name, 'a JSON object is required')
    return value


def _keys(value, allowed, name):
    if set(value) - set(allowed):
        raise ConfigError(name, 'unknown field; check the example configuration (secrets belong in environment variables)')


@dataclass(frozen=True)
class Credentials:
    telegram_token: str = field(repr=False)
    feishu_secret: str = field(repr=False)
    admin_token: str = field(repr=False)


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    mode: str
    data_dir: Path
    telegram: dict
    feishu: dict
    translation: dict
    llm: dict | None
    knowledge_path: Path
    admin: dict
    worker_interval: float

    @property
    def members(self):
        return tuple(m['open_id'] for m in self.feishu['members'])

    @property
    def staff_names(self):
        return {m['open_id']: m['name'] for m in self.feishu['members']}

    def credentials(self, environ=None):
        env = os.environ if environ is None else environ
        names = (self.telegram['bot_token_env'], self.feishu['app_secret_env'], self.admin['token_env'])
        values = []
        for name in names:
            value = env.get(name, '')
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(name, 'missing local environment secret')
            if any(c in value for c in ('\r', '\n', '\x00')):
                raise ConfigError(name, 'invalid environment secret')
            values.append(value)
        if len(values[2]) < 32:
            raise ConfigError(names[2], 'use at least 32 random characters')
        return Credentials(*values)


def load_env_file(path, *, environ=None):
    """Read literal KEY=VALUE pairs. No shell expansion or command execution."""
    env = os.environ if environ is None else environ
    path = Path(path)
    try:
        mode = path.stat().st_mode
        if os.name == 'posix' and stat.S_IMODE(mode) & 0o077:
            raise ConfigError('env_file', 'restrict permissions to 0600 before loading')
        if path.stat().st_size > 65_536:
            raise ConfigError('env_file', 'file exceeds 64 KiB')
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        raise ConfigError('env_file', 'cannot read the local environment file') from None
    pending = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ConfigError('env_file', 'expected literal KEY=VALUE lines')
        name, value = line.split('=', 1)
        name = _env_name(name.strip(), 'env_file')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if '\x00' in value or name in pending:
            raise ConfigError('env_file', 'invalid or duplicate entry')
        pending[name] = value
    # An explicitly supplied process environment takes precedence.
    for name, value in pending.items():
        env.setdefault(name, value)
    return tuple(pending)


def load_config(path):
    path = Path(path).resolve()
    try:
        if path.stat().st_size > 262_144:
            raise ConfigError('config', 'file exceeds 256 KiB')
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise ConfigError('config', 'cannot read a valid JSON file') from None
    raw = _mapping(raw, 'config')
    _keys(raw, ('mode', 'data_dir', 'telegram', 'feishu', 'translation', 'llm',
                'knowledge_path', 'admin', 'worker_interval', 'whatsapp', 'max'), 'config')
    mode = raw.get('mode', 'disabled')
    if mode not in ('disabled', 'live'):
        raise ConfigError('mode', 'choose disabled or live')
    telegram = dict(_mapping(raw.get('telegram'), 'telegram'))
    _keys(telegram, ('account_key', 'expected_owner_id', 'allowed_chat_ids', 'bot_token_env', 'poll_timeout'), 'telegram')
    owner = telegram.get('expected_owner_id')
    if type(owner) is not int or owner <= 0:
        raise ConfigError('telegram.expected_owner_id', 'enter the original account numeric user ID')
    allowed = telegram.get('allowed_chat_ids', [])
    if not isinstance(allowed, list) or any(type(x) is not int or x <= 0 for x in allowed):
        raise ConfigError('telegram.allowed_chat_ids', 'use positive numeric private chat IDs')
    if owner in allowed or len(set(allowed)) != len(allowed):
        raise ConfigError('telegram.allowed_chat_ids', 'exclude the owner and duplicate chat IDs')
    if not allowed:
        raise ConfigError('telegram.allowed_chat_ids', 'approve at least one test/customer chat; an empty list grants no access')
    telegram['account_key'] = _string(telegram.get('account_key', 'primary'), 'telegram.account_key')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', telegram['account_key']):
        raise ConfigError('telegram.account_key', 'use a stable non-secret account label')
    telegram['bot_token_env'] = _env_name(telegram.get('bot_token_env', 'TELEGRAM_BOT_TOKEN'), 'telegram.bot_token_env')
    timeout = telegram.get('poll_timeout', 25)
    if type(timeout) is not int or not 1 <= timeout <= 50:
        raise ConfigError('telegram.poll_timeout', 'use an integer from 1 to 50 seconds')
    telegram['poll_timeout'] = timeout
    feishu = dict(_mapping(raw.get('feishu'), 'feishu'))
    _keys(feishu, ('app_id', 'app_secret_env', 'members'), 'feishu')
    feishu['app_id'] = _string(feishu.get('app_id'), 'feishu.app_id')
    feishu['app_secret_env'] = _env_name(feishu.get('app_secret_env', 'FEISHU_APP_SECRET'), 'feishu.app_secret_env')
    members = feishu.get('members', [])
    if not isinstance(members, list) or not 1 <= len(members) <= 200:
        raise ConfigError('feishu.members', 'configure 1 to 200 authorized employee Open IDs')
    seen = set()
    cleaned = []
    for member in members:
        member = _mapping(member, 'feishu.members')
        _keys(member, ('open_id', 'name'), 'feishu.members')
        uid = _string(member.get('open_id'), 'feishu.members.open_id')
        if not uid.startswith('ou_') or uid in seen:
            raise ConfigError('feishu.members.open_id', 'use unique Open IDs from this application')
        seen.add(uid)
        cleaned.append({'open_id': uid, 'name': _string(member.get('name', uid), 'feishu.members.name')})
    feishu['members'] = cleaned
    translation = dict(_mapping(raw.get('translation'), 'translation'))
    llm = raw.get('llm')
    if llm is not None:
        llm = dict(_mapping(llm, 'llm'))
        _keys(llm, ('enabled', 'api_kind', 'base_url', 'model', 'api_key_env', 'timeout_seconds',
                    'allow_remote', 'require_api_key', 'structured_output'), 'llm')
        enabled = llm.pop('enabled', False)
        if type(enabled) is not bool:
            raise ConfigError('llm.enabled', 'use a boolean')
        if not enabled:
            llm = None
    for label, model in (('translation', translation), ('llm', llm)):
        if model is None:
            continue
        _keys(model, ('api_kind', 'base_url', 'model', 'api_key_env', 'timeout_seconds', 'allow_remote', 'require_api_key', 'structured_output'), label)
        kind = model.get('api_kind', 'openai-compatible')
        if kind not in ('openai-compatible', 'ollama', 'libretranslate') or (label == 'llm' and kind == 'libretranslate'):
            raise ConfigError(label + '.api_kind', 'unsupported model protocol')
        model['api_kind'] = kind
        _string(model.get('base_url'), label + '.base_url')
        if kind != 'libretranslate':
            _string(model.get('model'), label + '.model')
        if model.get('api_key_env'):
            _env_name(model['api_key_env'], label + '.api_key_env')
        limit = model.get('timeout_seconds', 15)
        if type(limit) not in (int, float) or not 1 <= limit <= 60:
            raise ConfigError(label + '.timeout_seconds', 'use 1 to 60 seconds')
        if type(model.get('allow_remote', False)) is not bool:
            raise ConfigError(label + '.allow_remote', 'use a boolean')
        if type(model.get('require_api_key', False)) is not bool:
            raise ConfigError(label + '.require_api_key', 'use a boolean')
        if model.get('structured_output', 'json_object') not in ('json_object', 'json_schema', 'prompt'):
            raise ConfigError(label + '.structured_output', 'choose json_object, json_schema or prompt')
    admin = dict(_mapping(raw.get('admin', {}), 'admin'))
    _keys(admin, ('host', 'port', 'token_env'), 'admin')
    admin.setdefault('host', '127.0.0.1')
    admin.setdefault('port', 8090)
    admin.setdefault('token_env', 'OMNI_ADMIN_TOKEN')
    if admin['host'] not in ('127.0.0.1', 'localhost'):
        raise ConfigError('admin.host', 'this release exposes management on loopback only')
    if type(admin['port']) is not int or not 0 <= admin['port'] <= 65535:
        raise ConfigError('admin.port', 'invalid TCP port')
    _env_name(admin['token_env'], 'admin.token_env')
    interval = raw.get('worker_interval', 0.25)
    if type(interval) not in (int, float) or not 0.05 <= interval <= 30:
        raise ConfigError('worker_interval', 'use 0.05 to 30 seconds')
    for channel in ('whatsapp', 'max'):
        value = _mapping(raw.get(channel, {'mode': 'blocked'}), channel)
        _keys(value, ('mode',), channel)
        if value.get('mode') != 'blocked':
            raise ConfigError(channel + '.mode', 'a personal-account live route is not enabled in this release')
    def relative(value, name):
        value = _string(value, name)
        return (path.parent / value).resolve()
    data_dir = relative(raw.get('data_dir', '../var/omni'), 'data_dir')
    knowledge = relative(raw.get('knowledge_path', '../fixtures/knowledge/logistics-zh-ru.md'), 'knowledge_path')
    if not knowledge.exists():
        raise ConfigError('knowledge_path', 'local knowledge file/directory does not exist')
    return BridgeConfig(path, mode, data_dir, telegram, feishu, translation, llm,
                        knowledge, admin, float(interval))
