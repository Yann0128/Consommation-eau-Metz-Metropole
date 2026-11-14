"""Polls the portal and writes the daily consumption into the sensor's statistics."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ANCHOR_RETRY_DELAY,
    AUTH_FAILURES_BEFORE_REAUTH,
    CONF_CONTRACT,
    CONF_LOGIN,
    CONF_PASSWORD,
    DOMAIN,
    MAX_CATCHUP_DAYS,
    UPDATE_INTERVAL,
    WINDOW_DAYS,
)
from .portal import (
    PortalAuthError,
    PortalClient,
    PortalDataError,
    PortalError,
)
from .readings import (
    daily_consumption,
    meter_serial,
    parse_readings,
    windows_from_daily,
)

_LOGGER = logging.getLogger(__name__)


class WaterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One poll = re-read the last days, rebuild the daily values, rewrite them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        configured = all(entry.data.get(k) for k in (CONF_LOGIN, CONF_PASSWORD, CONF_CONTRACT))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=UPDATE_INTERVAL if configured else None,
        )
        self.client: PortalClient | None = None
        if configured:
            self.client = PortalClient(
                async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar()),
                entry.data[CONF_LOGIN],
                entry.data[CONF_PASSWORD],
                entry.data[CONF_CONTRACT],
            )
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._auth_failures = 0
        self._cancel_retry = None
        self._lock = asyncio.Lock()

    def cancel_retry(self) -> None:
        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

    def _schedule_retry(self) -> None:
        if self._cancel_retry is not None:
            return

        async def _retry(_now) -> None:
            self._cancel_retry = None
            await self.async_request_refresh()

        self._cancel_retry = async_call_later(self.hass, ANCHOR_RETRY_DELAY, _retry)

    async def _start_date(self, today: date) -> date:
        start = today - timedelta(days=WINDOW_DAYS)
        stored = await self._store.async_load() or {}
        try:
            last = date.fromisoformat(stored["last_day"])
        except (KeyError, ValueError, TypeError):
            return start
        # HA was off for a while: catch up from the last written day (bounded)
        return min(start, max(last - timedelta(days=2), today - timedelta(days=MAX_CATCHUP_DAYS)))

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            return await self._async_poll()

    async def _async_poll(self) -> dict[str, Any]:
        if self.client is None:
            raise UpdateFailed(
                "Identifiants du portail manquants : utilise « Reconfigurer » sur l'intégration"
            )
        try:
            from .recorder_import import (
                AnchorMissingError,
                HistoryReplacementError,
                async_replace_entity_history,
            )
        except ImportError as err:  # recorder internals changed in this HA version
            raise UpdateFailed(f"Écriture d'historique indisponible : {err}") from err

        tz = dt_util.DEFAULT_TIME_ZONE
        today = dt_util.now().date()
        start = await self._start_date(today)
        try:
            items = await self.client.async_fetch(start, today)
        except PortalAuthError as err:
            self._auth_failures += 1
            if self._auth_failures >= AUTH_FAILURES_BEFORE_REAUTH:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise UpdateFailed(f"Portail : {err}") from err
        except PortalDataError as err:
            raise UpdateFailed(f"Réponse du portail inattendue : {err}") from err
        except PortalError as err:
            raise UpdateFailed(f"Portail injoignable : {err}") from err
        self._auth_failures = 0

        readings = parse_readings(items, tz)
        daily = daily_consumption(readings, tz)
        if daily:
            entity_id = er.async_get(self.hass).async_get_entity_id(
                "sensor", DOMAIN, self.config_entry.entry_id
            )
            if entity_id is None:
                raise UpdateFailed("Capteur pas encore enregistré")
            windows = windows_from_daily(daily, tz)
            try:
                result = await async_replace_entity_history(self.hass, entity_id, windows)
            except AnchorMissingError as err:
                self._schedule_retry()
                raise UpdateFailed(str(err)) from err
            except HistoryReplacementError as err:
                raise UpdateFailed(str(err)) from err
            await self._store.async_save({"last_day": daily[-1].day.isoformat()})
            _LOGGER.info(
                "%s : %d jours écrits (%s → %s, dernier : %s), %d lignes remplacées",
                entity_id, result.days, daily[0].day, daily[-1].day,
                daily[-1].status, result.cleared,
            )

        last_reading = readings[-1] if readings else None
        return {
            "meter_serial": meter_serial(items),
            "index_m3": round(last_reading.index_l / 1000, 3) if last_reading else None,
            "last_reading": last_reading.moment.isoformat() if last_reading else None,
            "last_reading_estimated": last_reading.estimated if last_reading else None,
            "last_sync": dt_util.utcnow().isoformat(),
            "days_written": len(daily),
            "written_range": f"{daily[0].day} → {daily[-1].day}" if daily else None,
            "last_day_status": daily[-1].status if daily else None,
            "daily_status": {d.day.isoformat(): d.status for d in daily},
        }
