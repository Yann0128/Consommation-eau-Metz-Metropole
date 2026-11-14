"""Button: fetch the portal right now."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import WaterCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_NAME):
        async_add_entities([WaterRefreshButton(entry.runtime_data, entry)])


class WaterRefreshButton(CoordinatorEntity[WaterCoordinator], ButtonEntity):
    """One press = one immediate portal call. The consumption sensor and the
    "index compteur" sensor share the same coordinator, so both are refreshed."""

    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: WaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_name = "Actualiser données"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        """Immediate call to the portal (not debounced)."""
        await self.coordinator.async_refresh()
        if not self.coordinator.last_update_success:
            raise HomeAssistantError(
                f"Actualisation impossible : {self.coordinator.last_exception}"
            )
