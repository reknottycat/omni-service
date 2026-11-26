"""Run the Telegram connected-account and Feishu text bridge."""

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
from .intake import Intake
from .language import SimulationLanguage
from .local_translation import LocalTranslation
from .models import ChannelError
from .personal import Connection, TelegramPersonal
from .service import Service, _id
from .telegram_live import TelegramAPI, TelegramPoller


class PersonalBridge(Service):
    """Reuse the durable service for explicitly authorized Telegram contacts."""

    def __init__(self, db_path, api, feishu, *, owner, connection_id, bot_id,
                 peer=None, peer_ids=None, **kwargs):
        self.managed = feishu.chat_id is None
        if self.managed:
            if peer is not None or not isinstance(peer_ids, (list, tuple)):
                raise ChannelError("invalid_test_contact")
            peers = tuple(peer_ids)
        else:
            if peer_ids is not None:
                raise ChannelError("invalid_test_contact")
            peers = (peer,)
        if (not peers or any(type(value) is not int or value <= 0 or value == owner for value in peers)
                or len(set(peers)) != len(peers)):
            raise ChannelError("invalid_test_contact")
        if self.managed and (kwargs.get("approval_intake") is not True
                             or kwargs.get("intake_enabled") is not True or kwargs.get("intake") is None):
            raise ChannelError("invalid_intake_config")
        self.api = api
        self.feishu = feishu
        self.connection_id = connection_id
        self.peer_ids = frozenset(map(str, peers))
        super().__init__(db_path, None, members=sorted(feishu.staff_ids), label_replies=True, **kwargs)
        try:
            policy = TelegramPersonal(expected_owner_id=owner, allowed_chat_ids=peers)
            self.telegram = TelegramPoller(api, policy, self.store, connection_id, clock=self.clock)
            with self.store.transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS personal_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1), binding TEXT NOT NULL)""")
                saved = db.execute("SELECT binding FROM personal_binding").fetchone()
                rows = db.execute("SELECT * FROM conversations").fetchall()
                if self.managed:
                    self._bind_managed(db, saved, rows, owner, bot_id, peers)
                else:
                    self._bind_pinned(db, saved, rows, owner, bot_id, peer)
        except BaseException:
            self.close()
            raise

    def _bind_managed(self, db, saved, rows, owner, bot_id, peers):
        binding = {
            "mode": "personal-managed", "bot_id": bot_id, "owner_id": owner,
            "peers": sorted(peers), "connection_id": self.connection_id, "app_id": self.feishu.app_id,
            "staff_ids": sorted(self.feishu.staff_ids), "bot_app_ids": sorted(self.feishu.bot_app_ids),
        }
        if saved is None and not rows:
            self.started_at = int(self.clock())
            db.execute("INSERT INTO personal_binding VALUES(1, ?)",
                       (json.dumps({**binding, "started_at": self.started_at}),))
            return
        record = json.loads(saved["binding"]) if saved is not None else None
        self.started_at = record.pop("started_at", None) if isinstance(record, dict) else None
        if (record != binding or type(self.started_at) is not int or self.started_at < 0
                or any(not self._valid_conversation(row) for row in rows)):
            raise ChannelError("personal_db_binding_mismatch")

    def _bind_pinned(self, db, saved, rows, owner, bot_id, peer):
        binding = json.dumps([bot_id, owner, peer, self.connection_id, self.feishu.app_id,
                              self.feishu.chat_id, sorted(self.feishu.staff_ids)])
        if saved is None and not rows:
            self.cid = _id("conv_")
            self.started_at = int(self.clock())
            db.execute("INSERT INTO personal_binding VALUES(1, ?)", (binding,))
            db.execute("""INSERT INTO conversations
                (id,channel,account_id,chat_id,user_id,recipient_type,mode,room_id,room_status,created_at)
                VALUES(?,'telegram',?,?,?,'chat','manual',?,'ready',?)""",
                       (self.cid, self.connection_id, str(peer), str(peer), self.feishu.chat_id, self.started_at))
            db.executemany("INSERT INTO members VALUES(?,?)", [(self.cid, staff) for staff in self.default_members])
        elif (saved is None or saved["binding"] != binding or len(rows) != 1
              or not self._valid_conversation(rows[0]) or rows[0]["room_id"] != self.feishu.chat_id):
            raise ChannelError("personal_db_binding_mismatch")
        else:
            self.cid = rows[0]["id"]
            self.started_at = rows[0]["created_at"]

    def _valid_conversation(self, conv):
        return (conv["channel"] == "telegram" and conv["account_id"] == self.connection_id
                and conv["chat_id"] in self.peer_ids and conv["user_id"] == conv["chat_id"]
                and conv["recipient_type"] == "chat")

    def room_allowed(self, room_id):
        if not self.managed:
            return room_id == self.feishu.chat_id
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT * FROM conversations WHERE room_id=? AND room_status='ready'", (room_id,)).fetchone()
            return row is not None and self._valid_conversation(row)

    def receive_telegram(self, payload):
        event = payload.event
        if (event.channel != "telegram" or event.account_id != self.connection_id or event.chat_id not in self.peer_ids
                or event.event_type != "message" or event.timestamp < self.started_at):
            return
        expected = event.chat_id if payload.role == "customer" else str(self.telegram.policy.owner_id)
        if payload.role in ("customer", "owner") and event.user_id == expected:
            return self.ingest(event)

    def receive_feishu(self, event):
        if (event.channel != "feishu" or event.account_id != self.feishu.app_id
                or event.user_id not in self.feishu.staff_ids or event.event_type != "message"
                or event.timestamp < self.started_at or not self.room_allowed(event.chat_id)):
            return
        return self.ingest(replace(event, sender_name="OmniService"))

    def _customer_event(self, db, event):
        if event.user_id != str(self.telegram.policy.owner_id):
            return super()._customer_event(db, event)
        conv = db.execute("""SELECT * FROM conversations WHERE channel='telegram'
            AND account_id=? AND chat_id=?""", (event.account_id, event.chat_id)).fetchone()
        if conv is None or not conv["last_customer_at"]:
            return {"status": "owner_unbound"}
        cid = conv["id"]
        duplicate = db.execute("""SELECT 1 FROM messages WHERE conversation_id=? AND
            source_channel='telegram' AND source_id=?""", (cid, event.message_id)).fetchone()
        if not duplicate:
            self._cancel_ai(db, cid)
            self._finish_intake(db, cid, 'owner_reply')
            db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (cid,))
            mid = self._message(db, cid, direction="outbound", source_channel="telegram",
                                source_id=event.message_id, author_id=event.user_id, text=event.text,
                                status="sent", language=self.language.detect(event.text, conv["language"]))
            self._queue(db, cid, "feishu", "send", "owner:" + mid,
                        {"text": "[人工客服] Telegram 本人已回复：" + event.text})
        return {"status": "owner_mirrored", "conversation_id": cid}

    def _request(self, db, item, conv):
        if not self._valid_conversation(conv):
            raise ChannelError("unsupported")
        if not self.managed and (conv["id"] != self.cid or conv["room_id"] != self.feishu.chat_id):
            raise ChannelError("unsupported")
        channel, operation = item["channel"], item["operation"]
        payload = json.loads(item["payload"])
        if self.managed and channel == "feishu" and operation == "create_room":
            members = {row[0] for row in db.execute(
                "SELECT user_id FROM members WHERE conversation_id=?", (conv["id"],))}
            if conv["room_id"] is None and conv["room_status"] == "pending" and members == self.feishu.staff_ids:
                return payload
        if operation == "send" and channel in ("telegram", "feishu"):
            if conv["room_id"] is not None and conv["room_status"] == "ready":
                return payload
            if self.managed and channel == "telegram" and payload.get("mandatory_welcome") is True:
                message = db.execute("SELECT author_id,source_channel FROM messages WHERE id=? AND conversation_id=?",
                                     (item["message_id"], conv["id"])).fetchone()
                if message is not None and (message["author_id"], message["source_channel"]) == ("AI", "ai_welcome"):
                    return payload
        raise ChannelError("unsupported")

    def _deliver(self, item, conv, request):
        # Revocation also blocks delivery of already queued Telegram content.
        connection = self.telegram.refresh()
        if not connection.enabled or not connection.can_reply:
            raise ChannelError("authorization_failed")
        if item["channel"] == "feishu":
            if item["operation"] == "create_room":
                room = self.feishu.create_room(request["name"], item["operation_key"])
                with self.store.lock:
                    if self.store.db.execute("SELECT 1 FROM conversations WHERE room_id=?", (room,)).fetchone():
                        raise ChannelError("delivery_unknown")
                return room
            options = {"chat_id": conv["room_id"]} if self.managed else {}
            if request.get("mention_all") is True:
                options["mention_all"] = True
            return self.feishu.send(request["text"], item["operation_key"], **options)
        if self.managed and conv["room_id"] is not None:
            self.feishu.preflight(conv["room_id"])
        elif not self.managed:
            self.feishu.preflight()
        method, payload = self.telegram.policy.send_payload(
            connection.connection_id, int(conv["chat_id"]), request["text"],
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
        options = {"room_allowed": bridge.room_allowed} if bridge.managed else {}
        while not stop.is_set():
            try:
                for event in bridge.feishu.events(stop, **options):
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
        print("OmniService Telegram–Feishu bridge started. Translation:", bridge.language.provider_name,
              "Intake:", "on" if bridge.intake_enabled else "off",
              "Agent:", "on" if bridge.agent_advisor is not None else "off", flush=True)
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


def intake_provider(config, language):
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ChannelError("invalid_intake_config")
    simulated = isinstance(language, SimulationLanguage)
    extract = (lambda _text, _fields: {}) if simulated else language.extract_intake
    return Intake(extract, max_replies=config.get("max_replies", 5), simulated=simulated), config.get("enabled", False)


def agent_provider(config, language):
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ChannelError("invalid_agent_config")
    if not config.get("enabled", False):
        return None
    if isinstance(language, SimulationLanguage):
        return lambda _messages: "您好，我来帮您处理。"
    advisor = getattr(language, "suggest_reply", None)
    if not callable(advisor):
        raise ChannelError("invalid_agent_config")
    return advisor


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
        tunnel_port = os.environ.get("TELEGRAM_TLS_TUNNEL_PORT")
        api = TelegramAPI(token, proxy_url=os.environ.get("TELEGRAM_HTTPS_PROXY"),
                          tunnel_port=int(tunnel_port) if tunnel_port is not None else None)
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
        mode = config.get("mode")
        if mode not in ("personal-text-pilot", "personal-managed") or (args.action == "run" and args.db is None):
            raise ChannelError("pilot_mode_required")
        managed = mode == "personal-managed"
        feishu_config = config["feishu"]
        room = feishu_config.get("chat_id")
        if (managed and room not in (None, "")) or (not managed and not room):
            raise ChannelError("feishu_invalid_config")
        feishu = FeishuLive(None if managed else room, feishu_config["staff_ids"],
                            app_id=feishu_config["app_id"], profile=feishu_config.get("profile"),
                            allowed_bot_app_ids=feishu_config.get("allowed_bot_app_ids", []))
        feishu.preflight()
        connection = Connection.from_update(api.call("getBusinessConnection",
            {"business_connection_id": telegram["connection_id"]}))
        if (connection.connection_id != telegram["connection_id"]
                or connection.owner_id != telegram["expected_owner_id"]
                or not connection.enabled or not connection.can_reply):
            raise ChannelError("telegram_connection_not_ready")
        peers = telegram["allowed_chat_ids"]
        if (not isinstance(peers, list) or not peers or (not managed and len(peers) != 1)
                or any(type(peer) is not int or peer <= 0 or peer == connection.owner_id for peer in peers)
                or len(set(peers)) != len(peers)):
            raise ChannelError("invalid_test_contact")
        language = translation_provider(config["translation"])
        intake, intake_enabled = intake_provider(config.get("intake", {}), language)
        intake_enabled = managed or intake_enabled
        advisor = agent_provider(config.get("agent", {}), language)
        if advisor is not None and not intake_enabled:
            raise ChannelError("invalid_agent_config")
        if args.action == "check":
            audience = "Feishu application identity" if managed else "Feishu group"
            print("Telegram connection and", audience, "verified. Translation:", language.provider_name,
                  "Intake:", "on" if intake_enabled else "off", "Agent:", "on" if advisor is not None else "off")
            return 0
        os.umask(0o077)
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with args.db.with_suffix(args.db.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ChannelError("pilot_already_running") from None
            scope = {"peer_ids": peers} if managed else {"peer": peers[0]}
            bridge = PersonalBridge(
                args.db, api, feishu, owner=connection.owner_id, **scope,
                connection_id=connection.connection_id, bot_id=bot["id"],
                language=language, intake=intake, intake_enabled=intake_enabled,
                approval_intake=intake_enabled, agent_advisor=advisor)
            try:
                if not intake_enabled:
                    bridge.set_intake_enabled(False)
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
