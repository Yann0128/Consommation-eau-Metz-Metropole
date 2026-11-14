"""Pure logic: meter readings -> consumption per local day -> statistics windows.

No Home Assistant import here, so it can be tested on its own.
Readings come at irregular times; the consumption between two readings is
spread over the local days it covers, proportionally to the time spent in each
day. Each day gets a status:
- "provisoire": the day is not fully covered yet (it contains the last reading);
- "estimée": fully covered, but at least one covering interval ends on a reading
  that the provider flags as estimated;
- "calculée": fully covered, computed from real readings only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import logging
import math
from typing import Any

_LOGGER = logging.getLogger(__name__)
HOUR = 3600

STATUS_COMPUTED = "calculée"
STATUS_ESTIMATED = "estimée"
STATUS_PROVISIONAL = "provisoire"


@dataclass(frozen=True)
class Reading:
    moment: datetime  # timezone-aware
    index_l: float  # meter index in litres
    estimated: bool  # the interval ending on this reading is an estimate


@dataclass(frozen=True)
class DailyValue:
    day: date
    consumption_m3: float
    end_index_m3: float  # meter index at the end of the day (at the last reading if provisional)
    status: str


def _num(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _parse_moment(text: Any, tz: tzinfo) -> datetime | None:
    if not isinstance(text, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def meter_serial(items: list[Any]) -> str | None:
    """Serial number of the meter (numserie), from the most recent row that has one."""
    for item in reversed(items):
        attrs = item.get("attributes") if isinstance(item, dict) else None
        serial = attrs.get("numserie") if isinstance(attrs, dict) else None
        if isinstance(serial, str) and serial.strip():
            return serial.strip()
    return None


def parse_readings(items: list[Any], tz: tzinfo) -> list[Reading]:
    """Real readings only, chronological, index never decreasing.

    Placeholder rows (add: true) are ignored. The row preceding the earliest
    reading (previous_dateni / previous_ni) is added as the starting point.
    """
    found: dict[float, Reading] = {}
    earliest: tuple[datetime, dict[str, Any]] | None = None
    for item in items:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict) or attrs.get("add") is True:
            continue
        moment = _parse_moment(attrs.get("dateni"), tz)
        index = attrs.get("ni")
        if moment is None or not _num(index):
            continue
        found[moment.timestamp()] = Reading(
            moment, float(index), bool(attrs.get("estimated_consumption"))
        )
        if earliest is None or moment.timestamp() < earliest[0].timestamp():
            earliest = (moment, attrs)
    if earliest is not None:
        moment, attrs = earliest
        previous = _parse_moment(attrs.get("previous_dateni"), tz)
        previous_index = attrs.get("previous_ni")
        if (
            previous is not None
            and _num(previous_index)
            and previous.timestamp() < moment.timestamp()
            and previous.timestamp() not in found
        ):
            found[previous.timestamp()] = Reading(previous, float(previous_index), False)
    kept: list[Reading] = []
    for reading in sorted(found.values(), key=lambda r: r.moment.timestamp()):
        if kept and reading.index_l < kept[-1].index_l:
            _LOGGER.warning(
                "Index décroissant ignoré au %s (%s < %s)",
                reading.moment, reading.index_l, kept[-1].index_l,
            )
            continue
        kept.append(reading)
    return kept


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _covering(readings: list[Reading], start: float, end: float) -> tuple[float, bool]:
    """Litres consumed in [start, end] and whether any covering interval is estimated."""
    total = 0.0
    estimated = False
    for a, b in zip(readings, readings[1:]):
        a_ts, b_ts = a.moment.timestamp(), b.moment.timestamp()
        span = b_ts - a_ts
        overlap = min(b_ts, end) - max(a_ts, start)
        if span > 0 and overlap > 0:
            total += (b.index_l - a.index_l) * overlap / span
            estimated = estimated or b.estimated
    return total, estimated


def _index_at(readings: list[Reading], ts: float) -> float:
    for a, b in zip(readings, readings[1:]):
        a_ts, b_ts = a.moment.timestamp(), b.moment.timestamp()
        if a_ts <= ts <= b_ts:
            if b_ts == a_ts:
                return b.index_l
            return a.index_l + (b.index_l - a.index_l) * (ts - a_ts) / (b_ts - a_ts)
    return readings[-1].index_l


def daily_consumption(readings: list[Reading], tz: tzinfo) -> list[DailyValue]:
    """Consumption per local day: every fully covered day, then the provisional
    day (the one containing the last reading) when it has started within the
    known span. The head day, started before the first reading, is never written."""
    if len(readings) < 2:
        return []
    first_ts = readings[0].moment.timestamp()
    last_ts = readings[-1].moment.timestamp()
    day = readings[0].moment.astimezone(tz).date()
    if _midnight(day, tz).timestamp() < first_ts:
        day += timedelta(days=1)
    result: list[DailyValue] = []
    while True:
        start = _midnight(day, tz).timestamp()
        end = _midnight(day + timedelta(days=1), tz).timestamp()
        if end > last_ts:
            break
        litres, estimated = _covering(readings, start, end)
        result.append(
            DailyValue(
                day,
                litres / 1000.0,
                _index_at(readings, end) / 1000.0,
                STATUS_ESTIMATED if estimated else STATUS_COMPUTED,
            )
        )
        day += timedelta(days=1)
    if start >= first_ts and last_ts > start:
        litres, _ = _covering(readings, start, last_ts)
        result.append(
            DailyValue(day, litres / 1000.0, readings[-1].index_l / 1000.0, STATUS_PROVISIONAL)
        )
    return result


def _ceil_hour_utc(ts: float) -> datetime:
    return datetime.fromtimestamp(-(-int(ts) // HOUR) * HOUR, timezone.utc)


def series_from_daily(
    daily: list[DailyValue], tz: tzinfo
) -> list[tuple[datetime, float, float]]:
    """[(UTC hour start of the local day, consumption m3, index m3)]."""
    return [
        (_ceil_hour_utc(_midnight(d.day, tz).timestamp()), d.consumption_m3, d.end_index_m3)
        for d in daily
    ]


def windows_from_series(
    series: list[tuple[datetime, float, float]],
    tz: tzinfo,
    provisional_start: datetime | None = None,
):
    """Group consecutive local days into [start, end) windows, one row per day.

    A provisional day may be today: its window only covers its first hour, so
    that live statistics after it can still anchor the write.
    """
    windows: list[list[Any]] = []
    for start, value, state in series:
        if provisional_start is not None and start == provisional_start:
            end = start + timedelta(hours=1)
        else:
            local_day = start.astimezone(tz).date()
            end = _ceil_hour_utc(_midnight(local_day + timedelta(days=1), tz).timestamp())
        if windows and windows[-1][1] == start:
            windows[-1][1] = end
            windows[-1][2].append((start, value, state))
        else:
            windows.append([start, end, [(start, value, state)]])
    return tuple((s, e, tuple(samples)) for s, e, samples in windows)


def windows_from_daily(daily: list[DailyValue], tz: tzinfo):
    series = series_from_daily(daily, tz)
    provisional = series[-1][0] if daily and daily[-1].status == STATUS_PROVISIONAL else None
    return windows_from_series(series, tz, provisional)
