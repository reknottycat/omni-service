"""Opt-in setup diagnostics: display IDs, never message bodies or credentials."""

import argparse
import json
import os
import signal

from .config import load_env_file
from .models import ChannelError


def telegram_metadata(client, *, owner_id=None):
    bot = client.get_me()
    webhook = client.get_webhook_info()
    if webhook.get('url'):
        raise ChannelError('webhook_conflict', 'Use a dedicated bot; this tool never deletes an existing webhook')
    # A single offset=0 read does not advance acknowledgment past any update.
    updates = client.get_updates(0, timeout=0)
    connections = {}
    chats = set()
    for update in updates:
        value = update.get('business_connection')
        if isinstance(value, dict) and isinstance(value.get('id'), str):
            user = value.get('user', {})
            if type(user.get('id')) is int and (owner_id is None or user['id'] == owner_id):
                rights = value.get('rights', {})
                connections[value['id']] = {'connection_id': value['id'], 'owner_user_id': user['id'],
                    'enabled': value.get('is_enabled') is True,
                    'can_reply': isinstance(rights, dict) and rights.get('can_reply') is True,
                    'can_delete_sent': isinstance(rights, dict) and rights.get('can_delete_sent_messages') is True}
    # Only show chat IDs when the operator explicitly supplied the expected owner.
    if owner_id is not None:
        for update in updates:
            value = update.get('business_message')
            if not isinstance(value, dict):
                continue
            cid = value.get('business_connection_id')
            chat = value.get('chat')
            if not isinstance(chat, dict) or chat.get('type') != 'private' or type(chat.get('id')) is not int:
                continue
            if isinstance(cid, str) and cid not in connections:
                data = client.get_connection(cid)
                if isinstance(data.get('user'), dict) and data['user'].get('id') == owner_id:
                    connections[cid] = {'connection_id': cid, 'owner_user_id': owner_id,
                                       'enabled': data.get('is_enabled') is True}
            if cid in connections and chat['id'] != owner_id:
                chats.add(chat['id'])
    return {'bot_id': bot.get('id'), 'bot_username': bot.get('username', ''),
            'secretary_mode': bot.get('can_connect_to_business') is True,
            'connections': list(connections.values()), 'private_chat_ids': sorted(chats),
            'message_bodies_displayed': 0, 'messages_sent': 0,
            'hint': 'Run before the bridge. Authorize only test contacts in Telegram, then copy the chosen IDs into the local config.'}


def employee_setup_id(payload, app_id):
    """Employees voluntarily DM the app /omni setup; all other content is ignored."""
    if not isinstance(payload, dict) or payload.get('schema') != '2.0':
        return None
    header, event = payload.get('header'), payload.get('event')
    if (not isinstance(header, dict) or header.get('app_id') != app_id or
            header.get('event_type') != 'im.message.receive_v1' or not isinstance(event, dict)):
        return None
    sender, message = event.get('sender'), event.get('message')
    if (not isinstance(sender, dict) or sender.get('sender_type') != 'user' or
            not isinstance(message, dict) or message.get('chat_type') != 'p2p' or
            message.get('message_type') != 'text'):
        return None
    try:
        content = json.loads(message.get('content', ''))
    except (ValueError, TypeError):
        return None
    if (not isinstance(content, dict) or not isinstance(content.get('text'), str)
            or content['text'].strip() != '/omni setup'):
        return None
    ids = sender.get('sender_id')
    uid = ids.get('open_id') if isinstance(ids, dict) else None
    return uid if isinstance(uid, str) and uid.startswith('ou_') else None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Print only setup IDs from authorized test-account events')
    parser.add_argument('--env-file')
    sub = parser.add_subparsers(dest='channel', required=True)
    tg = sub.add_parser('telegram', help='One read of pending connection events, no messages sent')
    tg.add_argument('--owner-id', type=int)
    fs = sub.add_parser('feishu', help='Read employee Open IDs from voluntary /omni setup DMs')
    fs.add_argument('--app-id', required=True)
    fs.add_argument('--count', type=int, default=7)
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        if args.channel == 'telegram':
            from .live_clients import TelegramClient
            if args.owner_id is not None and args.owner_id <= 0:
                raise ChannelError('invalid_owner_id')
            result = telegram_metadata(TelegramClient(os.environ.get('TELEGRAM_BOT_TOKEN', '')), owner_id=args.owner_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from .feishu_live import FeishuListener
            if not 1 <= args.count <= 200:
                raise ChannelError('invalid_count')
            found = set()
            def accept(payload):
                uid = employee_setup_id(payload, args.app_id)
                if uid and uid not in found:
                    found.add(uid)
                    print(json.dumps({'open_id': uid, 'employee_number': len(found)}, ensure_ascii=False), flush=True)
                    if len(found) >= args.count:
                        listener.stop()
            listener = FeishuListener(args.app_id, os.environ.get('FEISHU_APP_SECRET', ''), accept)
            old = {sig: signal.signal(sig, lambda *_args: listener.stop()) for sig in (signal.SIGTERM, signal.SIGINT)}
            print(json.dumps({'status': 'waiting_for_employee_setup_dms', 'command': '/omni setup',
                              'expected_count': args.count, 'messages_sent': 0}), flush=True)
            try:
                listener.start()
            finally:
                listener.stop()
                for sig, handler in old.items():
                    signal.signal(sig, handler)
        return 0
    except ChannelError as exc:
        print(json.dumps({'error': exc.code, 'detail': exc.detail}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
