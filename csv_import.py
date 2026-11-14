"""CSV parsing for the file import action (no Home Assistant import)."""
from __future__ import annotations

import csv
from datetime import datetime, timezone, tzinfo
import re
from typing import Any

HOUR = 3600
DATE_FORMATS = (
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


class ImportDataError(Exception):
    """The file content cannot be imported."""


def _parse_date(text: str, fmt: str | None) -> datetime | None:
    text = text.strip().strip('"')
    for candidate in (fmt,) if fmt else DATE_FORMATS:
        try:
            return datetime.strptime(text, candidate)
        except ValueError:
            continue
    if not fmt:
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            pass
    return None


def _parse_number(text: str) -> float | None:
    cleaned = re.sub(r"[\s\u00a0]", "", text.strip().strip('"')).replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def _split(line: str, delimiter: str) -> list[str]:
    """auto = ';' if present on the line, else ','."""
    if delimiter == "auto":
        delimiter = ";" if ";" in line else ","
    return next(csv.reader([line], delimiter=delimiter))


def read_rows(path: str, data: dict[str, Any]) -> tuple[list[tuple[datetime, float]], int]:
    """Blocking. Blank lines ignored; header or unparseable lines counted as skipped."""
    rows: list[tuple[datetime, float]] = []
    skipped = 0
    date_idx, value_idx = data["date_column"] - 1, data["value_column"] - 1
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = _split(line, data["delimiter"])
            if len(record) <= max(date_idx, value_idx):
                skipped += 1
                continue
            moment = _parse_date(record[date_idx], data.get("date_format"))
            value = _parse_number(record[value_idx])
            if moment is None or value is None:
                skipped += 1
                continue
            rows.append((moment, value))
    return rows, skipped


def build_series(
    rows: list[tuple[datetime, float]], data: dict[str, Any], zone: tzinfo
) -> list[tuple[datetime, float, float]]:
    """[(UTC hour start, consumption m3, state)] ; state = consumption (daily)
    or the meter index in m3 (index mode). A day listed twice keeps its highest value."""
    if not rows:
        raise ImportDataError("Aucune ligne exploitable dans le fichier")
    factor = 0.001 if data["unit"] == "L" else 1.0
    highest: dict[datetime, float] = {}
    for moment, value in rows:
        highest[moment] = max(value, highest.get(moment, value))
    stamped = sorted((moment, value * factor) for moment, value in highest.items())

    if data["value_type"] == "index":
        triples = []
        for (_, previous), (moment, current) in zip(stamped, stamped[1:]):
            delta = current - previous
            if delta < 0:
                raise ImportDataError(f"Index décroissant au {moment} (remplacement de compteur ?)")
            triples.append((moment, delta, current))
        if not triples:
            raise ImportDataError("Au moins deux relevés d'index sont nécessaires")
    else:
        if any(value < 0 for _, value in stamped):
            raise ImportDataError("Consommation négative dans le fichier")
        triples = [(moment, value, value) for moment, value in stamped]

    buckets: dict[int, list[float]] = {}
    for moment, value, state in triples:
        ts = int(moment.replace(tzinfo=zone).timestamp())
        ts = -(-ts // HOUR) * HOUR  # the recorder only stores whole-hour starts
        if ts in buckets:
            buckets[ts][0] += value
            buckets[ts][1] = state
        else:
            buckets[ts] = [value, state]
    return [
        (datetime.fromtimestamp(ts, timezone.utc), buckets[ts][0], buckets[ts][1])
        for ts in sorted(buckets)
    ]
