"""Import a completed session without triggering the single-login restriction."""

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import AuthSession, MansionAuthError, MansionClient, MansionError
from .const import CONF_ALLOW_UNLOCK_MODES, CONF_SESSION_CODE, DOMAIN
from .session_codec import InvalidSession, decode_session


def session_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_SESSION_CODE): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )})


class MansionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _async_validate(self, data: dict) -> None:
        client = MansionClient(
            async_get_clientsession(self.hass), AuthSession(data["user_id"], data["api_key"])
        )
        await client.async_get_snapshot()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                data = decode_session(user_input[CONF_SESSION_CODE])
                await self._async_validate(data)
            except InvalidSession:
                errors["base"] = "invalid_session"
            except MansionAuthError:
                errors["base"] = "invalid_auth"
            except MansionError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(data["user_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Mansion Security", data=data,
                                               options={CONF_ALLOW_UNLOCK_MODES: False})
        return self.async_show_form(step_id="user", data_schema=session_schema(), errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                data = decode_session(user_input[CONF_SESSION_CODE])
                entry = self._get_reauth_entry()
                if data["user_id"] != entry.data["user_id"]:
                    raise InvalidSession("The session belongs to another account")
                await self._async_validate(data)
            except InvalidSession:
                errors["base"] = "invalid_session"
            except MansionAuthError:
                errors["base"] = "invalid_auth"
            except MansionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(step_id="reauth_confirm", data_schema=session_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return MansionOptionsFlow()


class MansionOptionsFlow(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_ALLOW_UNLOCK_MODES,
                         default=self.config_entry.options.get(CONF_ALLOW_UNLOCK_MODES, False)): bool
        })
        return self.async_show_form(step_id="init", data_schema=schema)
