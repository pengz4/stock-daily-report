"""Dated ETF market-value proxies, never attributed national-team holdings."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

ETF_CODES = ("510300", "510310", "510330", "159919")
SHANGHAI = ZoneInfo("Asia/Shanghai")
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
DISCLAIMER = (
    "国家队观察代理，非国家队实际持仓。分子为固定四只ETF的收盘价×行情最新份额，"
    "不是基金净资产；分母为沪深300成分股流通市值之和，不是自由流通调整市值。"
    "最新份额可能滞后，份额变化不是已确认净申购或国家队买卖；"
    "价格、折溢价、分红、份额折算、成分调整及流通股本变化均可能影响指标。"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EtfQuote(_Model):
    code: Literal["510300", "510310", "510330", "159919"]
    name: str = Field(min_length=1)
    price: Positive
    shares: Positive
    source_timestamp: AwareDatetime


class ConstituentCap(_Model):
    code: str = Field(pattern=r"^(?:00|30|60|68)\d{4}$")
    circulating_market_cap: Positive
    source_timestamp: AwareDatetime


class EtfSnapshot(_Model):
    rule_version: Literal["csi300-etf-market-value-v1"] = "csi300-etf-market-value-v1"
    report_date: date
    membership_date: date
    generated_at: AwareDatetime
    membership_source: Literal["csindex:000300cons.xls"] = "csindex:000300cons.xls"
    quote_source: Literal["eastmoney:ulist.np"] = "eastmoney:ulist.np"
    etfs: tuple[EtfQuote, ...]
    constituents: tuple[ConstituentCap, ...]

    @model_validator(mode="after")
    def require_complete_closing_snapshot(self) -> EtfSnapshot:
        if (
            len(self.etfs) != 4
            or {row.code for row in self.etfs} != set(ETF_CODES)
            or len(self.constituents) != 300
            or len({row.code for row in self.constituents}) != 300
        ):
            raise ValueError("exactly four unique ETFs and 300 constituents required")
        if self.membership_date > self.report_date:
            raise ValueError("membership effective date must not be in the future")
        captured = self.generated_at.astimezone(SHANGHAI)
        if captured.date() != self.report_date or captured.time() < time(15, 10):
            raise ValueError("snapshot must be captured on report date after 15:10")
        for row in (*self.etfs, *self.constituents):
            stamp = row.source_timestamp.astimezone(SHANGHAI)
            if (
                stamp.date() != self.report_date
                or stamp.time() < time(15)
                or row.source_timestamp > self.generated_at
            ):
                raise ValueError(f"stale, intraday or future quote: {row.code}")
        return self

    @property
    def input_hash(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump(mode="json")).encode()).hexdigest()


class EtfPoint(_Model):
    report_date: date
    etf_market_cap: Positive
    circulating_market_cap: Positive
    ratio: Positive
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_ratio(self) -> EtfPoint:
        if not math.isclose(
            self.ratio, self.etf_market_cap / self.circulating_market_cap,
            rel_tol=1e-12,
        ):
            raise ValueError("ETF ratio does not match market values")
        return self


class EtfFundChange(EtfQuote):
    market_cap: Positive
    shares_change: Finite | None = None
    shares_change_pct: Finite | None = None


class EtfTrackerReport(_Model):
    status: Literal["available", "unavailable"] = "unavailable"
    reason: Literal["snapshot_missing", "snapshot_invalid", "not_provided"] | None = (
        "not_provided"
    )
    current: EtfPoint | None = None
    membership_date: date | None = None
    funds: tuple[EtfFundChange, ...] = ()
    comparison_date: date | None = None
    ratio_change_pp: Finite | None = None
    membership_changed: bool = False
    history: tuple[EtfPoint, ...] = ()
    skipped_history_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_consistent_state(self) -> EtfTrackerReport:
        if self.status == "available":
            if (
                self.reason is not None
                or self.current is None
                or self.membership_date is None
                or self.membership_date > self.current.report_date
                or len(self.funds) != 4
            ):
                raise ValueError("available tracker requires complete data")
            if (
                not self.history
                or self.history[-1] != self.current
                or [p.report_date for p in self.history]
                != sorted({p.report_date for p in self.history})
            ):
                raise ValueError("tracker history must end at current observation")
        elif (
            self.reason is None
            or self.current is not None
            or self.membership_date is not None
            or self.funds
            or self.history
        ):
            raise ValueError("unavailable tracker cannot contain computed observations")
        return self


class TrackerProvider(Protocol):
    def fetch(self, report_date: date) -> EtfSnapshot: ...


def snapshot_path(root: str | Path, day: date) -> Path:
    return Path(root) / "etf-tracker" / day.isoformat() / "input.json"


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def load_snapshot(path: str | Path) -> EtfSnapshot:
    path = Path(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"snapshot", "input_hash"}:
        raise ValueError("invalid ETF artifact schema")
    snapshot = EtfSnapshot.model_validate(document["snapshot"])
    if snapshot.input_hash != document["input_hash"]:
        raise ValueError("ETF snapshot hash mismatch")
    if snapshot.report_date.isoformat() != path.parent.name:
        raise ValueError("ETF snapshot date does not match directory")
    return snapshot


def save_snapshot(root: str | Path, snapshot: EtfSnapshot) -> Path:
    snapshot = EtfSnapshot.model_validate(snapshot.model_dump())
    path = snapshot_path(root, snapshot.report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".input.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if path.exists():
                existing = load_snapshot(path)
                if existing != snapshot:
                    raise ValueError(f"Refusing to overwrite immutable ETF snapshot: {path}")
                return path
            temporary = path.parent / f".input.{uuid4().hex}.tmp"
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    handle.write(_canonical({
                        "snapshot": snapshot.model_dump(mode="json"),
                        "input_hash": snapshot.input_hash,
                    }) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return path


def collect_tracker(
    root: str | Path,
    report_date: date,
    *,
    provider: TrackerProvider | None = None,
    now: datetime | None = None,
) -> Path:
    path = snapshot_path(root, report_date)
    if path.exists():
        load_snapshot(path)
        return path
    timestamp = now or datetime.now(UTC)
    local = timestamp.astimezone(SHANGHAI)
    if (
        timestamp.utcoffset() is None
        or local.date() != report_date
        or local.time() < time(15, 10)
        or report_date.weekday() >= 5
    ):
        raise ValueError("live ETF capture requires current market date after 15:10 CST")
    if provider is None:
        from stock_daily_report.providers.etf import Csi300EtfProvider

        provider = Csi300EtfProvider()
    snapshot = provider.fetch(report_date)
    if snapshot.report_date != report_date:
        raise ValueError("provider returned a different ETF snapshot date")
    return save_snapshot(root, snapshot)


def _point(snapshot: EtfSnapshot) -> EtfPoint:
    numerator = math.fsum(fund.price * fund.shares for fund in snapshot.etfs)
    denominator = math.fsum(row.circulating_market_cap for row in snapshot.constituents)
    return EtfPoint(
        report_date=snapshot.report_date,
        etf_market_cap=numerator,
        circulating_market_cap=denominator,
        ratio=numerator / denominator,
        input_hash=snapshot.input_hash,
    )


def build_tracker_report(root: str | Path, report_date: date) -> EtfTrackerReport:
    path = snapshot_path(root, report_date)
    try:
        current = load_snapshot(path)
        current_point = _point(current)
    except FileNotFoundError:
        return EtfTrackerReport(reason="snapshot_missing")
    except (OSError, ValueError, OverflowError):
        return EtfTrackerReport(reason="snapshot_invalid")
    history = []
    previous = None
    skipped = 0
    # Persist all observations; bound only the report's embedded history.
    for candidate in sorted(path.parent.parent.glob("????-??-??/input.json"), reverse=True):
        if candidate.parent.name >= report_date.isoformat():
            continue
        try:
            snapshot = load_snapshot(candidate)
            point = _point(snapshot)
        except (OSError, ValueError, OverflowError):
            skipped += 1
            continue
        if previous is None:
            previous = snapshot
        history.append(point)
        if len(history) == 251:
            break
    history.reverse()
    previous_funds = {row.code: row for row in previous.etfs} if previous else {}
    funds = []
    for fund in current.etfs:
        old = previous_funds.get(fund.code)
        change = fund.shares - old.shares if old else None
        funds.append(EtfFundChange(
            **fund.model_dump(),
            market_cap=fund.price * fund.shares,
            shares_change=change,
            shares_change_pct=change / old.shares * 100 if old else None,
        ))
    return EtfTrackerReport(
        status="available",
        reason=None,
        current=current_point,
        membership_date=current.membership_date,
        funds=tuple(funds),
        comparison_date=previous.report_date if previous else None,
        ratio_change_pp=(
            (current_point.ratio - history[-1].ratio) * 100 if previous else None
        ),
        membership_changed=(
            {row.code for row in current.constituents}
            != {row.code for row in previous.constituents} if previous else False
        ),
        history=(*history, current_point),
        skipped_history_count=skipped,
    )
