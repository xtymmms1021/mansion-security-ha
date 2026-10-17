"""Exercise the actual Home Assistant entity, coordinator and services."""

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.select import ATTR_OPTION, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mansion_security.api import (
    MansionAuthError, MansionConnectionError, MansionUnsafeModeError,
    PairingInfo, RoomState, Snapshot,
)
from custom_components.mansion_security.const import CONF_ALLOW_UNLOCK_MODES, DOMAIN
from custom_components.mansion_security.diagnostics import async_get_config_entry_diagnostics
from custom_components.mansion_security.select import preset_label


async def setup(hass, session_data, *, allow_unlock=False):
    entry = MockConfigEntry(domain=DOMAIN, title="Mansion Security", unique_id="test-user",
                            data=session_data, options={CONF_ALLOW_UNLOCK_MODES: allow_unlock})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("select", DOMAIN, "test-user_auto_response")
    assert entity_id
    return entry, entity_id


async def test_setup_restores_server_state_without_login_or_writes(hass, session_data, mock_client):
    entry, entity_id = await setup(hass, session_data)
    entity = hass.states.get(entity_id)
    assert entity.state == "off"
    assert entity.attributes["options"] == ["off", "稍后再来"]
    assert entry.runtime_data.update_interval == timedelta(seconds=60)
    mock_client.async_login.assert_not_awaited()
    mock_client.async_set_mode.assert_not_awaited()


async def test_reload_reads_real_state_and_reuses_session(hass, session_data, mock_client, presets):
    entry, entity_id = await setup(hass, session_data)
    mock_client.async_get_snapshot.return_value = Snapshot(RoomState(True, "24"), presets)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "稍后再来"
    mock_client.async_login.assert_not_awaited()
    mock_client.async_set_mode.assert_not_awaited()


async def test_select_service_updates_only_after_confirmed_readback(hass, session_data, mock_client):
    _, entity_id = await setup(hass, session_data)
    await hass.services.async_call(SELECT_DOMAIN, SERVICE_SELECT_OPTION,
                                  {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "稍后再来"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "稍后再来"
    assert hass.states.get(entity_id).attributes["mode_id"] == "24"
    mock_client.async_set_mode.assert_awaited_once_with("24", allow_unlock=False)


async def test_poll_observes_changes_from_another_client(hass, session_data, mock_client, presets):
    entry, entity_id = await setup(hass, session_data)
    mock_client.async_get_snapshot.return_value = Snapshot(RoomState(True, "24"), presets)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "稍后再来"
    mock_client.async_set_mode.assert_not_awaited()


async def test_unavailable_and_recovery(hass, session_data, mock_client):
    entry, entity_id = await setup(hass, session_data)
    mock_client.async_get_snapshot.side_effect = MansionConnectionError("Unavailable")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    mock_client.async_get_snapshot.side_effect = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"


async def test_write_failure_does_not_claim_success(hass, session_data, mock_client):
    _, entity_id = await setup(hass, session_data)
    mock_client.async_set_mode.side_effect = MansionConnectionError("Unavailable")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(SELECT_DOMAIN, SERVICE_SELECT_OPTION,
                                      {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: "稍后再来"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_unlock_modes_are_explicitly_labeled(hass, session_data, mock_client, presets):
    _, entity_id = await setup(hass, session_data, allow_unlock=True)
    options = hass.states.get(entity_id).attributes["options"]
    assert preset_label(presets[0]) in options
    assert "自动开门" in preset_label(presets[0])


async def test_blocked_unlock_write_preserves_state(hass, session_data, mock_client, presets):
    entry, entity_id = await setup(hass, session_data)
    mock_client.async_set_mode.side_effect = MansionUnsafeModeError("Automatically unlocks")
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_set_mode("23")
    assert hass.states.get(entity_id).state == "off"


async def test_unknown_current_mode_is_visible(hass, session_data, mock_client, presets):
    mock_client.async_get_snapshot.return_value = Snapshot(RoomState(True, "new-mode"), presets)
    _, entity_id = await setup(hass, session_data)
    state = hass.states.get(entity_id)
    assert state.state == "new-mode: unknown preset"
    assert state.state in state.attributes["options"]


async def test_pairing_renewal_is_saved_and_not_repeated(hass, session_data, mock_client):
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    session_data["expireDate"] = (today + timedelta(days=2)).strftime("%Y/%m/%d")
    mock_client.async_renew_pairing.return_value = PairingInfo("2099/01/01", "6")
    entry, _ = await setup(hass, session_data)
    mock_client.async_renew_pairing.assert_awaited_once_with("test-pairing-code", "test-push-token")
    assert entry.data["expireDate"] == "2099/01/01"
    await entry.runtime_data.async_refresh()
    assert mock_client.async_renew_pairing.await_count == 1
    mock_client.async_login.assert_not_awaited()


async def test_authentication_failure_starts_reauth(hass, session_data, mock_client):
    entry, _ = await setup(hass, session_data)
    mock_client.async_get_snapshot.side_effect = MansionAuthError(400, "invalidUser")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" and flow["context"].get("entry_id") == entry.entry_id for flow in flows)


async def test_diagnostics_exclude_all_credentials(hass, session_data, mock_client):
    entry, _ = await setup(hass, session_data)
    result = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result)
    for secret in ("test-user", "test-key", "test-pairing-code", "test-push-token"):
        assert secret not in serialized
    assert result["state"] == {"enabled": False, "pattern": ""}
