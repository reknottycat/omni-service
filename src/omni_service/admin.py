"""Loopback management API. Live platform events enter through their SDK clients."""

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

from .models import ChannelError


def create_admin_server(service, token, *, host='127.0.0.1', port=8090, status=None):
    if host not in ('127.0.0.1', 'localhost') or not isinstance(token, str) or len(token) < 32:
        raise ValueError('Loopback host and a strong management token are required')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'OmniService/0.2'

        def log_message(self, *_args):
            pass  # URLs and bodies can contain private identifiers.

        def respond(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            supplied = self.headers.get('Authorization', '').encode()
            return hmac.compare_digest(supplied, ('Bearer ' + token).encode())

        def route(self):
            return [unquote(p) for p in urlsplit(self.path).path.strip('/').split('/')]

        def body(self):
            if self.headers.get('Transfer-Encoding'):
                raise ChannelError('invalid_payload')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ChannelError('invalid_payload') from None
            if not 0 <= size <= 65_536:
                raise ChannelError('payload_too_large')
            self.connection.settimeout(5)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ChannelError('invalid_payload')
            try:
                value = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise ChannelError('invalid_payload') from None
            if not isinstance(value, dict):
                raise ChannelError('invalid_payload')
            return value

        def error(self, exc):
            code = getattr(exc, 'code', 'invalid_request')
            status_code = {'forbidden': 403, 'not_found': 404, 'payload_too_large': 413,
                           'invalid_state': 409}.get(code, 400)
            self.respond(status_code, {'error': code})

        def do_GET(self):
            parts = self.route()
            if parts == ['health']:
                snapshot = status() if status else {'state': 'running'}
                # Public health contains no customer IDs or credential metadata.
                state = snapshot.get('state', 'unknown')
                return self.respond(200 if state in ('running', 'ready') else 503,
                                    {'mode': 'personal', 'state': state})
            if not self.authenticated():
                return self.respond(401, {'error': 'unauthorized'})
            try:
                if parts == ['api', 'status']:
                    return self.respond(200, status() if status else {'state': 'running'})
                if parts == ['api', 'conversations']:
                    return self.respond(200, {'conversations': service.list_conversations()})
                if len(parts) == 3 and parts[:2] == ['api', 'conversations']:
                    return self.respond(200, service.get_conversation(parts[2]))
                return self.respond(404, {'error': 'not_found'})
            except ChannelError as exc:
                self.error(exc)

        def do_POST(self):
            if not self.authenticated():
                return self.respond(401, {'error': 'unauthorized'})
            try:
                parts, body = self.route(), self.body()
                if len(parts) == 4 and parts[:2] == ['api', 'conversations']:
                    cid, action = parts[2:]
                    if action == 'handoff':
                        selected = body.get('members')
                        if selected is not None and (not isinstance(selected, list) or
                                any(not isinstance(x, str) or not x for x in selected)):
                            raise ChannelError('invalid_payload')
                        return self.respond(200, service.handoff(cid, selected))
                    if action == 'members':
                        uid = body.get('staff_id')
                        if not isinstance(uid, str) or not uid:
                            raise ChannelError('invalid_payload')
                        return self.respond(200, service.add_member(cid, uid))
                    if action == 'resume':
                        return self.respond(200, service.resume(cid))
                    if action == 'retry-room':
                        return self.respond(200, service.retry_room(cid))
                if len(parts) == 6 and parts[:2] == ['api', 'conversations'] and parts[3] == 'messages':
                    actor = body.get('actor_id')
                    if not isinstance(actor, str) or not actor:
                        raise ChannelError('invalid_payload')
                    if parts[5] == 'recall':
                        return self.respond(200, service.recall(parts[4], parts[2], actor))
                    if parts[5] == 'retry-translation':
                        return self.respond(200, service.retry_translation(parts[4], parts[2], actor))
                if len(parts) == 4 and parts[:2] == ['api', 'deliveries'] and parts[3] == 'resolve':
                    if not parts[2].isdigit() or type(body.get('delivered')) is not bool:
                        raise ChannelError('invalid_payload')
                    external_id = body.get('external_id', '')
                    evidence = body.get('evidence', '')
                    if (not isinstance(external_id, str) or len(external_id) > 256 or
                            not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500):
                        raise ChannelError('invalid_payload')
                    return self.respond(200, service.resolve_delivery(
                        int(parts[2]), delivered=body['delivered'], external_id=external_id, evidence=evidence))
                return self.respond(404, {'error': 'not_found'})
            except (ChannelError, ValueError, KeyError, TypeError) as exc:
                self.error(exc)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
