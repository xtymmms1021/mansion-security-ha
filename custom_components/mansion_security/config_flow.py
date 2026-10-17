"""Username/password registration and optional completed-session import."""

from __future__ import annotations

import re
import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import (
    AuthSession, MansionApiError, MansionAuthError, MansionClient,
    MansionConnectionError, MansionError, MansionProtocolError,
)
from .auth_store import async_get_pending_registrations
from .const import (
    CONF_ACCEPT_TERMS, CONF_CONFIRM_PASSWORD,
    CONF_NEW_PASSWORD, CONF_NICKNAME, CONF_PAIR_HASH, CONF_SESSION_CODE, DOMAIN,
)
from .session_codec import InvalidSession, decode_session


def private_text():
    return selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


def session_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_SESSION_CODE): private_text()})


class MansionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._client: MansionClient | None = None
        self._data: dict[str, Any] = {}
        self._current_password: str | None = None
        self._initialization_sent = False
        self._initialization_uncertain = False

    @property
    def _is_reauth(self) -> bool:
        return self.context.get("source") == config_entries.SOURCE_REAUTH

    def _make_client(self, data: dict | None = None) -> MansionClient:
        auth = AuthSession(data["user_id"], data["api_key"]) if data else None
        return MansionClient(async_get_clientsession(self.hass), auth)

    async def _async_validate(self, data: dict) -> None:
        await self._make_client(data).async_get_snapshot()

    async def _async_save(self, data: dict):
        self._current_password = None
        if self._is_reauth:
            entry = self._get_reauth_entry()
            if data["user_id"] != entry.data["user_id"]:
                return self.async_abort(reason="wrong_account")
            result = self.async_update_reload_and_abort(entry, data_updates=data)
        else:
            await self.async_set_unique_id(data["user_id"])
            self._abort_if_unique_id_configured()
            result = self.async_create_entry(title="Mansion Security", data=data)
        if data.get("login_name"):
            await async_get_pending_registrations(self.hass).async_remove(data["login_name"])
        return result

    async def _async_remember(self, stage: str) -> None:
        await async_get_pending_registrations(self.hass).async_save(self._data["login_name"], self._data, stage)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input and CONF_SESSION_CODE in user_input:
            return await self.async_step_import_session(user_input)
        return self.async_show_menu(step_id="user", menu_options=["login", "import_session"])

    async def async_step_login(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            self._current_password = user_input[CONF_PASSWORD]
            if not username or not self._current_password:
                errors["base"] = "invalid_auth"
            else:
                if not self._is_reauth:
                    for entry in self._async_current_entries():
                        if entry.data.get("login_name") == username:
                            return self.async_abort(reason="already_configured")
                pending = await async_get_pending_registrations(self.hass).async_get(username)
                seed = self._get_reauth_entry().data if self._is_reauth else pending["data"] if pending else {}
                self._client = self._make_client()
                try:
                    try:
                        auth = await self._client.async_login(username, self._current_password)
                    except MansionAuthError as error:
                        # Recover our own saved registration key after a restart.
                        if error.code != "alreadyLoggedIn" or not seed or seed.get("login_name") != username:
                            raise
                        await self._async_validate(seed)
                        return await self._async_save(dict(seed))
                    if self._is_reauth and auth.user_id != self._get_reauth_entry().data["user_id"]:
                        errors["base"] = "wrong_account"
                    else:
                        await self.async_set_unique_id(auth.user_id)
                        if not self._is_reauth:
                            self._abort_if_unique_id_configured()
                        self._data = {"user_id": auth.user_id, "api_key": auth.api_key, "login_name": username}
                        if seed.get("user_id") == auth.user_id:
                            for key in (CONF_PAIR_HASH, "push_token", "expireDate", "validTerm", CONF_NICKNAME):
                                if key in seed:
                                    self._data[key] = seed[key]
                        try:
                            await self._client.async_get_snapshot()
                        except MansionAuthError as error:
                            if error.code != "invalidUser":
                                raise
                        else:
                            if self._data.get(CONF_PAIR_HASH) and self._data.get("push_token"):
                                return await self._async_save(dict(self._data))
                        self._initialization_sent = False
                        self._initialization_uncertain = False
                        return await self.async_step_pairing()
                except MansionAuthError as error:
                    errors["base"] = "already_logged_in" if error.code == "alreadyLoggedIn" else "invalid_auth"
                except MansionError:
                    errors["base"] = "cannot_connect"
        self._current_password = None
        schema = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): private_text()})
        return self.async_show_form(step_id="login", data_schema=schema, errors=errors)

    async def async_step_pairing(self, user_input: dict[str, Any] | None = None):
        if self._client is None:
            return await self.async_step_login()
        if self._initialization_sent:
            return await self.async_step_finish_registration()
        errors = {}
        if user_input is not None:
            pair_hash = user_input[CONF_PAIR_HASH].strip()
            nickname = user_input[CONF_NICKNAME].strip()
            if not pair_hash or len(pair_hash) > 8192 or any(ord(char) < 32 for char in pair_hash):
                errors[CONF_PAIR_HASH] = "invalid_pairing_code"
            elif not 1 <= len(nickname) <= 32:
                errors[CONF_NICKNAME] = "invalid_nickname"
            elif not user_input[CONF_ACCEPT_TERMS]:
                errors[CONF_ACCEPT_TERMS] = "terms_required"
            else:
                self._data.update({CONF_PAIR_HASH: pair_hash, CONF_NICKNAME: nickname})
                self._data.setdefault("push_token", "HomeAssistant:" + secrets.token_urlsafe(96))
                try:
                    await self._async_remember("pairing")
                    info = await self._client.async_renew_pairing(pair_hash, self._data["push_token"])
                    self._data.update(expireDate=info.expires, validTerm=info.valid_term)
                    await self._async_remember("paired")
                    try:
                        await self._client.async_get_snapshot()
                    except MansionAuthError as error:
                        if error.code != "invalidUser":
                            raise
                        return await self.async_step_initialize()
                    return await self._async_save(dict(self._data))
                except MansionAuthError:
                    errors["base"] = "invalid_auth"
                except MansionConnectionError:
                    errors["base"] = "cannot_connect"
                except MansionError:
                    errors["base"] = "pairing_failed"
        schema = vol.Schema({
            vol.Required(CONF_PAIR_HASH, default=self._data.get(CONF_PAIR_HASH, "")): private_text(),
            vol.Required(CONF_NICKNAME, default=self._data.get(CONF_NICKNAME, "Home Assistant")): str,
            vol.Required(CONF_ACCEPT_TERMS, default=False): bool,
        })
        return self.async_show_form(step_id="pairing", data_schema=schema, errors=errors)

    async def async_step_initialize(self, user_input: dict[str, Any] | None = None):
        if self._initialization_sent:
            return await self.async_step_finish_registration()
        if self._client is None or self._current_password is None:
            return await self.async_step_login()
        errors = {}
        if user_input is not None:
            new_password = user_input[CONF_NEW_PASSWORD]
            if new_password != user_input[CONF_CONFIRM_PASSWORD]:
                errors[CONF_CONFIRM_PASSWORD] = "password_mismatch"
            elif new_password == self._current_password:
                errors[CONF_NEW_PASSWORD] = "password_unchanged"
            elif not re.fullmatch(r"[\x21-\x7e]{8,32}", new_password):
                errors[CONF_NEW_PASSWORD] = "invalid_password"
            else:
                try:
                    auth = await self._client.async_login(self._data["login_name"], self._current_password)
                    if auth.user_id != self._data["user_id"]:
                        return self.async_abort(reason="wrong_account")
                    self._data["api_key"] = auth.api_key
                    await self._async_remember("initializing")
                    self._initialization_sent = True
                    await self._client.async_initialize_account(self._current_password, new_password, self._data[CONF_NICKNAME])
                except (MansionConnectionError, MansionProtocolError):
                    if self._initialization_sent:
                        self._initialization_uncertain = True
                        self._current_password = None
                        await self._async_remember("uncertain")
                        return await self.async_step_finish_registration()
                    errors["base"] = "cannot_connect"
                except MansionApiError as error:
                    if self._initialization_sent and error.status >= 500:
                        self._initialization_uncertain = True
                        self._current_password = None
                        await self._async_remember("uncertain")
                        return await self.async_step_finish_registration()
                    self._initialization_sent = False
                    errors["base"] = "already_logged_in" if error.code == "alreadyLoggedIn" else "initialization_failed"
                else:
                    self._current_password = None
                    await self._async_remember("initialized")
                    return await self._async_finish()
        schema = vol.Schema({vol.Required(CONF_NEW_PASSWORD): private_text(),
                             vol.Required(CONF_CONFIRM_PASSWORD): private_text()})
        return self.async_show_form(step_id="initialize", data_schema=schema, errors=errors)

    async def _async_finish(self):
        errors = {}
        try:
            info = await self._client.async_renew_pairing(self._data[CONF_PAIR_HASH], self._data["push_token"])
            self._data.update(expireDate=info.expires, validTerm=info.valid_term)
            await self._async_remember("finishing")
            await self._client.async_get_snapshot()
            return await self._async_save(dict(self._data))
        except MansionAuthError:
            errors["base"] = "initialization_uncertain" if self._initialization_uncertain else "invalid_auth"
        except MansionError:
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="finish_registration", data_schema=vol.Schema({}), errors=errors)

    async def async_step_finish_registration(self, user_input: dict[str, Any] | None = None):
        if self._client is None:
            return await self.async_step_login()
        if user_input is not None:
            return await self._async_finish()
        return self.async_show_form(step_id="finish_registration", data_schema=vol.Schema({}), errors={})

    async def async_step_import_session(self, user_input: dict[str, Any] | None = None):
        return await self._async_import(user_input, "import_session")

    async def _async_import(self, user_input, step_id):
        errors = {}
        if user_input is not None:
            try:
                data = decode_session(user_input[CONF_SESSION_CODE])
                if self._is_reauth and data["user_id"] != self._get_reauth_entry().data["user_id"]:
                    raise InvalidSession("The session belongs to another account")
                await self._async_validate(data)
            except InvalidSession:
                errors["base"] = "invalid_session"
            except MansionAuthError:
                errors["base"] = "invalid_auth"
            except MansionError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_save(data)
        return self.async_show_form(step_id=step_id, data_schema=session_schema(), errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return self.async_show_menu(step_id="reauth", menu_options=["login", "reauth_confirm"])

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        return await self._async_import(user_input, "reauth_confirm")
