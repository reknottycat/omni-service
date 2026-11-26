"""Validate Telegram connected-account events before they reach the bridge."""
from dataclasses import dataclass

from .channels import profile_name
from .media import safe_filename
from .models import Attachment, ChannelError, InboundEvent


def _positive(value):
    return type(value) is int and value > 0


def valid_file_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and value.isascii() and value.isprintable() and " " not in value)


def _attachment(message, caption):
    photos = message.get("photo")
    if isinstance(photos, list):
        for photo in reversed(photos):
            if isinstance(photo, dict) and valid_file_id(photo.get("file_id")):
                return Attachment("image", photo["file_id"], caption=caption)
    for field, kind in (("animation", "file"), ("document", "file"), ("video", "file"),
                        ("video_note", "file"), ("voice", "audio"), ("audio", "audio")):
        media = message.get(field)
        if isinstance(media, dict) and valid_file_id(media.get("file_id")):
            name = safe_filename(media.get("file_name"), fallback="")
            return Attachment(kind, media["file_id"], name, caption)
    return None


@dataclass(frozen=True)
class Connection:
    connection_id: str
    owner_id: int
    enabled: bool
    can_reply: bool
    can_delete_sent: bool

    @classmethod
    def from_update(cls, data):
        if not isinstance(data, dict):
            raise ChannelError("invalid_connection")
        user = data.get("user")
        rights = data.get("rights")
        cid = data.get("id")
        if not isinstance(cid, str) or not cid or not isinstance(user, dict) or not _positive(user.get("id")):
            raise ChannelError("invalid_connection")
        rights = rights if isinstance(rights, dict) else {}
        return cls(cid, user["id"], data.get("is_enabled") is True,
                   rights.get("can_reply") is True,
                   rights.get("can_delete_sent_messages") is True
                   or rights.get("can_delete_all_messages") is True)


@dataclass(frozen=True)
class PersonalEvent:
    role: str
    event: InboundEvent


class TelegramPersonal:
    allowed_updates = ("business_connection", "business_message",
                       "edited_business_message", "deleted_business_messages")

    def __init__(self, *, expected_owner_id, allowed_chat_ids=()):
        allowed_chat_ids = tuple(allowed_chat_ids)
        if not _positive(expected_owner_id) or not all(_positive(x) for x in allowed_chat_ids):
            raise ChannelError("invalid_policy")
        self.owner_id = expected_owner_id
        self.allowed = frozenset(allowed_chat_ids)
        self.connections = {}

    def apply_connection(self, data):
        connection = Connection.from_update(data)
        if connection.owner_id != self.owner_id:
            return False
        self.connections[connection.connection_id] = connection
        return True

    def _allowed(self, cid, chat_id):
        connection = self.connections.get(cid) if isinstance(cid, str) else None
        return connection if (connection and connection.enabled
                              and _positive(chat_id) and chat_id in self.allowed
                              and chat_id != self.owner_id) else None

    def parse(self, update):
        """Return only approved private-chat events. No raw update logging."""
        if not isinstance(update, dict) or not _positive(update.get("update_id")):
            return []
        if "business_connection" in update:
            self.apply_connection(update["business_connection"])
            return []
        for key, kind in (("business_message", "message"),
                          ("edited_business_message", "edit"),
                          ("deleted_business_messages", "delete")):
            data = update.get(key)
            if not isinstance(data, dict):
                continue
            chat = data.get("chat")
            cid = data.get("business_connection_id")
            if not isinstance(chat, dict) or chat.get("type") != "private":
                return []
            chat_id = chat.get("id")
            if not self._allowed(cid, chat_id):
                return []
            uid = update["update_id"]
            if kind == "delete":
                ids = data.get("message_ids")
                if not isinstance(ids, list) or not all(_positive(x) for x in ids):
                    return []
                return [PersonalEvent("deletion", InboundEvent(
                    "telegram", cid, f"{uid}:{mid}", str(chat_id), str(chat_id),
                    str(mid), event_type="delete")) for mid in ids]
            sender = data.get("from")
            mid, date = data.get("message_id"), data.get("date")
            if not isinstance(sender, dict) or not _positive(mid) or not _positive(date):
                return []
            sender_id = sender.get("id")
            if not _positive(sender_id):
                return []
            if data.get("sender_business_bot") is not None:
                return []  # never feed our own bridge echoes to AI
            if sender_id == self.owner_id:
                role = "owner"
            elif sender_id == chat_id and sender.get("is_bot") is False:
                role = "customer"
            else:
                return []
            caption = data.get("caption")
            caption = caption if isinstance(caption, str) and caption.strip() else ""
            attachment = _attachment(data, caption)
            body = data.get("text")
            if not isinstance(body, str) or not body.strip():
                body = caption if attachment else ""
            if not (body or attachment):
                return []
            try:
                sender_name = " ".join(profile_name(sender).split())
            except ChannelError:
                sender_name = ""  # optional display metadata must not drop the message
            return [PersonalEvent(role, InboundEvent(
                "telegram", cid, str(uid), str(chat_id), str(sender_id), str(mid),
                text=body, sender_name=sender_name, event_type=kind, timestamp=float(date),
                attachments=(attachment,) if attachment else ()))]
        return []

    def send_payload(self, cid, chat_id, text, *, last_incoming_at, now):
        connection = self._allowed(cid, chat_id)
        if not connection or not connection.can_reply:
            raise ChannelError("forbidden")
        if not (type(last_incoming_at) in (int, float) and type(now) in (int, float)
                and 0 <= now - last_incoming_at < 86400):
            raise ChannelError("blocked_window")
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ChannelError("invalid_payload")
        return "sendMessage", {"business_connection_id": cid, "chat_id": chat_id, "text": text}

    def delete_payload(self, cid, chat_id, message_id, *, confirmed_sent):
        """Caller must resolve receipt from durable, account/chat-scoped outbox."""
        connection = self._allowed(cid, chat_id)
        if not connection or not connection.can_delete_sent:
            raise ChannelError("unsupported")
        if not _positive(message_id) or confirmed_sent != (cid, chat_id, message_id):
            raise ChannelError("forbidden")
        return "deleteBusinessMessages", {"business_connection_id": cid, "message_ids": [message_id]}

    @classmethod
    def poll_payload(cls, offset):
        if type(offset) is not int or offset < 0:
            raise ChannelError("invalid_cursor")
        return "getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": list(cls.allowed_updates)}
