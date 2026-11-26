"""Optional one-click creation through the official Feishu SDK."""

import asyncio
import re
from urllib.parse import urlsplit

from .models import ChannelError


_APP_ID = re.compile(r"cli_[A-Za-z0-9_]{1,125}")
_APP_SECRET = re.compile(r"[!-~]{1,512}")
_FEISHU_REGISTRATION_HOSTS = {"accounts.feishu.cn", "open.feishu.cn", "applink.feishu.cn"}
BRIDGE_APP_SCOPES = (
    "im:chat:create", "im:chat:read", "im:chat.members:read", "im:message.group_msg",
    "im:message:readonly", "im:message:send_as_bot", "im:resource",
)


def provision_feishu(*, show_verification_url, app_name="OmniService"):
    """Return this new app's ID and secret after its owner confirms in Feishu."""
    try:
        from lark_oapi import aregister_app
    except ImportError:
        raise ChannelError("feishu_provision_sdk_missing") from None

    def display(info):
        url = info.get("url") if isinstance(info, dict) else None
        if not isinstance(url, str) or not re.fullmatch(r"[!-~]{1,16384}", url):
            raise ChannelError("feishu_provision_invalid_verification")
        target = urlsplit(url)
        if (target.scheme != "https" or target.netloc not in _FEISHU_REGISTRATION_HOSTS
                or not target.path.startswith("/") or "\\" in url):
            raise ChannelError("feishu_provision_invalid_verification")
        show_verification_url(url)

    try:
        registration = aregister_app(
            on_qr_code=display, create_only=True, source="omniservice",
            app_preset={"name": app_name, "desc": "在飞书客服群处理 Telegram 客户消息与附件"},
            addons={"preset": False,
                    "scopes": {"tenant": list(BRIDGE_APP_SCOPES)},
                    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
                    "callbacks": {"items": ["card.action.trigger"]}},
        )
        result = asyncio.run(asyncio.wait_for(registration, timeout=720))
    except ChannelError:
        raise
    except Exception:
        # Vendor failures can contain a device code, verification URL, or secret.
        raise ChannelError("feishu_provision_unavailable") from None

    app_id = result.get("client_id") if isinstance(result, dict) else None
    secret = result.get("client_secret") if isinstance(result, dict) else None
    if (not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id)
            or not isinstance(secret, str) or not _APP_SECRET.fullmatch(secret)):
        raise ChannelError("feishu_provision_invalid_credentials")
    return app_id, secret
