"""Return only non-secret operating information."""

from dataclasses import asdict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    return {
        "integration_version": "0.3.0",
        "last_update_success": coordinator.last_update_success,
        "pairing_expires": entry.data.get("expireDate"),
        "allow_unlock_modes": coordinator.allow_unlock_modes,
        "auto_off_at": coordinator.auto_off_at,
        "state": asdict(snapshot.state) if snapshot else None,
        "presets": [asdict(item) for item in snapshot.presets] if snapshot else [],
    }
