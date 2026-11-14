"""Atomic replacement of an existing sensor's long-term statistics.

Adapted from intuis_connect/recorder_import.py (daily branch).

HA continues a total_increasing / total sensor from its *short-term*
statistics, so the import refuses to run unless a live short-term row exists
after the imported period (the "anchor"). Imported days replace the rows they
cover; every later row keeps its own increments and only follows the sum
offset, so the series stays continuous.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import math
import threading
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.tasks import RecorderTask
from sqlalchemy import delete, select, update

HOUR = 3600
# Statistic unit of the target sensor -> factor from m³.
UNIT_FACTORS = {"m³": 1.0, "L": 1000.0}

# (start, end, ((hour start, consumption, state), ...)) ; state = index in the sensor unit
Window = tuple[datetime, datetime, tuple[tuple[datetime, float, float], ...]]


class HistoryReplacementError(Exception):
    """History cannot safely be replaced; the previous state is preserved."""


class AnchorMissingError(HistoryReplacementError):
    """The sensor has no live statistics yet (just created): retry a bit later."""


@dataclass(frozen=True)
class ReplacementResult:
    days: int
    total: float
    final_sum: float
    cleared: int
    adjusted: int
    unit: str


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _replace_windows(
    session: Any,
    tables: tuple[type, ...],
    metadata_id: int,
    windows: tuple[Window, ...],
    cancelled: threading.Event,
) -> tuple[float, int, int]:
    """Replace each window in order; rows between windows keep their values."""

    def sum_before(boundary: float) -> float:
        candidates = [
            session.scalar(
                select(table)
                .where(table.metadata_id == metadata_id, table.start_ts < boundary)
                .order_by(table.start_ts.desc())
                .limit(1)
            )
            for table in tables
        ]
        existing = [row for row in candidates if row is not None]
        return max(existing, key=lambda row: row.start_ts).sum if existing else 0.0

    cumulative = 0.0
    cleared = adjusted = 0
    for start, end, samples in windows:
        start_ts, end_ts = start.timestamp(), end.timestamp()
        baseline = sum_before(start_ts)
        old_final = sum_before(end_ts)
        if not _finite(baseline) or not _finite(old_final):
            raise HistoryReplacementError(
                "Les cumuls existants sont invalides ; l'historique est préservé"
            )
        if cancelled.is_set():
            raise asyncio.CancelledError
        cumulative = baseline
        rows = []
        for timestamp, value, state in samples:
            cumulative += value
            if not math.isfinite(cumulative):
                raise HistoryReplacementError("Cumul non fini")
            rows.append(
                Statistics.from_stats(
                    metadata_id, {"start": timestamp, "state": state, "sum": cumulative}
                )
            )
        for table in tables:
            cleared += session.execute(
                delete(table).where(
                    table.metadata_id == metadata_id,
                    table.start_ts >= start_ts,
                    table.start_ts < end_ts,
                )
            ).rowcount
        session.add_all(rows)
        delta = cumulative - old_final
        if delta:
            for table in tables:
                adjusted += session.execute(
                    update(table)
                    .where(table.metadata_id == metadata_id, table.start_ts >= end_ts)
                    .values(sum=table.sum + delta)
                ).rowcount
        session.flush()
    if cancelled.is_set():
        raise asyncio.CancelledError
    return cumulative, cleared, adjusted


def _replace_in_transaction(
    instance: Any,
    entity_id: str,
    windows: tuple[Window, ...],
    cancelled: threading.Event,
) -> ReplacementResult:
    """Run only on the recorder worker; any exception rolls back all changes."""
    end_ts = windows[-1][1].timestamp()
    with instance.get_session() as session, session.begin():
        meta = session.scalar(
            select(StatisticsMeta).where(StatisticsMeta.statistic_id == entity_id)
        )
        if meta is None:
            raise AnchorMissingError(
                f"{entity_id} n'a pas encore de statistiques ; attends environ 10 minutes "
                "après sa création, puis réessaie"
            )
        if meta.source != "recorder" or not meta.has_sum:
            raise HistoryReplacementError(
                f"{entity_id} n'a pas de statistiques cumulatives (état « total » ou "
                "« total_increasing » requis)"
            )
        factor = UNIT_FACTORS.get(meta.unit_of_measurement)
        if factor is None:
            raise HistoryReplacementError(
                f"Unité du capteur non gérée : {meta.unit_of_measurement!r} (m³ ou L)"
            )
        anchor = session.scalar(
            select(StatisticsShortTerm)
            .where(
                StatisticsShortTerm.metadata_id == meta.id,
                StatisticsShortTerm.start_ts >= end_ts,
            )
            .order_by(StatisticsShortTerm.start_ts.desc())
            .limit(1)
        )
        if anchor is None or not _finite(anchor.sum) or not _finite(anchor.state):
            raise AnchorMissingError(
                "Aucune statistique live du capteur après la période importée ; "
                "attends qu'il en produise, puis réessaie"
            )
        scaled = tuple(
            (start, end, tuple((ts, value * factor, state * factor) for ts, value, state in samples))
            for start, end, samples in windows
        )
        final_sum, cleared, adjusted = _replace_windows(
            session, (Statistics, StatisticsShortTerm), meta.id, scaled, cancelled
        )
        count = sum(len(samples) for _, _, samples in scaled)
        total = sum(value for _, _, samples in scaled for _, value, _ in samples)
        return ReplacementResult(
            count, total, final_sum, cleared, adjusted, meta.unit_of_measurement
        )


@dataclass
class _AcknowledgedTask(RecorderTask):
    transaction: Callable[[Any, threading.Event], Any]
    cancelled: threading.Event
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop

    def run(self, instance: Any) -> None:
        try:
            result = self.transaction(instance, self.cancelled)
        except asyncio.CancelledError as err:
            self.loop.call_soon_threadsafe(self._finish, None, err)
        except HistoryReplacementError as err:
            self.loop.call_soon_threadsafe(self._finish, None, err)
        except Exception as err:
            error = HistoryReplacementError(f"Échec de l'écriture : {err}")
            self.loop.call_soon_threadsafe(self._finish, None, error)
        else:
            self.loop.call_soon_threadsafe(self._finish, result, None)

    def _finish(self, result: Any, error: BaseException | None) -> None:
        if self.future.done():
            return
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(result)


def _running_instance(hass: Any) -> Any:
    instance = get_instance(hass)
    if not callable(getattr(instance, "queue_task", None)) or not callable(
        getattr(instance, "get_session", None)
    ):
        raise HistoryReplacementError(
            "Cette version du recorder ne permet pas le remplacement transactionnel"
        )
    if (
        not callable(getattr(instance, "is_alive", None))
        or not instance.is_alive()
        or not getattr(instance, "recording", False)
        or getattr(instance, "stop_requested", False)
    ):
        raise HistoryReplacementError("Le recorder n'est pas actif ; rien n'a été modifié")
    return instance


def _validate_windows(windows: tuple[Window, ...]) -> tuple[Window, ...]:
    windows = tuple(windows)
    if not windows:
        raise HistoryReplacementError("Période vide")
    previous_end = None
    for start, end, samples in windows:
        if not samples or start.tzinfo is None or end.tzinfo is None or start >= end:
            raise HistoryReplacementError("Période invalide ou vide")
        if previous_end is not None and start < previous_end:
            raise HistoryReplacementError("Les fenêtres doivent être disjointes et ordonnées")
        previous_ts = None
        for timestamp, value, state in samples:
            ts = timestamp.timestamp()
            if not start.timestamp() <= ts < end.timestamp():
                raise HistoryReplacementError("Échantillon hors de sa fenêtre")
            if previous_ts is not None and ts <= previous_ts:
                raise HistoryReplacementError("Échantillons non chronologiques")
            if not _finite(value) or value < 0 or not _finite(state) or state < 0:
                raise HistoryReplacementError("Valeur négative ou non finie")
            previous_ts = ts
        if any(
            moment.timestamp() % HOUR
            for moment in (start, end, *(timestamp for timestamp, _, _ in samples))
        ):
            raise HistoryReplacementError("Les bornes doivent tomber sur une heure pleine")
        previous_end = end
    return windows


async def async_replace_entity_history(
    hass: Any, entity_id: str, windows: tuple[Window, ...]
) -> ReplacementResult:
    """Replace the given days of a sensor's statistics in one acknowledged transaction."""
    instance = _running_instance(hass)
    windows = _validate_windows(windows)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    cancelled = threading.Event()
    instance.queue_task(
        _AcknowledgedTask(
            lambda worker, cancel: _replace_in_transaction(
                worker, entity_id, windows, cancel
            ),
            cancelled,
            future,
            loop,
        )
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancelled.set()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if future.done() and not future.cancelled():
            future.exception()
        raise
