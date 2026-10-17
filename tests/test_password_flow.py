"""Exercise real HA login forms and password-changing failure recovery."""

import json
from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mansion_security.api import (
    AuthSession, MansionApiError, MansionAuthError, MansionConnectionError,
)
from custom_components.mansion_security.auth_store import async_get_pending_registrations
from custom_components.mansion_security.const import DOMAIN

CURRENT = "test-current-password"
NEW = "test-new-password"
LOGIN = {"username": "test-account", "password": CURRENT}
PAIR = {"pair_hash": "test-pairing-code", "nickname": "Home Assistant", "accept_terms": True}
PASSWORD = {"new_password": NEW, "confirm_password": NEW}


async def start(hass, login=LOGIN, *, entry=None):
    context = {"source": SOURCE_USER} if entry is None else {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
    result = await hass.config_entries.flow.async_init(DOMAIN, context=context, data=entry.data if entry else None)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    assert result["step_id"] == "login"
    return await hass.config_entries.flow.async_configure(result["flow_id"], login)


def require_initialization(client):
    snapshot = client.async_get_snapshot.return_value
    client.async_get_snapshot.side_effect = [MansionAuthError(400, "invalidUser"),
                                            MansionAuthError(400, "invalidUser"), snapshot]
    return snapshot


async def initialize_form(hass, client):
    require_initialization(client)
    result = await start(hass)
    assert result["step_id"] == "pairing"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PAIR)
    assert result["step_id"] == "initialize"
    return result


async def test_username_login_manual_qr_and_no_unnecessary_password_change(hass, mock_client):
    result = await start(hass)
    assert result["step_id"] == "pairing"
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], PAIR)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["pair_hash"] == PAIR["pair_hash"]
    assert result["data"]["push_token"].startswith("HomeAssistant:")
    assert result["data"]["login_name"] == LOGIN["username"]
    assert CURRENT not in json.dumps(result["data"])
    mock_client.async_login.assert_awaited_once_with(LOGIN["username"], CURRENT)
    mock_client.async_initialize_account.assert_not_awaited()
    mock_client.async_set_mode.assert_not_awaited()


async def test_required_initialization_follows_native_order_and_forgets_passwords(hass, mock_client):
    result = await initialize_form(hass, mock_client)
    mock_client.async_initialize_account.assert_not_awaited()
    pending = await async_get_pending_registrations(hass).async_get(LOGIN["username"])
    assert pending["stage"] == "paired"
    assert CURRENT not in json.dumps(pending)
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], PASSWORD)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CURRENT not in json.dumps(result["data"])
    assert NEW not in json.dumps(result["data"])
    mock_client.async_initialize_account.assert_awaited_once_with(CURRENT, NEW, PAIR["nickname"])
    assert [call[0] for call in mock_client.method_calls] == [
        "async_login", "async_get_snapshot", "async_renew_pairing", "async_get_snapshot",
        "async_login", "async_initialize_account", "async_renew_pairing", "async_get_snapshot",
    ]
    assert await async_get_pending_registrations(hass).async_get(LOGIN["username"]) is None


@pytest.mark.parametrize("new,confirm,field,code", [
    (CURRENT, CURRENT, "new_password", "password_unchanged"),
    (NEW, "different-password", "confirm_password", "password_mismatch"),
    ("short", "short", "new_password", "invalid_password"),
    ("has a space", "has a space", "new_password", "invalid_password"),
])
async def test_invalid_new_password_never_calls_init(hass, mock_client, new, confirm, field, code):
    result = await initialize_form(hass, mock_client)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"new_password": new, "confirm_password": confirm})
    assert result["errors"] == {field: code}
    mock_client.async_initialize_account.assert_not_awaited()


