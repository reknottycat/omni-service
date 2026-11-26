"""Run a manually staffed Telegram Business and Feishu text pilot."""

import argparse
from dataclasses import replace
import fcntl
import getpass
import json
import os
from pathlib import Path
import queue
import re
import threading

from .feishu_live import FeishuLive
from .language import SimulationLanguage
from .local_translation import LocalTranslation
from .models import ChannelError
from .personal import Connection, TelegramPersonal
from .service import Service, _id
from .telegram_live import TelegramAPI, TelegramPoller


class PersonalBridge(Service):
    """Reuse the service inbox and outbox for one preauthorized session."""

    def __init__(self, db_path, api, feishu, *, owner, peer, connection_id, bot_id, **kwargs):
        if type(peer) is not int or peer <= 0 or peer == owner:
            raise ChannelError("invalid_test_contact")
        self.api = api
        self.feishu = feishu
        self.scope = connection_id, str(peer)
        super().__init__(db_path, None, members=sorted(feishu.staff_ids), **kwargs)
        try:
            policy = TelegramPersonal(expected_owner_id=owner, allowed_chat_ids=[peer])
            self.telegram = TelegramPoller(api, policy, self.store, connection_id, clock=self.clock)
            binding = json.dumps([bot_id, owner, peer, connection_id, feishu.app_id,
                                  feishu.chat_id, sorted(feishu.staff_ids)])
            with self.store.transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS personal_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1), binding TEXT NOT NULL)""")
                saved = db.execute("SELECT binding FROM personal_binding").fetchone()
                rows = db.execute("SELECT * FROM conversations").fetchall()
                if saved is None and not rows:
                    self.cid = _id("conv_")
                    db.execute("INSERT INTO personal_binding VALUES(1, ?)", (binding,))
                    db.execute("""INSERT INTO conversations
                        (id,channel,account_id,chat_id,user_id,recipient_type,mode,room_id,room_status,created_at)
                        VALUES(?,'telegram',?,?,?,'chat','manual',?,'ready',?)""",
                               (self.cid, *self.scope, str(peer), feishu.chat_id, int(self.clock())))
                    db.executemany("INSERT INTO members VALUES(?,?)",
                                   [(self.cid, staff) for staff in self.default_members])
                elif (saved is None or saved["binding"] != binding or len(rows) != 1
                      or (rows[0]["channel"], rows[0]["account_id"], rows[0]["chat_id"], rows[0]["room_id"])
                      != ("telegram", *self.scope, feishu.chat_id) or rows[0]["mode"] != "manual"):
                    raise ChannelError("personal_db_binding_mismatch")
                else:
                    self.cid = rows[0]["id"]
                self.started_at = self._conversation(db, self.cid)["created_at"]
        except BaseException:
            self.close()
            raise

    def receive_telegram(self, payload):
        event = payload.event
        if (event.channel != "telegram" or (event.account_id, event.chat_id) != self.scope
                or event.event_type != "message" or event.timestamp < self.started_at):
            return
        expected = self.scope[1] if payload.role == "customer" else str(self.telegram.policy.owner_id)
        if payload.role in ("customer", "owner") and event.user_id == expected:
            self.ingest(event)

    def receive_feishu(self, event):
        if (event.channel != "feishu" or event.account_id != self.feishu.app_id
                or event.chat_id != self.feishu.chat_id or event.user_id not in self.feishu.staff_ids
                or event.event_type != "message" or event.timestamp < self.started_at):
            return
        self.ingest(replace(event, sender_name="OmniService"))

    def _customer_event(self, db, event):
        if event.user_id != str(self.telegram.policy.owner_id):
            return super()._customer_event(db, event)
        conv = self._conversation(db, self.cid)
        self._cancel_ai(db, self.cid)
        duplicate = db.execute("""SELECT 1 FROM messages WHERE conversation_id=? AND
            source_channel='telegram' AND source_id=?""", (self.cid, event.message_id)).fetchone()
        if not duplicate:
            mid = self._message(db, self.cid, direction="outbound", source_channel="telegram",
                                source_id=event.message_id, author_id=event.user_id, text=event.text,
                                status="sent", language=self.language.detect(event.text, conv["language"]))
            self._queue(db, self.cid, "feishu", "send", "owner:" + mid,
                        {"text": "Telegram 本人已回复：" + event.text})
        return {"status": "owner_mirrored", "conversation_id": self.cid}

    def _request(self, db, item, conv):
        if (conv["id"] != self.cid or (conv["account_id"], conv["chat_id"]) != self.scope
                or conv["room_id"] != self.feishu.chat_id or item["operation"] != "send"):
            raise ChannelError("unsupported")
        return json.loads(item["payload"])["text"]

    def _deliver(self, item, conv, text):
        if item["channel"] == "feishu":
            # Revocation also blocks delivery of already queued Telegram content.
            connection = self.telegram.refresh()
            if not connection.enabled or not connection.can_reply:
                raise ChannelError("authorization_failed")
            return self.feishu.send(text, item["operation_key"])
        if item["channel"] != "telegram":
            raise ChannelError("invalid_channel")
        self.feishu.preflight()
        connection = self.telegram.refresh()
        method, payload = self.telegram.policy.send_payload(
            connection.connection_id, int(self.scope[1]), text,
            last_incoming_at=conv["last_customer_at"], now=self.clock())
        result = self.api.call(method, payload)
        chat = result.get("chat") if isinstance(result, dict) else None
        if (not isinstance(chat, dict) or chat.get("id") != payload["chat_id"]
                or result.get("business_connection_id") != payload["business_connection_id"]
                or type(result.get("message_id")) is not int or result["message_id"] <= 0):
            raise ChannelError("delivery_unknown")
        return str(result["message_id"])


def telegram_ready(api, username):
    me = api.call("getMe")
    if (not isinstance(me, dict) or me.get("is_bot") is not True or type(me.get("id")) is not int
            or not isinstance(me.get("username"), str) or me["username"].lower() != username.lower()):
        raise ChannelError("telegram_bot_identity_mismatch")
    hook = api.call("getWebhookInfo")
    if not isinstance(hook, dict) or hook.get("url") != "":
        raise ChannelError("telegram_webhook_in_use")
    return me


def discover_connections(api):
    # Telegram retains allowed_updates between calls. Keep business messages enabled
    # before the first test message arrives, and leave all updates unacknowledged.
    updates = api.call("getUpdates", {"timeout": 5,
                        "allowed_updates": list(TelegramPersonal.allowed_updates)})
    if not isinstance(updates, list):
        raise ChannelError("invalid_telegram_updates")
    connections = {}
    for entry in updates:
        if isinstance(entry, dict) and "business_connection" in entry:
            connection = Connection.from_update(entry["business_connection"])
            connections[connection.connection_id] = connection
    return [{"connection_id": c.connection_id, "owner_id": c.owner_id,
             "enabled": c.enabled, "can_reply": c.can_reply} for c in connections.values()]


def run(bridge):
    stop = threading.Event()
    events = queue.Queue(maxsize=64)

    def receive():
        while not stop.is_set():
            try:
                for event in bridge.feishu.events(stop):
                    while not stop.is_set():
                        try:
                            events.put(event, timeout=0.25)
                            break
                        except queue.Full:
                            continue
            except ChannelError as exc:
                print("Feishu:", exc.code, flush=True)
                stop.wait(3)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    try:
        print("OmniService Telegram–Feishu pilot started. Translation:",
              bridge.language.provider_name, flush=True)
        while not stop.is_set():
            try:
                for event in bridge.telegram.poll(timeout=2):
                    bridge.receive_telegram(event)
                bridge.telegram.ack()
            except ChannelError as exc:
                print("Telegram:", exc.code, flush=True)
                if exc.code == "telegram_poll_conflict":
                    raise
                stop.wait(exc.retry_after or 3)
                continue
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                bridge.receive_feishu(event)
            for outcome in bridge.drain():
                print("Delivery:", outcome["id"], outcome["status"], flush=True)
    finally:
        stop.set()
        thread.join(timeout=4)


def translation_provider(config):
    if config["mode"] == "fixtures":
        return SimulationLanguage()
    if config["mode"] != "private-model":
        raise ChannelError("invalid_translation_config")
    key_env = config.get("api_key_env")
    return LocalTranslation(config["base_url"], config["model"],
                            api_key=os.environ.get(key_env) if key_env else None,
                            timeout=config.get("timeout", 30))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "discover", "enable-secretary", "run"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--ask-token", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        telegram = config["telegram"]
        username = telegram["bot_username"]
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ChannelError("invalid_bot_username")
        token = getpass.getpass("Telegram bot token: ") if args.ask_token else os.environ.get(
            telegram["bot_token_env"])
        api = TelegramAPI(token)
        bot = telegram_ready(api, username)
        if args.action == "enable-secretary":
            if bot.get("can_connect_to_business") is not True:
                result = api.call("setMySettings", {"can_connect_to_business": True, "can_join_groups": False})
                if result is not True or telegram_ready(api, username).get("can_connect_to_business") is not True:
                    raise ChannelError("telegram_secretary_not_confirmed")
            print("Telegram Secretary Mode verified.")
            return 0
        if bot.get("can_connect_to_business") is not True:
            raise ChannelError("telegram_secretary_disabled")
        if args.action == "discover":
            print(json.dumps(discover_connections(api)))
            return 0
        feishu_config = config["feishu"]
        feishu = FeishuLive(feishu_config["chat_id"], feishu_config["staff_ids"],
                            app_id=feishu_config["app_id"], profile=feishu_config.get("profile"))
        feishu.preflight()
        connection = Connection.from_update(api.call("getBusinessConnection",
            {"business_connection_id": telegram["connection_id"]}))
        if (connection.connection_id != telegram["connection_id"]
                or connection.owner_id != telegram["expected_owner_id"]
                or not connection.enabled or not connection.can_reply):
            raise ChannelError("telegram_connection_not_ready")
        peers = telegram["allowed_chat_ids"]
        if (not isinstance(peers, list) or len(peers) != 1 or type(peers[0]) is not int
                or peers[0] <= 0 or peers[0] == connection.owner_id):
            raise ChannelError("invalid_test_contact")
        language = translation_provider(config["translation"])
        if args.action == "check":
            print("Telegram connection and Feishu group verified. Translation configuration:",
                  language.provider_name)
            return 0
        if config["mode"] != "personal-text-pilot" or args.db is None:
            raise ChannelError("pilot_mode_required")
        os.umask(0o077)
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with args.db.with_suffix(args.db.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ChannelError("pilot_already_running") from None
            bridge = PersonalBridge(
                args.db, api, feishu, owner=connection.owner_id, peer=peers[0],
                connection_id=connection.connection_id, bot_id=bot["id"],
                language=language)
            try:
                run(bridge)
            finally:
                bridge.close()
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, KeyError, ChannelError) as exc:
        print("Pilot:", getattr(exc, "code", "configuration_or_local_io_error"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
