"""Expose the server's persistent entrance presets as a select entity."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import OFF, Preset
from .const import DOMAIN
from .coordinator import MansionCoordinator


def preset_label(preset: Preset) -> str:
    suffix = "（自动开门）" if preset.unlocks else ""
    return preset.title + suffix


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([MansionAutoResponseSelect(entry.runtime_data)])


class MansionAutoResponseSelect(CoordinatorEntity[MansionCoordinator], SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "auto_response"
    _attr_icon = "mdi:doorbell"

    def __init__(self, coordinator: MansionCoordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data['user_id']}_auto_response"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.data["user_id"])},
            name="Mansion Security", manufacturer="SECOM", model="Mansion Security",
        )

    @property
    def current_option(self) -> str:
        state = self.coordinator.data.state
        if not state.enabled:
            return "off"
        preset = next((item for item in self.coordinator.data.presets if item.pattern == state.pattern), None)
        return preset_label(preset) if preset else f"{state.pattern}: unknown preset"

    @property
    def options(self) -> list[str]:
        options = ["off"] + [preset_label(item) for item in self.coordinator.data.presets
                              if item.preset]
        # Preserve the observed state even if it is an unknown or nonpersistent preset.
        if self.current_option not in options:
            options.append(self.current_option)
        return options

    @property
    def extra_state_attributes(self) -> dict:
        state = self.coordinator.data.state
        preset = next((item for item in self.coordinator.data.presets if item.pattern == state.pattern), None)
        return {"mode_id": state.mode,
                "automatically_unlocks": preset.unlocks if state.enabled and preset else False if not state.enabled else None,
                "preset_duration_hours": 2,
                "pairing_expires": self.coordinator.entry.data.get("expireDate")}

    async def async_select_option(self, option: str) -> None:
        if option == "off":
            await self.coordinator.async_set_mode(OFF)
            return
        preset = next((item for item in self.coordinator.data.presets
                       if item.preset and preset_label(item) == option), None)
        if preset is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_option")
        await self.coordinator.async_set_mode(preset.pattern)
