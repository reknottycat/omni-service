"""Independent loopback fake platforms/models for the runnable personal demo.

These are synthetic API responders, never production clients or model fallbacks.
The actual runtime, HTTP clients, SQLite state and management API are exercised.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .knowledge import KnowledgeBase
from .language import PHRASES


class PersonalPlatforms:
    def __init__(self, knowledge_path):
        self.knowledge = KnowledgeBase(knowledge_path)
        self.lock = threading.RLock()
        self.updates = []
        self.requests = []
        self.telegram_sent = []
        self.telegram_deleted = []
        self.feishu_sent = []
        self.feishu_deleted = []
        self.rooms = []
        self.connections = {}
        self.model_requests = []
        self.sequence = 1000
        self.webhook_url = ''
        self.fail_next_telegram_send = False
        self.drop_next_receipt = False
        self.translation_down = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.handle_api()

            def do_POST(self):
                self.handle_api()

            def do_DELETE(self):
                self.handle_api()

            def handle_api(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(data) if data else {}
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.requests.append({'method': self.command, 'path': path, 'body': body})
                    code, payload = owner.respond(path, body, self.command)
                if code == 0:
                    self.close_connection = True
                    return
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def push(self, update):
        with self.lock:
            self.updates.append(update)
            connection = update.get('business_connection')
            if connection:
                self.connections[connection['id']] = connection

    def respond(self, path, body, method):
        if path.endswith('/getMe'):
            return 200, {'ok': True, 'result': {'id': 900, 'is_bot': True, 'can_connect_to_business': True}}
        if path.endswith('/getWebhookInfo'):
            return 200, {'ok': True, 'result': {'url': self.webhook_url}}
        if path.endswith('/getBusinessConnection'):
            connection = self.connections.get(body['business_connection_id'])
            if not connection:
                return 400, {'ok': False, 'error_code': 400}
            return 200, {'ok': True, 'result': connection}
        if path.endswith('/getUpdates'):
            offset = body['offset']
            self.updates = [u for u in self.updates if u['update_id'] >= offset]
            return 200, {'ok': True, 'result': list(self.updates)}
        if path.endswith('/sendMessage'):
            if self.fail_next_telegram_send:
                self.fail_next_telegram_send = False
                return 429, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}}
            self.sequence += 1
            self.telegram_sent.append({'message_id': self.sequence, **body})
            if self.drop_next_receipt:
                self.drop_next_receipt = False
                return 0, None
            return 200, {'ok': True, 'result': {'message_id': self.sequence, 'chat': {'id': body['chat_id']},
                                               'business_connection_id': body['business_connection_id']}}
        if path.endswith('/deleteBusinessMessages'):
            self.telegram_deleted.append(body)
            return 200, {'ok': True, 'result': True}
        if path == '/oauth/v3/token':
            return 200, {'code': 0, 'access_token': 'synthetic-tenant-token', 'expires_in': 7200, 'token_type': 'Bearer'}
        if path == '/open-apis/bot/v3/info/':
            return 200, {'code': 0, 'bot': {'app_name': 'Omni Test', 'open_id': 'ou_bot'}}
        if path == '/open-apis/im/v1/chats':
            room = {'chat_id': f'oc_room_{len(self.rooms) + 1}', **body}
            self.rooms.append(room)
            return 200, {'code': 0, 'data': {'chat_id': room['chat_id']}}
        if re.fullmatch(r'/open-apis/im/v1/chats/[^/]+/members', path):
            return 200, {'code': 0, 'data': {'invalid_id_list': []}}
        if path == '/open-apis/im/v1/messages':
            mid = f'om_mirror_{len(self.feishu_sent) + 1}'
            self.feishu_sent.append({'message_id': mid, **body})
            return 200, {'code': 0, 'data': {'message_id': mid}}
        if method == 'DELETE' and path.startswith('/open-apis/im/v1/messages/'):
            self.feishu_deleted.append(path.rsplit('/', 1)[1])
            return 200, {'code': 0, 'data': {}}
        if path == '/v1/chat/completions':
            self.model_requests.append(body)
            if self.translation_down and body['model'] == 'synthetic-translation':
                return 503, {'error': 'synthetic model unavailable'}
            user = json.loads(body['messages'][-1]['content'])
            if 'candidates' in user:
                candidates = user['candidates']
                identifier = candidates[0]['id'] if candidates else None
                output = {'action': 'reply' if identifier else 'handoff', 'answer_id': identifier,
                          'source_ids': [identifier] if identifier else []}
            elif 'target_language' in user:
                source, target = user['source_language'], user['target_language']
                text = user['text']
                translated = None
                for row in PHRASES:
                    if row.get(source) == text:
                        translated = row.get(target)
                        break
                if translated is None:
                    for entry in self.knowledge.entries.values():
                        if entry.answers.get(source) == text:
                            translated = entry.answers.get(target)
                            break
                # Only a fixed sample beyond PHRASES is supported; no fake generic translation.
                if translated is None and text == 'Какие документы нужны для отправки груза?':
                    translated = '发送货物需要哪些文件？' if target == 'zh' else None
                if translated is None:
                    return 503, {'error': 'synthetic translation fixture missing'}
                output = {'translation': translated, 'source_language': source, 'target_language': target}
            else:
                text = user['text']
                language = 'ru' if re.search(r'[\u0400-\u04ff]', text) else ('zh' if re.search(r'[\u3400-\u9fff]', text) else 'en')
                output = {'language': language, 'confidence': 0.99}
            return 200, {'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': json.dumps(output, ensure_ascii=False)}}]}
        return 404, {'error': 'unknown_synthetic_endpoint'}


def connection_update(uid=1, *, enabled=True):
    return {'update_id': uid, 'business_connection': {
        'id': 'synthetic-connection', 'user': {'id': 10, 'is_bot': False}, 'date': 1,
        'user_chat_id': 10, 'is_enabled': enabled,
        'rights': {'can_reply': True, 'can_delete_sent_messages': True}}}


def customer_update(uid, text='Здравствуйте', *, chat=20, sender=None, timestamp=None, message_id=None):
    import time
    return {'update_id': uid, 'business_message': {
        'business_connection_id': 'synthetic-connection',
        'chat': {'id': chat, 'type': 'private'},
        'from': {'id': sender if sender is not None else chat, 'is_bot': False, 'first_name': 'Synthetic'},
        'message_id': message_id or uid, 'date': int(timestamp or time.time()), 'text': text}}


def staff_payload(room, sequence=1, *, staff='ou_staff_1', text='请提供订单号。', app='cli_test'):
    import time
    return {'schema': '2.0', 'header': {'event_id': f'ev_staff_{sequence}',
            'event_type': 'im.message.receive_v1', 'app_id': app},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': staff}},
                      'message': {'message_id': f'om_staff_{sequence}', 'chat_id': room,
                                  'chat_type': 'group', 'message_type': 'text',
                                  'create_time': str(int(time.time() * 1000)),
                                  'content': json.dumps({'text': text}, ensure_ascii=False)}}}


def recall_payload(room, sequence=1):
    import time
    return {'schema': '2.0', 'header': {'event_id': f'ev_recall_{sequence}',
            'event_type': 'im.message.recalled_v1', 'app_id': 'cli_test'},
            'event': {'message_id': f'om_staff_{sequence}', 'chat_id': room,
                      'recall_time': str(int(time.time() * 1000)), 'recall_type': 'message_owner'}}
