"""Management policy shared by the web console and the Feishu rooms."""

import hashlib
import json
import re
import secrets
import uuid

from .models import ChannelError


COMMANDS = {
    'reply_assist': '回复辅助', 'show_labels': '来源标签', 'auto_reply': '自动回复',
    'approval_required': '审批设置', 'handoff_assignee': '会话负责人',
    'draft': '生成待审草稿', 'approve': '处理审批',
}
POLICIES = ('room_member', 'staff', 'admin')
_ROOM_FIELDS = {'reply_assist', 'show_labels', 'auto_reply', 'approval_required', 'handoff_staff_id'}
_FEISHU_OPEN_ID = re.compile(r'ou_[A-Za-z0-9_]{1,125}')
_STAFF_INVITE_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
_STAFF_INVITE_TTL = 86400
_STAFF_INVITE_RETENTION = 7 * 86400
_STAFF_INVITE_LIMIT = 200
_STAFF_INVITE_FIELDS = ('id', 'status', 'expected_user_id', 'user_id', 'display_name', 'created_at', 'expires_at')


def _identities(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 50 or not all(
            isinstance(value, str) and re.fullmatch(r'(?:ou_[A-Za-z0-9_]+|employee-[A-Za-z0-9_-]+)', value)
            for value in values) or len(set(values)) != len(values):
        raise ChannelError('invalid_admin_settings')
    return list(values)


def _invite_status(row, now):
    status = row['status']
    return 'expired' if status in ('open', 'pending') and row['expires_at'] <= now else status


def _public_invite(row, now):
    view = {field: row[field] for field in _STAFF_INVITE_FIELDS}
    view['status'] = _invite_status(row, now)
    return view


class Management:
    def configure_management(self, admin_ids, *, welcome_document='', preview_peers=()):
        admins = _identities(admin_ids)
        self.welcome_preview_peers = frozenset(map(str, preview_peers))
        initial = {'admin_ids': admins, 'staff_ids': list(self.default_members),
                   'handoff_staff_id': self.handoff_staff_id, 'reply_assist': self.agent_enabled,
                   'handoff_strategy': 'fixed',
                   'show_labels': False, 'welcome_document': welcome_document,
                   'published_welcome_version': None}
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO management_config VALUES(1,?)', (json.dumps(initial),))

    def _management(self, db):
        row = db.execute('SELECT settings FROM management_config WHERE singleton=1').fetchone()
        return json.loads(row['settings']) if row else None

    def _save_management(self, db, config):
        db.execute('UPDATE management_config SET settings=? WHERE singleton=1', (json.dumps(config),))

    def _management_role(self, db, actor, config):
        if config and actor in config['admin_ids']:
            return 'admin'
        if actor in (config['staff_ids'] if config else self.default_members):
            return 'staff'
        if db.execute('SELECT 1 FROM members WHERE user_id=? UNION SELECT 1 FROM verified_room_members WHERE user_id=?',
                      (actor, actor)).fetchone():
            return 'member'
        raise ChannelError('forbidden')

    def _require_admin(self, db, actor):
        config = self._management(db)
        if not config or actor not in config['admin_ids']:
            raise ChannelError('forbidden')
        return config

    def _command_policy(self, db, command):
        row = db.execute('SELECT policy FROM command_permissions WHERE command_id=?', (command,)).fetchone()
        return row['policy'] if row else 'room_member'

    def _require_command(self, db, cid, actor, command, *, from_web=False):
        role = self._management_role(db, actor, self._management(db))
        if not (from_web and role == 'admin') and not self._command_member(db, cid, actor):
            raise ChannelError('forbidden')
        policy = self._command_policy(db, command)
        if (policy == 'admin' and role != 'admin') or (policy == 'staff' and role == 'member'):
            raise ChannelError('forbidden')

    def _command_member(self, db, cid, actor):
        return db.execute('''SELECT 1 FROM members WHERE conversation_id=? AND user_id=?
            UNION SELECT 1 FROM verified_room_members WHERE conversation_id=? AND user_id=?''',
                          (cid, actor, cid, actor)).fetchone() is not None

    def sync_room_members(self, room_id, actual_members):
        """Save a verified platform roster for controls without granting outbound staff rights."""
        users = _identities(sorted(actual_members))
        with self.store.transaction() as db:
            row = db.execute("SELECT id FROM conversations WHERE room_id=? AND room_status='ready'", (room_id,)).fetchone()
            if row is None:
                raise ChannelError('forbidden')
            db.execute('DELETE FROM verified_room_members WHERE conversation_id=?', (row['id'],))
            db.executemany('INSERT INTO verified_room_members VALUES(?,?)', [(row['id'], user) for user in users])

    def _live_room_access(self, actor, cid=None):
        fetch = getattr(self, 'room_members_provider', None)
        if not callable(fetch):
            return None
        with self.store.lock:
            db = self.store.db
            config = self._management(db)
            if config and actor in config['admin_ids']:
                return None
            mapped = db.execute("""SELECT id,room_id FROM conversations WHERE room_status='ready'
                AND (? IS NULL OR id=?)""", (cid, cid)).fetchall()
        authorized = set()
        for row in mapped:
            try:
                members = fetch(row['room_id'])
                self.sync_room_members(row['room_id'], members)
            except ChannelError:
                continue
            if actor in members:
                authorized.add(row['id'])
        return authorized

    def _snapshot_defaults(self, db, cid):
        config = self._management(db)
        if config:
            strategy = config.get('handoff_strategy', 'fixed')
            staff = config['staff_ids']
            index = config.get('handoff_index', 0)
            assignee = staff[index % len(staff)] if strategy == 'round_robin' else config['handoff_staff_id']
            result = db.execute('''INSERT INTO conversation_settings
                (conversation_id,reply_assist,show_labels,handoff_staff_id,welcome_version) VALUES(?,?,?,?,?)
                ON CONFLICT(conversation_id) DO NOTHING''',
                       (cid, config['reply_assist'], config['show_labels'], assignee,
                        config['published_welcome_version']))
            if result.rowcount and strategy == 'round_robin':
                config['handoff_index'] = (index + 1) % len(staff)
                self._save_management(db, config)

    def _assign_handoff(self, db, cid, staff_id):
        if not isinstance(staff_id, str) or not db.execute(
                'SELECT 1 FROM members WHERE conversation_id=? AND user_id=?', (cid, staff_id)).fetchone():
            raise ChannelError('invalid_handoff_staff')
        db.execute('INSERT OR IGNORE INTO conversation_settings(conversation_id) VALUES(?)', (cid,))
        db.execute('UPDATE conversation_settings SET handoff_staff_id=? WHERE conversation_id=?', (staff_id, cid))
        return staff_id

    def admin_state(self, actor):
        live_rooms = self._live_room_access(actor)
        with self.store.lock:
            db = self.store.db
            config = self._management(db)
            if not config:
                raise ChannelError('forbidden')
            role = self._management_role(db, actor, config)
            rooms = []
            for row in db.execute('SELECT id,channel,mode,room_id,room_status,chat_id FROM conversations ORDER BY created_at DESC,id'):
                cid = row['id']
                members = [item['user_id'] for item in db.execute(
                    'SELECT user_id FROM members WHERE conversation_id=? ORDER BY rowid', (cid,))]
                if (role != 'admin' and (live_rooms is not None and cid not in live_rooms
                        or not self._command_member(db, cid, actor))):
                    continue
                sender = db.execute("""SELECT sender_name FROM messages WHERE conversation_id=? AND direction='inbound'
                    AND status!='deleted' AND sender_name!='' ORDER BY rowid DESC LIMIT 1""", (cid,)).fetchone()
                rooms.append({**{key: row[key] for key in row.keys() if key != 'chat_id'},
                              **self._room_settings(db, cid), 'members': members,
                              'handoff_staff_id': self._handoff_recipient(db, cid),
                              'customer_name': sender['sender_name'] if sender else '',
                              'preview_allowed': role == 'admin' and row['channel'] == 'telegram' and
                              row['chat_id'] in self.welcome_preview_peers})
            published = config['published_welcome_version'] if role == 'admin' else None
            return {'role': role, 'conversations': rooms,
                    'admin_ids': config['admin_ids'] if role == 'admin' else [],
                    'staff_ids': config['staff_ids'] if role == 'admin' else [],
                    'defaults': {key: config[key] for key in ('staff_ids','handoff_staff_id','reply_assist','show_labels','welcome_document')}
                    if role == 'admin' else {},
                    'handoff_strategy': config.get('handoff_strategy', 'fixed') if role == 'admin' else None,
                    'commands': [{'id': key, 'label': label, 'policy': self._command_policy(db, key), 'default_policy': 'room_member'}
                                 for key, label in COMMANDS.items()],
                    **({'staff_invites': [_public_invite(row, self.clock()) for row in db.execute(
                        'SELECT * FROM staff_invites ORDER BY created_at DESC, rowid DESC')]}
                       if role == 'admin' else {}),
                    'welcome_versions': [{**dict(row), 'published': row['version'] == published}
                                         for row in db.execute('SELECT version,created_at,published_at FROM welcome_versions ORDER BY created_at DESC')]
                    if role == 'admin' else [], 'published_welcome_version': published}

    def admin_set_command_policy(self, actor, command, policy):
        with self.store.transaction() as db:
            self._require_admin(db, actor)
            if not isinstance(command, str) or command not in COMMANDS or policy not in POLICIES:
                raise ChannelError('invalid_command_policy')
            db.execute('''INSERT INTO command_permissions VALUES(?,?)
                ON CONFLICT(command_id) DO UPDATE SET policy=excluded.policy''', (command, policy))
            self._audit(db, None, 'command_policy_changed', {'command': command, 'policy': policy, 'actor': actor})
        return {'id': command, 'policy': policy}

    def admin_update_defaults(self, actor, updates):
        with self.store.transaction() as db:
            config = self._require_admin(db, actor)
            if not isinstance(updates, dict) or not set(updates) <= {
                    'staff_ids','handoff_staff_id','handoff_strategy','reply_assist','show_labels','welcome_document'}:
                raise ChannelError('invalid_admin_settings')
            config.update(updates)
            config['staff_ids'] = _identities(config['staff_ids'])
            if (config['handoff_staff_id'] not in config['staff_ids']
                    or config.get('handoff_strategy', 'fixed') not in ('fixed', 'round_robin')
                    or type(config['reply_assist']) is not bool
                    or type(config['show_labels']) is not bool or not isinstance(config['welcome_document'], str)
                    or len(config['welcome_document']) > 1500):
                raise ChannelError('invalid_admin_settings')
            self._save_management(db, config)
            self._audit(db, None, 'management_defaults_updated', {'actor': actor, 'fields': sorted(updates)})
        return {'status': 'saved'}

    def admin_create_staff_invite(self, actor, expected_user_id=None):
        with self.store.transaction() as db:
            self._require_admin(db, actor)
            if expected_user_id == '':
                expected_user_id = None
            if expected_user_id is not None and (not isinstance(expected_user_id, str)
                                                 or not _FEISHU_OPEN_ID.fullmatch(expected_user_id)):
                raise ChannelError('invalid_staff_invite')
            now = self.clock()
            db.execute('DELETE FROM staff_invites WHERE expires_at<=?', (now - _STAFF_INVITE_RETENTION,))
            if db.execute('SELECT count(*) FROM staff_invites').fetchone()[0] >= _STAFF_INVITE_LIMIT:
                raise ChannelError('staff_invite_limit')
            token, invite_id = secrets.token_urlsafe(32), 'invite_' + uuid.uuid4().hex
            row = db.execute('''INSERT INTO staff_invites
                (id,token_hash,status,expected_user_id,created_at,expires_at) VALUES(?,?,'open',?,?,?) RETURNING *''',
                             (invite_id, hashlib.sha256(token.encode('ascii')).hexdigest(), expected_user_id,
                              now, now + _STAFF_INVITE_TTL)).fetchone()
            self._audit(db, None, 'staff_invite_created', {'actor': actor, 'invite_id': invite_id})
            return {'token': token, 'invite': _public_invite(row, now)}

    def _enroll_invited_staff(self, db, config, user_id):
        if user_id not in config['staff_ids']:
            if len(config['staff_ids']) >= 50:
                raise ChannelError('staff_roster_full')
            config['staff_ids'].append(user_id)
            self._save_management(db, config)

    def claim_staff_invite(self, actor, token, *, display_name=''):
        if not isinstance(actor, str) or not _FEISHU_OPEN_ID.fullmatch(actor):
            raise ChannelError('forbidden')
        if not isinstance(token, str) or not _STAFF_INVITE_TOKEN.fullmatch(token):
            raise ChannelError('invalid_staff_invite')
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self.store.transaction() as db:
            config = self._management(db)
            if config is None:
                raise ChannelError('forbidden')
            row = db.execute('SELECT * FROM staff_invites WHERE token_hash=?', (token_hash,)).fetchone()
            now = self.clock()
            if row is None or _invite_status(row, now) in ('revoked', 'expired'):
                raise ChannelError('staff_invite_unavailable')
            if row['status'] != 'open':
                if row['user_id'] != actor:
                    raise ChannelError('staff_invite_unavailable')
                return {'status': row['status'], 'invite': _public_invite(row, now)}
            expected = row['expected_user_id']
            if expected is not None and expected != actor:
                raise ChannelError('forbidden')
            status = 'pending'
            if expected == actor:
                self._enroll_invited_staff(db, config, actor)
                status = 'approved'
            claimed = db.execute('''UPDATE staff_invites SET status=?,user_id=?,display_name=?
                WHERE id=? RETURNING *''', (status, actor, display_name, row['id'])).fetchone()
            self._audit(db, None, 'staff_invite_claimed', {'actor': actor, 'invite_id': row['id'], 'status': status})
            return {'status': status, 'invite': _public_invite(claimed, now)}

    def admin_decide_staff_invite(self, actor, invite_id, *, approve):
        with self.store.transaction() as db:
            config = self._require_admin(db, actor)
            if not isinstance(invite_id, str) or not re.fullmatch(r'invite_[a-f0-9]{32}', invite_id):
                raise ChannelError('not_found')
            row = db.execute('SELECT * FROM staff_invites WHERE id=?', (invite_id,)).fetchone()
            if row is None:
                raise ChannelError('not_found')
            now = self.clock()
            status = 'approved' if approve else 'revoked'
            before = _invite_status(row, now)
            if before == status:
                return {'status': status, 'invite': _public_invite(row, now)}
            if before != 'pending' and (approve or before != 'open'):
                raise ChannelError('staff_invite_unavailable')
            if approve:
                self._enroll_invited_staff(db, config, row['user_id'])
            decided = db.execute('UPDATE staff_invites SET status=? WHERE id=? RETURNING *',
                                 (status, invite_id)).fetchone()
            self._audit(db, None, 'staff_invite_decided', {'actor': actor, 'invite_id': invite_id, 'status': status})
            return {'status': status, 'invite': _public_invite(decided, now)}

    def admin_update_conversation(self, actor, cid, updates):
        live_rooms = self._live_room_access(actor, cid)
        if live_rooms is not None and cid not in live_rooms:
            raise ChannelError('forbidden')
        with self.store.transaction() as db:
            self._conversation(db, cid)
            if not isinstance(updates, dict) or not updates or not set(updates) <= _ROOM_FIELDS:
                raise ChannelError('invalid_admin_settings')
            for key, value in updates.items():
                self._require_command(db, cid, actor, 'handoff_assignee' if key == 'handoff_staff_id' else key, from_web=True)
                if key == 'handoff_staff_id':
                    self._assign_handoff(db, cid, value)
                else:
                    self._set_room_setting(db, cid, key, value)
            self._audit(db, cid, 'management_conversation_updated', {'actor': actor, 'fields': sorted(updates)})
            return self._room_settings(db, cid)

    def admin_prepare_welcome(self, actor):
        with self.store.lock:
            config = self._require_admin(self.store.db, actor)
        if not config['welcome_document']:
            raise ChannelError('welcome_document_missing')
        return {'document_url': config['welcome_document']}

    def admin_preview_welcome(self, actor, compiled):
        if (not isinstance(compiled, dict) or set(compiled) != {'items'} or not isinstance(compiled['items'], list)
                or not 1 <= len(compiled['items']) <= 32):
            raise ChannelError('invalid_welcome_template')
        for item in compiled['items']:
            if (not isinstance(item, dict) or item.get('type') not in ('text','image') or
                    (item['type'] == 'text' and (set(item) != {'type','text','html'} or
                     not all(isinstance(item[k], str) and item[k].strip() for k in ('text','html')))) or
                    (item['type'] == 'image' and (set(item) != {'type','mime','data','alt'} or
                     item['mime'] not in ('image/jpeg','image/png') or not isinstance(item['data'], str)
                     or not isinstance(item['alt'], str)))):
                raise ChannelError('invalid_welcome_template')
        content = json.dumps(compiled, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if len(content.encode()) > 24 * 1024 * 1024:
            raise ChannelError('invalid_welcome_template')
        version = hashlib.sha256(content.encode()).hexdigest()
        with self.store.transaction() as db:
            self._require_admin(db, actor)
            db.execute('INSERT OR IGNORE INTO welcome_versions(version,content,created_at) VALUES(?,?,?)',
                       (version, content, self.clock()))
        return {'version': version, 'items': compiled['items'], 'status': 'previewed'}

    def _publish_welcome(self, actor, version, *, rollback):
        with self.store.transaction() as db:
            config = self._require_admin(db, actor)
            row = db.execute('SELECT published_at FROM welcome_versions WHERE version=?', (version,)).fetchone()
            if row is None or (rollback and row['published_at'] is None):
                raise ChannelError('not_found')
            db.execute('UPDATE welcome_versions SET published_at=COALESCE(published_at,?) WHERE version=?',
                       (self.clock(), version))
            config['published_welcome_version'] = version
            self._save_management(db, config)
            self._audit(db, None, 'welcome_rolled_back' if rollback else 'welcome_published', {'actor': actor, 'version': version})
        return {'status': 'published', 'version': version}

    def admin_publish_welcome(self, actor, version):
        return self._publish_welcome(actor, version, rollback=False)

    def admin_rollback_welcome(self, actor, version):
        return self._publish_welcome(actor, version, rollback=True)

    def admin_send_welcome_preview(self, actor, version, cid):
        with self.store.transaction() as db:
            self._require_admin(db, actor)
            conv = self._conversation(db, cid)
            if conv['channel'] != 'telegram' or conv['chat_id'] not in self.welcome_preview_peers:
                raise ChannelError('forbidden')
            if not self._queue_welcome_version(db, conv, version, key='welcome-preview:' + uuid.uuid4().hex):
                raise ChannelError('not_found')
        return {'status': 'queued', 'version': version, 'conversation_id': cid}
