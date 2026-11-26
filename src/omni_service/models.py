"""Values shared across the platform, service, and test boundaries."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Attachment:
    kind: str
    remote_id: str
    name: str = ""
    caption: str = ""
    url: str = ""


@dataclass(frozen=True)
class InboundEvent:
    channel: str
    account_id: str
    event_id: str
    chat_id: str
    user_id: str
    message_id: str
    text: str = ""
    sender_name: str = ""
    language_hint: str = ""
    event_type: str = "message"
    is_bot: bool = False
    timestamp: float = 0.0
    recipient_type: str = "chat"
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class ChannelError(Exception):
    def __init__(self, code: str, detail: str = "", retry_after: float | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code
        self.retry_after = retry_after
