"""Opt-in Telegram Business Bot API transport and acknowledged polling."""

import http.client
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

from .models import ChannelError
from .personal import Connection


class TelegramAPI:
    _METHODS = frozenset({
        "getMe", "getWebhookInfo", "getBusinessConnection", "getUpdates",
        "sendMessage", "deleteBusinessMessages", "setMySettings",
    })

    def __init__(self, token, *, proxy_url=None):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ChannelError("invalid_telegram_token")
        self._token = token
        self._proxy = None
        if proxy_url is not None:
            try:
                proxy = urlsplit(proxy_url)
                host = "127.0.0.1" if proxy.hostname == "localhost" else proxy.hostname
                address, port = ipaddress.ip_address(host), proxy.port
                private = (address.is_loopback or address.is_private
                           or address in ipaddress.ip_network("100.64.0.0/10"))
                if (proxy.scheme != "http" or not private or not port or proxy.username is not None
                        or proxy.password is not None or proxy.path not in ("", "/")
                        or proxy.query or proxy.fragment):
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise ChannelError("invalid_telegram_proxy") from None
            self._proxy = str(address), port

    def call(self, method, payload=None):
        if method not in self._METHODS:
            raise ChannelError("invalid_telegram_method")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ChannelError("invalid_payload")
        poll_timeout = payload.get("timeout", 0) if method == "getUpdates" else 0
        if type(poll_timeout) is not int or not 0 <= poll_timeout <= 50:
            raise ChannelError("invalid_poll_timeout")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeError):
            raise ChannelError("invalid_payload") from None
        connection = http.client.HTTPSConnection(*(self._proxy or ("api.telegram.org",)),
                                                  timeout=poll_timeout + 10)
        if self._proxy:
            connection.set_tunnel("api.telegram.org", 443)
        try:
            connection.request("POST", f"/bot{self._token}/{method}", body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(8_000_001)
            status = response.status
            if len(raw) > 8_000_000 or status >= 500 or 300 <= status < 400:
                raise ChannelError("delivery_unknown")
            if status in (401, 403):
                raise ChannelError("authorization_failed")
            if status == 409 and method == "getUpdates":
                raise ChannelError("telegram_poll_conflict")
            if status == 429:
                try:
                    delay = int(response.getheader("Retry-After") or 1)
                except (TypeError, ValueError):
                    delay = 1
                try:
                    data = json.loads(raw)
                    delay = int(data.get("parameters", {}).get("retry_after", delay))
                except (AttributeError, TypeError, ValueError):
                    pass
                raise ChannelError("rate_limited", retry_after=max(1, min(delay, 3600)))
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ChannelError("delivery_unknown")
            if data.get("ok") is False and 400 <= status < 500:
                raise ChannelError("platform_rejected")
            if status != 200 or data.get("ok") is not True or "result" not in data:
                raise ChannelError("delivery_unknown")
            return data["result"]
        except (OSError, http.client.HTTPException, ValueError):
            # Exception strings can contain the path, which includes the token.
            raise ChannelError("delivery_unknown") from None
        finally:
            connection.close()


class TelegramPoller:
    """One consumer per dedicated bot. Commit service writes before ack()."""

    # After a week of inactivity Telegram can issue a lower update ID.
    # Retained updates expire after 24 hours, so stop using an old offset early.
    _CURSOR_MAX_AGE = 5 * 86400

    def __init__(self, api, policy, store, connection_id, *, clock=time.time):
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ChannelError("invalid_connection")
        if not policy.allowed.difference({policy.owner_id}):
            raise ChannelError("empty_telegram_allowlist")
        self.api = api
        self.policy = policy
        self.store = store
        self.connection_id = connection_id
        self.clock = clock
        self._pending = None
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_business_cursor (
                connection_id TEXT PRIMARY KEY,
                next_offset INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )""")

    def refresh(self):
        """Fetch current Telegram authorization. Failed fetch clears local rights."""
        self.policy.connections.pop(self.connection_id, None)
        data = self.api.call("getBusinessConnection", {"business_connection_id": self.connection_id})
        connection = Connection.from_update(data)
        if connection.connection_id != self.connection_id or connection.owner_id != self.policy.owner_id:
            raise ChannelError("invalid_connection")
        self.policy.connections[self.connection_id] = connection
        return connection

    def poll(self, *, timeout=30):
        self._pending = None
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise ChannelError("invalid_poll_timeout")
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT next_offset, updated_at FROM telegram_business_cursor WHERE connection_id=?",
                (self.connection_id,),
            ).fetchone()
        age = self.clock() - row["updated_at"] if row else None
        offset = row["next_offset"] if age is not None and 0 <= age < self._CURSOR_MAX_AGE else 0
        method, payload = self.policy.poll_payload(offset)
        payload.update(timeout=timeout, limit=25)
        updates = self.api.call(method, payload)
        if not isinstance(updates, list) or any(
            not isinstance(update, dict) or type(update.get("update_id")) is not int
            or not 0 < update["update_id"] < 2**63 - 1 for update in updates
        ):
            raise ChannelError("invalid_telegram_updates")
        updates = sorted((update for update in updates if update["update_id"] >= offset),
                         key=lambda update: update["update_id"])
        ids = [update["update_id"] for update in updates]
        if len(set(ids)) != len(ids):
            raise ChannelError("invalid_telegram_updates")
        if not updates:
            return []
        current = self.refresh()
        events = []
        if current.enabled:
            for update in updates:
                # Telegram can return queued updates that precede allowed_updates.
                # Route only the configured business connection, regardless of queue.
                if "business_connection" in update:
                    continue
                for key in self.policy.allowed_updates[1:]:
                    data = update.get(key)
                    if isinstance(data, dict) and data.get("business_connection_id") == self.connection_id:
                        events.extend(self.policy.parse({"update_id": update["update_id"], key: data}))
                        break
        self._pending = ids[-1] + 1, self.clock()
        return events

    def ack(self):
        """Advance the durable offset after the caller has persisted the batch."""
        if self._pending is None:
            return
        with self.store.transaction() as db:
            db.execute("""INSERT INTO telegram_business_cursor
                (connection_id, next_offset, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                next_offset = excluded.next_offset, updated_at = excluded.updated_at""",
                       (self.connection_id, *self._pending))
        self._pending = None
