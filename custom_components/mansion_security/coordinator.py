"""Poll confirmed room settings and renew the existing pairing when needed."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OFF, MansionAuthError, MansionClient, MansionError, MansionUnsafeModeError, Snapshot
from .const import (
    AUTO_OFF_RETRY_INTERVAL, AUTO_OFF_TIMEOUT, CONF_ALLOW_UNLOCK_MODES,
    CONF_MODE_DEADLINE, CONF_TRACKED_MODE, DEFAULT_UPDATE_INTERVAL, DOMAIN,
    PAIRING_RENEWAL_DAYS,
)

_LOGGER = logging.getLogger(__name__)


class MansionCoordinator(DataUpdateCoordinator[Snapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MansionClient):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=DEFAULT_UPDATE_INTERVAL, always_update=False)
        self.entry = entry
        self.client = client
        self._action_lock = asyncio.Lock()
        self._tracked_mode: str | None = entry.data.get(CONF_TRACKED_MODE)
        self._deadline: datetime | None = None
        stored_deadline = entry.data.get(CONF_MODE_DEADLINE)
        if isinstance(stored_deadline, str):
            try:
                value = datetime.fromisoformat(stored_deadline)
                if value.tzinfo is not None:
                    self._deadline = value.astimezone(timezone.utc)
            except ValueError:
                pass
        self._expiry_cancel: CALLBACK_TYPE | None = None
        self._expiry_task: asyncio.Task | None = None
        self._expiry_generation = 0
        self._expiry_running = False
        self._stopped = False

    @property
    def allow_unlock_modes(self) -> bool:
        return self.entry.options.get(CONF_ALLOW_UNLOCK_MODES, False)

    @property
    def auto_off_at(self) -> str | None:
        return self._deadline.isoformat() if self._deadline else None

    def _expiry_due(self) -> bool:
        return not self._stopped and self._deadline is not None and self._deadline <= dt_util.utcnow()

    @callback
    def _cancel_expiry(self) -> None:
        self._expiry_generation += 1
        if self._expiry_cancel is not None:
            self._expiry_cancel()
            self._expiry_cancel = None

    @callback
    def _schedule_expiry(self, when: datetime | None = None) -> None:
        self._cancel_expiry()
        if self._stopped or self._deadline is None:
            return
        generation = self._expiry_generation

        @callback
        def fire(_now: datetime) -> None:
            self._expiry_cancel = None
            if self._stopped or generation != self._expiry_generation:
                return
            self._expiry_running = True
            self._expiry_task = self.hass.async_create_task(
                self._async_expire_mode(generation), name=f"{DOMAIN} automatic mode expiry",
                eager_start=False,
            )

        self._expiry_cancel = async_track_point_in_utc_time(
            self.hass, fire, max(when or self._deadline, dt_util.utcnow()),
        )

    @callback
    def _observe_snapshot(self, snapshot: Snapshot) -> None:
        if self._stopped:
            return
        if not snapshot.state.enabled:
            if self._tracked_mode is None and self._deadline is None:
                return
            self._tracked_mode = None
            self._deadline = None
            self._cancel_expiry()
        elif self._tracked_mode != snapshot.state.mode or self._deadline is None:
            self._tracked_mode = snapshot.state.mode
            self._deadline = dt_util.utcnow() + AUTO_OFF_TIMEOUT
            self._schedule_expiry()
        else:
            if self._expiry_cancel is None and not self._expiry_running:
                self._schedule_expiry()
            return
        # ConfigEntry data survives restarts; a poll or selecting the same mode
        # again does not change this deadline.
        data = dict(self.entry.data)
        if self._deadline is None:
            data.pop(CONF_MODE_DEADLINE, None)
            data.pop(CONF_TRACKED_MODE, None)
        else:
            data.update({CONF_MODE_DEADLINE: self.auto_off_at, CONF_TRACKED_MODE: self._tracked_mode})
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    async def _async_close_if_due(self, snapshot: Snapshot) -> Snapshot:
        self._observe_snapshot(snapshot)
        if snapshot.state.enabled and self._expiry_due():
            snapshot = await self.client.async_set_mode(OFF, allow_unlock=False)
            self._observe_snapshot(snapshot)
        return snapshot

    async def _async_update_data(self) -> Snapshot:
        try:
            async with self._action_lock:
                await self._async_renew_if_needed()
                snapshot = await self.client.async_get_snapshot()
                return await self._async_close_if_due(snapshot)
        except MansionAuthError as error:
            self._cancel_expiry()
            raise ConfigEntryAuthFailed("The Mansion Security session requires authentication") from error
        except MansionError as error:
            if self._expiry_due():
                self._schedule_expiry(dt_util.utcnow() + AUTO_OFF_RETRY_INTERVAL)
            raise UpdateFailed(str(error)) from error

    async def _async_expire_mode(self, generation: int) -> None:
        try:
            async with self._action_lock:
                if self._stopped or generation != self._expiry_generation:
                    return
                if not self._expiry_due():
                    self._schedule_expiry()
                    return
                # Verify the latest cloud state before closing: another client
                # may have selected a different mode since the last poll.
                snapshot = await self.client.async_get_snapshot()
                snapshot = await self._async_close_if_due(snapshot)
                self.async_set_updated_data(snapshot)
        except MansionAuthError as error:
            self._cancel_expiry()
            self.async_set_update_error(error)
            self.entry.async_start_reauth(self.hass)
            _LOGGER.warning("Automatic mode expiry requires reauthentication")
        except MansionError as error:
            self.async_set_update_error(error)
            if self._expiry_due():
                self._schedule_expiry(dt_util.utcnow() + AUTO_OFF_RETRY_INTERVAL)
            _LOGGER.warning("Automatic mode expiry was not confirmed: %s", error)
        finally:
            self._expiry_running = False
            self._expiry_task = None

    async def async_shutdown(self) -> None:
        self._stopped = True
        self._cancel_expiry()
        task = self._expiry_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await super().async_shutdown()

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
            async with self._action_lock:
                if self._stopped:
                    raise MansionError("Integration is unloading")
                snapshot = await self.client.async_set_mode(mode, allow_unlock=self.allow_unlock_modes)
                self._observe_snapshot(snapshot)
                self.async_set_updated_data(snapshot)
        except MansionUnsafeModeError:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unlock_mode_disabled") from None
        except MansionAuthError as error:
            self.async_set_update_error(error)
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="authentication_required") from None
        except MansionError as error:
            self.async_set_update_error(error)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="cannot_set_mode") from None
