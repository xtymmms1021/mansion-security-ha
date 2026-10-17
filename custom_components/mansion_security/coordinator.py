"""Poll confirmed room settings and renew the existing pairing when needed."""

from datetime import datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MansionAuthError, MansionClient, MansionError, MansionUnsafeModeError, Snapshot
from .const import CONF_ALLOW_UNLOCK_MODES, DEFAULT_UPDATE_INTERVAL, DOMAIN, PAIRING_RENEWAL_DAYS

_LOGGER = logging.getLogger(__name__)


class MansionCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MansionClient):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=DEFAULT_UPDATE_INTERVAL, always_update=False)
        self.entry = entry
        self.client = client

    @property
    def allow_unlock_modes(self) -> bool:
        return self.entry.options.get(CONF_ALLOW_UNLOCK_MODES, False)

    async def _async_update_data(self) -> Snapshot:
        try:
            await self._async_renew_if_needed()
            return await self.client.async_get_snapshot()
        except MansionAuthError as error:
            raise ConfigEntryAuthFailed("The Mansion Security session requires authentication") from error
        except MansionError as error:
            raise UpdateFailed(str(error)) from error

    async def _async_renew_if_needed(self) -> None:
        expires = self.entry.data.get("expireDate")
        if not expires:
            return
        try:
            expiry_date = datetime.strptime(expires, "%Y/%m/%d").date()
        except ValueError:
            raise UpdateFailed("Invalid pairing expiry metadata") from None
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        if expiry_date > today + timedelta(days=PAIRING_RENEWAL_DAYS):
            return
        info = await self.client.async_renew_pairing(
            self.entry.data["pair_hash"], self.entry.data["push_token"]
        )
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, "expireDate": info.expires, "validTerm": info.valid_term},
        )

    async def async_set_mode(self, mode: str) -> None:
        try:
            snapshot = await self.client.async_set_mode(mode, allow_unlock=self.allow_unlock_modes)
        except MansionUnsafeModeError:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unlock_mode_disabled") from None
        except MansionAuthError as error:
            self.async_set_update_error(error)
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="authentication_required") from None
        except MansionError as error:
            self.async_set_update_error(error)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cannot_set_mode") from None
        self.async_set_updated_data(snapshot)
