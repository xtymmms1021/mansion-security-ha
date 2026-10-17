"""Versioned session transfer format. Codes are credentials, not encryption."""

import base64
from datetime import datetime
import json

PREFIX = "MSHA1."
REQUIRED_FIELDS = ("user_id", "api_key", "pair_hash", "push_token")
OPTIONAL_FIELDS = ("expireDate", "validTerm", "nickname", "login_name")


class InvalidSession(ValueError):
    """A session transfer code is incomplete or malformed."""


def _validate(data: object) -> dict:
    if not isinstance(data, dict):
        raise InvalidSession("Expected a session object")
    result = {}
    for key in REQUIRED_FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value or len(value) > 8192 or "\r" in value or "\n" in value:
            raise InvalidSession("Missing or invalid session field")
        result[key] = value
    for key in OPTIONAL_FIELDS:
        value = data.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 256:
                raise InvalidSession("Invalid session metadata")
            result[key] = value
    if result.get("expireDate"):
        try:
            datetime.strptime(result["expireDate"], "%Y/%m/%d")
        except ValueError:
            raise InvalidSession("Invalid pairing expiry") from None
    return result


def encode_session(data: dict) -> str:
    payload = {"version": 1, **_validate(data)}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_session(code: str) -> dict:
    if not isinstance(code, str) or len(code) > 32768 or not code.strip().startswith(PREFIX):
        raise InvalidSession("Invalid session transfer code")
    encoded = code.strip()[len(PREFIX):]
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InvalidSession("Invalid session transfer code") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise InvalidSession("Unsupported session version")
    return _validate(data)
