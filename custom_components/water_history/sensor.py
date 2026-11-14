"""The two water sensors: consumption (statistics holder) and meter index."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WaterCoordinator

READING_REAL = "réel"
READING_ESTIMATED = "estimé"
RESTORED_ATTRIBUTES = ("measured_at", "reading_type", "meter_serial")


DEVICE_NAME = "Compteur d'eau"
DEVICE_MODEL = "Portail en ligne"
DEVICE_MANUFACTURER = "Régie de l'eau de Metz Métropole"


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=DEVICE_NAME,
        model=DEVICE_MODEL,
        manufacturer=DEVICE_MANUFACTURER,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_NAME):
        coordinator: WaterCoordinator = entry.runtime_data
        async_add_entities(
            [WaterConsumptionSensor(coordinator, entry), WaterIndexSensor(coordinator, entry)]
        )


class WaterConsumptionSensor(CoordinatorEntity[WaterCoordinator], SensorEntity):
    """Statistics holder. The state is constant on purpose.

    Any state change would be counted again by HA's own statistics compiler as
    live consumption, on top of the days written into the statistics. The real
    meter index lives in the "index compteur" sensor.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_native_value = 0.0
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: WaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.data[CONF_NAME]
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        # Never "unavailable": that would leave holes in the live statistics.
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {"last_sync_ok": self.coordinator.last_update_success}
        attributes.update(self.coordinator.data or {})
        return attributes


class WaterIndexSensor(CoordinatorEntity[WaterCoordinator], RestoreSensor):
    """Last known meter index. No state_class: no statistics, so changing its
    state cannot be counted as consumption. Its history shows the meter curve."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:counter"
    _attr_name = "index compteur"

    def __init__(self, coordinator: WaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_index"
        self._attr_device_info = device_info(entry)
        self._restored_value: float | None = None
        self._restored_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        if data is not None and data.native_value is not None:
            self._restored_value = data.native_value
        state = await self.async_get_last_state()
        if state is not None:
            self._restored_attributes = {
                key: state.attributes[key] for key in RESTORED_ATTRIBUTES if key in state.attributes
            }

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        index = (self.coordinator.data or {}).get("index_m3")
        return index if index is not None else self._restored_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        if data.get("last_reading") is None:
            return dict(self._restored_attributes)
        return {
            "measured_at": data["last_reading"],
            "reading_type": READING_ESTIMATED if data.get("last_reading_estimated") else READING_REAL,
            "meter_serial": data.get("meter_serial"),
        }
