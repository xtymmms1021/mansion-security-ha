"""Validate setup, reauthentication and options with Home Assistant."""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mansion_security.api import MansionAuthError, MansionConnectionError
from custom_components.mansion_security.const import CONF_ALLOW_UNLOCK_MODES, CONF_SESSION_CODE, DOMAIN
from custom_components.mansion_security.session_codec import encode_session


async def test_import_completed_session(hass, session_data, mock_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "import_session"})
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SESSION_CODE: encode_session(session_data)})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == session_data
    assert result["options"] == {CONF_ALLOW_UNLOCK_MODES: False}
    mock_client.async_get_snapshot.assert_awaited_once()
    mock_client.async_login.assert_not_awaited()
    mock_client.async_set_mode.assert_not_awaited()


async def test_reject_incomplete_session(hass, mock_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                       data={CONF_SESSION_CODE: "not-a-session"})
    assert result["errors"] == {"base": "invalid_session"}
    mock_client.async_get_snapshot.assert_not_awaited()


async def test_authentication_failure(hass, session_data, mock_client):
    mock_client.async_get_snapshot.side_effect = MansionAuthError(400, "invalidUser")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                       data={CONF_SESSION_CODE: encode_session(session_data)})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_connection_failure(hass, session_data, mock_client):
    mock_client.async_get_snapshot.side_effect = MansionConnectionError("Unavailable")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                       data={CONF_SESSION_CODE: encode_session(session_data)})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_account(hass, session_data, mock_client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data=session_data)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
                                                       data={CONF_SESSION_CODE: encode_session(session_data)})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_same_account(hass, session_data, mock_client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data=session_data)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data)
    assert result["step_id"] == "reauth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_confirm"})
    updated = {**session_data, "api_key": "replacement-test-key"}
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SESSION_CODE: encode_session(updated)})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == "replacement-test-key"
    mock_client.async_login.assert_not_awaited()


async def test_reauth_rejects_another_account(hass, session_data, mock_client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data=session_data)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_confirm"})
    other = {**session_data, "user_id": "different-test-user"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SESSION_CODE: encode_session(other)})
    assert result["errors"] == {"base": "invalid_session"}
    mock_client.async_get_snapshot.assert_not_awaited()


async def test_unlock_modes_require_explicit_option(hass, session_data):
    entry = MockConfigEntry(domain=DOMAIN, data=session_data, options={CONF_ALLOW_UNLOCK_MODES: False})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ALLOW_UNLOCK_MODES: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOW_UNLOCK_MODES] is True
