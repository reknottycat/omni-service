"""Configure a dedicated OmniService instance without copying platform IDs by hand."""

import argparse
from collections.abc import Mapping
from contextlib import contextmanager, suppress
import fcntl
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import time
import warnings
from urllib.parse import urlsplit

from .admin_auth import FeishuAdminAuth
from .company_accounts import CompanyConfigurationError, validate_company
from .feishu_provision import provision_feishu
from .local_translation import LocalTranslation
from .models import ChannelError
from .personal import Connection, TelegramPersonal
from .personal_runner import main as run_bridge
from .telegram_live import TelegramAPI, enable_secretary, telegram_ready, telegram_setup_link


_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_HTTPS_PROXY", "TELEGRAM_TLS_TUNNEL_PORT",
    "OMNI_FEISHU_APP_SECRET", "OMNI_ADMIN_SIGNING_KEY", "TRANSLATION_API_KEY", "LLM_API_KEY",
})
_VALUE = re.compile(r'[\x21-\x7e]*')
_APP_ID = re.compile(r"cli_[A-Za-z0-9_]{1,125}")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_]{1,125}")
_TENANT = re.compile(r"[A-Za-z0-9_-]{1,128}")
_USER_INFO = "/open-apis/authen/v1/user_info"
_SETUP_HINTS = {
    'secret_input_requires_terminal': '请从交互终端输入密钥，或使用仅当前用户可读的凭据文件。',
    'feishu_provision_sdk_missing': '请安装 requirements-onboarding.txt，或用 --feishu-app 复用现有应用。',
    'feishu_setup_unavailable': '请检查此飞书应用已发布、登录身份在可见范围内，并重试。',
    'telegram_pairing_timed_out': '请用原账号扫码点「开始」并重新保存聊天自动化授权，然后重试。',
    'telegram_pairing_backlog': '专用机器人的待处理队列已满。请核对这枚 Token 是否属于新的专用机器人。',
    'model_key_requires_tls': '使用 API Key 时，非本机模型网关需要 HTTPS。',
}


def _secret_value(value):
    return bool(_VALUE.fullmatch(value)) and not any(char in value for char in "\"'\\#;")


