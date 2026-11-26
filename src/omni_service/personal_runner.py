"""Run the Telegram connected-account and Feishu message bridge."""

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

from .admin_auth import FeishuAdminAuth
from .admin_web import create_admin_server
from .feishu_live import FeishuLive
from .intake import Intake
from .language import SimulationLanguage
from .local_translation import LocalTranslation
from .media import MEDIA_KINDS, normalize_blob, safe_filename
from .models import Attachment, ChannelError
from .personal import Connection, TelegramPersonal
from .service import Service, _id
from .telegram_live import TelegramAPI, TelegramPoller, enable_secretary, telegram_ready
from .welcome_templates import FeishuWelcomeDocument


def _relay_attachment(attachments):
    return (len(attachments) == 1 and attachments[0]['kind'] in MEDIA_KINDS
            and bool(attachments[0]['remote_id']))


class PersonalBridge(Service):
    """Reuse the durable service for explicitly authorized Telegram contacts."""

    def __init__(self, db_path, api, feishu, *, owner, connection_id, bot_id,
                 peer=None, peer_ids=None, connection_scope=False, **kwargs):
        self.managed = feishu.chat_id is None
        if self.managed:
            if peer is not None or not isinstance(peer_ids, (list, tuple)):
                raise ChannelError("invalid_test_contact")
            peers = tuple(peer_ids)
        else:
            if peer_ids is not None:
                raise ChannelError("invalid_test_contact")
            peers = (peer,)
        if (type(connection_scope) is not bool or (connection_scope and (not self.managed or peers))
                or (not connection_scope and not peers)
                or any(type(value) is not int or value <= 0 or value == owner for value in peers)
                or len(set(peers)) != len(peers)):
            raise ChannelError("invalid_test_contact")
        if self.managed and (kwargs.get("approval_intake") is not True
                             or kwargs.get("intake_enabled") is not True or kwargs.get("intake") is None):
            raise ChannelError("invalid_intake_config")
        self.api = api
        self.feishu = feishu
        self.connection_id = connection_id
        self.peer_ids = frozenset(map(str, peers))
        self.connection_scope = connection_scope
        self.owner_id = str(owner)
        super().__init__(db_path, None, members=sorted(feishu.staff_ids), label_replies=True, **kwargs)
        self.room_members_provider = getattr(feishu, "group_members", None)
        try:
            policy = TelegramPersonal(expected_owner_id=owner, allowed_chat_ids=peers, connection_scope=connection_scope)
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
        if self.connection_scope:
            binding['peer_scope'] = 'connection'
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

    def _peer_allowed(self, chat_id):
        return (chat_id in self.peer_ids or (self.connection_scope and chat_id != self.owner_id
                                            and re.fullmatch(r'[1-9][0-9]{0,18}', chat_id) is not None))

    def _valid_conversation(self, conv):
        return (conv["channel"] == "telegram" and conv["account_id"] == self.connection_id
                and self._peer_allowed(conv["chat_id"]) and conv["user_id"] == conv["chat_id"]
                and conv["recipient_type"] == "chat")

    def room_allowed(self, room_id):
        if not self.managed:
            return room_id == self.feishu.chat_id
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT * FROM conversations WHERE room_id=? AND room_status='ready'", (room_id,)).fetchone()
            return row is not None and self._valid_conversation(row)

    @property
    def management_enabled(self):
        with self.store.lock:
            return self.store.db.execute("SELECT 1 FROM management_config WHERE singleton=1").fetchone() is not None

    def _room_staff_member(self, chat_id, user_id):
        if not self.management_enabled:
            return user_id in self.feishu.staff_ids
        with self.store.lock:
            return self.store.db.execute("""SELECT 1 FROM members JOIN conversations
                ON conversations.id=members.conversation_id
                WHERE conversations.room_id=? AND members.user_id=?""", (chat_id, user_id)).fetchone() is not None

    def receive_telegram(self, payload):
        event = payload.event
        if (event.channel != "telegram" or event.account_id != self.connection_id or not self._peer_allowed(event.chat_id)
                or event.event_type != "message" or event.timestamp < self.started_at):
            return
        expected = event.chat_id if payload.role == "customer" else str(self.telegram.policy.owner_id)
        if payload.role in ("customer", "owner") and event.user_id == expected:
            return self.ingest(event)

    def receive_feishu(self, event):
        if (event.channel != "feishu" or event.account_id != self.feishu.app_id
                or event.event_type != "message"
                or event.timestamp < self.started_at or not self.room_allowed(event.chat_id)):
            return
        control = (event.text.lstrip().startswith(("/", "／")) and not event.has_mentions
                   and "@" not in event.text and "＠" not in event.text)
        if control and self.management_enabled:
            actual = self.feishu.group_members(event.chat_id)
            self.sync_room_members(event.chat_id, actual)
            if event.user_id not in actual:
                return
        elif not self._room_staff_member(event.chat_id, event.user_id):
            return
        return self.ingest(replace(event, sender_name="OmniService"))

    def receive_card_action(self, event):
        if (not self.approval_cards or event.app_id != self.feishu.app_id
                or not self.room_allowed(event.chat_id) or not self._room_staff_member(event.chat_id, event.user_id)):
            return
        return self.decide_draft(event)

    def admin_state(self, actor):
        state = super().admin_state(actor)
        if state['role'] == 'admin':
            current = self.telegram.policy.connections.get(self.connection_id)
            state['integrations'] = {
                'telegram': bool(current and current.enabled and current.can_reply),
                'chat_scope': 'connection' if self.connection_scope else 'allowlist',
                'feishu': True,
                'model': self.language.provider_name,
            }
        return state

    def _room_rules(self, db, cid):
        return (super()._room_rules(db, cid) + '\n普通图片和文件也会转发给客户。'
                '请将内部附件单独留在带 @ 的消息中。')

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
            label = self._source_label(db, cid, '同事') if self.approval_intake else '[人工客服] '
            self._queue(db, cid, "feishu", "send", "owner:" + mid,
                        {"text": label + "Telegram 本人已回复：" + event.text})
        return {"status": "owner_mirrored", "conversation_id": cid}

    def _mirror(self, db, conv, message):
        attachments = json.loads(message['attachments'])
        if message['source_channel'] != 'telegram' or not _relay_attachment(attachments):
            return super()._mirror(db, conv, message)
        if conv['room_status'] == 'absent' or message['status'] == 'deleted':
            return
        mid, cid = message['id'], conv['id']
        attachment = attachments[0]
        label = {'image': '图片', 'file': '文件', 'audio': '音频'}[attachment['kind']]
        name = safe_filename(attachment['name'], '')
        prefix = f'发来{label}' + (f' {name}' if name else '') + '\n'
        body = self._customer_mirror_body(db, conv, message, prefix)
        self._queue(db, cid, 'feishu', 'send', 'media-caption:' + mid, {'text': body})
        self._queue(db, cid, 'feishu', 'send', 'mirror:' + mid, {'relay_media': True}, mid)

    def _outgoing(self, db, conv, text, *, author='AI', source_channel='ai', source_id=None,
                  sender_name='', parent_id=None, already_translated=False, attachments=()):
        if source_channel != 'feishu' or not _relay_attachment(attachments):
            return super()._outgoing(db, conv, text, author=author, source_channel=source_channel,
                                     source_id=source_id, sender_name=sender_name, parent_id=parent_id,
                                     already_translated=already_translated, attachments=attachments)
        cid = conv['id']
        translated = ''
        if text:
            try:
                translated = self.language.translate(text, 'zh', conv['language'])
            except ChannelError as exc:
                if exc.code != 'translation_unavailable':
                    raise
        caption = self._display_reply(translated, conv['language'], author, sender_name) if translated else ''
        if len(caption.encode('utf-16-le')) > 2048:
            caption = ''
        mid = self._message(db, cid, direction='outbound', source_channel=source_channel, source_id=source_id,
                            author_id=author, sender_name=sender_name, text=text, translated=translated,
                            language=conv['language'], kind='media', attachments=attachments, status='queued',
                            parent_id=parent_id)
        self._queue(db, cid, 'telegram', 'send', 'out:' + mid, {'text': caption, 'relay_media': True}, mid)
        if text and not caption:
            self._notify(db, cid, 'media-caption:' + mid, '附件说明无法随文件发送，请另发文字。')
        return mid, 'queued'

    def _request(self, db, item, conv):
        if not self._valid_conversation(conv):
            raise ChannelError("unsupported")
        if not self.managed and (conv["id"] != self.cid or conv["room_id"] != self.feishu.chat_id):
            raise ChannelError("unsupported")
        channel, operation = item["channel"], item["operation"]
        payload = json.loads(item["payload"])
        management = self._management(db) is not None
        if management or (self.managed and channel == "feishu" and operation == "create_room"):
            members = frozenset(row[0] for row in db.execute(
                "SELECT user_id FROM members WHERE conversation_id=?", (conv["id"],)))
        if management:
            payload["feishu_staff_ids"] = members
        if self.managed and channel == "feishu" and operation == "create_room":
            if (conv["room_id"] is None and conv["room_status"] == "pending"
                    and (management or members == self.feishu.staff_ids)):
                return payload
        if (operation == "send" and channel in ("telegram", "feishu")
                or operation == 'update_card' and channel == 'feishu'):
            if conv["room_id"] is not None and conv["room_status"] == "ready":
                if payload.get('relay_media') is True:
                    source, direction = ('telegram', 'inbound') if channel == 'feishu' else ('feishu', 'outbound')
                    message = db.execute('''SELECT source_id, attachments FROM messages WHERE id=?
                        AND conversation_id=? AND source_channel=? AND direction=? AND kind='media'
                        AND status NOT IN ('deleted','cancelled')''',
                                         (item['message_id'], conv['id'], source, direction)).fetchone()
                    attachments = json.loads(message['attachments']) if message is not None else []
                    if not _relay_attachment(attachments):
                        raise ChannelError('unsupported_media')
                    payload['media_source_id'] = message['source_id']
                    payload['relay_attachment'] = Attachment(**attachments[0])
                return payload
            if self.managed and channel == "telegram" and payload.get("mandatory_welcome") is True:
                message = db.execute("SELECT author_id,source_channel FROM messages WHERE id=? AND conversation_id=?",
                                     (item["message_id"], conv["id"])).fetchone()
                if message is not None and (message["author_id"], message["source_channel"]) == ("AI", "ai_welcome"):
                    return payload
        raise ChannelError("unsupported")

    def _failure(self, db, item, exc):
        super()._failure(db, item, exc)
        if item['channel'] == 'feishu' and json.loads(item['payload']).get('relay_media') is True:
            row = db.execute('SELECT status FROM outbox WHERE id=?', (item['id'],)).fetchone()
            if row['status'] == 'failed':
                self._notify(db, item['conversation_id'], 'media-failed:' + str(item['id']),
                             '客户附件未能传入飞书。错误 ' + exc.code + '。')

    def _deliver(self, item, conv, request):
        # Revocation also blocks delivery of already queued Telegram content.
        connection = self.telegram.refresh()
        if not connection.enabled or not connection.can_reply:
            raise ChannelError("authorization_failed")
        staff_options = {"staff_ids": request["feishu_staff_ids"]} if "feishu_staff_ids" in request else {}
        attachment = request.get('relay_attachment')
        if item["channel"] == "feishu":
            if item["operation"] == "create_room":
                room = self.feishu.create_room(request["name"], item["operation_key"], **staff_options)
                with self.store.lock:
                    if self.store.db.execute("SELECT 1 FROM conversations WHERE room_id=?", (room,)).fetchone():
                        raise ChannelError("delivery_unknown")
                return room
            options = dict(staff_options)
            if self.managed:
                options["chat_id"] = conv["room_id"]
            if attachment is not None:
                blob = self.api.download_attachment(attachment.remote_id, kind=attachment.kind,
                                                    filename=attachment.name)
                return self.feishu.send_attachment(attachment.kind, normalize_blob(blob, attachment.kind),
                                                  item['operation_key'], **options)
            if "card" in request:
                if not self.approval_cards:
                    raise ChannelError("approval_cards_disabled")
                if item['operation'] == 'update_card':
                    return self.feishu.patch_card(request['card_message_id'], request['card'], **options)
                return self.feishu.send_card(request["card"], item["operation_key"], **options)
            if request.get("mention_all") is True:
                options["mention_all"] = True
            if "mention_user_id" in request:
                options["mention_user_id"] = request["mention_user_id"]
            return self.feishu.send(request["text"], item["operation_key"], **options)
        if attachment is None:  # Media downloads validate the Feishu source room and roster themselves.
            if self.managed and conv["room_id"] is not None:
                self.feishu.preflight(conv["room_id"], **staff_options)
            elif not self.managed:
                self.feishu.preflight(**staff_options)
        text, photo = request["text"], request.get("welcome_photo")
        method, payload = self.telegram.policy.send_payload(
            connection.connection_id, int(conv["chat_id"]), (text or ".") if photo or attachment else text,
            last_incoming_at=conv["last_customer_at"], now=self.clock())
        if attachment is not None:
            blob = self.feishu.download_attachment(conv['room_id'], request['media_source_id'], attachment,
                                                   **staff_options)
            result = self.api.send_attachment(payload['business_connection_id'], payload['chat_id'], attachment.kind,
                                              normalize_blob(blob, attachment.kind), caption=text)
        elif photo is not None:
            result = self.api.send_welcome_photo(payload["business_connection_id"], payload["chat_id"], photo,
                                                 caption=text)
        else:
            if "welcome_html" in request:
                payload.update(text=request["welcome_html"], parse_mode="HTML")
            result = self.api.call(method, payload)
        chat = result.get("chat") if isinstance(result, dict) else None
        if (not isinstance(chat, dict) or chat.get("id") != payload["chat_id"]
                or result.get("business_connection_id") != payload["business_connection_id"]
                or type(result.get("message_id")) is not int or result["message_id"] <= 0):
            raise ChannelError("delivery_unknown")
        return str(result["message_id"])


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


