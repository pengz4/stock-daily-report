"""Versioned JSON report for structural replay comparisons."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from numbers import Real
from pathlib import Path

from .replay import ReplayEvent
from .structure_metrics import compare_structures

REPORT_SCHEMA_VERSION = 1
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def build_report(
    *,
    report_date: date,
    snapshot_hash: str,
    events_by_analyzer: Mapping[str, Sequence[ReplayEvent]],
    analyzer_profile_hashes: Mapping[str, str],
    reference_analyzer: str | None = None,
    price_tolerance: float = 0.0,
    time_tolerance_days: int = 0,
    backtest_assumptions: Mapping[str, object] | None = None,
    evaluations: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Build deterministic structural comparison report data."""

    if not _HASH_PATTERN.fullmatch(snapshot_hash):
        raise ValueError("snapshot_hash must be a lowercase SHA-256 hex digest")
    if (
        isinstance(price_tolerance, bool)
        or not isinstance(price_tolerance, Real)
        or not math.isfinite(price_tolerance)
        or price_tolerance < 0
    ):
        raise ValueError("price_tolerance must be finite and non-negative")
    if (
        isinstance(time_tolerance_days, bool)
        or not isinstance(time_tolerance_days, int)
        or time_tolerance_days < 0
    ):
        raise ValueError("time_tolerance_days must be a non-negative integer")
    if set(events_by_analyzer) != set(analyzer_profile_hashes):
        raise ValueError("analyzer events and profile hashes must have the same keys")
    if any(not _HASH_PATTERN.fullmatch(value) for value in analyzer_profile_hashes.values()):
        raise ValueError("analyzer profile hashes must be lowercase SHA-256 digests")
    names = sorted(events_by_analyzer)
    comparisons: dict[str, object] = {}
    if reference_analyzer is None:
        strict_names = [name for name in names if name.startswith("strict-")]
        if len(strict_names) == 1:
            reference_analyzer = strict_names[0]
    if reference_analyzer is not None:
        if reference_analyzer not in events_by_analyzer:
            raise ValueError("reference_analyzer must name a supplied analyzer")
        reference = events_by_analyzer[reference_analyzer]
        for name in names:
            if name == reference_analyzer:
                continue
            comparisons[name] = compare_structures(
                reference,
                events_by_analyzer[name],
                price_tolerance=price_tolerance,
                time_tolerance_days=time_tolerance_days,
            ).to_dict()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "snapshot_hash": snapshot_hash,
        "analyzer_profile_hashes": {
            name: analyzer_profile_hashes[name] for name in names
        },
        "event_counts": {
            name: len(events_by_analyzer[name]) for name in names
        },
        "comparison_tolerances": {
            "price": price_tolerance,
            "time_days": time_tolerance_days,
        },
        "comparisons": comparisons,
        "backtest_assumptions": dict(backtest_assumptions or {}),
        "evaluations": {
            name: dict(value) for name, value in (evaluations or {}).items()
        },
    }


def write_report(report: Mapping[str, object], output_dir: str | Path) -> Path:
    """Write a report below ``output_dir/YYYY-MM-DD/report.json``."""

    report_date = report.get("report_date")
    if not isinstance(report_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", report_date
    ):
        raise ValueError("report_date must be an ISO date string")
    destination = Path(output_dir) / report_date
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "report.json"
    path.write_text(
        json.dumps(
            report,
            allow_nan=False,
            default=_json_default,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


__all__ = ["REPORT_SCHEMA_VERSION", "build_report", "write_report"]
