"""Pure channel contracts: no credentials, network calls, or hidden mock success.

Request.path is relative to API_BASE_URLS[channel].  Media remote_id is always
an ID obtained by uploading to the *destination* platform, not a source ID.
"""

import hashlib
import hmac
import json
import math
import re
from urllib.parse import quote, urlencode

from .models import Attachment, ChannelError, InboundEvent, Request


API_BASE_URLS = {
    "telegram": "https://api.telegram.org",
    "max": "https://platform-api2.max.ru",
    "whatsapp": "https://graph.facebook.com",
    "feishu": "https://open.feishu.cn",
}
CAPABILITIES = {
    "telegram": {"text": True, "media": ("image", "file", "audio"),
                 "delete": True, "delete_window_seconds": 48 * 3600,
                 "customer_delete_events": False},
    "max": {"text": True, "media": ("image", "file", "audio"),
            "delete": True, "delete_window_seconds": None,
            "customer_delete_events": True},
    "whatsapp": {"text": True, "media": ("image", "file", "audio"),
                 "delete": False, "delete_window_seconds": None,
                 "customer_delete_events": False, "service_window_seconds": 24 * 3600},
    "feishu": {"text": True, "media": ("image", "file", "audio"),
               "delete": True, "delete_window_seconds": None,
               "customer_delete_events": True, "create_room": True, "add_members": True},
}
capabilities = CAPABILITIES


def _channel(channel):
    if channel not in CAPABILITIES:
        raise ChannelError("unsupported_channel", "Unknown channel")


def _obj(value, name="object"):
    if not isinstance(value, dict):
        raise ChannelError("invalid_payload", name + " must be an object")
    return value


def _items(value, name="items"):
    if not isinstance(value, list):
        raise ChannelError("invalid_payload", name + " must be an array")
    return value


def _id(value, name="identifier"):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ChannelError("invalid_payload", name + " is required")
    return str(value)


def _text(value, name="text", nullable=False):
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        raise ChannelError("invalid_payload", name + " must be a string")
    return value


