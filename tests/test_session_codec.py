import base64
import json

import pytest

from custom_components.mansion_security.session_codec import InvalidSession, decode_session, encode_session


def test_roundtrip_strips_unneeded_passwords(session_data):
    code = encode_session({**session_data, "password": "must-not-be-transferred"})
    decoded = decode_session(code)
    assert decoded == session_data
    assert "password" not in decoded


@pytest.mark.parametrize("code", ["", "MSHA1.invalid!", "MSHA1." + base64.urlsafe_b64encode(json.dumps({"version": 2}).encode()).decode()])
def test_bad_format_or_version(code):
    with pytest.raises(InvalidSession):
        decode_session(code)


def test_header_injection_is_rejected(session_data):
    with pytest.raises(InvalidSession):
        encode_session({**session_data, "api_key": "test-key\r\nInjected: true"})