def run(bridge, *, admin_server=None):
    stop = threading.Event()
    events = queue.Queue(maxsize=64)

    def receive(source, consumer, options):
        while not stop.is_set():
            try:
                for event in consumer(stop, **options):
                    while not stop.is_set():
                        try:
                            events.put((source, event), timeout=0.25)
                            break
                        except queue.Full:
                            continue
            except ChannelError as exc:
                print(f"Feishu {source}:", exc.code, flush=True)
                stop.wait(3)

    managed_membership = bridge.management_enabled
    message_options = {"room_allowed": bridge.room_allowed} if bridge.managed or managed_membership else {}
    card_options = {"room_allowed": bridge.room_allowed}
    if managed_membership:
        message_options["managed_membership"] = card_options["managed_membership"] = True
    consumers = [("message", bridge.feishu.events, message_options)]
    if bridge.approval_cards:
        consumers.append(("card", bridge.feishu.card_events, card_options))
    threads = [threading.Thread(target=receive, args=consumer, daemon=True) for consumer in consumers]
    admin_thread = None
    try:
        for thread in threads:
            thread.start()
        if admin_server is not None:
            admin_thread = threading.Thread(target=admin_server.serve_forever, name="omni-admin")
            admin_thread.start()
            print("OmniService management web started.", flush=True)
        print("OmniService Telegram–Feishu bridge started. Translation:", bridge.language.provider_name,
              "Intake:", "on" if bridge.intake_enabled else "off",
              "Approval cards:", "on" if bridge.approval_cards else "off",
              "Agent default:", "on" if bridge.agent_enabled else "off",
              "Customer prompt guard:", "on" if bridge.customer_prompt_guard else "off", flush=True)
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
                    source, event = events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if source == "card":
                        bridge.receive_card_action(event)
                    else:
                        bridge.receive_feishu(event)
                except ChannelError as exc:
                    print(f"Feishu {source}:", exc.code, flush=True)
            for outcome in bridge.drain():
                print("Delivery:", outcome["id"], outcome["status"], flush=True)
    finally:
        stop.set()
        if admin_thread is not None and admin_thread.ident is not None:
            admin_server.shutdown()
            admin_thread.join()
        for thread in threads:
            if thread.ident is not None:
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