def read_credentials(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ChannelError("unsafe_credentials_file")
        raw = stream.read(16385).decode("ascii")
    if len(raw) > 16384:
        raise ChannelError("unsafe_credentials_file")
    result = {}
    for line in raw.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in _KEYS or key in result or not _secret_value(value):
            raise ChannelError("invalid_credentials_file")
        result[key] = value
    return result


def _write_private(path, content):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _save_profile(args, credentials, config):
    _write_private(args.secrets_file, ''.join(f'{key}={value}\n' for key, value in sorted(credentials.items())))
    _write_private(args.config, json.dumps(config, ensure_ascii=False, indent=2) + '\n')


@contextmanager
def _setup_lock(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as handle:
        profile = os.fstat(handle.fileno())
        if (not stat.S_ISREG(profile.st_mode) or profile.st_uid != os.geteuid()
                or profile.st_mode & 0o077):
            raise ChannelError("unsafe_setup_lock")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ChannelError("setup_already_running") from None
        yield


def _lark_environment(config_directory):
    # A host may already be administering another Feishu application.
    env = {key: value for key, value in os.environ.items()
           if key not in _KEYS and not key.startswith("LARKSUITE_CLI_")}
    return {**env, "LARKSUITE_CLI_CONFIG_DIR": str(config_directory / "lark"),
            "LARKSUITE_CLI_DATA_DIR": str(config_directory / "lark-data"),
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}


def _lark(args, env, *, input_text=None, timeout=20, show=False, read_json=True):
    try:
        result = subprocess.run(["lark-cli", *args], input=input_text, env=env, text=True,
                                capture_output=not show, check=False, timeout=timeout)
        if result.returncode:
            raise ChannelError("feishu_setup_unavailable")
        if not show and read_json:
            return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ChannelError("feishu_setup_unavailable") from None


def _prepare_feishu(app_id, secret, directory, *, name=None):
    env = _lark_environment(directory)
    for name in ("LARKSUITE_CLI_CONFIG_DIR", "LARKSUITE_CLI_DATA_DIR"):
        Path(env[name]).mkdir(mode=0o700, exist_ok=True)
    name = name or app_id
    profile = f"--profile={name}"
    _lark(["config", "init", "--brand", "feishu", "--name", name, "--app-id", app_id,
           "--app-secret-stdin"], env, input_text=secret + "\n", read_json=False)
    identity = _lark([profile, "whoami", "--as", "bot"], env)
    if (not isinstance(identity, dict) or identity.get("appId") != app_id
            or identity.get("brand") != "feishu" or identity.get("identity") != "bot"
            or identity.get("available") is not True):
        raise ChannelError("feishu_identity_mismatch")
    return profile, env


def _feishu_owner(app_id, secret, directory):
    _prepare_feishu(app_id, secret, directory)
    with tempfile.TemporaryDirectory(prefix='.feishu-login-', dir=directory) as temporary:
        profile, env = _prepare_feishu(app_id, secret, Path(temporary))
        return _read_feishu_owner(profile, env)


def _read_feishu_owner(profile, env):
    print("飞书应用已识别。请使用准备管理 OmniService 的飞书账号完成官方授权。", flush=True)
    verified = False
    try:
        _lark([profile, "auth", "login", "--scope", "user_profile"], env, timeout=480, show=True)
        response = _lark([profile, "api", "GET", _USER_INFO, "--as", "user", "--format", "json"], env)
        info = response.get("data") if isinstance(response, dict) and response.get("ok") is True else None
        if (not isinstance(info, dict) or response.get("identity") != "user" or not isinstance(info.get("open_id"), str)
                or not _OPEN_ID.fullmatch(info["open_id"]) or not isinstance(info.get("tenant_key"), str)
                or not _TENANT.fullmatch(info["tenant_key"])):
            raise ChannelError("feishu_setup_identity_unverified")
        verified = True
    finally:
        # The running bridge needs the app credentials only, including after a failed sign-in.
        if verified:
            _lark([profile, "auth", "logout", "--json"], env)
        else:
            with suppress(ChannelError):
                _lark([profile, "auth", "logout", "--json"], env)
    return info["open_id"], info["tenant_key"]


def _start_actor(message, nonce, issued_at):
    if not isinstance(message, dict) or message.get("text") != "/start " + nonce:
        return None
    sender, chat, sent_at = message.get("from"), message.get("chat"), message.get("date")
    if (not isinstance(sender, dict) or not isinstance(chat, dict) or chat.get("type") != "private"
            or sender.get("is_bot") is not False or type(sender.get("id")) is not int
            or not 0 < sender["id"] < 2**52 or type(chat.get("id")) is not int or chat["id"] != sender["id"]
            or type(sent_at) is not int or sent_at < issued_at):
        return None
    return sender["id"]


def paired_connection(api, updates, nonce, issued_at):
    if not isinstance(updates, list):
        raise ChannelError("invalid_telegram_updates")
    actors, connections = set(), {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        if (actor := _start_actor(update.get("message"), nonce, issued_at)) is not None:
            actors.add(actor)
        if "business_connection" in update:
            connection = Connection.from_update(update["business_connection"])
            connections[connection.connection_id] = connection
    if len(actors) > 1:
        raise ChannelError("telegram_pairing_ambiguous")
    matches = [connection for connection in connections.values()
               if connection.owner_id in actors and connection.enabled and connection.can_reply]
    if len(matches) > 1:
        raise ChannelError("telegram_pairing_ambiguous")
    if not matches:
        return None
    candidate = matches[0]
    current = Connection.from_update(api.call("getBusinessConnection",
                                             {"business_connection_id": candidate.connection_id}))
    if (current.owner_id != candidate.owner_id or current.connection_id != candidate.connection_id
            or not current.enabled or not current.can_reply):
        raise ChannelError("telegram_connection_not_ready")
    return current


def _telegram_pair(api, env):
    bot = enable_secretary(api, telegram_ready(api))
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(24)
    link = telegram_setup_link(bot["username"], nonce)
    print(f"用 Telegram 原账号打开以下链接并点「开始」：\n{link}", flush=True)
    _render_qr(link, env)
    print("在原账号「设置 → 聊天自动化」连接该机器人，选择授权聊天，并允许回复。", flush=True)
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        updates = api.call("getUpdates", {"timeout": 5, "limit": 100,
                                          "allowed_updates": ["message", *TelegramPersonal.allowed_updates]})
        connection = paired_connection(api, updates, nonce, issued_at)
        if connection is not None:
            return bot, connection
        if len(updates) == 100:
            raise ChannelError('telegram_pairing_backlog')
        time.sleep(1)
    raise ChannelError("telegram_pairing_timed_out")


def _ask(credentials, key, title, *, optional=False):
    value = credentials.get(key)
    if not value:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                value = getpass.getpass(title)
            except getpass.GetPassWarning:
                raise ChannelError('secret_input_requires_terminal') from None
    if (not value and not optional) or not _secret_value(value):
        raise ChannelError("invalid_credentials_file")
    credentials[key] = value
    return value


def _render_qr(link, env):
    try:
        _lark(["auth", "qrcode", link, "--ascii"], env, show=True)
    except ChannelError:
        pass  # The official link remains available without terminal rendering.


def configure(args, credentials, config):
    if config.get("mode") != "disabled" or not args.callback_url:
        raise ChannelError("invalid_setup_config")
    if args.chat_scope is None or not args.model_url or not args.model or not 1 <= args.port <= 65535:
        raise ChannelError("invalid_setup_config")
    company_name = args.company_name or args.config.parent.name
    if (len(company_name) > 40 or not company_name.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in company_name)):
        raise ChannelError('invalid_company_name')
    scopes = args.chat_scope
    if scopes == "allowlist" and (not args.test_chat_id or len(args.test_chat_id) != len(set(args.test_chat_id))
                                 or any(not 0 < peer < 2**52 for peer in args.test_chat_id)):
        raise ChannelError("invalid_telegram_scope")
    if scopes == "connection" and args.test_chat_id:
        raise ChannelError("invalid_telegram_scope")
    saved_app = config['feishu'].get('app_id')
    app = args.feishu_app or saved_app
    if (app and not _APP_ID.fullmatch(app)) or (saved_app and app != saved_app):
        raise ChannelError('invalid_setup_config')
    telegram_token = _ask(credentials, "TELEGRAM_BOT_TOKEN", "Telegram Bot Token: ")
    model_key = _ask(credentials, "TRANSLATION_API_KEY", "翻译模型 API Key（无可直接回车）: ", optional=True)
    signing_key = credentials.get("OMNI_ADMIN_SIGNING_KEY") or secrets.token_hex(32)
    credentials["OMNI_ADMIN_SIGNING_KEY"] = signing_key
    # Validate form fields before making changes to either platform.
    FeishuAdminAuth(app_id=app or 'cli_setup', app_secret='setup', tenant_key="setup",
                    callback_url=args.callback_url, signing_key=signing_key)
    language = LocalTranslation(args.model_url, args.model, api_key=model_key or None)
    model_url = urlsplit(args.model_url)
    if model_key and model_url.scheme != 'https' and model_url.hostname != 'localhost':
        if not ipaddress.ip_address(model_url.hostname).is_loopback:
            raise ChannelError('model_key_requires_tls')
    language.translate("Здравствуйте, это проверка подключения.", "ru", "zh")
    print("模型连接已通过合成消息验证。", flush=True)
    if app:
        app_secret = _ask(credentials, "OMNI_FEISHU_APP_SECRET", "飞书 App Secret: ")
    else:
        def show_verification_url(link):
            print(f'使用飞书扫描二维码创建应用，或打开以下官方链接：\n{link}', flush=True)
            _render_qr(link, _lark_environment(args.config.parent))

        app, app_secret = provision_feishu(show_verification_url=show_verification_url)
        if not _APP_ID.fullmatch(app) or not _secret_value(app_secret):
            raise ChannelError('feishu_setup_identity_unverified')
        credentials['OMNI_FEISHU_APP_SECRET'] = app_secret
        config['feishu'].update(app_id=app, profile=app)
        _save_profile(args, credentials, config)
        print('飞书应用已保存。请在应用后台登记管理页的 OAuth 回调地址并完成必要审批。', flush=True)
    admin, organization = _feishu_owner(app, app_secret, args.config.parent)
    api = TelegramAPI(telegram_token, proxy_url=credentials.get("TELEGRAM_HTTPS_PROXY") or None,
                      tunnel_port=int(credentials["TELEGRAM_TLS_TUNNEL_PORT"])
                      if credentials.get("TELEGRAM_TLS_TUNNEL_PORT") else None)
    bot, connection = _telegram_pair(api, _lark_environment(args.config.parent))
    if connection.owner_id in args.test_chat_id:
        raise ChannelError("invalid_telegram_scope")
    config["telegram"].update(bot_token_env="TELEGRAM_BOT_TOKEN", bot_username=bot["username"],
                              connection_id=connection.connection_id, expected_owner_id=connection.owner_id,
                              peer_scope=scopes, allowed_chat_ids=args.test_chat_id, welcome_preview_chat_ids=[])
    config["feishu"].update(app_id=app, profile=app, chat_id="",
                            handoff_staff_id=admin, staff_ids=[admin], allowed_bot_app_ids=[])
    config["translation"].update(mode="private-model", base_url=args.model_url, model=args.model,
                                 api_key_env="TRANSLATION_API_KEY")
    config["management"].update(enabled=True, admin_ids=[admin], tenant_key=organization,
                                 callback_url=args.callback_url, app_secret_env="OMNI_FEISHU_APP_SECRET",
                                 signing_key_env="OMNI_ADMIN_SIGNING_KEY", bind_host="127.0.0.1", port=args.port)
    config['company_name'] = company_name.strip()
    config["mode"] = "personal-managed"
    _save_profile(args, credentials, config)
    print("已写入服务配置。启动前请完成飞书应用审批及事件订阅，然后运行 check。", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "prepare-feishu", "check", "run"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--feishu-app")
    parser.add_argument("--callback-url")
    parser.add_argument("--model-url")
    parser.add_argument("--model")
    parser.add_argument("--port", type=int, default=8081, help="管理页环回端口")
    parser.add_argument("--company-name", help="飞书服务群使用的公司名称")
    parser.add_argument("--chat-scope", choices=("connection", "allowlist"))
    parser.add_argument("--test-chat-id", action="append", type=int, default=[])
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        args.config = args.config.absolute()
        args.secrets_file = args.secrets_file.absolute()
        if args.config == args.secrets_file or not args.config.parent.samefile(args.secrets_file.parent):
            raise ChannelError("invalid_setup_config")
        if args.action == "configure":
            with _setup_lock(args.config.with_suffix(".setup.lock")):
                config = json.loads(args.config.read_text(encoding="utf-8"))
                configure(args, read_credentials(args.secrets_file), config)
            return 0
        credentials = read_credentials(args.secrets_file)
        if args.action == 'prepare-feishu':
            config = json.loads(args.config.read_text(encoding='utf-8'))['feishu']
            if not _APP_ID.fullmatch(config['app_id']) or not credentials.get('OMNI_FEISHU_APP_SECRET'):
                raise ChannelError('invalid_setup_config')
            with _setup_lock(args.config.with_suffix('.setup.lock')):
                _prepare_feishu(config['app_id'], credentials['OMNI_FEISHU_APP_SECRET'],
                                args.config.parent, name=config.get('profile'))
            print('飞书应用已写入此团队的独立配置。')
            return 0
        if args.config.name == 'personal.json' and args.config.parent.parent.name == 'omni-service':
            validate_company(args.config.parent.parent, args.config.parent.name)
        for key in _KEYS:
            os.environ.pop(key, None)
        os.environ.update({key: value for key, value in credentials.items() if value})
        for key in [key for key in os.environ if key.startswith("LARKSUITE_CLI_")]:
            del os.environ[key]
        os.environ.update(_lark_environment(args.config.parent))
        runner_args = [args.action, "--config", str(args.config)]
        if args.action == "run" and args.db is not None:
            runner_args.extend(("--db", str(args.db)))
        return run_bridge(runner_args)
    except KeyboardInterrupt:
        return 130
    except (ChannelError, CompanyConfigurationError, OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
        code = getattr(exc, 'code', 'setup_config_or_local_io')
        message = str(exc) if isinstance(exc, CompanyConfigurationError) else _SETUP_HINTS.get(code, '')
        print('OmniService setup:', code, message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
