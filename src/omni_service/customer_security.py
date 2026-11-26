"""Recognize Telegram text that should be passed to a human instead of a model."""

import re
import unicodedata


_CONTROL_TEXT = re.compile(
    r'^\s*/(?:approve|reject|draft|takeover|resume|回复辅助|自动回复|审批|标签|负责人)\b'
    r'|<\|(?:im_start|im_end|system|assistant|developer|endoftext|start_header_id|end_header_id)\|>'
    r'|\[(?:/?inst|/?system|/?developer)\]|</?(?:system|developer|assistant)\s*>'
    r'|["\']role["\']\s*:\s*["\'](?:system|developer|assistant)["\']'
    r'|\b(?:ignore|override|disregard|forget)\b[^.!?\n]{0,70}'
    r'\b(?:instructions?|system\s+(?:messages?|prompts?)|developer\s+messages?|safety\s+rules)\b'
    r'|\b(?:show|reveal|print|leak|output)\b[^.!?\n]{0,60}\b(?:system prompt|developer message|api key|bot token)\b'
    r'|(?:忽略|覆盖|绕过|无视|泄露|输出|展示|透露)[^。！？\n]{0,30}'
    r'(?:指令|系统提示|提示词|前置规则|安全规则|审批规则|机器人令牌|密钥)'
    r'|\b(?:игнориру\w*|забуд\w*|раскро\w*|покажи\w*)\b[^.!?\n]{0,65}'
    r'\b(?:инструкц\w*|системн\w+\s+(?:промпт\w*|сообщени\w*)|токен\w*)\b',
    re.IGNORECASE | re.MULTILINE,
)


def customer_control_text(text):
    normalized = unicodedata.normalize('NFKC', text)
    normalized = ''.join(char for char in normalized if unicodedata.category(char) != 'Cf')
    return _CONTROL_TEXT.search(normalized) is not None