def customer_security_setting(config):
    if not isinstance(config, dict) or type(config.get("customer_prompt_guard", True)) is not bool:
        raise ChannelError("invalid_customer_security_config")
    return config.get("customer_prompt_guard", True)


def agent_provider(config, language, *, approval_intake):
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ChannelError("invalid_agent_config")
    enabled = config.get("enabled", False)
    if not approval_intake:
        if enabled:
            raise ChannelError("invalid_agent_config")
        return None, False
    advisor = getattr(language, "suggest_reply", None)
    if not callable(advisor):
        raise ChannelError("invalid_agent_config")
    return advisor, enabled


def management_providers(config, feishu):
    if config is None:
        return None
    if not isinstance(config, dict) or type(config.get("enabled", True)) is not bool:
        raise ChannelError("invalid_management_config")
    if not config.get("enabled", True):
        return None
    admins = config.get("admin_ids")
    host, port = config.get("bind_host", "127.0.0.1"), config.get("port", 8081)
    document = config.get("welcome_document", "")
    secret_env, signing_env = config.get("app_secret_env"), config.get("signing_key_env")
    if (not isinstance(admins, list) or not 1 <= len(admins) <= 50
            or not all(isinstance(admin, str) and re.fullmatch(r"ou_[A-Za-z0-9_]{1,125}", admin)
                       for admin in admins)
            or len(set(admins)) != len(admins) or not isinstance(host, str)
            or re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) is None
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(document, str) or len(document) > 1500
            or any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                   for name in (secret_env, signing_env)) or secret_env == signing_env):
        raise ChannelError("invalid_management_config")
    identity = FeishuAdminAuth(app_id=feishu.app_id, app_secret=os.environ.get(secret_env),
                               tenant_key=config.get("tenant_key"), callback_url=config.get("callback_url"),
                               signing_key=os.environ.get(signing_env))
    return {"identity": identity, "admin_ids": admins, "host": host, "port": port, "welcome_document": document}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "discover", "enable-secretary", "run"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--ask-token", action="store_true")
    args = parser.parse_args(argv)
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
            enable_secretary(api, bot)
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
        cards_configured = feishu_config.get("approval_cards", managed)
        if type(cards_configured) is not bool:
            raise ChannelError("feishu_invalid_config")
        room = feishu_config.get("chat_id")
        if (managed and room not in (None, "")) or (not managed and not room):
            raise ChannelError("feishu_invalid_config")
        staff_ids = feishu_config["staff_ids"]
        excluded_env = frozenset(filter(None, (telegram['bot_token_env'],
            config['translation'].get('api_key_env'), (config.get('llm') or {}).get('api_key_env'),
            (config.get('management') or {}).get('signing_key_env'),
            (config.get('management') or {}).get('app_secret_env'))))
        feishu = FeishuLive(None if managed else room, staff_ids,
                            app_id=feishu_config["app_id"], profile=feishu_config.get("profile"),
                            allowed_bot_app_ids=feishu_config.get("allowed_bot_app_ids", []), excluded_env=excluded_env)
        handoff_staff_id = feishu_config.get("handoff_staff_id")
        if handoff_staff_id is None:
            handoff_staff_id = staff_ids[0]
        if not isinstance(handoff_staff_id, str) or handoff_staff_id not in feishu.staff_ids:
            raise ChannelError("invalid_handoff_staff")
        feishu.preflight()
        connection = Connection.from_update(api.call("getBusinessConnection",
            {"business_connection_id": telegram["connection_id"]}))
        if (connection.connection_id != telegram["connection_id"]
                or connection.owner_id != telegram["expected_owner_id"]
                or not connection.enabled or not connection.can_reply):
            raise ChannelError("telegram_connection_not_ready")
        peers = telegram["allowed_chat_ids"]
        scope_mode = telegram.get('peer_scope', 'allowlist')
        if scope_mode not in ('allowlist', 'connection'):
            raise ChannelError('invalid_telegram_scope')
        connection_scope = scope_mode == 'connection'
        if (not isinstance(peers, list) or (connection_scope and (not managed or peers))
                or (not connection_scope and (not peers or (not managed and len(peers) != 1)))
                or any(type(peer) is not int or peer <= 0 or peer == connection.owner_id for peer in peers)
                or len(set(peers)) != len(peers)):
            raise ChannelError("invalid_test_contact")
        preview_peers = telegram.get("welcome_preview_chat_ids", [])
        if (not isinstance(preview_peers, list)
                or any(type(peer) is not int or peer not in peers for peer in preview_peers)
                or len(set(preview_peers)) != len(preview_peers)):
            raise ChannelError("invalid_welcome_preview_contact")
        management = management_providers(config.get("management"), feishu)
        language = translation_provider(config["translation"])
        intake, intake_enabled = intake_provider(config.get("intake", {}), language)
        intake_enabled = managed or intake_enabled
        approval_cards = cards_configured and intake_enabled
        advisor, agent_enabled = agent_provider(config.get("agent", {}), language, approval_intake=intake_enabled)
        prompt_guard = customer_security_setting(config.get("security", {}))
        if args.action == "check":
            audience = "Feishu application identity" if managed else "Feishu group"
            print("Telegram connection and", audience, "verified. Translation:", language.provider_name,
                  "Intake:", "on" if intake_enabled else "off", "Approval cards:", "on" if approval_cards else "off",
                  "Agent default:", "on" if agent_enabled else "off",
                  "Customer prompt guard:", "on" if prompt_guard else "off",
                  "Management configuration:", "present" if management else "disabled")
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
                approval_intake=intake_enabled, approval_cards=approval_cards, agent_advisor=advisor,
                agent_enabled=agent_enabled, handoff_staff_id=handoff_staff_id, customer_prompt_guard=prompt_guard,
                connection_scope=connection_scope)
            bridge.telegram.policy.connections[connection.connection_id] = connection
            try:
                if not intake_enabled:
                    bridge.set_intake_enabled(False)
                if management is None:
                    run(bridge)
                else:
                    templates = FeishuWelcomeDocument(feishu.app_id, profile=feishu_config.get("profile"),
                                                     excluded_env=excluded_env)
                    with create_admin_server(bridge, management["identity"], templates,
                                             host=management["host"], port=management["port"],
                                             invite_delivery=feishu.send_staff_invite) as server:
                        bridge.configure_management(management["admin_ids"],
                                                    welcome_document=management["welcome_document"],
                                                    preview_peers=preview_peers)
                        run(bridge, admin_server=server)
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
