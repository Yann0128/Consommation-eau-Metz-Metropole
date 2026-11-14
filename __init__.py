"""Consommation eau Metz Métropole: water sensors fed from the provider's portal (or a CSV file).

Each config entry creates a consumption sensor (name chosen by the user, unit m³,
device_class water, state_class total_increasing), an "index compteur" sensor and a refresh button. Its long-term statistics are
written directly (same technique as Intuis, see recorder_import.py) by:
- the coordinator, which polls the portal 4 times a day (coordinator.py);
- the action water_history.import_water_history, which reads a CSV file.
The sensor's own state stays constant on purpose (see sensor.py).
"""
from __future__ import annotations

import csv
from datetime import datetime, time
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import CONF_CONTRACT, CONF_LOGIN, CONF_PASSWORD, DOMAIN, FIRST_POLL_DELAY
from .coordinator import WaterCoordinator
from .csv_import import ImportDataError, build_series, read_rows
from .readings import windows_from_series

SERVICE = "import_water_history"
PLATFORMS = ["sensor", "button"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("file_path"): cv.string,
        vol.Optional("value_type", default="daily"): vol.In(["daily", "index"]),
        vol.Optional("unit", default="m3"): vol.In(["m3", "L"]),
        vol.Optional("delimiter", default="auto"): cv.string,
        vol.Optional("date_column", default=1): vol.All(int, vol.Range(min=1)),
        vol.Optional("value_column", default=2): vol.All(int, vol.Range(min=1)),
        vol.Optional("date_format"): cv.string,
    }
)


async def _async_import_file(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    data = dict(call.data)
    entity_id = data["entity_id"]
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.platform != DOMAIN:
        raise ServiceValidationError(
            f"{entity_id} n'est pas un capteur créé par Consommation eau Metz Métropole."
        )
    path = hass.config.path(data["file_path"])
    if not hass.config.is_allowed_path(path):
        raise ServiceValidationError(
            f"Chemin non autorisé : {path}. Place le fichier dans /config "
            "(ou ajoute son dossier à allowlist_external_dirs)."
        )
    try:
        rows, skipped = await hass.async_add_executor_job(read_rows, path, data)
    except FileNotFoundError as err:
        raise ServiceValidationError(f"Fichier introuvable : {path}") from err
    except (OSError, UnicodeDecodeError, csv.Error) as err:
        raise ServiceValidationError(f"Lecture impossible : {err}") from err

    zone = dt_util.DEFAULT_TIME_ZONE
    # One value per day: time of day is ignored, the highest value wins.
    rows = [(datetime.combine(m.date(), time.min), v) for m, v in rows]
    try:
        series = build_series(rows, data, zone)
    except ImportDataError as err:
        raise ServiceValidationError(str(err)) from err

    try:
        from .recorder_import import (
            HistoryReplacementError,
            async_replace_entity_history,
        )
    except ImportError as err:  # recorder internals changed in this HA version
        raise ServiceValidationError(f"Import indisponible avec cette version de HA : {err}") from err
    try:
        result = await async_replace_entity_history(
            hass, entity_id, windows_from_series(series, zone)
        )
    except HistoryReplacementError as err:
        raise ServiceValidationError(str(err)) from err

    _LOGGER.info("%s: %d days imported (%.3f %s)", entity_id, result.days, result.total, result.unit)
    return {
        "entity_id": entity_id,
        "days_imported": result.days,
        "rows_skipped": skipped,
        "first": dt_util.as_local(series[0][0]).date().isoformat(),
        "last": dt_util.as_local(series[-1][0]).date().isoformat(),
        "total": round(result.total, 3),
        "unit": result.unit,
        "final_sum": round(result.final_sum, 3),
        "rows_replaced": result.cleared,
        "later_rows_shifted": result.adjusted,
    }


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def handler(call: ServiceCall) -> ServiceResponse:
        return await _async_import_file(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE,
        handler,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WaterCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.cancel_retry)
    if coordinator.client is not None:

        async def _first_poll(_now) -> None:
            await coordinator.async_refresh()

        entry.async_on_unload(async_call_later(hass, FIRST_POLL_DELAY, _first_poll))
    else:
        _LOGGER.warning(
            "%s : identifiants du portail absents, utilise « Reconfigurer » sur l'intégration",
            entry.title,
        )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: the entry is now titled "Contrat <id>" instead of the sensor name."""
    if entry.version < 2:
        contract = entry.data.get(CONF_CONTRACT)
        title = f"Contrat {contract}" if contract else entry.title
        hass.config_entries.async_update_entry(entry, title=title, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
