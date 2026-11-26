"""Durable opt-in bridge for an existing Telegram account and a Feishu app.

Callbacks store only normalized, authorized events. Model work happens before
transactions in the worker; network mutations are delivered through the outbox.
No passwords, tokens, raw denied updates or model credentials enter this store.
"""

from collections import OrderedDict
from dataclasses import asdict, replace
import hashlib
import json
import threading
import time
import uuid

from . import channels
from .language import SimulationLanguage
from .models import Attachment, ChannelError, InboundEvent, Request
from .personal import Connection, TelegramPersonal
from .service import Service, _json, _id


SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_connections (
 account_key TEXT NOT NULL, connection_id TEXT NOT NULL, owner_id INTEGER NOT NULL,
 enabled INTEGER NOT NULL, can_reply INTEGER NOT NULL, can_delete_sent INTEGER NOT NULL,
 updated_at REAL NOT NULL, PRIMARY KEY(account_key,connection_id)
);
CREATE TABLE IF NOT EXISTS personal_cursor (
 account_key TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
 epoch INTEGER NOT NULL DEFAULT 0, last_update_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS personal_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
 account_id TEXT NOT NULL, event_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 role TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', last_error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, UNIQUE(channel,account_id,event_id)
);
CREATE INDEX IF NOT EXISTS personal_queue_pending ON personal_queue(status,id);
"""


class _Stopping(Exception):
    pass


class _PreparedLanguage:
    """A bounded cache consumed inside transactions; it never performs I/O."""
    def __init__(self, provider):
        self.provider = provider
        self.provider_name = getattr(provider, 'provider_name', 'configured-local-model')
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def warm(self, method, *args, force=False):
        key = (method, *args)
        with self.lock:
            cached = self.cache.get(key)
            negative = (isinstance(cached, ChannelError) or
                        (method == 'detect' and cached == 'und') or
                        (method == 'reply' and isinstance(cached, dict) and cached.get('action') == 'handoff'))
            if not force and key in self.cache and not negative:
                return self._get(method, *args)
        try:
            result = getattr(self.provider, method)(*args)
        except ChannelError as exc:
            result = ChannelError(exc.code)
        with self.lock:
            self.cache[key] = result
            self.cache.move_to_end(key)
            while len(self.cache) > 4096:
                self.cache.popitem(last=False)
        return self._get(method, *args)

    def _get(self, method, *args):
        key = (method, *args)
        with self.lock:
            value = self.cache.get(key)
        if isinstance(value, ChannelError):
            if method == 'reply':
                return {'action': 'handoff', 'text': ''}
            if method == 'detect':
                return args[1] or 'und'
            raise ChannelError('translation_unavailable')
        if value is not None:
            return value
        if method == 'reply':
            return {'action': 'handoff', 'text': ''}
        if method == 'detect':
            return args[1] or 'und'
        if args[1] == args[2] and args[2] != 'und':
            return args[0]
        if not args[0] or not any(c.isalpha() for c in args[0]):
            return args[0]
        raise ChannelError('translation_unavailable')

    def detect(self, text, previous='und', hint=''):
        return self._get('detect', text, previous, hint)

    def translate(self, text, source, target):
        return self._get('translate', text, source, target)

    def reply(self, text, language):
        return self._get('reply', text, language)


def _event(payload):
    value = json.loads(payload) if isinstance(payload, str) else dict(payload)
    value['attachments'] = tuple(Attachment(**item) for item in value.get('attachments', []))
    return InboundEvent(**value)


class PersonalService(Service):
    def __init__(self, db_path, transport, *, expected_owner_id, allowed_chat_ids,
                 feishu_account_id, members, language=None, clock=time.time,
                 telegram_account_key='primary', staff_names=None):
        if not isinstance(feishu_account_id, str) or not feishu_account_id:
            raise ChannelError('invalid_account')
        if not isinstance(telegram_account_key, str) or not telegram_account_key:
            raise ChannelError('invalid_account')
        members = tuple(dict.fromkeys(members))
        if not members or not all(isinstance(x, str) and x for x in members):
            raise ChannelError('invalid_members')
        self.policy = TelegramPersonal(expected_owner_id=expected_owner_id,
                                       allowed_chat_ids=allowed_chat_ids)
        self.telegram_account_key = telegram_account_key
        self.staff_names = dict(staff_names or {})
        self.prepared = _PreparedLanguage(language or SimulationLanguage())
        self._worker_lock = threading.Lock()
        self._verified_connections = set()
        super().__init__(db_path, transport, clock=clock, members=members,
                         language=self.prepared, feishu_account_id=feishu_account_id)
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO personal_cursor(account_key) VALUES(?)',
                       (self.telegram_account_key,))
            db.execute("UPDATE personal_queue SET status='pending' WHERE status='processing'")
            for row in db.execute('SELECT * FROM personal_connections WHERE account_key=?',
                                  (self.telegram_account_key,)):
                if row['owner_id'] == self.policy.owner_id:
                    self.policy.connections[row['connection_id']] = Connection(
                        row['connection_id'], row['owner_id'], bool(row['enabled']),
                        bool(row['can_reply']), bool(row['can_delete_sent']))

    def connection_ids(self):
        with self.store.lock:
            return tuple(self.policy.connections)

    def mark_connections_unverified(self):
        with self.store.lock:
            self._verified_connections.clear()

    def _persist_connection(self, db, connection):
        db.execute('''INSERT INTO personal_connections VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(account_key,connection_id) DO UPDATE SET
          owner_id=excluded.owner_id,enabled=excluded.enabled,can_reply=excluded.can_reply,
          can_delete_sent=excluded.can_delete_sent,updated_at=excluded.updated_at''',
          (self.telegram_account_key, connection.connection_id, connection.owner_id,
           int(connection.enabled), int(connection.can_reply), int(connection.can_delete_sent), self.clock()))
        self.policy.connections[connection.connection_id] = connection
        self._verified_connections.add(connection.connection_id)
        if not connection.enabled:
            # Revocation takes effect before the next worker turn. No denied text
            # is retained in callbacks, and already queued work is checked again.
            for row in db.execute("SELECT id FROM conversations WHERE channel='telegram' AND account_id=?",
                                  (connection.connection_id,)).fetchall():
                self._cancel_ai(db, row['id'])

    def hydrate_connection(self, data):
        """Apply a fresh getBusinessConnection result before dispatch on restart."""
        connection = Connection.from_update(data)
        if connection.owner_id != self.policy.owner_id:
            return False
        with self.store.transaction() as db:
            self._persist_connection(db, connection)
        return True

    def connection_needed(self, update):
        """Return only an unknown connection for an allowlisted private chat."""
        if not isinstance(update, dict):
            return None
        for key in ('business_message', 'edited_business_message', 'deleted_business_messages'):
            data = update.get(key)
            if not isinstance(data, dict):
                continue
            chat = data.get('chat')
            cid = data.get('business_connection_id')
            if (isinstance(chat, dict) and chat.get('type') == 'private'
                    and type(chat.get('id')) is int and chat['id'] in self.policy.allowed
                    and chat['id'] != self.policy.owner_id and isinstance(cid, str) and cid):
                with self.store.lock:
                    if cid not in self._verified_connections:
                        return cid
        return None

    def telegram_offset(self):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM personal_cursor WHERE account_key=?',
                             (self.telegram_account_key,)).fetchone()
            # Telegram chooses a fresh random update_id after at least a week
            # without updates. Reset only after that idle period, not on failure.
            if row['last_update_at'] and self.clock() - row['last_update_at'] >= 7 * 86400:
                db.execute('UPDATE personal_cursor SET offset=0,epoch=epoch+1,last_update_at=? WHERE account_key=?',
                           (self.clock(), self.telegram_account_key))
                return 0
            return row['offset']

    def _enqueue(self, db, event, role):
        payload = _json(asdict(event))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        old = db.execute('SELECT digest FROM personal_queue WHERE channel=? AND account_id=? AND event_id=?',
                         (event.channel, event.account_id, event.event_id)).fetchone()
        if old:
            if old['digest'] != digest:
                raise ChannelError('event_collision')
            return False
        db.execute('''INSERT INTO personal_queue
          (channel,account_id,event_id,chat_id,role,payload,digest,created_at) VALUES(?,?,?,?,?,?,?,?)''',
          (event.channel, event.account_id, event.event_id, event.chat_id, role, payload, digest, self.clock()))
        return True

    def accept_telegram_update(self, update):
        if not isinstance(update, dict) or type(update.get('update_id')) is not int or update['update_id'] < 0:
            raise ChannelError('invalid_update')
        uid = update['update_id']
        with self.store.transaction() as db:
            cursor = db.execute('SELECT * FROM personal_cursor WHERE account_key=?',
                                (self.telegram_account_key,)).fetchone()
            if uid < cursor['offset']:
                return {'status': 'duplicate', 'accepted': 0, 'offset': cursor['offset']}
            count = 0
            if 'business_connection' in update:
                try:
                    connection = Connection.from_update(update['business_connection'])
                except ChannelError:
                    connection = None
                if connection and connection.owner_id == self.policy.owner_id:
                    self._persist_connection(db, connection)
            else:
                for item in self.policy.parse(update):
                    event = replace(item.event, event_id=f"{cursor['epoch']}:{item.event.event_id}")
                    if event.event_type == 'delete':
                        self._accept_delete(db, event)
                    if self._enqueue(db, event, item.role):
                        count += 1
                    if item.role == 'owner':
                        conv = db.execute("SELECT id FROM conversations WHERE channel='telegram' AND account_id=? AND chat_id=?",
                                          (event.account_id, event.chat_id)).fetchone()
                        if conv:
                            self._cancel_ai(db, conv['id'])
                            db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (conv['id'],))
            db.execute('UPDATE personal_cursor SET offset=?,last_update_at=? WHERE account_key=?',
                       (uid + 1, self.clock(), self.telegram_account_key))
            return {'status': 'accepted' if count else 'ignored', 'accepted': count, 'offset': uid + 1}

    def accept_feishu_payload(self, payload):
        """Called by the authenticated app's official SDK; no model/network work."""
        if not isinstance(payload, dict):
            raise ChannelError('invalid_payload')
        header = payload.get('header')
        if not isinstance(header, dict) or header.get('app_id') != self.feishu_account_id:
            raise ChannelError('forbidden')
        events = channels.parse_events('feishu', self.feishu_account_id, payload)
        count = 0
        with self.store.transaction() as db:
            for event in events:
                conv = db.execute('SELECT * FROM conversations WHERE room_id=? AND feishu_account_id=?',
                                  (event.chat_id, self.feishu_account_id)).fetchone()
                if not conv:
                    continue
                if not self._approved(conv['account_id'], conv['chat_id']):
                    continue
                if event.event_type == 'delete':
                    self._accept_delete(db, event)
                else:
                    if not self._staff_allowed(db, conv['id'], event.user_id):
                        continue
                    name = self.staff_names.get(event.user_id, event.user_id)
                    event = replace(event, sender_name=name if isinstance(name, str) else event.user_id)
                    # The message can arrive while a model call is in progress.
                    # Stop queued AI immediately, before acknowledging the SDK.
                    self._cancel_ai(db, conv['id'])
                    db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (conv['id'],))
                count += int(self._enqueue(db, event, 'staff'))
        return {'status': 'accepted' if count else 'ignored', 'accepted': count}

    def _accept_delete(self, db, event):
        """Preempt unsent work before acknowledging an already known deletion.

        A worker may process one event and immediately drain; merely queuing a
        recall behind its original would otherwise briefly publish withdrawn
        content. These delete handlers only mutate the database, never call I/O.
        """
        db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)',
                   (event.channel, event.account_id, event.chat_id, event.message_id, self.clock()))
        if event.channel == 'feishu':
            self._staff_event(db, event)
        else:
            self._customer_event(db, event)

    def _source_deleted(self, db, event):
        return event.event_type != 'delete' and db.execute('''SELECT 1 FROM tombstones
          WHERE channel=? AND account_id=? AND chat_id=? AND source_message_id=?''',
          (event.channel, event.account_id, event.chat_id, event.message_id)).fetchone() is not None

    def _staff_allowed(self, db, cid, user_id):
        return (user_id in self.default_members and db.execute(
            'SELECT 1 FROM members WHERE conversation_id=? AND user_id=?', (cid, user_id)).fetchone() is not None)

    def _approved(self, connection_id, chat_id):
        try:
            chat = int(chat_id)
        except (TypeError, ValueError):
            return False
        return self.policy._allowed(connection_id, chat) is not None

    def _queue_allowed(self, db, item, event):
        if event.channel == 'telegram':
            return self._approved(event.account_id, event.chat_id)
        conv = db.execute('SELECT * FROM conversations WHERE room_id=? AND feishu_account_id=?',
                          (event.chat_id, self.feishu_account_id)).fetchone()
        if not conv or event.account_id != self.feishu_account_id or not self._approved(conv['account_id'], conv['chat_id']):
            return False
        return event.event_type == 'delete' or self._staff_allowed(db, conv['id'], event.user_id)

    def _prepare_language(self, item, event, stop_event=None):
        if event.event_type == 'delete':
            return
        with self.store.lock:
            db = self.store.db
            if not self._queue_allowed(db, item, event) or self._source_deleted(db, event):
                return
            row = (db.execute("SELECT * FROM conversations WHERE channel='telegram' AND account_id=? AND chat_id=?",
                              (event.account_id, event.chat_id)).fetchone()
                   if event.channel == 'telegram' else
                   db.execute('SELECT * FROM conversations WHERE room_id=?', (event.chat_id,)).fetchone())
            conv = dict(row) if row else {'id': None, 'language': 'und', 'mode': 'auto'}
            history = [dict(r) for r in db.execute('''SELECT m.* FROM messages m WHERE m.conversation_id=?
              AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.operation_key=
                CASE WHEN m.direction IN ('inbound','owner') THEN 'mirror:' ELSE 'history-ai:' END || m.id)''',
              (conv['id'],))] if conv['id'] else []
            pending_owner = db.execute("SELECT 1 FROM personal_queue WHERE account_id=? AND chat_id=? AND role='owner' AND status IN ('pending','processing')",
                                       (event.account_id, event.chat_id)).fetchone()
        def warm(method, *args):
            if stop_event is not None and stop_event.is_set():
                raise _Stopping()
            with self.store.lock:
                if not self._queue_allowed(self.store.db, item, event):
                    raise ChannelError('forbidden')
                if self._source_deleted(self.store.db, event):
                    raise ChannelError('source_deleted')
            try:
                return self.prepared.warm(method, *args)
            except ChannelError as exc:
                if method == 'reply':
                    return {'action': 'handoff', 'text': ''}
                if method == 'detect':
                    return conv['language']
                if exc.code == 'forbidden':
                    raise
                return None
        if event.channel == 'feishu':
            warm('translate', event.text, 'zh', conv['language'])
        else:
            language = warm('detect', event.text, conv['language'], event.language_hint)
            warm('translate', event.text, language, 'zh')
            if item['role'] == 'customer' and conv['mode'] == 'auto' and not pending_owner and not event.attachments:
                answer = warm('reply', event.text, language)
                if answer.get('action') == 'reply':
                    warm('translate', answer['text'], language, 'zh')
        # Handoff can replay historical customer/AI messages; prewarm them here,
        # outside the transaction. Cache misses later show an explicit notice.
        for message in history:
            if message['direction'] in ('inbound', 'owner'):
                warm('translate', message['original'], message['language'], 'zh')
            elif message['author_id'] == 'AI' and message['status'] == 'sent':
                warm('translate', message['translated'], message['language'], 'zh')

    def process_inbound(self, limit=100, stop_event=None):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ChannelError('invalid_limit')
        outcomes = []
        with self._worker_lock:
            for _ in range(limit):
                if stop_event is not None and stop_event.is_set():
                    break
                with self.store.transaction() as db:
                    row = db.execute("SELECT * FROM personal_queue WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
                    if row is None:
                        break
                    item = dict(row)
                    event = _event(item['payload'])
                    if self._source_deleted(db, event):
                        db.execute("UPDATE personal_queue SET status='cancelled',last_error='source_deleted' WHERE id=?", (item['id'],))
                        outcomes.append({'id': item['id'], 'status': 'cancelled'})
                        continue
                    if not self._queue_allowed(db, item, event):
                        db.execute("UPDATE personal_queue SET status='cancelled',last_error='policy_revoked' WHERE id=?", (item['id'],))
                        outcomes.append({'id': item['id'], 'status': 'cancelled'})
                        continue
                    db.execute("UPDATE personal_queue SET status='processing' WHERE id=?", (item['id'],))
                try:
                    self._prepare_language(item, event, stop_event)
                    if stop_event is not None and stop_event.is_set():
                        raise _Stopping()
                    # Recheck and apply while holding the same RLock used by
                    # authorization updates. No model I/O occurs in this block.
                    with self.store.lock:
                        if not self._queue_allowed(self.store.db, item, event):
                            raise ChannelError('forbidden')
                        if self._source_deleted(self.store.db, event):
                            raise ChannelError('source_deleted')
                        result = super().ingest(event)
                    with self.store.transaction() as db:
                        db.execute("UPDATE personal_queue SET status='done',last_error='' WHERE id=?", (item['id'],))
                    outcomes.append({'id': item['id'], **result})
                except _Stopping:
                    with self.store.transaction() as db:
                        db.execute("UPDATE personal_queue SET status='pending',last_error='' WHERE id=?", (item['id'],))
                    break
                except ChannelError as exc:
                    with self.store.transaction() as db:
                        db.execute("UPDATE personal_queue SET status=?,last_error=? WHERE id=?",
                                   ('cancelled' if exc.code in ('forbidden', 'source_deleted') else 'failed', exc.code, item['id']))
                    outcomes.append({'id': item['id'], 'status': 'cancelled' if exc.code in ('forbidden', 'source_deleted') else 'failed', 'error': exc.code})
                except Exception:
                    # No acknowledged event is silently discarded. The operator
                    # sees this item after restart; raw exception text is omitted.
                    with self.store.transaction() as db:
                        db.execute("UPDATE personal_queue SET status='pending',last_error='worker_interrupted' WHERE id=?", (item['id'],))
                    raise
        return outcomes

    def ingest(self, event):
        raise ChannelError('unsupported_entrypoint', 'Use the authorized Telegram or Feishu receiver.')

    def _new_conversation(self, db, event):
        row = db.execute("SELECT * FROM conversations WHERE channel='telegram' AND account_id=? AND chat_id=?",
                         (event.account_id, event.chat_id)).fetchone()
        if row:
            return dict(row)
        cid = _id('conv_')
        db.execute('''INSERT INTO conversations
          (id,channel,account_id,chat_id,user_id,recipient_type,created_at) VALUES(?,?,?,?,?,?,?)''',
          (cid, 'telegram', event.account_id, event.chat_id, event.chat_id, 'chat', self.clock()))
        return self._conversation(db, cid)

    def _ensure_room(self, db, cid, staff_ids=None):
        selected = tuple(staff_ids) if staff_ids is not None else self.default_members
        if not selected or any(member not in self.default_members for member in selected):
            raise ChannelError('forbidden', 'Only configured staff may join customer rooms.')
        return super()._ensure_room(db, cid, selected)

    def _owner_event(self, db, event):
        conv = self._new_conversation(db, event)
        self._cancel_ai(db, conv['id'])
        db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (conv['id'],))
        self._ensure_room(db, conv['id'])
        existing = db.execute("SELECT * FROM messages WHERE conversation_id=? AND source_channel='telegram' AND source_id=?",
                              (conv['id'], event.message_id)).fetchone()
        if existing and event.event_type != 'edit':
            return {'status': 'duplicate_message', 'conversation_id': conv['id'], 'message_id': existing['id']}
        language = self.language.detect(event.text, conv['language'], event.language_hint)
        mid = self._message(db, conv['id'], direction='owner', source_channel='telegram',
                            source_id=event.message_id if not existing else event.message_id + ':edit:' + event.event_id,
                            author_id=event.user_id, sender_name='账号本人', text=event.text, language=language,
                            attachments=[asdict(a) for a in event.attachments], kind='media' if event.attachments else 'text',
                            parent_id=existing['id'] if existing else None)
        self._mirror(db, self._conversation(db, conv['id']), dict(db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()))
        self._audit(db, conv['id'], 'owner_takeover', {'message_id': mid})
        return {'status': 'owner_mirrored', 'conversation_id': conv['id'], 'message_id': mid}

    def _customer_event(self, db, event):
        if event.event_type != 'delete' and event.user_id == str(self.policy.owner_id):
            return self._owner_event(db, event)
        if event.event_type == 'delete':
            conv = db.execute("SELECT * FROM conversations WHERE channel='telegram' AND account_id=? AND chat_id=?",
                              (event.account_id, event.chat_id)).fetchone()
            if conv:
                sent = db.execute("""SELECT messages.* FROM outbox JOIN messages ON messages.id=outbox.message_id
                  WHERE outbox.conversation_id=? AND outbox.channel='telegram' AND outbox.operation='send'
                  AND outbox.external_id=? AND messages.direction='outbound'""",
                  (conv['id'], event.message_id)).fetchone()
                if sent:
                    db.execute("UPDATE messages SET status='deleted',recall_status='deleted' WHERE id=?", (sent['id'],))
                    db.execute("UPDATE outbox SET status='cancelled' WHERE message_id=? AND operation='delete' AND status='pending'", (sent['id'],))
                    self._delete_ai_mirror(db, conv['id'], sent['id'])
                    self._notify(db, conv['id'], 'external-delete:' + sent['id'], 'Telegram 已通知原账号消息被删除。消息 ' + sent['id'])
                    return {'status': 'deleted', 'conversation_id': conv['id'], 'message_id': sent['id']}
                original = db.execute("SELECT id FROM messages WHERE conversation_id=? AND source_channel='telegram' AND source_id=?",
                                      (conv['id'], event.message_id)).fetchone()
                if original:
                    versions = db.execute("""WITH RECURSIVE versions(id) AS (
                      SELECT ? UNION ALL SELECT m.id FROM messages m JOIN versions v ON m.parent_id=v.id
                      WHERE m.direction IN ('inbound','owner') AND m.source_channel='telegram')
                      SELECT m.source_id FROM messages m JOIN versions v ON m.id=v.id""", (original['id'],)).fetchall()
                    result = None
                    for version in versions:
                        result = super()._customer_event(db, replace(event, message_id=version['source_id']))
                    return result
            return super()._customer_event(db, event)
        conv = self._new_conversation(db, event)
        if db.execute("SELECT 1 FROM personal_queue WHERE account_id=? AND chat_id=? AND role='owner' AND status IN ('pending','processing')",
                      (event.account_id, event.chat_id)).fetchone():
            self._cancel_ai(db, conv['id'])
            db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (conv['id'],))
        if conv['room_status'] == 'absent':
            self._ensure_room(db, conv['id'])
        return super()._customer_event(db, event)

    def _staff_event(self, db, event):
        if event.event_type != 'delete':
            return super()._staff_event(db, event)
        conv = db.execute('SELECT * FROM conversations WHERE room_id=? AND feishu_account_id=?',
                          (event.chat_id, event.account_id)).fetchone()
        if not conv:
            raise ChannelError('forbidden')
        original = db.execute("SELECT * FROM messages WHERE conversation_id=? AND source_channel='feishu' AND source_id=?",
                              (conv['id'], event.message_id)).fetchone()
        if not original:
            return {'status': 'unknown_message', 'conversation_id': conv['id']}
        if not self._staff_allowed(db, conv['id'], original['author_id']):
            raise ChannelError('forbidden')
        versions = db.execute("""WITH RECURSIVE versions(id) AS (
          SELECT ? UNION ALL SELECT m.id FROM messages m JOIN versions v ON m.parent_id=v.id
          WHERE m.direction='outbound' AND m.source_channel='feishu')
          SELECT m.* FROM messages m JOIN versions v ON m.id=v.id""", (original['id'],)).fetchall()
        result = None
        for version in versions:
            result = self._recall(db, version['id'], conv['id'], original['author_id'])
        return result

    def _request(self, db, item, conv):
        if conv['channel'] != 'telegram' or not self._approved(conv['account_id'], conv['chat_id']):
            raise ChannelError('forbidden')
        if conv['account_id'] not in self._verified_connections:
            raise ChannelError('connection_refresh_required')
        if item['channel'] == 'telegram':
            payload = json.loads(item['payload'])
            if item['message_id']:
                message = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
                if message and message['author_id'] != 'AI' and not self._staff_allowed(db, conv['id'], message['author_id']):
                    raise ChannelError('forbidden')
            if item['operation'] == 'delete':
                try:
                    mid = int(payload['external_id'])
                except (KeyError, TypeError, ValueError):
                    raise ChannelError('invalid_payload') from None
                receipt = db.execute("""SELECT external_id FROM outbox WHERE conversation_id=?
                  AND channel='telegram' AND operation='send' AND status='sent' AND external_id=?
                  AND message_id=?""", (conv['id'], str(mid), item['message_id'])).fetchone()
                confirmed = (conv['account_id'], int(conv['chat_id']), mid) if receipt else None
                method, body = self.policy.delete_payload(conv['account_id'], int(conv['chat_id']), mid, confirmed_sent=confirmed)
            else:
                method, body = self.policy.send_payload(conv['account_id'], int(conv['chat_id']), payload['text'],
                                                        last_incoming_at=conv['last_customer_at'], now=self.clock())
            return Request('POST', '/' + method, body)
        if item['channel'] != 'feishu' or conv['feishu_account_id'] != self.feishu_account_id:
            raise ChannelError('forbidden')
        request = super()._request(db, item, conv)
        # Feishu accepts a stable UUID for create-chat and send-message. It remains
        # stable across a documented retry; ambiguous outcomes are never replayed.
        stable = str(uuid.uuid5(uuid.NAMESPACE_URL, f"omni:{self.feishu_account_id}:{conv['id']}:{item['operation_key']}"))
        body = dict(request.body) if request.body is not None else None
        path = request.path
        if item['operation'] == 'create_room':
            path += '&uuid=' + stable
        elif item['operation'] == 'send':
            body['uuid'] = stable
        return Request(request.method, path, body, {'Content-Type': 'application/json'})

    def _failure(self, db, item, exc):
        if exc.code == 'connection_refresh_required':
            db.execute("UPDATE outbox SET status='pending',available_at=?,last_error=? WHERE id=?",
                       (self.clock() + 5, exc.code, item['id']))
            return
        if exc.code == 'blocked_window':
            db.execute("UPDATE outbox SET status='blocked_window',last_error=? WHERE id=?", (exc.code, item['id']))
            if item['message_id']:
                db.execute("UPDATE messages SET status='blocked_window' WHERE id=?", (item['message_id'],))
            self._notify(db, item['conversation_id'], 'window:' + str(item['id']),
                         'Telegram 原账号回复窗口已过期，消息未发送；等待客户再次发来消息。')
            return
        super()._failure(db, item, exc)

    def _response(self, item, conv, request, payload):
        external_id = super()._response(item, conv, request, payload)
        if item['channel'] == 'telegram' and item['operation'] == 'send':
            result = payload.get('result', {})
            chat = result.get('chat')
            if (result.get('business_connection_id') != conv['account_id']
                    or not isinstance(chat, dict) or type(chat.get('id')) is not int
                    or str(chat['id']) != conv['chat_id']):
                raise ChannelError('invalid_response', 'The receipt is not bound to the requested account and chat.')
        return external_id

    def _success(self, db, item, external_id):
        super()._success(db, item, external_id)
        if item['channel'] != 'telegram' or item['operation'] != 'send' or not item['message_id']:
            return
        conv = self._conversation(db, item['conversation_id'])
        if db.execute('''SELECT 1 FROM tombstones WHERE channel='telegram' AND account_id=?
          AND chat_id=? AND source_message_id=?''', (conv['account_id'], conv['chat_id'], external_id)).fetchone():
            # The platform's deletion update can beat the HTTP send receipt.
            db.execute("UPDATE messages SET status='deleted',recall_status='deleted' WHERE id=?", (item['message_id'],))
            db.execute("UPDATE outbox SET status='cancelled' WHERE message_id=? AND operation='delete' AND status='pending'", (item['message_id'],))
            self._delete_ai_mirror(db, conv['id'], item['message_id'])

    def retry_translation(self, message_id, conversation_id, actor_id):
        with self._worker_lock:
            with self.store.lock:
                db = self.store.db
                conv = self._conversation(db, conversation_id)
                if not self._staff_allowed(db, conversation_id, actor_id) or not self._approved(conv['account_id'], conv['chat_id']):
                    raise ChannelError('forbidden')
                row = db.execute("SELECT * FROM messages WHERE id=? AND conversation_id=? AND direction='outbound'",
                                 (message_id, conversation_id)).fetchone()
                if row is None or row['status'] != 'translation_unavailable' or row['recall_status']:
                    raise ChannelError('invalid_state')
                message = dict(row)
            translated = self.prepared.warm('translate', message['original'], 'zh', conv['language'], force=True)
            with self.store.transaction() as db:
                current = self._conversation(db, conversation_id)
                row = db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
                if not self._approved(current['account_id'], current['chat_id']) or not self._staff_allowed(db, conversation_id, actor_id):
                    raise ChannelError('forbidden')
                if row['status'] != 'translation_unavailable' or row['recall_status'] or current['language'] != conv['language']:
                    raise ChannelError('invalid_state')
                db.execute("UPDATE messages SET translated=?,language=?,status='queued' WHERE id=?", (translated, current['language'], message_id))
                display = f"{message['sender_name'] or message['author_id']}: {translated}"
                self._queue(db, conversation_id, current['channel'], 'send', 'out:' + message_id,
                            {'text': display, 'recipient_type': 'chat'}, message_id)
                self._audit(db, conversation_id, 'translation_retried', {'message_id': message_id, 'actor': actor_id})
            return {'status': 'queued', 'message_id': message_id, 'conversation_id': conversation_id}

    def retry_room(self, conversation_id):
        with self.store.lock:
            conv = self._conversation(self.store.db, conversation_id)
            if not self._approved(conv['account_id'], conv['chat_id']):
                raise ChannelError('forbidden')
            return super().retry_room(conversation_id)

    def queue_status(self):
        with self.store.lock:
            return {row['status']: row['count'] for row in self.store.db.execute(
                'SELECT status,COUNT(*) AS count FROM personal_queue GROUP BY status')}
