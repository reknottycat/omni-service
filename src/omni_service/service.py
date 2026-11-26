"""Durable customer conversations, translation, and platform delivery."""

from dataclasses import asdict
import hashlib
import json
import threading
import time
import uuid

from . import channels
from .language import SimulationLanguage
from .models import ChannelError
from .store import Store


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _id(prefix):
    return prefix + uuid.uuid4().hex


class Service:
    def __init__(self, db_path, transport, *, clock=time.time,
                 members=tuple(f'employee-{n}' for n in range(1, 8)), language=None):
        self.store = Store(db_path)
        self.transport = transport
        self.clock = clock
        self.default_members = tuple(dict.fromkeys(members))
        self.language = language or SimulationLanguage()
        self._drain_lock = threading.Lock()

    def close(self):
        self.store.close()

    def _audit(self, db, cid, action, detail):
        db.execute('INSERT INTO audit(created_at,conversation_id,action,detail) VALUES(?,?,?,?)',
                   (self.clock(), cid, action, _json(detail)))

    def _conversation(self, db, cid):
        row = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
        if row is None:
            raise ChannelError('not_found', 'Conversation does not exist.')
        return dict(row)

    def _queue(self, db, cid, channel, operation, key, payload, mid=None):
        db.execute('''INSERT OR IGNORE INTO outbox
          (operation_key,conversation_id,message_id,channel,operation,payload,available_at)
          VALUES(?,?,?,?,?,?,?)''', (key, cid, mid, channel, operation, _json(payload), self.clock()))

    def _notify(self, db, cid, key, text):
        conv = self._conversation(db, cid)
        if conv['room_status'] != 'absent':
            self._queue(db, cid, 'feishu', 'send', 'notice:' + key,
                        {'text': 'OmniService 服务通知：' + text})

    def _message(self, db, cid, *, direction, source_channel, source_id, author_id,
                 text, translated='', language='und', sender_name='', kind='text',
                 attachments=(), status='received', parent_id=None):
        mid = _id('msg_')
        db.execute('''INSERT INTO messages
          (id,conversation_id,direction,source_channel,source_id,author_id,sender_name,
           original,translated,language,kind,attachments,status,parent_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (mid, cid, direction, source_channel, source_id, author_id, sender_name,
           text, translated, language, kind, _json(attachments), status, parent_id, self.clock()))
        return mid

    def _cancel_ai(self, db, cid):
        db.execute("UPDATE outbox SET status='cancelled' WHERE conversation_id=? AND operation='send' AND status='pending' AND message_id IN (SELECT id FROM messages WHERE author_id='AI')", (cid,))
        db.execute("UPDATE messages SET status='cancelled' WHERE conversation_id=? AND author_id='AI' AND status='queued' AND id IN (SELECT message_id FROM outbox WHERE status='cancelled')", (cid,))

    def _outgoing(self, db, conv, text, *, author='AI', source_channel='ai', source_id=None,
                  sender_name='', parent_id=None, already_translated=False, attachments=()):
        cid = conv['id']
        status, translated = 'queued', ''
        if attachments:
            status = 'media_pending'
        else:
            try:
                translated = text if already_translated else self.language.translate(text, 'zh', conv['language'])
            except ChannelError as exc:
                if exc.code != 'translation_unavailable':
                    raise
                status = 'translation_unavailable'
        if status == 'queued' and conv['channel'] == 'whatsapp' and self.clock() - conv['last_customer_at'] >= 86400:
            status = 'blocked_window'
        mid = self._message(db, cid, direction='outbound', source_channel=source_channel,
                            source_id=source_id or _id('ai_'), author_id=author, sender_name=sender_name,
                            text=text, translated=translated, language=conv['language'],
                            kind='media' if attachments else 'text', attachments=attachments,
                            status=status, parent_id=parent_id)
        if status == 'queued':
            display = translated if author == 'AI' else f'{sender_name or author}: {translated}'
            self._queue(db, cid, conv['channel'], 'send', 'out:' + mid,
                        {'text': display, 'recipient_type': conv['recipient_type']}, mid)
        elif status != 'media_pending':
            self._audit(db, cid, status, {'message_id': mid})
            notice = ('模拟翻译未收录此短语，消息未发送，请修改内容后重试。'
                      if status == 'translation_unavailable' else 'WhatsApp 客服窗口已过期，消息未发送，需要获批模板。')
            self._notify(db, cid, status + ':' + mid, notice + ' 消息 ' + mid)
        else:
            self._notify(db, cid, 'media:' + mid, '媒体已保存，等待目的平台上传适配，尚未发送。消息 ' + mid)
        return mid, status

    def _mirror(self, db, conv, message):
        if conv['room_status'] == 'absent' or message['status'] == 'deleted':
            return
        text = message['original']
        try:
            translated = self.language.translate(text, message['language'], 'zh')
        except ChannelError:
            translated = '[模拟翻译未收录此短语]'
        if message['kind'] == 'media':
            text = '[媒体待转码/上传] ' + text
        body = f"{message['sender_name'] or '客户'} ({conv['channel']}): {text}"
        if translated and translated != text:
            body += '\n中文: ' + translated
        self._queue(db, conv['id'], 'feishu', 'send', 'mirror:' + message['id'], {'text': body}, message['id'])

    def _mirror_ai(self, db, conv, message):
        if conv['room_status'] == 'absent' or message['status'] != 'sent':
            return
        customer_text = message['translated']
        try:
            chinese = self.language.translate(customer_text, message['language'], 'zh')
        except ChannelError:
            chinese = '[模拟翻译未收录此短语]'
        body = 'AI → 客户: ' + customer_text
        if chinese and chinese != customer_text:
            body += '\n中文: ' + chinese
        self._queue(db, conv['id'], 'feishu', 'send', 'history-ai:' + message['id'],
                    {'text': body, 'original_message_id': message['id']})

    def _delete_ai_mirror(self, db, cid, mid):
        mirror = db.execute('SELECT * FROM outbox WHERE operation_key=?', ('history-ai:' + mid,)).fetchone()
        if mirror:
            if mirror['status'] == 'pending':
                db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (mirror['id'],))
            elif mirror['external_id']:
                self._queue(db, cid, 'feishu', 'delete', 'delete-ai-history:' + mid,
                            {'external_id': mirror['external_id']})

    def _handoff(self, db, cid, staff_ids=None):
        conv = self._conversation(db, cid)
        selected = tuple(dict.fromkeys(staff_ids if staff_ids is not None else self.default_members))
        if not selected or any(not isinstance(s, str) or not s for s in selected):
            raise ChannelError('invalid_members', 'At least one staff identity is required.')
        for staff in selected:
            inserted = db.execute('INSERT OR IGNORE INTO members VALUES(?,?)', (cid, staff)).rowcount
            if inserted and conv['room_id']:
                self._queue(db, cid, 'feishu', 'add_member', 'member:' + cid + ':' + staff,
                            {'members': [staff]})
        self._cancel_ai(db, cid)
        db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (cid,))
        if conv['room_status'] == 'absent':
            db.execute("UPDATE conversations SET room_status='pending' WHERE id=?", (cid,))
            self._queue(db, cid, 'feishu', 'create_room', 'room:' + cid,
                        {'name': 'OmniService ' + cid[-8:]})
        conv = self._conversation(db, cid)
        for row in db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid', (cid,)).fetchall():
            message = dict(row)
            if message['direction'] == 'inbound':
                self._mirror(db, conv, message)
            elif message['author_id'] == 'AI':
                self._mirror_ai(db, conv, message)
        self._audit(db, cid, 'handoff', {'members': selected})
        return {'status': 'manual', 'conversation_id': cid, 'room_status': conv['room_status']}

    def handoff(self, conversation_id, staff_ids=None):
        with self.store.transaction() as db:
            return self._handoff(db, conversation_id, staff_ids)

    def add_member(self, conversation_id, staff_id):
        with self.store.transaction() as db:
            return self._handoff(db, conversation_id, [staff_id])

    def resume(self, conversation_id):
        with self.store.transaction() as db:
            self._conversation(db, conversation_id)
            db.execute("UPDATE conversations SET mode='auto' WHERE id=?", (conversation_id,))
            self._audit(db, conversation_id, 'resume_ai', {})
            return {'status': 'auto', 'conversation_id': conversation_id}

    def ingest(self, event):
        if event.is_bot:
            return {'status': 'ignored_bot'}
        if event.channel not in ('telegram', 'max', 'whatsapp', 'feishu'):
            raise ChannelError('invalid_channel', 'Unsupported source channel.')
        if not all(isinstance(v, str) and v for v in (event.account_id, event.event_id, event.chat_id, event.message_id)):
            raise ChannelError('invalid_event', 'The event is missing stable source identifiers.')
        digest = hashlib.sha256(_json(asdict(event)).encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM inbox WHERE channel=? AND account_id=? AND event_id=?',
                             (event.channel, event.account_id, event.event_id)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ChannelError('event_collision', 'An event identifier was reused for different content.')
                return {**json.loads(old['result']), 'status': 'duplicate'}
            source = (event.channel, event.account_id, event.chat_id, event.message_id)
            if event.event_type == 'delete':
                db.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)', (*source, self.clock()))
                result = self._staff_event(db, event) if event.channel == 'feishu' else self._customer_event(db, event)
            elif db.execute('''SELECT 1 FROM tombstones WHERE
                               channel=? AND account_id=? AND chat_id=? AND source_message_id=?''', source).fetchone():
                result = {'status': 'suppressed_deleted', 'source_message_id': event.message_id}
                self._audit(db, None, 'late_deleted_message_suppressed',
                            {'channel': event.channel, 'account_id': event.account_id,
                             'chat_id': event.chat_id, 'source_message_id': event.message_id})
            else:
                result = self._staff_event(db, event) if event.channel == 'feishu' else self._customer_event(db, event)
            db.execute('INSERT INTO inbox VALUES(?,?,?,?,?)',
                       (event.channel, event.account_id, event.event_id, digest, _json(result)))
            return result

    def _customer_event(self, db, event):
        row = db.execute('SELECT * FROM conversations WHERE channel=? AND account_id=? AND chat_id=?',
                         (event.channel, event.account_id, event.chat_id)).fetchone()
        if row is None:
            if event.event_type == 'delete':
                return {'status': 'unknown_message'}
            cid = _id('conv_')
            db.execute('''INSERT INTO conversations
              (id,channel,account_id,chat_id,user_id,recipient_type,created_at) VALUES(?,?,?,?,?,?,?)''',
              (cid, event.channel, event.account_id, event.chat_id, event.user_id,
               event.recipient_type, self.clock()))
            conv = self._conversation(db, cid)
        else:
            conv = dict(row)
        cid = conv['id']
        existing = db.execute('SELECT * FROM messages WHERE conversation_id=? AND source_channel=? AND source_id=?',
                              (cid, event.channel, event.message_id)).fetchone()
        if event.event_type == 'delete':
            if existing is None:
                return {'status': 'unknown_message', 'conversation_id': cid}
            inflight = db.execute("SELECT 1 FROM outbox WHERE message_id=? AND operation='send' AND status='inflight'", (existing['id'],)).fetchone()
            recall = 'pending' if (existing['external_id'] or inflight) else 'cancelled'
            db.execute("UPDATE messages SET status='deleted',recall_status=? WHERE id=?", (recall, existing['id']))
            db.execute("UPDATE outbox SET status='cancelled' WHERE message_id=? AND status='pending'", (existing['id'],))
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND message_id IN (SELECT id FROM messages WHERE parent_id=?)", (existing['id'],))
            db.execute("UPDATE messages SET status='cancelled' WHERE parent_id=? AND status='queued'", (existing['id'],))
            if existing['external_id'] and conv['room_id']:
                self._queue(db, cid, 'feishu', 'delete', 'delete-mirror:' + existing['id'],
                            {'external_id': existing['external_id'], 'customer_mirror': True}, existing['id'])
            self._audit(db, cid, 'customer_deleted', {'message_id': existing['id']})
            return {'status': 'deleted', 'conversation_id': cid, 'message_id': existing['id']}
        if existing and event.event_type != 'edit':
            return {'status': 'duplicate_message', 'conversation_id': cid, 'message_id': existing['id']}
        timestamp = min(event.timestamp or self.clock(), self.clock())
        language = self.language.detect(event.text, conv['language'], event.language_hint)
        if timestamp >= conv['language_at']:
            db.execute('UPDATE conversations SET language=?,language_at=? WHERE id=?', (language, timestamp, cid))
        if timestamp > conv['last_customer_at']:
            db.execute('UPDATE conversations SET last_customer_at=? WHERE id=?', (timestamp, cid))
        conv = self._conversation(db, cid)
        mid = self._message(db, cid, direction='inbound', source_channel=event.channel,
                            source_id=event.message_id if not existing else event.message_id + ':edit:' + event.event_id,
                            author_id=event.user_id, sender_name=event.sender_name, text=event.text,
                            language=language, kind='media' if event.attachments else 'text',
                            attachments=[asdict(a) for a in event.attachments], status='received',
                            parent_id=existing['id'] if existing else None)
        message = dict(db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone())
        if existing:
            self._cancel_ai(db, cid)
        self._mirror(db, conv, message)
        if conv['mode'] == 'auto' and timestamp >= conv['language_at']:
            answer = self.language.reply(event.text, language) if not event.attachments else {'action': 'handoff'}
            if answer['action'] == 'reply':
                self._outgoing(db, conv, answer['text'], parent_id=mid, already_translated=True)
            else:
                self._handoff(db, cid)
        elif conv['mode'] == 'auto':
            self._audit(db, cid, 'stale_message_no_auto_reply', {'message_id': mid})
        self._audit(db, cid, 'customer_message', {'message_id': mid, 'language': language})
        return {'status': 'accepted', 'conversation_id': cid, 'message_id': mid}

    def _staff_event(self, db, event):
        row = db.execute('SELECT * FROM conversations WHERE room_id=?', (event.chat_id,)).fetchone()
        if row is None:
            raise ChannelError('unmapped_room', 'This Feishu room is not a customer conversation.')
        conv = dict(row)
        existing = db.execute("SELECT * FROM messages WHERE conversation_id=? AND source_channel='feishu' AND source_id=?",
                              (conv['id'], event.message_id)).fetchone()
        if event.event_type == 'delete':
            if not existing:
                return {'status': 'unknown_message', 'conversation_id': conv['id']}
            actor = event.user_id or existing['author_id']
            return self._recall(db, existing['id'], conv['id'], actor)
        if not db.execute('SELECT 1 FROM members WHERE conversation_id=? AND user_id=?', (conv['id'], event.user_id)).fetchone():
            raise ChannelError('forbidden', 'The sender is not a member of this conversation.')
        if existing and event.event_type != 'edit':
            return {'status': 'duplicate_message', 'conversation_id': conv['id'], 'message_id': existing['id']}
        self._cancel_ai(db, conv['id'])
        db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (conv['id'],))
        mid, status = self._outgoing(db, conv, event.text, author=event.user_id,
                                     sender_name=event.sender_name, source_channel='feishu',
                                     source_id=event.message_id if not existing else event.message_id + ':edit:' + event.event_id,
                                     parent_id=existing['id'] if existing else None,
                                     attachments=[asdict(a) for a in event.attachments])
        self._audit(db, conv['id'], 'staff_message', {'message_id': mid, 'status': status})
        return {'status': status, 'conversation_id': conv['id'], 'message_id': mid}

    def _recall(self, db, mid, cid, actor):
        conv = self._conversation(db, cid)
        if not db.execute('SELECT 1 FROM members WHERE conversation_id=? AND user_id=?', (cid, actor)).fetchone():
            raise ChannelError('forbidden', 'The actor is not a member of this conversation.')
        row = db.execute("SELECT * FROM messages WHERE id=? AND conversation_id=? AND direction='outbound'", (mid, cid)).fetchone()
        if row is None:
            raise ChannelError('not_found', 'No outbound message exists in this conversation.')
        message = dict(row)
        if message['recall_status'] in ('pending', 'deleted', 'cancelled'):
            return {'status': message['recall_status'], 'message_id': mid, 'conversation_id': cid}
        inflight = db.execute("SELECT 1 FROM outbox WHERE message_id=? AND operation='send' AND status='inflight'", (mid,)).fetchone()
        if message['status'] in ('queued', 'translation_unavailable', 'blocked_window', 'media_pending', 'failed') and not inflight:
            db.execute("UPDATE outbox SET status='cancelled' WHERE message_id=? AND status='pending'", (mid,))
            db.execute("UPDATE messages SET status='cancelled',recall_status='cancelled' WHERE id=?", (mid,))
            status = 'cancelled'
        elif message['status'] == 'delivery_unknown':
            status = 'delivery_unknown'
            db.execute('UPDATE messages SET recall_status=? WHERE id=?', (status, mid))
        elif conv['channel'] == 'whatsapp':
            status = 'unsupported'
            db.execute('UPDATE messages SET recall_status=? WHERE id=?', (status, mid))
        else:
            status = 'pending'
            db.execute("UPDATE messages SET recall_status='pending' WHERE id=?", (mid,))
            if message['external_id']:
                self._queue(db, cid, conv['channel'], 'delete', 'recall:' + mid,
                            {'external_id': message['external_id']}, mid)
        self._audit(db, cid, 'recall_requested', {'message_id': mid, 'actor': actor, 'status': status})
        if status in ('unsupported', 'delivery_unknown', 'cancelled'):
            notice = {'unsupported': '此平台不支持撤回已发送消息，原消息仍可能对客户可见。',
                      'delivery_unknown': '原消息送达状态未知，无法确认撤回结果。',
                      'cancelled': '消息已在发送前取消。'}[status]
            self._notify(db, cid, 'recall:' + status + ':' + mid, notice + ' 消息 ' + mid)
        return {'status': status, 'message_id': mid, 'conversation_id': cid}

    def recall(self, message_id, conversation_id, actor_id):
        with self.store.transaction() as db:
            return self._recall(db, message_id, conversation_id, actor_id)

    def list_conversations(self):
        with self.store.lock:
            return [dict(r) for r in self.store.db.execute('SELECT * FROM conversations ORDER BY created_at,id')]

    def get_conversation(self, conversation_id):
        with self.store.lock:
            db = self.store.db
            result = self._conversation(db, conversation_id)
            result['members'] = [r[0] for r in db.execute('SELECT user_id FROM members WHERE conversation_id=? ORDER BY user_id', (conversation_id,))]
            result['messages'] = [dict(r) for r in db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid', (conversation_id,))]
            for message in result['messages']:
                message['attachments'] = json.loads(message['attachments'])
            result['outbox'] = [dict(r) for r in db.execute('SELECT * FROM outbox WHERE conversation_id=? ORDER BY id', (conversation_id,))]
            for item in result['outbox']:
                item['payload'] = json.loads(item['payload'])
            return result

    def _request(self, db, item, conv):
        payload = json.loads(item['payload'])
        channel, operation = item['channel'], item['operation']
        if operation == 'create_room':
            members = [r[0] for r in db.execute('SELECT user_id FROM members WHERE conversation_id=?', (conv['id'],))]
            return channels.feishu_create_room_request(payload['name'], members)
        if operation == 'add_member':
            return channels.feishu_add_members_request(conv['room_id'], payload['members'])
        target = conv['room_id'] if channel == 'feishu' else conv['chat_id']
        if operation == 'delete':
            return channels.delete_request(channel, conv['account_id'], target, payload['external_id'])
        return channels.send_request(channel, conv['account_id'], target, payload['text'],
                                     recipient_type=payload.get('recipient_type', 'chat'))

    def drain(self):
        outcomes = []
        with self._drain_lock:
            while True:
                with self.store.transaction() as db:
                    rows = db.execute("SELECT * FROM outbox WHERE status='pending' AND available_at<=? ORDER BY id", (self.clock(),)).fetchall()
                    item = conv = request = None
                    for candidate in rows:
                        c = self._conversation(db, candidate['conversation_id'])
                        if candidate['channel'] == 'feishu' and candidate['operation'] != 'create_room' and not c['room_id']:
                            continue
                        earlier = db.execute("SELECT 1 FROM outbox WHERE conversation_id=? AND channel=? AND id<? AND status IN ('pending','inflight','unknown')", (candidate['conversation_id'], candidate['channel'], candidate['id'])).fetchone()
                        if earlier:
                            continue
                        item, conv = dict(candidate), c
                        if item['channel'] == 'whatsapp' and item['operation'] == 'send' and self.clock() - c['last_customer_at'] >= 86400:
                            db.execute("UPDATE outbox SET status='blocked_window',last_error='template_required' WHERE id=?", (item['id'],))
                            db.execute("UPDATE messages SET status='blocked_window' WHERE id=?", (item['message_id'],))
                            self._notify(db, c['id'], 'window:' + str(item['id']),
                                         'WhatsApp 客服窗口已过期，排队消息未发送，需要获批模板。')
                            outcomes.append({'id': item['id'], 'status': 'blocked_window'})
                            item = None
                            continue
                        try:
                            request = self._request(db, item, c)
                        except ChannelError as exc:
                            self._failure(db, item, exc)
                            outcomes.append({'id': item['id'], 'status': exc.code})
                            item = None
                            continue
                        db.execute("UPDATE outbox SET status='inflight',attempts=attempts+1 WHERE id=?", (item['id'],))
                        item['attempts'] += 1
                        break
                if item is None:
                    break
                try:
                    payload = self.transport.send(item['channel'], request)
                    external_id = channels.parse_response(item['channel'], item['operation'], payload)
                except ChannelError as exc:
                    with self.store.transaction() as db:
                        self._failure(db, item, exc)
                    outcomes.append({'id': item['id'], 'status': exc.code})
                except Exception:
                    with self.store.transaction() as db:
                        self._failure(db, item, ChannelError('delivery_unknown'))
                    raise
                else:
                    with self.store.transaction() as db:
                        self._success(db, item, external_id)
                    outcomes.append({'id': item['id'], 'status': 'sent', 'external_id': external_id})
        return outcomes

    def _failure(self, db, item, exc):
        retryable = exc.code in ('rate_limited', '429')
        unknown = exc.code in ('delivery_unknown', 'timeout', 'invalid_response')
        if retryable and item['attempts'] < 3:
            status = 'pending'
            db.execute("UPDATE outbox SET status='pending',available_at=?,last_error=? WHERE id=?",
                       (self.clock() + max(0.1, exc.retry_after or 1), exc.code, item['id']))
        else:
            status = 'unknown' if unknown else 'failed'
            db.execute('UPDATE outbox SET status=?,last_error=? WHERE id=?', (status, exc.code, item['id']))
        if item['operation'] == 'create_room' and status != 'pending':
            db.execute('UPDATE conversations SET room_status=? WHERE id=?', (status, item['conversation_id']))
        if item['message_id'] and status != 'pending':
            if item['operation'] == 'delete':
                recall = 'delivery_unknown' if unknown else ('unsupported' if exc.code == 'unsupported' else 'failed')
                db.execute('UPDATE messages SET recall_status=? WHERE id=?', (recall, item['message_id']))
            elif item['operation'] == 'send':
                delivery = 'delivery_unknown' if unknown else 'failed'
                db.execute("UPDATE messages SET status=CASE WHEN status='deleted' THEN status ELSE ? END WHERE id=?", (delivery, item['message_id']))
                db.execute("UPDATE messages SET recall_status=? WHERE id=? AND recall_status='pending'", (delivery, item['message_id']))
        self._audit(db, item['conversation_id'], 'delivery_failure', {'outbox_id': item['id'], 'error': exc.code, 'state': status})
        if status != 'pending' and item['channel'] != 'feishu':
            action = '撤回' if item['operation'] == 'delete' else '发送'
            self._notify(db, item['conversation_id'], 'failure:' + str(item['id']),
                         action + '未确认成功，状态 ' + status + '，错误 ' + exc.code + '。原消息不会被标记为已撤回。')

    def _success(self, db, item, external_id):
        db.execute("UPDATE outbox SET status='sent',last_error='',external_id=? WHERE id=?", (external_id, item['id']))
        if item['operation'] == 'create_room':
            db.execute("UPDATE conversations SET room_id=?,room_status='ready' WHERE id=?", (external_id, item['conversation_id']))
        elif item['operation'] == 'send' and item['message_id']:
            db.execute("UPDATE messages SET status=CASE WHEN status='deleted' THEN status ELSE 'sent' END,external_id=? WHERE id=?", (external_id, item['message_id']))
            msg = db.execute('SELECT * FROM messages WHERE id=?', (item['message_id'],)).fetchone()
            if msg['recall_status'] == 'pending':
                key = ('delete-mirror:' if msg['direction'] == 'inbound' else 'recall:') + msg['id']
                self._queue(db, item['conversation_id'], item['channel'], 'delete', key,
                            {'external_id': external_id}, msg['id'])
            elif msg['author_id'] == 'AI':
                self._mirror_ai(db, self._conversation(db, item['conversation_id']), dict(msg))
        elif item['operation'] == 'send':
            original_id = json.loads(item['payload']).get('original_message_id')
            if original_id:
                original = db.execute('SELECT status FROM messages WHERE id=?', (original_id,)).fetchone()
                if original and original['status'] == 'deleted':
                    self._delete_ai_mirror(db, item['conversation_id'], original_id)
        elif item['operation'] == 'delete' and item['message_id']:
            db.execute("UPDATE messages SET status='deleted',recall_status='deleted' WHERE id=?", (item['message_id'],))
            if item['channel'] != 'feishu':
                self._delete_ai_mirror(db, item['conversation_id'], item['message_id'])
                self._notify(db, item['conversation_id'], 'deleted:' + item['message_id'],
                             '平台已确认删除对应消息。消息 ' + item['message_id'])
        self._audit(db, item['conversation_id'], 'delivery_confirmed', {'outbox_id': item['id'], 'external_id': external_id})
