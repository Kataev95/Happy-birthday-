"""Cryptographic validation of Telegram WebApp.initData."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def verify_init_data(raw: str, bot_token: str, *, max_age_seconds: int = 86400, now: int | None = None) -> dict:
    if not raw or len(raw) > 16000:
        raise AuthError("Open the application from Telegram")
    pairs = parse_qsl(raw, keep_blank_values=True)
    data = dict(pairs)
    if len(pairs) != len(data) or "hash" not in data:
        raise AuthError("Invalid Telegram authorization data")
    check = "\n".join(f"{key}={val}" for key, val in sorted(pairs) if key != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, data["hash"]):
        raise AuthError("Telegram signature mismatch")
    current = int(time.time()) if now is None else now
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        uid = user["id"]
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise AuthError("Invalid Telegram user data") from None
    if auth_date > current + 60 or current - auth_date > max_age_seconds:
        raise AuthError("Telegram authorization expired; reopen the Mini App")
    return user
