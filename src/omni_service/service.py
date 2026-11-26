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


_INTAKE_LABELS = {
    'company': '公司', 'cargo': '货物', 'quantity': '数量',
    'origin': '起点', 'destination': '终点',
}
_HANDOFF_LABELS = {
    'complete': '已收齐', 'reply_limit': '达到问询上限',
    'requested_human': '客户要求人工', 'extract_unavailable': 'AI 识别暂不可用',
    'server_disabled': '服务端已关闭自动接待', 'staff_reply': '员工已回复',
    'owner_reply': 'Telegram 本人已回复', 'manual_handoff': '人工接管',
    'media': '收到需要人工查看的附件',
    'reply_not_confirmed': '机器人问询送达未确认',
}
_STAFF_LABELS = {'zh': '人工客服', 'ru': 'Сотрудник', 'en': 'Agent', 'es': 'Agente'}
_ROOM_RULES = ('本群的普通文字消息会翻译后发给客户。'
               '带 @ 的消息只在群内使用，包括 @所有人、@机器人和 @同事。')
_APPROVAL_RULES = _ROOM_RULES + ' 机器人草稿须由客服按预览中的 /approve 指令逐条批准。'
_PREFERENCE_COMMANDS = {'/回复辅助': 'reply_assist', '/标签': 'show_labels'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _id(prefix):
    return prefix + uuid.uuid4().hex


class Service:
    def __init__(self, db_path, transport, *, clock=time.time,
                 members=tuple(f'employee-{n}' for n in range(1, 8)), language=None,
                 intake=None, intake_enabled=False, label_replies=False,
                 approval_intake=False, agent_advisor=None, agent_enabled=False, handoff_staff_id=None):
        if approval_intake and intake is None:
            raise ChannelError('invalid_intake_config')
        self.default_members = tuple(dict.fromkeys(members))
        if handoff_staff_id is not None and handoff_staff_id not in self.default_members:
            raise ChannelError('invalid_handoff_staff')
        if type(agent_enabled) is not bool or (agent_enabled and not callable(agent_advisor)):
            raise ChannelError('invalid_agent_config')
        self.store = Store(db_path)
        self.transport = transport
        self.clock = clock
        self.handoff_staff_id = handoff_staff_id or next(iter(self.default_members), None)
        self.language = language or SimulationLanguage()
        self.intake = intake
        self.approval_intake = approval_intake
        self.intake_enabled = approval_intake or intake_enabled
        self.agent_advisor = agent_advisor
        self.agent_enabled = agent_enabled
        self.label_replies = label_replies or intake is not None
        self._drain_lock = threading.Lock()

    def set_intake_enabled(self, enabled):
        if self.intake is None or self.approval_intake or type(enabled) is not bool:
            raise ChannelError('invalid_intake_config')
        with self.store.transaction() as db:
            if not enabled:
                affected = db.execute('''SELECT conversation_id FROM intake_sessions WHERE handoff_reason IS NULL
                    UNION SELECT conversation_id FROM messages WHERE author_id='AI' AND status='queued' ''').fetchall()
                for row in affected:
                    cid = row['conversation_id']
                    self._cancel_ai(db, cid)
                    self._finish_intake(db, cid, 'server_disabled')
            self.intake_enabled = enabled
        return {'enabled': enabled, 'max_replies': self.intake.max_replies}

    def set_agent_enabled(self, enabled):
        if not self.approval_intake or type(enabled) is not bool or (enabled and not callable(self.agent_advisor)):
            raise ChannelError('invalid_agent_config')
        with self.store.transaction() as db:
            if not enabled:
                rows = db.execute('''SELECT conversation_id FROM agent_reply_jobs
                    UNION SELECT conversation_id FROM reply_drafts WHERE kind='agent_draft'
                    AND (status='pending' OR message_id IN (SELECT id FROM messages WHERE status='queued'))''').fetchall()
                for row in rows:
                    cid = row['conversation_id']
                    if db.execute('SELECT reply_assist FROM conversation_settings WHERE conversation_id=? AND reply_assist IS NOT NULL',
                                  (cid,)).fetchone() is None:
                        self._cancel_agent_work(db, cid)
            self.agent_enabled = enabled
        return {'enabled': enabled}

    def _room_settings(self, db, cid):
        row = db.execute('SELECT reply_assist,show_labels FROM conversation_settings WHERE conversation_id=?', (cid,)).fetchone()
        return {
            'reply_assist': bool(row['reply_assist']) if row and row['reply_assist'] is not None else self.agent_enabled,
            'show_labels': bool(row and row['show_labels']),
        }

    def _source_label(self, db, cid, label):
        return f'[{label}] ' if self._room_settings(db, cid)['show_labels'] else ''

    def _room_rules(self, db, cid):
        settings = self._room_settings(db, cid)
        assist = '开' if settings['reply_assist'] else '关'
        labels = '开' if settings['show_labels'] else '关'
        return (_APPROVAL_RULES + '\n'
                f'/回复辅助 开|关|状态（当前：{assist}，转人工后生效）\n'
                f'/标签 开|关|状态（当前：{labels}，仅影响飞书显示）\n'
                '/draft 手动请求跟进草稿。所有斜杠命令都留在飞书群内。')

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

    def _notify(self, db, cid, key, text, *, mention_user_id=None):
        conv = self._conversation(db, cid)
        if conv['room_status'] != 'absent':
            payload = {'text': 'OmniService 服务通知：' + text}
            if mention_user_id:
                payload['mention_user_id'] = mention_user_id
            self._queue(db, cid, 'feishu', 'send', 'notice:' + key, payload)

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
        db.execute('DELETE FROM agent_reply_jobs WHERE conversation_id=?', (cid,))
        self._cancel_drafts(db, cid)
        self._cancel_queued_ai(db, cid)

    def _cancel_agent_work(self, db, cid):
        db.execute('DELETE FROM agent_reply_jobs WHERE conversation_id=?', (cid,))
        self._cancel_drafts(db, cid, kind='agent_draft')
        self._cancel_queued_ai(db, cid, source='agent_draft')

    def _cancel_queued_ai(self, db, cid, *, source=None):
        db.execute("""UPDATE outbox SET status='cancelled' WHERE conversation_id=? AND operation='send'
            AND status='pending' AND message_id IN (SELECT id FROM messages WHERE author_id='AI'
                AND source_channel!='ai_welcome' AND (? IS NULL OR source_channel=?))""", (cid, source, source))
        db.execute("UPDATE messages SET status='cancelled' WHERE conversation_id=? AND author_id='AI' AND status='queued' AND id IN (SELECT message_id FROM outbox WHERE status='cancelled')", (cid,))

    def _cancel_drafts(self, db, cid, *, kind=None):
        db.execute("""UPDATE outbox SET status='cancelled' WHERE conversation_id=? AND status='pending'
            AND operation_key IN (SELECT 'notice:draft:' || id FROM reply_drafts
                                   WHERE conversation_id=? AND status='pending' AND (? IS NULL OR kind=?))""", (cid, cid, kind, kind))
        db.execute("UPDATE reply_drafts SET status='cancelled' WHERE conversation_id=? AND status='pending' AND (? IS NULL OR kind=?)",
                   (cid, kind, kind))

    def _display_reply(self, text, language, author, sender_name=''):
        if author == 'AI':
            label = 'AI 模拟' if self.intake and self.intake.simulated else 'AI'
            return f'[{label}] {text}' if self.label_replies else text
        display = f'{sender_name or author}: {text}'
        return f'[{_STAFF_LABELS.get(language, "Agent")}] {display}' if self.label_replies else display

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
            display = self._display_reply(translated, conv['language'], author, sender_name)
            self._queue(db, cid, conv['channel'], 'send', 'out:' + mid,
                        {'text': display, 'recipient_type': conv['recipient_type'],
                         **({'mandatory_welcome': True} if source_channel == 'ai_welcome' else {})}, mid)
        elif status != 'media_pending':
            self._audit(db, cid, status, {'message_id': mid})
            notice = ('翻译不可用，消息未发送，请修改内容后重试。'
                      if status == 'translation_unavailable' else 'WhatsApp 客服窗口已过期，消息未发送，需要获批模板。')
            self._notify(db, cid, status + ':' + mid, notice + ' 消息 ' + mid)
        else:
            self._notify(db, cid, 'media:' + mid, '媒体已保存，等待目的平台上传适配，尚未发送。消息 ' + mid)
        return mid, status

    def _mirror(self, db, conv, message):
        if conv['room_status'] == 'absent' or message['status'] == 'deleted':
            return
        if db.execute('SELECT 1 FROM outbox WHERE operation_key=?', ('mirror:' + message['id'],)).fetchone():
            return
        text = message['original']
        try:
            translated = self.language.translate(text, message['language'], 'zh')
        except ChannelError:
            translated = '[翻译不可用]'
        if message['kind'] == 'media':
            text = '[媒体待转码/上传] ' + text
        body = f"{message['sender_name'] or '客户'} ({conv['channel']}): {text}"
        if self.approval_intake:
            body = self._source_label(db, conv['id'], '用户') + body
        if translated and translated != text:
            body += '\n中文: ' + translated
        self._queue(db, conv['id'], 'feishu', 'send', 'mirror:' + message['id'], {'text': body}, message['id'])

    def _mirror_ai(self, db, conv, message):
        if conv['room_status'] == 'absent' or message['status'] != 'sent':
            return
        key = 'history-ai:' + message['id']
        if db.execute('SELECT 1 FROM outbox WHERE operation_key=?', (key,)).fetchone():
            return
        customer_text = message['translated']
        try:
            chinese = self.language.translate(customer_text, message['language'], 'zh')
        except ChannelError:
            chinese = '[中文翻译暂不可用]'
        label = 'AI 模拟' if self.intake and self.intake.simulated else 'AI'
        if self.approval_intake:
            simulated = '模拟投递。' if self.intake.simulated else ''
            body = self._source_label(db, conv['id'], label) + simulated + '已发给客户：' + customer_text
        else:
            body = f'{label} → 客户: ' + customer_text
        if chinese and chinese != customer_text:
            body += '\n中文: ' + chinese
        self._queue(db, conv['id'], 'feishu', 'send', key,
                    {'text': body, 'original_message_id': message['id']})

    def _delete_ai_mirror(self, db, cid, mid):
        mirror = db.execute('SELECT * FROM outbox WHERE operation_key=?', ('history-ai:' + mid,)).fetchone()
        if mirror:
            if mirror['status'] == 'pending':
                db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (mirror['id'],))
            elif mirror['external_id']:
                self._queue(db, cid, 'feishu', 'delete', 'delete-ai-history:' + mid,
                            {'external_id': mirror['external_id']})

    def _ensure_service_room(self, db, cid, staff_ids=None):
        conv = self._conversation(db, cid)
        selected = tuple(dict.fromkeys(staff_ids if staff_ids is not None else self.default_members))
        if not selected or any(not isinstance(s, str) or not s for s in selected):
            raise ChannelError('invalid_members', 'At least one staff identity is required.')
        for staff in selected:
            inserted = db.execute('INSERT OR IGNORE INTO members VALUES(?,?)', (cid, staff)).rowcount
            if inserted and conv['room_id']:
                self._queue(db, cid, 'feishu', 'add_member', 'member:' + cid + ':' + staff,
                            {'members': [staff]})
        if conv['room_status'] == 'absent':
            db.execute("UPDATE conversations SET room_status='pending' WHERE id=?", (cid,))
            self._queue(db, cid, 'feishu', 'create_room', 'room:' + cid,
                        {'name': 'OmniService ' + cid[-8:]})
            self._queue(db, cid, 'feishu', 'send', 'room-rules:' + cid,
                        {'text': self._room_rules(db, cid) if self.approval_intake else _ROOM_RULES, 'mention_all': True})
        return selected

    def _finish_intake(self, db, cid, reason):
        closed = db.execute('''UPDATE intake_sessions SET handoff_reason=?
            WHERE conversation_id=? AND handoff_reason IS NULL RETURNING facts,source_messages''', (reason, cid)).fetchone()
        if closed is None:
            return
        db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (cid,))
        facts = json.loads(closed['facts'])
        lines = [f'{label}：{facts.get(key) or "待补"}' for key, label in _INTAKE_LABELS.items()]
        title = ('AI 接待结束，请转人工' if self.approval_intake else '自动接待已转人工')
        if self.approval_intake:
            sources = list(json.loads(closed['source_messages']).values())
            latest = db.execute("""SELECT id FROM messages WHERE conversation_id=? AND direction='inbound'
                AND status!='deleted' ORDER BY rowid DESC LIMIT 1""", (cid,)).fetchone()
            if latest:
                sources.append(latest['id'])
            for mid in dict.fromkeys(sources):
                evidence = db.execute("SELECT original FROM messages WHERE id=? AND status!='deleted'", (mid,)).fetchone()
                if evidence:
                    lines.append('客户原话：' + evidence['original'][:800])
        self._notify(db, cid, 'intake:' + cid,
                     title + '（' + _HANDOFF_LABELS[reason] + '）。\n' + '\n'.join(lines),
                     mention_user_id=self._handoff_recipient(db, cid) if self.approval_intake else None)
        self._audit(db, cid, 'intake_handoff', {'reason': reason})

    def _handoff_recipient(self, db, cid):
        row = db.execute('''SELECT user_id FROM members WHERE conversation_id=?
            ORDER BY (user_id=?) DESC, rowid LIMIT 1''', (cid, self.handoff_staff_id)).fetchone()
        return row['user_id'] if row else None

    def _handoff(self, db, cid, staff_ids=None, *, intake_reason='manual_handoff'):
        selected = self._ensure_service_room(db, cid, staff_ids)
        self._cancel_ai(db, cid)
        self._finish_intake(db, cid, intake_reason)
        db.execute("UPDATE conversations SET mode='manual' WHERE id=?", (cid,))
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
            if self.intake is not None:
                raise ChannelError('intake_new_conversations_only')
            db.execute("UPDATE conversations SET mode='auto' WHERE id=?", (conversation_id,))
            self._audit(db, conversation_id, 'resume_ai', {})
            return {'status': 'auto', 'conversation_id': conversation_id}

    def _forget_intake_source(self, db, cid, mid):
        row = db.execute('SELECT facts,source_messages FROM intake_sessions WHERE conversation_id=?', (cid,)).fetchone()
        if row is None:
            return
        sources = json.loads(row['source_messages'])
        removed = [key for key, source in sources.items() if source == mid]
        if removed:
            facts = json.loads(row['facts'])
            for key in removed:
                del sources[key], facts[key]
            db.execute('UPDATE intake_sessions SET facts=?,source_messages=? WHERE conversation_id=?',
                       (_json(facts), _json(sources), cid))

    def _draft(self, db, conv, translated, *, kind, parent_id=None, reference=''):
        cid = conv['id']
        self._cancel_drafts(db, cid)
        draft_id = _id('draft_')
        customer_text = self._display_reply(translated, conv['language'], 'AI')
        if '@' in customer_text or '＠' in customer_text or channels.feishu_text(customer_text) != customer_text:
            return None  # the preview must represent the exact outbound text
        version = hashlib.sha256(customer_text.encode()).hexdigest()[:16]
        db.execute('''INSERT INTO reply_drafts
            (id,conversation_id,kind,language,translated,customer_text,version,parent_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',
                   (draft_id, cid, kind, conv['language'], translated, customer_text, version, parent_id, self.clock()))
        label = self._source_label(db, cid, 'AI 模拟' if self.intake.simulated else 'AI')
        staff_context = reference + '\n\n' if reference else ''
        self._notify(db, cid, 'draft:' + draft_id,
                     f'{label}机器人草稿，等待客服审批。将向客户发送的译文如下。\n{customer_text}\n\n'
                     f'{staff_context}/approve {draft_id} {version}')
        self._audit(db, cid, 'reply_drafted', {'draft_id': draft_id, 'kind': kind, 'version': version})
        return draft_id

    def _sent_questions(self, db, cid):
        return db.execute("""SELECT count(*) FROM messages WHERE conversation_id=?
            AND source_channel='intake_question' AND external_id IS NOT NULL""", (cid,)).fetchone()[0]

    def _approve(self, db, conv, event, args):
        cid = conv['id']
        if len(args) != 2:
            return {'status': 'approval_invalid', 'conversation_id': cid}
        draft = db.execute('SELECT * FROM reply_drafts WHERE id=? AND conversation_id=? AND version=?',
                           (args[0], cid, args[1])).fetchone()
        if draft is None or draft['status'] == 'cancelled' or draft['language'] != conv['language']:
            return {'status': 'approval_stale', 'conversation_id': cid}
        if draft['status'] == 'approved':
            return {'status': 'duplicate_approval', 'conversation_id': cid}
        if not db.execute("SELECT 1 FROM outbox WHERE operation_key=? AND status='sent'",
                          ('notice:draft:' + draft['id'],)).fetchone():
            return {'status': 'approval_preview_not_delivered', 'conversation_id': cid}
        if draft['kind'] == 'intake_question':
            if conv['mode'] != 'auto' or self._sent_questions(db, cid) >= self.intake.max_replies:
                return {'status': 'approval_stale', 'conversation_id': cid}
        elif not self._room_settings(db, cid)['reply_assist'] or conv['mode'] != 'manual':
            return {'status': 'agent_disabled', 'conversation_id': cid}
        if (conv['channel'] == 'whatsapp' and self.clock() - conv['last_customer_at'] >= 86400):
            return {'status': 'blocked_window', 'conversation_id': cid}
        mid = self._message(db, cid, direction='outbound', source_channel=draft['kind'],
                            source_id=draft['id'], author_id='AI', text=draft['translated'],
                            translated=draft['translated'], language=draft['language'],
                            parent_id=draft['parent_id'], status='queued')
        db.execute("UPDATE reply_drafts SET status='approved',approved_by=?,message_id=? WHERE id=?",
                   (event.user_id, mid, draft['id']))
        self._queue(db, cid, conv['channel'], 'send', 'out:' + mid,
                    {'text': draft['customer_text'], 'recipient_type': conv['recipient_type']}, mid)
        self._audit(db, cid, 'reply_approved', {'draft_id': draft['id'], 'version': draft['version'],
                                              'actor': event.user_id, 'message_id': mid})
        return {'status': 'approved', 'conversation_id': cid, 'message_id': mid}

    def _queue_agent_job(self, db, conv):
        cid = conv['id']
        self._cancel_agent_work(db, cid)
        request_id = _id('agent_')
        db.execute('INSERT INTO agent_reply_jobs VALUES(?,?,?)', (cid, request_id, self.clock()))
        self._audit(db, cid, 'agent_reply_requested', {'request_id': request_id})

    def _agent_context(self, db, cid):
        rows = db.execute("""SELECT direction,author_id,original FROM messages WHERE conversation_id=?
            AND (direction='inbound' AND status!='deleted' OR direction='outbound' AND status='sent')
            ORDER BY rowid DESC LIMIT 12""", (cid,)).fetchall()
        transcript = []
        for row in reversed(rows):
            actor = 'customer' if row['direction'] == 'inbound' else ('ai' if row['author_id'] == 'AI' else 'staff')
            if row['original'].strip():
                transcript.append({'actor': actor, 'text': row['original'][:500]})
        last_customer = next((turn['text'] for turn in reversed(transcript) if turn['actor'] == 'customer'), '')
        session = db.execute('SELECT facts FROM intake_sessions WHERE conversation_id=?', (cid,)).fetchone()
        facts = json.loads(session['facts']) if session else {}
        confirmed = '、'.join(f'{label} {facts[key]}' for key, label in _INTAKE_LABELS.items() if key in facts)
        reference = '\n'.join(part for part in (
            f'供客服参考，客户最近来信：{last_customer}' if last_customer else '',
            f'接待阶段已确认：{confirmed}' if confirmed else '',
        ) if part)
        return transcript, reference

    def _one_agent_job(self):
        with self.store.transaction() as db:
            job = db.execute('''SELECT j.* FROM agent_reply_jobs j JOIN conversations c ON c.id=j.conversation_id
                WHERE c.room_status='ready' ORDER BY j.requested_at,j.request_id LIMIT 1''').fetchone()
            if job is None:
                return False
            cid, request_id = job['conversation_id'], job['request_id']
            conv = self._conversation(db, cid)
            if (self.agent_advisor is None or conv['mode'] != 'manual'
                    or not self._room_settings(db, cid)['reply_assist']):
                db.execute('DELETE FROM agent_reply_jobs WHERE conversation_id=?', (cid,))
                return True
            transcript, reference = self._agent_context(db, cid)
        translated = None
        try:
            chinese = self.agent_advisor(transcript)
            if isinstance(chinese, str) and chinese.strip():
                translated = self.language.translate(chinese, 'zh', conv['language'])
        except ChannelError:
            pass
        with self.store.transaction() as db:
            if not db.execute('DELETE FROM agent_reply_jobs WHERE conversation_id=? AND request_id=?',
                              (cid, request_id)).rowcount:
                return True
            current = self._conversation(db, cid)
            if (current['mode'] != 'manual' or current['language'] != conv['language']
                    or not self._room_settings(db, cid)['reply_assist']):
                return True
            if not isinstance(translated, str) or not translated.strip() or self._draft(
                    db, current, translated, kind='agent_draft', reference=reference) is None:
                self._notify(db, cid, 'advisor:' + request_id, 'Agent 草稿暂不可用，请人工处理。')
        return True

    def _guide_customer(self, db, conv, event, mid, language, *, first):
        cid = conv['id']
        session = db.execute('SELECT * FROM intake_sessions WHERE conversation_id=?', (cid,)).fetchone()
        if session is None:
            self._handoff(db, cid)
            return
        if session['handoff_reason'] is not None or not self.intake_enabled:
            return
        if first and self.approval_intake:
            self._outgoing(db, conv, self.intake.welcome(language), source_channel='ai_welcome',
                           already_translated=True)
        if event.attachments:
            self._handoff(db, cid, intake_reason='media')
            return
        # Several customer messages may arrive before the worker sends a question.
        # Keep the latest question and count only replies that were not cancelled.
        self._cancel_ai(db, cid)
        replies = self._sent_questions(db, cid) if self.approval_intake else db.execute('''
            SELECT count(*) FROM messages WHERE conversation_id=? AND author_id='AI' AND status!='cancelled'
        ''', (cid,)).fetchone()[0]
        previous = json.loads(session['facts'])
        sources = json.loads(session['source_messages'])
        decision = self.intake.evaluate(event.text, language, previous, replies,
                                        first=not self.approval_intake and (first or replies == 0),
                                        handoff_on_last=not self.approval_intake)
        for key in decision.facts.keys() - previous.keys():
            sources[key] = mid
        db.execute('UPDATE intake_sessions SET facts=?,source_messages=? WHERE conversation_id=?',
                   (_json(decision.facts), _json(sources), cid))
        if decision.reason:
            self._handoff(db, cid, intake_reason=decision.reason)
        if self.approval_intake:
            if not decision.reason and decision.text:
                self._draft(db, conv, decision.text, kind='intake_question', parent_id=mid)
        elif decision.text:
            self._outgoing(db, conv, decision.text, parent_id=mid, already_translated=True)

    def ingest(self, event):
        if event.is_bot:
            return {'status': 'ignored_bot'}
        if event.channel not in ('telegram', 'max', 'whatsapp', 'feishu'):
            raise ChannelError('invalid_channel', 'Unsupported source channel.')
        if not all(isinstance(v, str) and v for v in (event.account_id, event.event_id, event.chat_id, event.message_id)):
            raise ChannelError('invalid_event', 'The event is missing stable source identifiers.')
        data = asdict(event)
        if data.get('has_mentions') is False:
            del data['has_mentions']  # preserve inbox hashes for older events without mentions
        digest = hashlib.sha256(_json(data).encode()).hexdigest()
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
            if self.intake is not None:
                self._forget_intake_source(db, cid, existing['id'])
            if self.approval_intake:
                self._cancel_ai(db, cid)
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
        first_contact = False
        if self.intake is not None and event.event_type == 'message' and not conv['last_customer_at']:
            first_contact = db.execute('SELECT 1 FROM messages WHERE conversation_id=?', (cid,)).fetchone() is None
            if first_contact:
                self._ensure_service_room(db, cid)
                mode = 'auto' if self.intake_enabled else 'manual'
                db.execute('UPDATE conversations SET mode=? WHERE id=?', (mode, cid))
                if self.intake_enabled:
                    db.execute('INSERT INTO intake_sessions(conversation_id) VALUES(?)', (cid,))
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
            if self.intake is not None:
                self._forget_intake_source(db, cid, existing['id'])
        elif self.approval_intake and conv['mode'] == 'manual' and timestamp >= conv['language_at']:
            self._cancel_ai(db, cid)
        self._mirror(db, conv, message)
        if (self.approval_intake and conv['mode'] == 'manual' and event.event_type == 'message'
                and timestamp >= conv['language_at'] and self._room_settings(db, cid)['reply_assist']):
            self._queue_agent_job(db, conv)
        if conv['mode'] == 'auto' and event.event_type == 'message' and self.intake is not None:
            if timestamp >= conv['language_at']:
                self._guide_customer(db, conv, event, mid, language, first=first_contact)
            else:
                self._audit(db, cid, 'stale_message_no_auto_reply', {'message_id': mid})
        elif self.intake is None and conv['mode'] == 'auto' and timestamp >= conv['language_at']:
            answer = self.language.reply(event.text, language) if not event.attachments else {'action': 'handoff'}
            if answer['action'] == 'reply':
                self._outgoing(db, conv, answer['text'], parent_id=mid, already_translated=True)
            else:
                self._handoff(db, cid)
        elif conv['mode'] == 'auto':
            self._audit(db, cid, 'stale_message_no_auto_reply', {'message_id': mid})
        self._audit(db, cid, 'customer_message', {'message_id': mid, 'language': language})
        return {'status': 'accepted', 'conversation_id': cid, 'message_id': mid}

    def _change_room_setting(self, db, cid, command, arguments):
        key = _PREFERENCE_COMMANDS[command]
        if len(arguments) != 1 or arguments[0] not in ('开', '关', '状态'):
            return 'settings_invalid', f'用法 {command} 开|关|状态'
        action = arguments[0]
        if key == 'reply_assist' and action == '开' and not callable(self.agent_advisor):
            return 'agent_unavailable', '当前服务端没有配置 Agent 回复辅助。'
        if action != '状态':
            enabled = action == '开'
            db.execute('INSERT OR IGNORE INTO conversation_settings(conversation_id) VALUES(?)', (cid,))
            db.execute(f'UPDATE conversation_settings SET {key}=? WHERE conversation_id=?', (int(enabled), cid))
            if key == 'reply_assist' and not enabled:
                self._cancel_agent_work(db, cid)
            self._audit(db, cid, 'room_setting_changed', {'setting': key, 'enabled': enabled})
        state = '开' if self._room_settings(db, cid)[key] else '关'
        return ('settings_status' if action == '状态' else 'settings_updated'), f'{command[1:]}当前：{state}'

    def _room_command(self, db, conv, event, text):
        cid = conv['id']
        command, *args = text.split()
        if not self.approval_intake:
            return {'status': 'internal_only', 'conversation_id': cid}
        if command == '/approve':
            result = self._approve(db, conv, event, args)
            feedback = {
                'approved': '译文已批准，等待平台投递回执。',
                'duplicate_approval': '该译文已处理，请查看对应的投递结果。',
                'approval_invalid': '请复制机器人草稿下方的完整 /approve 指令。',
                'approval_stale': '该草稿版本已失效或不属于本群，请查看最新草稿。',
                'approval_preview_not_delivered': '译文预览还未确认送达，请等群内预览。',
                'agent_disabled': 'Agent 辅助已关闭，草稿未发出。',
                'blocked_window': '客户消息窗口已过期，草稿未发出。',
            }
            self._notify(db, cid, 'approval:' + event.message_id, feedback[result['status']])
            return result
        if command in _PREFERENCE_COMMANDS:
            status, feedback = self._change_room_setting(db, cid, command, args)
        elif command == '/draft':
            if args:
                status, feedback = 'approval_invalid', '用法 /draft'
            elif conv['mode'] != 'manual' or not self._room_settings(db, cid)['reply_assist']:
                status, feedback = 'agent_disabled', '客服跟进阶段的 Agent 辅助未开启或尚未转人工。'
            else:
                self._queue_agent_job(db, conv)
                return {'status': 'draft_queued', 'conversation_id': cid}
        else:
            status, feedback = 'internal_only', '该群命令暂不可用。可用 /回复辅助 状态 或 /标签 状态 查询。'
        self._notify(db, cid, 'command:' + event.message_id, feedback)
        return {'status': status, 'conversation_id': cid}

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
        if event.has_mentions or '@' in event.text or '＠' in event.text:
            self._audit(db, conv['id'], 'staff_internal_mention', {'source_message_id': event.message_id})
            return {'status': 'internal_only', 'conversation_id': conv['id']}
        command_text = event.text.lstrip()
        if command_text.startswith(('/', '／')):
            return self._room_command(db, conv, event, '/' + command_text[1:])
        if existing and event.event_type != 'edit':
            return {'status': 'duplicate_message', 'conversation_id': conv['id'], 'message_id': existing['id']}
        self._cancel_ai(db, conv['id'])
        if self.intake is not None:
            self._finish_intake(db, conv['id'], 'staff_reply')
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
            if self.intake is not None:
                session = db.execute('SELECT facts,source_messages,handoff_reason FROM intake_sessions WHERE conversation_id=?',
                                     (conversation_id,)).fetchone()
                if session:
                    result['intake'] = {**dict(session), 'facts': json.loads(session['facts']),
                                        'source_messages': json.loads(session['source_messages'])}
            if self.approval_intake:
                result['settings'] = self._room_settings(db, conversation_id)
                result['drafts'] = [dict(row) for row in db.execute('''SELECT * FROM reply_drafts
                    WHERE conversation_id=? ORDER BY rowid''', (conversation_id,))]
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
                                     recipient_type=payload.get('recipient_type', 'chat'),
                                     mention_all=payload.get('mention_all', False),
                                     mention_user_id=payload.get('mention_user_id'))

    def _deliver(self, item, conv, request):
        payload = self.transport.send(item['channel'], request)
        return channels.parse_response(item['channel'], item['operation'], payload)

    def _check_outbound_approval(self, db, item):
        if not self.approval_intake or item['channel'] == 'feishu' or not item['message_id']:
            return
        message = db.execute('SELECT author_id,source_channel FROM messages WHERE id=?', (item['message_id'],)).fetchone()
        if not message or message['author_id'] != 'AI' or message['source_channel'] == 'ai_welcome':
            return
        draft = db.execute('''SELECT customer_text,kind FROM reply_drafts WHERE conversation_id=?
            AND message_id=? AND status='approved' ''', (item['conversation_id'], item['message_id'])).fetchone()
        if not draft or draft['customer_text'] != json.loads(item['payload']).get('text'):
            raise ChannelError('approval_required')
        if draft['kind'] == 'agent_draft' and not self._room_settings(db, item['conversation_id'])['reply_assist']:
            raise ChannelError('agent_disabled')

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
                            if item['operation'] == 'send':
                                self._check_outbound_approval(db, item)
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
                    if self._one_agent_job():
                        continue
                    break
                try:
                    external_id = self._deliver(item, conv, request)
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
                        if item['operation'] == 'create_room' and db.execute('''SELECT 1 FROM conversations
                            WHERE room_id=? AND id!=?''', (external_id, item['conversation_id'])).fetchone():
                            self._failure(db, item, ChannelError('delivery_unknown'))
                            outcomes.append({'id': item['id'], 'status': 'delivery_unknown'})
                            continue
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
                if self.approval_intake:
                    message = db.execute('SELECT source_channel FROM messages WHERE id=?', (item['message_id'],)).fetchone()
                    if message and message['source_channel'] == 'intake_question':
                        self._handoff(db, item['conversation_id'], intake_reason='reply_not_confirmed')
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
                if (self.approval_intake and msg['source_channel'] == 'intake_question'
                        and self._sent_questions(db, item['conversation_id']) >= self.intake.max_replies):
                    self._handoff(db, item['conversation_id'], intake_reason='reply_limit')
            elif (self.approval_intake and msg['status'] == 'sent' and msg['source_channel'] == 'feishu'
                  and self._room_settings(db, item['conversation_id'])['show_labels']):
                self._queue(db, item['conversation_id'], 'feishu', 'send', 'staff-sent:' + msg['id'],
                            {'text': '[同事] 已发给客户：' + msg['translated']})
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
