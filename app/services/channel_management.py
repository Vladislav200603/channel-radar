"""Short-lived confirmation tokens scoped to one concrete channel record."""

import hashlib
import hmac
import re
import secrets
import time

from app.models import Channel

_TOKEN_PATTERN = re.compile(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}")


def deletion_token(channel: Channel, signing_key: bytes) -> str:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = _signature(channel, timestamp, nonce, signing_key)
    return f"{timestamp}.{nonce}.{signature}"


def _signature(channel: Channel, timestamp: str, nonce: str, signing_key: bytes) -> str:
    identity = f"{channel.id}:{channel.username}:{channel.created_at.isoformat()}:{timestamp}:{nonce}"
    return hmac.new(signing_key, identity.encode(), hashlib.sha256).hexdigest()


def valid_deletion_token(channel: Channel, token: str, cookie: str | None, signing_key: bytes) -> bool:
    # Both values originate in HTTP input. Restrict their shape before compare_digest:
    # its string overload raises TypeError on non-ASCII strings instead of returning False.
    if (
        not isinstance(token, str)
        or not isinstance(cookie, str)
        or not _TOKEN_PATTERN.fullmatch(token)
        or not _TOKEN_PATTERN.fullmatch(cookie)
        or not secrets.compare_digest(token, cookie)
    ):
        return False
    try:
        timestamp, nonce, signature = token.split(".")
        age = time.time() - int(timestamp)
    except (ValueError, TypeError):
        return False
    if not 0 <= age <= 600 or len(nonce) != 32:
        return False
    return hmac.compare_digest(signature, _signature(channel, timestamp, nonce, signing_key))
