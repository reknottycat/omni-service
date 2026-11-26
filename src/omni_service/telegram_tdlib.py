"""Optional, one-time Telegram account authorization through the official TDLib JSON API."""

import base64
import ctypes
from getpass import GetPassWarning, getpass
import json
import os
import re
import secrets
import stat
from tempfile import TemporaryDirectory
import time
import warnings

from .models import ChannelError


RECIPIENT_FLAGS = {
    "new": "select_new_chats",
    "existing": "select_existing_chats",
    "contacts": "select_contacts",
    "non-contacts": "select_non_contacts",
}
_QR_LINK = re.compile(r"tg://login\?token=[A-Za-z0-9_-]+={0,2}")
_OWNER = re.compile(r"(?:@?[A-Za-z][A-Za-z0-9_]{4,31}|[1-9][0-9]{0,15})")


def valid_owner(value):
    return bool(_OWNER.fullmatch(value or "")) and (
        not value.isdecimal() or int(value) < 2**52
    )


def private_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except (GetPassWarning, EOFError):
            raise ChannelError("secret_input_requires_terminal") from None


def application_credentials(path):
    """Read operator-provided API credentials from a small private file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 4096):
            raise ChannelError("invalid_telegram_api_credentials")
        fields = json.loads(stream.read(4097))
    if not isinstance(fields, dict) or set(fields) != {"api_id", "api_hash"}:
        raise ChannelError("invalid_telegram_api_credentials")
    return _validate_application(fields["api_id"], fields["api_hash"])


def _validate_application(api_id, api_hash):
    if (type(api_id) is not int or not 0 < api_id < 2**31
            or not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)):
        raise ChannelError("invalid_telegram_api_credentials")
    return api_id, api_hash


class TDLib:
    def __init__(self, library):
        try:
            self._lib = ctypes.CDLL(str(library))
            self._lib.td_create_client_id.argtypes = []
            self._lib.td_create_client_id.restype = ctypes.c_int
            self._lib.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
            self._lib.td_send.restype = None
            self._lib.td_receive.argtypes = [ctypes.c_double]
            self._lib.td_receive.restype = ctypes.c_char_p
            self._lib.td_execute.argtypes = [ctypes.c_char_p]
            self._lib.td_execute.restype = ctypes.c_char_p
        except (OSError, AttributeError):
            raise ChannelError("tdlib_unavailable") from None
        self._lib.td_execute(b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}')
        self.client = self._lib.td_create_client_id()
        self.sequence = 0
        self.authorization = None

    def _receive(self, deadline):
        while (remaining := deadline - time.monotonic()) > 0:
            raw = self._lib.td_receive(min(1.0, remaining))
            if not raw:
                continue
            event = json.loads(raw)
            if event.get("@client_id") != self.client:
                continue
            if event.get("@type") == "updateAuthorizationState":
                self.authorization = event["authorization_state"]
            return event
        raise ChannelError("tdlib_timeout")

    def call(self, method, *, timeout=30, allow_missing=False, on_update=None, **fields):
        self.sequence += 1
        request = {"@type": method, "@extra": self.sequence, **fields}
        self._lib.td_send(self.client, json.dumps(request).encode())
        deadline = time.monotonic() + timeout
        while True:
            event = self._receive(deadline)
            if event.get("@extra") != self.sequence:
                if on_update is not None and "@extra" not in event:
                    on_update(event)
                continue
            if event.get("@type") == "error":
                if allow_missing and event.get("code") == 404:
                    return None
                # TDLib responses may contain private details. Report the failed step only.
                raise ChannelError(f"tdlib_{method}_failed")
            return event

    def send_start_confirmed(self, *, bot_user_id, chat_id, parameter, timeout=30):
        deadline = time.monotonic() + timeout
        early = []

        def capture(event):
            kind = event.get("@type")
            msg = event.get("message")
            relevant = (
                kind in ("updateMessageSendSucceeded", "updateMessageSendFailed")
                and isinstance(msg, dict) and msg.get("chat_id") == chat_id
            ) or (kind == "updateDeleteMessages" and event.get("chat_id") == chat_id)
            if relevant:
                if len(early) == 64:
                    raise ChannelError("telegram_setup_send_unknown")
                early.append(event)

        try:
            message = self.call("sendBotStartMessage", timeout=timeout, on_update=capture,
                                bot_user_id=bot_user_id, chat_id=chat_id, parameter=parameter)
            mid = message.get("id")
            if (message.get("@type") != "message" or message.get("chat_id") != chat_id
                    or type(mid) is not int or mid <= 0 or message.get("is_outgoing") is not True):
                raise ChannelError("telegram_setup_send_unknown")
            state = message.get("sending_state")
            if state is None:
                return
            kind = state.get("@type") if isinstance(state, dict) else None
            if kind == "messageSendingStateFailed":
                raise ChannelError("telegram_setup_send_failed")
            if kind != "messageSendingStatePending":
                raise ChannelError("telegram_setup_send_unknown")
            updates = iter(early)
            while True:
                event = next(updates, None)
                if event is None:
                    event = self._receive(deadline)
                kind = event.get("@type")
                if kind == "updateDeleteMessages" and event.get("chat_id") == chat_id:
                    ids = event.get("message_ids")
                    if isinstance(ids, list) and mid in ids:
                        raise ChannelError("telegram_setup_send_failed")
                if kind not in ("updateMessageSendSucceeded", "updateMessageSendFailed"):
                    continue
                msg = event.get("message")
                if event.get("old_message_id") != mid or not isinstance(msg, dict) or msg.get("chat_id") != chat_id:
                    continue
                if kind == "updateMessageSendFailed":
                    raise ChannelError("telegram_setup_send_failed")
                if (msg.get("@type") == "message" and type(msg.get("id")) is int and msg["id"] > 0
                        and msg.get("is_outgoing") is True and msg.get("sending_state") is None):
                    return
                raise ChannelError("telegram_setup_send_unknown")
        except ChannelError as exc:
            if exc.code == "tdlib_timeout":
                raise ChannelError("telegram_setup_send_unknown") from None
            if exc.code == "tdlib_sendBotStartMessage_failed":
                raise ChannelError("telegram_setup_send_failed") from None
            raise

    def next_state(self, previous=None, *, timeout=30):
        deadline = time.monotonic() + timeout
        while self.authorization is None or self.authorization == previous:
            self._receive(deadline)
        return self.authorization

    def close(self):
        if self.authorization and self.authorization.get("@type") == "authorizationStateClosed":
            return
        ready = self.authorization and self.authorization.get("@type") == "authorizationStateReady"
        if ready:
            try:
                self.call("logOut", timeout=15)
                self._wait_closed()
                return
            except ChannelError:
                print("Telegram 临时会话未确认退出。请在 Telegram「设备」中检查 OmniService setup 会话。", flush=True)
        self.call("close", timeout=10)
        self._wait_closed()

    def _wait_closed(self):
        deadline = time.monotonic() + 15
        while not self.authorization or self.authorization.get("@type") != "authorizationStateClosed":
            self._receive(deadline)


def _sign_in(td, directory, *, login, show_qr, proxy, application=None):
    if application is None:
        prompt_id = private_input("Telegram API ID（my.telegram.org）: ").strip()
        if not re.fullmatch(r"[0-9]{1,10}", prompt_id):
            raise ChannelError("invalid_telegram_api_credentials")
        application = _validate_application(int(prompt_id), private_input("Telegram API hash: ").strip())
    api_id, api_hash = application
    state = td.call("getAuthorizationState")
    if state.get("@type") != "authorizationStateWaitTdlibParameters":
        raise ChannelError("tdlib_unexpected_session")
    td.call("setTdlibParameters", database_directory=directory, files_directory=directory,
            database_encryption_key=base64.b64encode(secrets.token_bytes(32)).decode(),
            use_file_database=False, use_chat_info_database=False, use_message_database=False,
            api_id=api_id, api_hash=api_hash, system_language_code="zh",
            device_model="OmniService setup", application_version="1.0")
    if proxy:
        host, port = proxy
        td.call("addProxy", proxy={"@type": "proxy", "server": host, "port": port,
                                  "type": {"@type": "proxyTypeHttp", "http_only": False}},
                enable=True, comment="OmniService setup")
    deadline = time.monotonic() + 420
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChannelError("tdlib_timeout")
        state = td.next_state(state, timeout=remaining)
        kind = state.get("@type")
        if kind == "authorizationStateReady":
            return
        if kind == "authorizationStateWaitPhoneNumber":
            if login == "qr":
                td.call("requestQrCodeAuthentication", other_user_ids=[])
            else:
                phone = private_input("Telegram 手机号（国际格式）: ").strip()
                if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                    raise ChannelError("invalid_telegram_phone")
                td.call("setAuthenticationPhoneNumber", phone_number=phone)
        elif kind == "authorizationStateWaitOtherDeviceConfirmation":
            link = state.get("link")
            if not isinstance(link, str) or not _QR_LINK.fullmatch(link):
                raise ChannelError("tdlib_invalid_qr")
            show_qr(link)
        elif kind == "authorizationStateWaitCode":
            td.call("checkAuthenticationCode", code=private_input("Telegram 登录验证码: ").strip())
        elif kind == "authorizationStateWaitPassword":
            td.call("checkAuthenticationPassword", password=private_input("Telegram 两步验证密码: "))
        elif kind == "authorizationStateWaitEmailAddress":
            td.call("setAuthenticationEmailAddress", email_address=private_input("Telegram 登录邮箱: ").strip())
        elif kind == "authorizationStateWaitEmailCode":
            td.call("checkAuthenticationEmailCode", code={"@type": "emailAddressAuthenticationCode",
                    "code": private_input("Telegram 邮箱验证码: ").strip()})
        else:
            raise ChannelError("tdlib_login_requires_client")


def _owner_id(account, expected):
    user_id = account.get("id")
    kind = account.get("type") or {}
    if type(user_id) is not int or not 0 < user_id < 2**52 or kind.get("@type") != "userTypeRegular":
        raise ChannelError("telegram_setup_wrong_owner")
    if expected.isdecimal():
        matches = user_id == int(expected)
    else:
        names = (account.get("usernames") or {}).get("active_usernames", [])
        matches = any(name.casefold() == expected.lstrip("@").casefold() for name in names)
    if not matches:
        raise ChannelError("telegram_setup_wrong_owner")
    return user_id


def _private_chat(chat, user_id):
    kind = chat.get("type") or {}
    if (kind.get("@type") != "chatTypePrivate" or kind.get("user_id") != user_id
            or type(chat.get("id")) is not int or chat["id"] != user_id):
        raise ChannelError("telegram_setup_invalid_private_chat")
    return chat["id"]


def _current_bot(td):
    info = td.call("getBusinessConnectedBot", allow_missing=True)
    if info is None:
        return None
    bot = info.get("bot") if info.get("@type") == "businessConnectedBotInfo" else info
    if not isinstance(bot, dict) or bot.get("@type") != "businessConnectedBot":
        raise ChannelError("tdlib_invalid_connected_bot")
    return bot


def _bind(td, bot, expected_owner, nonce, included, excluded, selectors):
    owner = _owner_id(td.call("getMe"), expected_owner)
    if owner in included or bot["id"] in (*included, *excluded):
        raise ChannelError("telegram_setup_invalid_private_chat")
    targets = (*included, *excluded)
    bot_chat = _private_chat(td.call("searchPublicChat", username=bot["username"]), bot["id"])
    current = _current_bot(td)
    if current is not None and current.get("bot_user_id") != bot["id"]:
        raise ChannelError("telegram_setup_other_bot_connected")
    for user_id in targets:
        _private_chat(td.call("createPrivateChat", user_id=user_id, force=False), user_id)

    recipients = {"@type": "businessRecipients", "chat_ids": list(included),
                  "excluded_chat_ids": list(excluded), "exclude_selected": False}
    recipients.update({field: selector in selectors for selector, field in RECIPIENT_FLAGS.items()})
    td.call("setBusinessConnectedBot", bot={"@type": "businessConnectedBot", "bot_user_id": bot["id"],
            "recipients": recipients, "rights": {"@type": "businessBotRights", "can_reply": True}})
    verified = _current_bot(td) or {}
    scope = verified.get("recipients") or {}
    rights = verified.get("rights") or {}
    if (verified.get("bot_user_id") != bot["id"] or rights.get("can_reply") is not True
            or any(value is True for key, value in rights.items() if key != "can_reply")
            or set(scope.get("chat_ids", [])) != set(included)
            or set(scope.get("excluded_chat_ids", [])) != set(excluded)
            or any(scope.get(key, False) != value for key, value in recipients.items()
                   if isinstance(value, bool))):
        raise ChannelError("telegram_setup_binding_unverified")
    td.send_start_confirmed(bot_user_id=bot["id"], chat_id=bot_chat, parameter=nonce)
    return owner


def connect_account(library, directory, *, bot, expected_owner, nonce, included, excluded,
                    selectors, login, show_qr, proxy, application):
    with TemporaryDirectory(prefix=".telegram-setup-", dir=directory) as session:
        td = TDLib(library)
        try:
            _sign_in(td, session, login=login, show_qr=show_qr, proxy=proxy, application=application)
            return _bind(td, bot, expected_owner, nonce, included, excluded, selectors)
        finally:
            td.close()