def _time(value, *, milliseconds=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ChannelError("invalid_payload", "timestamp is required")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ChannelError("invalid_payload", "timestamp must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ChannelError("invalid_payload", "timestamp must be nonnegative and finite")
    return result / 1000 if milliseconds else result


def profile_name(user):
    return " ".join(_text(user.get(k, ""), nullable=True) for k in ("first_name", "last_name")).strip()


def _telegram(account, payload):
    event_id = _id(payload.get("update_id"), "update_id")
    key = "edited_message" if "edited_message" in payload else "message"
    if key not in payload:
        return []
    msg = _obj(payload[key], "message")
    sender = _obj(msg.get("from"), "from")
    if sender.get("is_bot") is True:
        return []
    chat = _obj(msg.get("chat"), "chat")
    text = _text(msg.get("text", msg.get("caption", "")), nullable=True)
    attachments = []
    if "photo" in msg:
        photos = _items(msg["photo"], "photo")
        if not photos:
            raise ChannelError("invalid_payload", "photo array is empty")
        photo = _obj(photos[-1], "photo size")
        attachments.append(Attachment("image", _id(photo.get("file_id")), caption=text))
    for source, kind in (("document", "file"), ("voice", "audio"), ("audio", "audio")):
        if source in msg:
            media = _obj(msg[source], source)
            attachments.append(Attachment(kind, _id(media.get("file_id")),
                                          _text(media.get("file_name", "")), text))
    if any(k in msg for k in ("video", "video_note", "animation", "sticker", "location", "contact", "poll")):
        raise ChannelError("unsupported_media", "Telegram message type is outside the MVP contract")
    if not text and not attachments:
        if any(k in msg for k in ("new_chat_members", "left_chat_member", "new_chat_title", "pinned_message")):
            return []
        raise ChannelError("invalid_payload", "Message has no supported content")
    return [InboundEvent("telegram", account, event_id,
                         _id(chat.get("id"), "chat.id"), _id(sender.get("id"), "from.id"),
                         _id(msg.get("message_id")), text, profile_name(sender),
                         _text(sender.get("language_code", "")),
                         "edit" if key == "edited_message" else "message",
                         timestamp=_time(msg.get("edit_date", msg.get("date"))),
                         attachments=tuple(attachments))]


def _max(account, payload):
    event_type = _text(payload.get("update_type"), "update_type")
    if event_type not in ("message_created", "message_edited", "message_removed"):
        return []
    timestamp = _time(payload.get("timestamp"), milliseconds=True)
    if event_type == "message_removed":
        mid = _id(payload.get("message_id"))
        chat_id = _id(payload.get("chat_id"))
        return [InboundEvent("max", account, f"delete:{chat_id}:{mid}:{timestamp}",
                             chat_id, _id(payload.get("user_id")), mid,
                             event_type="delete", timestamp=timestamp)]
    msg = _obj(payload.get("message"), "message")
    sender = _obj(msg.get("sender"), "sender")
    if sender.get("is_bot") is True:
        return []
    recipient = _obj(msg.get("recipient"), "recipient")
    body = _obj(msg.get("body"), "body")
    mid = _id(body.get("mid"), "body.mid")
    user_id = _id(sender.get("user_id"))
    chat_id = _id(recipient.get("chat_id") if recipient.get("chat_id") is not None else user_id)
    recipient_type = "chat" if recipient.get("chat_id") is not None else "user"
    text = _text(body.get("text", ""), nullable=True)
    attachments = []
    for item in _items(body.get("attachments") if body.get("attachments") is not None else [], "attachments"):
        item = _obj(item, "attachment")
        kind = item.get("type")
        if kind not in ("image", "file", "audio"):
            raise ChannelError("unsupported_media", "Unsupported MAX attachment")
        data = _obj(item.get("payload"), "attachment.payload")
        # Incoming files may expose only a URL; it is a locator for a separate
        # authenticated download/upload stage, never an outgoing platform token.
        remote_id = _id(data.get("token") or data.get("photo_id") or data.get("url"))
        attachments.append(Attachment(kind, remote_id, _text(data.get("filename", "")),
                                      text, _text(data.get("url", ""))))
    if not text and not attachments:
        raise ChannelError("invalid_payload", "Message has no supported content")
    normalized = "edit" if event_type == "message_edited" else "message"
    eid = f"{normalized}:{chat_id}:{mid}" + (f":{timestamp}" if normalized == "edit" else "")
    return [InboundEvent("max", account, eid, chat_id, user_id, mid, text,
                         profile_name(sender), _text(payload.get("user_locale", "")), normalized,
                         timestamp=timestamp, recipient_type=recipient_type,
                         attachments=tuple(attachments))]


def _whatsapp(account, payload):
    if payload.get("object") != "whatsapp_business_account":
        raise ChannelError("invalid_payload", "Expected whatsapp_business_account")
    events = []
    for entry in _items(payload.get("entry"), "entry"):
        for change in _items(_obj(entry).get("changes"), "changes"):
            change = _obj(change)
            if change.get("field") != "messages":
                continue
            value = _obj(change.get("value"), "value")
            if value.get("messaging_product") != "whatsapp":
                raise ChannelError("invalid_payload", "Expected WhatsApp messaging_product")
            metadata = _obj(value.get("metadata"), "metadata")
            phone_id = _id(metadata.get("phone_number_id"), "phone_number_id")
            if phone_id != account:
                raise ChannelError("invalid_payload", "WhatsApp phone_number_id does not match account")
            contacts = [_obj(c) for c in _items(value.get("contacts", []), "contacts")]
            for msg in _items(value.get("messages", []), "messages"):
                msg = _obj(msg, "message")
                if msg.get("is_echo") is True:
                    continue
                bsuid = msg.get("from_user_id")
                user_id = _id(bsuid or msg.get("from"), "message sender")
                mid = _id(msg.get("id"), "message.id")
                kind = msg.get("type")
                media = ()
                if kind == "text":
                    text = _text(_obj(msg.get("text")).get("body"))
                    if not text:
                        raise ChannelError("invalid_payload", "Empty text message")
                elif kind in ("image", "document", "audio"):
                    data = _obj(msg.get(kind), "media")
                    text = _text(data.get("caption", ""))
                    media = (Attachment("file" if kind == "document" else kind,
                                        _id(data.get("id")), _text(data.get("filename", "")), text),)
                else:
                    raise ChannelError("unsupported_media", "Unsupported WhatsApp message type")
                sender_name = ""
                for contact in contacts:
                    if user_id in (str(contact.get("user_id", "")), str(contact.get("wa_id", ""))):
                        sender_name = _text(_obj(contact.get("profile", {})).get("name", ""))
                        break
                events.append(InboundEvent("whatsapp", account, mid, user_id, user_id, mid,
                                           text, sender_name, timestamp=_time(msg.get("timestamp")),
                                           recipient_type="bsuid" if bsuid else "phone",
                                           attachments=media))
    return events


def _feishu(account, payload):
    if "encrypt" in payload:
        raise ChannelError("unsupported", "Encrypted Feishu payload requires the official SDK decryption stage")
    if payload.get("type") == "url_verification":
        return []
    if payload.get("schema") != "2.0":
        raise ChannelError("invalid_payload", "Expected Feishu event schema 2.0")
    header = _obj(payload.get("header"), "header")
    event_id = _id(header.get("event_id"), "event_id")
    event_type = _text(header.get("event_type"), "event_type")
    if event_type not in ("im.message.receive_v1", "im.message.recalled_v1"):
        return []
    event = _obj(payload.get("event"), "event")
    if event_type == "im.message.recalled_v1":
        # Official event has no actor ID. The service resolves the source
        # message mapping; it must never trust a fabricated employee ID.
        return [InboundEvent("feishu", account, event_id, _id(event.get("chat_id")), "",
                             _id(event.get("message_id")), event_type="delete",
                             timestamp=_time(event.get("recall_time"), milliseconds=True))]
    sender = _obj(event.get("sender"), "sender")
    if sender.get("sender_type") in ("app", "bot"):
        return []
    if sender.get("sender_type") != "user":
        raise ChannelError("invalid_payload", "Unknown Feishu sender_type")
    ids = _obj(sender.get("sender_id"), "sender_id")
    user_id = _id(ids.get("open_id") or ids.get("user_id"), "sender ID")
    msg = _obj(event.get("message"), "message")
    try:
        content = _obj(json.loads(_text(msg.get("content"), "content")), "content JSON")
    except (ValueError, TypeError) as exc:
        raise ChannelError("invalid_payload", "Invalid Feishu content JSON") from exc
    kind = msg.get("message_type")
    attachments = ()
    if kind == "text":
        text = _text(content.get("text"))
        if not text:
            raise ChannelError("invalid_payload", "Empty Feishu text")
    elif kind in ("image", "file", "audio"):
        remote_id = _id(content.get("image_key" if kind == "image" else "file_key"))
        text = ""
        attachments = (Attachment(kind, remote_id, _text(content.get("file_name", ""))),)
    else:
        raise ChannelError("unsupported_media", "Unsupported Feishu message type")
    return [InboundEvent("feishu", account, event_id, _id(msg.get("chat_id")), user_id,
                         _id(msg.get("message_id")), text,
                         timestamp=_time(msg.get("create_time"), milliseconds=True),
                         attachments=attachments)]


def parse_events(channel: str, account_id: str, payload: dict) -> list[InboundEvent]:
    """Normalize official webhook payloads; malformed supported events fail closed."""
    _channel(channel)
    account = _id(account_id, "account_id")
    payload = _obj(payload, "payload")
    try:
        return {"telegram": _telegram, "max": _max, "whatsapp": _whatsapp,
                "feishu": _feishu}[channel](account, payload)
    except ChannelError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ChannelError("invalid_payload", "Malformed channel payload") from exc


def _headers(channel, token):
    token = _id(token, "token")
    headers = {"Content-Type": "application/json"}
    if channel != "telegram":
        headers["Authorization"] = token if channel == "max" else "Bearer " + token
    return headers


def _media(attachment):
    if not isinstance(attachment, Attachment):
        raise ChannelError("invalid_payload", "attachment must be an Attachment")
    kind = "audio" if attachment.kind == "voice" else attachment.kind
    if kind not in ("image", "file", "audio"):
        raise ChannelError("unsupported_media", "Only image, file and audio are supported")
    return kind, _id(attachment.remote_id, "destination remote_id")


def send_request(channel, account_id, chat_id, text, *, token="simulation-token",
                 recipient_type="chat", attachment: Attachment | None = None,
                 api_version="v27.0") -> Request:
    """Build an official API request. The caller owns service-window enforcement."""
    _channel(channel)
    account_id, chat_id = _id(account_id, "account_id"), _id(chat_id, "chat_id")
    text = _text(text)
    if not text and attachment is None:
        raise ChannelError("invalid_payload", "Cannot send an empty message")
    if len(text) > {"telegram": 4096, "max": 4000, "whatsapp": 4096, "feishu": 20000}[channel]:
        raise ChannelError("invalid_payload", "Message exceeds platform text limit")
    headers = _headers(channel, token)
    kind, remote_id = _media(attachment) if attachment is not None else (None, None)
    if channel == "telegram":
        method = {None: "sendMessage", "image": "sendPhoto", "file": "sendDocument", "audio": "sendAudio"}[kind]
        body = {"chat_id": chat_id}
        if kind:
            body[{"image": "photo", "file": "document", "audio": "audio"}[kind]] = remote_id
            caption = text or attachment.caption
            if len(caption) > 1024:
                raise ChannelError("invalid_payload", "Telegram media caption exceeds 1024 characters")
            if caption:
                body["caption"] = caption
        else:
            body["text"] = text
        return Request("POST", f"/bot{quote(token, safe=':')}/{method}", body, headers)
    if channel == "max":
        if recipient_type not in ("chat", "user"):
            raise ChannelError("invalid_payload", "MAX recipient_type must be chat or user")
        body = {"text": text or (attachment.caption if attachment else "")}
        if kind:
            body["attachments"] = [{"type": kind, "payload": {"token": remote_id}}]
        return Request("POST", "/messages?" + urlencode({recipient_type + "_id": chat_id}), body, headers)
    if channel == "whatsapp":
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", api_version):
            raise ChannelError("invalid_payload", "Invalid Graph API version")
        if recipient_type not in ("chat", "phone", "bsuid"):
            raise ChannelError("invalid_payload", "Unknown WhatsApp recipient type")
        wire_kind = {None: "text", "image": "image", "file": "document", "audio": "audio"}[kind]
        body = {"messaging_product": "whatsapp", "recipient_type": "individual",
                "recipient" if recipient_type == "bsuid" else "to": chat_id, "type": wire_kind}
        if kind:
            body[wire_kind] = {"id": remote_id}
            if kind in ("image", "file") and (text or attachment.caption):
                body[wire_kind]["caption"] = text or attachment.caption
            if kind == "file" and attachment.name:
                body[wire_kind]["filename"] = attachment.name
            if kind == "audio" and (text or attachment.caption):
                raise ChannelError("unsupported", "WhatsApp audio has no caption; send separate text")
        else:
            body["text"] = {"body": text, "preview_url": False}
        return Request("POST", f"/{api_version}/{quote(account_id, safe='')}/messages", body, headers)
    if kind:
        if text or attachment.caption:
            raise ChannelError("unsupported", "Feishu media has no caption; send separate text")
        content = {"image_key" if kind == "image" else "file_key": remote_id}
    else:
        content = {"text": text}
    body = {"receive_id": chat_id, "msg_type": kind or "text",
            "content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))}
    return Request("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", body, headers)


def delete_request(channel, account_id, chat_id, message_id, *, token="simulation-token") -> Request:
    _channel(channel)
    _id(account_id, "account_id")
    chat_id, message_id = _id(chat_id, "chat_id"), _id(message_id, "message_id")
    if channel == "whatsapp":
        raise ChannelError("unsupported", "WhatsApp Cloud API recall is not supported by this adapter")
    headers = _headers(channel, token)
    if channel == "telegram":
        if not message_id.isdecimal():
            raise ChannelError("invalid_payload", "Telegram message_id must be an integer")
        return Request("POST", f"/bot{quote(token, safe=':')}/deleteMessage",
                       {"chat_id": chat_id, "message_id": int(message_id)}, headers)
    if channel == "max":
        return Request("DELETE", "/messages?" + urlencode({"message_id": message_id}), headers=headers)
    return Request("DELETE", "/open-apis/im/v1/messages/" + quote(message_id, safe=""), headers=headers)


def _provider_error(payload, code_key="code"):
    code = payload.get(code_key)
    category = ({429: "rate_limited", 401: "unauthorized", 403: "forbidden"}.get(code, "provider_error")
                if type(code) is int else "provider_error")
    parameters = payload.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if retry_after is not None:
        try:
            retry_after = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            retry_after = None
    raise ChannelError(category, str(payload.get("description") or payload.get("message") or
                                    payload.get("msg") or "Platform rejected the operation"), retry_after)


def _response_id(value, *, integer=False):
    """A corrupt receipt cannot prove that a mutation failed or succeeded."""
    if integer:
        if type(value) is not int or value <= 0:
            raise ChannelError("invalid_response", "Invalid platform message ID")
        return str(value)
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ChannelError("invalid_response", "Invalid platform identifier")
    return value


def _explicit_error_code(value):
    return ((type(value) is int and value != 0) or
            (isinstance(value, str) and bool(value.strip()) and value.strip() != "0"))


def parse_response(channel, operation: str, payload: dict) -> str:
    """Validate semantic success; broken receipts mean unknown, not rejection.

    ``invalid_response`` means the caller must preserve an uncertain mutation
    outcome: the platform may have acted before its receipt was lost/corrupted.
    """
    _channel(channel)
    if operation == "add_member":
        operation = "add_members"
    if operation not in ("send", "delete", "create_room", "add_members"):
        raise ChannelError("unsupported", "Unknown operation")
    if operation in ("create_room", "add_members") and channel != "feishu":
        raise ChannelError("unsupported", "Room operations are Feishu-only")
    if operation == "delete" and channel == "whatsapp":
        raise ChannelError("unsupported", "WhatsApp recall is unavailable")
    try:
        payload = _obj(payload)
        if channel == "telegram":
            if payload.get("ok") is False:
                _provider_error(payload, "error_code")
            if payload.get("ok") is not True:
                raise ChannelError("invalid_response", "Missing or invalid Telegram success flag")
            if operation == "delete":
                if payload.get("result") is not True:
                    raise ChannelError("invalid_response", "Telegram deletion receipt is incomplete")
                return ""
            return _response_id(_obj(payload.get("result")).get("message_id"), integer=True)
        if channel == "max":
            if payload.get("success") is False:
                _provider_error(payload)
            if operation == "delete":
                if payload.get("success") is True:
                    return ""
                if _explicit_error_code(payload.get("code")):
                    _provider_error(payload)
                raise ChannelError("invalid_response", "Missing or invalid MAX deletion success flag")
            if _explicit_error_code(payload.get("code")):
                _provider_error(payload)
            return _response_id(_obj(_obj(payload.get("message")).get("body")).get("mid"))
        if channel == "whatsapp":
            if "error" in payload:
                error = _obj(payload["error"])
                if type(error.get("code")) is int and error["code"] != 0:
                    _provider_error(error)
                raise ChannelError("invalid_response", "Malformed WhatsApp error receipt")
            messages = _items(payload.get("messages"))
            if len(messages) != 1:
                raise ChannelError("invalid_response", "Missing or ambiguous WhatsApp message ID")
            return _response_id(_obj(messages[0]).get("id"))
        if type(payload.get("code")) is not int:
            raise ChannelError("invalid_response", "Missing or invalid Feishu result code")
        if payload["code"] != 0:
            _provider_error(payload)
        if operation == "delete":
            return ""
        data = _obj(payload.get("data"))
        if operation == "add_members":
            for key in ("invalid_id_list", "not_existed_id_list", "pending_approval_id_list"):
                if key in data:
                    for value in _items(data[key], key):
                        _response_id(value)
            if any(data.get(key) for key in ("invalid_id_list", "not_existed_id_list", "pending_approval_id_list")):
                raise ChannelError("partial_failure", "Some Feishu members were not added")
            return ""
        return _response_id(data.get("chat_id" if operation == "create_room" else "message_id"))
    except ChannelError as exc:
        if exc.code == "invalid_payload":
            raise ChannelError("invalid_response", "Malformed platform response") from exc
        raise


def verify_webhook(channel, raw_body: bytes, headers: dict, secret: str) -> bool:
    """Verify secrets/signatures without modifying the raw body.

    Feishu: if any signature header is present, all three must verify; otherwise
    secret is the Verification Token in the JSON envelope. Encrypted envelopes
    can be signature-checked but need SDK decryption before parse_events.
    """
    if channel not in CAPABILITIES or not secret or not isinstance(secret, str):
        return False
    if not isinstance(raw_body, bytes) or not isinstance(headers, dict):
        return False
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    try:
        if channel in ("telegram", "max"):
            name = "x-telegram-bot-api-secret-token" if channel == "telegram" else "x-max-bot-api-secret"
            return hmac.compare_digest(headers.get(name, "").encode(), secret.encode())
        if channel == "whatsapp":
            expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(headers.get("x-hub-signature-256", ""), expected)
        names = ("x-lark-request-timestamp", "x-lark-request-nonce", "x-lark-signature")
        if any(name in headers for name in names):
            if not all(headers.get(name) for name in names):
                return False
            prefix = (headers[names[0]] + headers[names[1]] + secret).encode()
            expected = hashlib.sha256(prefix + raw_body).hexdigest()
            return hmac.compare_digest(headers[names[2]], expected)
        payload = _obj(json.loads(raw_body))
        token = payload.get("token") if payload.get("type") == "url_verification" else _obj(payload.get("header")).get("token")
        return isinstance(token, str) and hmac.compare_digest(token.encode(), secret.encode())
    except (ChannelError, ValueError, TypeError, UnicodeError):
        return False


def feishu_create_room_request(name, members, token="simulation-token") -> Request:
    name = _text(name, "room name")
    if not name.strip():
        raise ChannelError("invalid_payload", "Room name is required")
    ids = list(dict.fromkeys(_id(x, "member open_id") for x in _items(members, "members")))
    return Request("POST", "/open-apis/im/v1/chats?user_id_type=open_id",
                   {"name": name, "user_id_list": ids, "chat_mode": "group", "chat_type": "private"},
                   _headers("feishu", token))


def feishu_add_members_request(room_id, members, token="simulation-token") -> Request:
    room_id = _id(room_id, "room_id")
    ids = list(dict.fromkeys(_id(x, "member open_id") for x in _items(members, "members")))
    if not ids:
        raise ChannelError("invalid_payload", "Members must not be empty")
    return Request("POST", "/open-apis/im/v1/chats/" + quote(room_id, safe="") + "/members?member_id_type=open_id",
                   {"id_list": ids}, _headers("feishu", token))
