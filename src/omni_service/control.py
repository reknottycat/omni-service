"""Local operator commands; credentials never appear in command-line arguments."""

import argparse
import http.client
import json
import os
from urllib.parse import quote

from .config import load_config, load_env_file
from .models import ChannelError


def call(config, method, path, body=None):
    token = os.environ.get(config.admin['token_env'], '')
    if len(token) < 32 or any(ch in token for ch in ('\r', '\n', '\x00')):
        raise ChannelError('missing_admin_token')
    connection = http.client.HTTPConnection('127.0.0.1', config.admin['port'], timeout=65)
    try:
        wire = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        connection.request(method, path, body=wire,
                           headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ChannelError('management_response_too_large')
        result = json.loads(raw)
        if response.status >= 300:
            error = result.get('error') if isinstance(result, dict) else None
            raise ChannelError(error if isinstance(error, str) else 'management_error')
        return result
    except (OSError, http.client.HTTPException, ValueError):
        raise ChannelError('management_outcome_unknown', 'Read conversation status before repeating a mutation') from None
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Operate the local OmniService management API')
    parser.add_argument('--config', default='config/personal.local.json')
    parser.add_argument('--env-file')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('list')
    for name in ('show', 'handoff', 'resume', 'retry-room'):
        command = sub.add_parser(name)
        command.add_argument('conversation_id')
    for name in ('recall', 'retry-translation'):
        command = sub.add_parser(name)
        command.add_argument('conversation_id')
        command.add_argument('message_id')
        command.add_argument('--actor-id', required=True)
    command = sub.add_parser('resolve')
    command.add_argument('outbox_id', type=int)
    command.add_argument('--delivered', choices=('yes', 'no'), required=True)
    command.add_argument('--external-id', default='')
    command.add_argument('--evidence', required=True)
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        config = load_config(args.config)
        method, body = 'GET', None
        if args.command in ('status', 'list'):
            path = '/api/status' if args.command == 'status' else '/api/conversations'
        elif args.command == 'resolve':
            method, path = 'POST', f'/api/deliveries/{args.outbox_id}/resolve'
            body = {'delivered': args.delivered == 'yes', 'external_id': args.external_id, 'evidence': args.evidence}
        else:
            path = '/api/conversations/' + quote(args.conversation_id, safe='')
            if args.command != 'show':
                method, body = 'POST', {}
                if args.command in ('recall', 'retry-translation'):
                    path += '/messages/' + quote(args.message_id, safe='') + '/' + args.command
                    body = {'actor_id': args.actor_id}
                else:
                    path += '/' + args.command
        print(json.dumps(call(config, method, path, body), ensure_ascii=False, indent=2))
        return 0
    except ChannelError as exc:
        print(json.dumps({'error': exc.code, 'detail': exc.detail}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
