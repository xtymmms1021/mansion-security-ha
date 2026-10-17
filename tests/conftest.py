"""Use Home Assistant 2026.9.4's real test fixtures."""

from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest_plugins = ("pytest_homeassistant_custom_component",)

from custom_components.mansion_security.api import AuthSession, MansionClient, PairingInfo, Preset, RoomState, Snapshot


@pytest.fixture(autouse=True)
def allow_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def session_data():
    return {"user_id": "test-user", "api_key": "test-key", "pair_hash": "test-pairing-code",
            "push_token": "test-push-token", "expireDate": "2099/01/01", "validTerm": "6"}


@pytest.fixture
def presets():
    return (Preset("33", "放入楼层快递柜", True, True),
            Preset("24", "稍后再来", False, True), Preset("23", "请进", True, True))


@pytest.fixture
def mock_client(presets):
    client = AsyncMock(spec=MansionClient)
    client.async_get_snapshot.return_value = Snapshot(RoomState(False, ""), presets)
    client.async_set_mode.return_value = Snapshot(RoomState(True, "24"), presets)
    client.async_login.return_value = AuthSession("test-user", "test-key")
    client.async_renew_pairing.return_value = PairingInfo("2099/01/01", "6")
    with patch("custom_components.mansion_security.MansionClient", return_value=client), \
            patch("custom_components.mansion_security.config_flow.MansionClient", return_value=client):
        yield client
