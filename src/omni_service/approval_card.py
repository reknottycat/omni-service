"""Feishu approval card rendering and the callback wire boundary."""

import re

from .models import ChannelError, DraftDecision


CARD_ACTION = 'card.action.trigger'
_DRAFT_ID = re.compile(r'draft_[a-f0-9]{32}')
_VERSION = re.compile(r'[a-f0-9]{16}')
_EVENT_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_CHAT_ID = re.compile(r'oc_[A-Za-z0-9_]{1,125}')
_MESSAGE_ID = re.compile(r'om_[A-Za-z0-9_]{1,125}')
_USER_ID = re.compile(r'(?:ou_[A-Za-z0-9_]{1,125}|employee-[0-9]{1,10})')
_VALUE_FIELDS = {'omni_action', 'draft_id', 'version'}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _plain(content, **style):
    return {'tag': 'plain_text', 'content': content, **style}


def build_approval_card(*, draft_id, version, customer_text, reference=''):
    if (not _matches(_DRAFT_ID, draft_id) or not _matches(_VERSION, version)
            or not isinstance(customer_text, str) or not customer_text.strip()
            or not isinstance(reference, str)):
        raise ChannelError('invalid_payload')

    elements = [{'tag': 'div', 'element_id': 'customer_text', 'text': _plain(customer_text)}]
    if reference:
        elements.append({'tag': 'div', 'element_id': 'staff_reference',
                         'text': _plain(reference, text_size='notation', text_color='grey')})
    for label, decision, style in (('可以发送', 'approve', 'primary_filled'),
                                   ('不可以发送', 'reject', 'danger')):
        elements.append({
            'tag': 'button', 'element_id': decision, 'text': _plain(label), 'type': style,
            'behaviors': [{'type': 'callback', 'value': {
                'omni_action': decision, 'draft_id': draft_id, 'version': version,
            }}],
        })
    return {
        'schema': '2.0',
        'config': {'width_mode': 'default', 'enable_forward': False},
        'header': {'template': 'blue', 'title': _plain('对客译文待确认')},
        'body': {'direction': 'vertical', 'vertical_spacing': '8px', 'elements': elements},
    }


def card_decision(*, app_id, event_id, chat_id, user_id, message_id, value):
    if (not isinstance(app_id, str) or not app_id
            or not _matches(_EVENT_ID, event_id) or not _matches(_CHAT_ID, chat_id)
            or not _matches(_USER_ID, user_id) or not _matches(_MESSAGE_ID, message_id)
            or not isinstance(value, dict) or value.keys() != _VALUE_FIELDS
            or value['omni_action'] not in ('approve', 'reject')
            or not _matches(_DRAFT_ID, value['draft_id']) or not _matches(_VERSION, value['version'])):
        raise ChannelError('invalid_payload')
    return DraftDecision(app_id, event_id, chat_id, user_id, message_id,
                         value['draft_id'], value['version'], value['omni_action'])


def parse_feishu_card_action(payload, *, app_id):
    """Parse the Feishu 2.0 event envelope used by the authenticated HTTP simulator."""
    try:
        header, event = payload['header'], payload['event']
        action, context = event['action'], event['context']
        if (payload['schema'] != '2.0' or header['event_type'] != CARD_ACTION
                or header.get('app_id', app_id) != app_id or event['host'] != 'im_message'
                or action['tag'] != 'button' or action.get('form_value')):
            raise ChannelError('invalid_payload')
        return card_decision(app_id=app_id, event_id=header['event_id'],
                             chat_id=context['open_chat_id'], user_id=event['operator']['open_id'],
                             message_id=context['open_message_id'], value=action['value'])
    except (KeyError, TypeError, AttributeError):
        raise ChannelError('invalid_payload') from None
