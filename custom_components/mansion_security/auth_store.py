"""Keep recoverable registration sessions in HA storage, never passwords."""

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .session_codec import InvalidSession, decode_session, encode_session


class PendingRegistrations:
    def __init__(self, hass: HomeAssistant):
        self._store = Store(hass, 1, f"{DOMAIN}.pending_registration")
        self._lock = asyncio.Lock()

    async def async_get(self, username: str) -> dict | None:
        async with self._lock:
            records = await self._store.async_load() or {}
            record = records.get(username)
            if not isinstance(record, dict):
                return None
            try:
                data = decode_session(encode_session(record["data"]))
            except (InvalidSession, KeyError):
                return None
            return {"data": data, "stage": record.get("stage", "unknown")}

    async def async_save(self, username: str, data: dict, stage: str) -> None:
        normalized = decode_session(encode_session(data))
        async with self._lock:
            records = await self._store.async_load() or {}
            records[username] = {"data": normalized, "stage": stage}
            await self._store.async_save(records)

    async def async_remove(self, username: str) -> None:
        async with self._lock:
            records = await self._store.async_load() or {}
            if username in records:
                del records[username]
                await self._store.async_save(records)


def async_get_pending_registrations(hass: HomeAssistant) -> PendingRegistrations:
    key = f"{DOMAIN}.pending_registrations"
    if key not in hass.data:
        hass.data[key] = PendingRegistrations(hass)
    return hass.data[key]