@pytest.mark.parametrize("updates,field,code", [
    ({"pair_hash": ""}, "pair_hash", "invalid_pairing_code"),
    ({"pair_hash": "code\nextra"}, "pair_hash", "invalid_pairing_code"),
    ({"nickname": ""}, "nickname", "invalid_nickname"),
    ({"accept_terms": False}, "accept_terms", "terms_required"),
])
async def test_invalid_pairing_input_does_not_contact_service(hass, mock_client, updates, field, code):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**PAIR, **updates})
    assert result["errors"] == {field: code}
    mock_client.async_renew_pairing.assert_not_awaited()


@pytest.mark.parametrize("error,code", [
    (MansionAuthError(400, "loginFailed"), "invalid_auth"),
    (MansionAuthError(400, "alreadyLoggedIn"), "already_logged_in"),
    (MansionConnectionError("Unavailable"), "cannot_connect"),
])
async def test_login_error_messages(hass, mock_client, error, code):
    mock_client.async_login.side_effect = error
    result = await start(hass)
    assert result["step_id"] == "login" and result["errors"] == {"base": code}
    mock_client.async_initialize_account.assert_not_awaited()


@pytest.mark.parametrize("failure", [MansionConnectionError("Timeout"), MansionApiError(500, "serverError")])
async def test_uncertain_init_never_repeats_password_change(hass, mock_client, failure):
    result = await initialize_form(hass, mock_client)
    mock_client.async_initialize_account.side_effect = failure
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PASSWORD)
    assert result["step_id"] == "finish_registration"
    pending = await async_get_pending_registrations(hass).async_get(LOGIN["username"])
    assert pending["stage"] == "uncertain"
    assert CURRENT not in json.dumps(pending) and NEW not in json.dumps(pending)
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_client.async_initialize_account.await_count == 1


async def test_confirmed_password_change_then_retry_only_remaining_registration(hass, mock_client):
    result = await initialize_form(hass, mock_client)
    mock_client.async_renew_pairing.side_effect = MansionConnectionError("Timeout")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PASSWORD)
    assert result["step_id"] == "finish_registration"
    mock_client.async_renew_pairing.side_effect = None
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_client.async_initialize_account.await_count == 1


async def test_registration_can_resume_after_flow_restart(hass, mock_client, session_data):
    data = {**session_data, "login_name": LOGIN["username"]}
    store = async_get_pending_registrations(hass)
    await store.async_save(LOGIN["username"], data, "initialized")
    mock_client.async_login.side_effect = MansionAuthError(400, "alreadyLoggedIn")
    with patch("custom_components.mansion_security.async_setup_entry", return_value=True):
        result = await start(hass)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == data
    mock_client.async_initialize_account.assert_not_awaited()
    assert await store.async_get(LOGIN["username"]) is None


async def test_username_reauth_keeps_same_account_and_uses_cached_pairing(hass, mock_client, session_data):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data={**session_data, "login_name": LOGIN["username"]})
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await start(hass, entry=entry)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    mock_client.async_initialize_account.assert_not_awaited()


async def test_username_reauth_rejects_other_account_before_pairing(hass, mock_client, session_data):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data=session_data)
    entry.add_to_hass(hass)
    mock_client.async_login.return_value = AuthSession("other-user", "other-key")
    result = await start(hass, entry=entry)
    assert result["errors"] == {"base": "wrong_account"}
    mock_client.async_renew_pairing.assert_not_awaited()
    mock_client.async_initialize_account.assert_not_awaited()


async def test_duplicate_username_does_not_invalidate_existing_session(hass, mock_client, session_data):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test-user", data={**session_data, "login_name": LOGIN["username"]})
    entry.add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    mock_client.async_login.assert_not_awaited()


async def test_explicit_init_rejection_stays_on_password_form(hass, mock_client):
    result = await initialize_form(hass, mock_client)
    mock_client.async_initialize_account.side_effect = MansionApiError(400, "changeFailed")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PASSWORD)
    assert result["step_id"] == "initialize"
    assert result["errors"] == {"base": "initialization_failed"}
