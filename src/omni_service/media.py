"""Bounded, inert media shared by the personal-bridge adapters."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import ChannelError


MAX_IMAGE_BYTES = 10_000_000
MAX_FILE_BYTES = 20_000_000
MEDIA_KINDS = frozenset({'image', 'file', 'audio'})


@dataclass(frozen=True)
class MediaBlob:
    data: bytes
    name: str
    mime: str


def safe_filename(value, fallback='attachment'):
    if not isinstance(value, str):
        return fallback
    name = unicodedata.normalize('NFC', value).replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(char for char in name if unicodedata.category(char)[0] != 'C'
                   and char not in '\"<>:|?*').strip(' .')
    if len(name) > 120:
        suffix = PurePosixPath(name).suffix[:16]
        name = name[:120 - len(suffix)] + suffix
    return name or fallback


def normalize_blob(blob, kind):
    if kind not in MEDIA_KINDS or not isinstance(blob, MediaBlob):
        raise ChannelError('unsupported_media')
    data = blob.data
    if not isinstance(data, bytes) or not data:
        raise ChannelError('media_source_unavailable')
    if len(data) > (MAX_IMAGE_BYTES if kind == 'image' else MAX_FILE_BYTES):
        raise ChannelError('media_too_large')
    if kind == 'image':
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            mime, extension = 'image/png', '.png'
        elif data.startswith(b'\xff\xd8\xff'):
            mime, extension = 'image/jpeg', '.jpg'
        elif data.startswith((b'GIF87a', b'GIF89a')):
            mime, extension = 'image/gif', '.gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            mime, extension = 'image/webp', '.webp'
        else:
            raise ChannelError('unsupported_media')
        name = safe_filename(blob.name, 'photo' + extension)
        if PurePosixPath(name).suffix.lower() not in ({extension, '.jpeg'} if extension == '.jpg' else {extension}):
            name = safe_filename(PurePosixPath(name).stem + extension, 'photo' + extension)
        return MediaBlob(data, name, mime)
    mime = blob.mime if isinstance(blob.mime, str) and re.fullmatch(r'[\w.+-]+/[\w.+-]+', blob.mime, re.ASCII) else ''
    return MediaBlob(data, safe_filename(blob.name), mime or 'application/octet-stream')
