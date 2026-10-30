"""Bounded CSI membership and Eastmoney closing-quote requests for four ETFs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from http.client import HTTPException
from io import BytesIO
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from stock_daily_report.etf_tracker import (
    ETF_CODES,
    ConstituentCap,
    EtfQuote,
    EtfSnapshot,
)

MEMBERSHIP_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/cons/000300cons.xls"
)
QUOTES_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"


def _request(url: str) -> bytes:
    for attempt in range(2):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=15) as response:
                return response.read()
        except (OSError, HTTPException) as error:
            if attempt:
                raise OSError(f"ETF data request failed: {error}") from error
            sleep(1)
    raise AssertionError("unreachable")


def _membership() -> list[dict[str, object]]:
    try:
        import pandas as pd
    except ImportError as error:
        raise ValueError("ETF membership requires stock-daily-report[akshare]") from error
    frame = pd.read_excel(BytesIO(_request(MEMBERSHIP_URL)))
    if len(frame.columns) != 9:
        raise ValueError("unexpected CSI membership schema")

    def parse_date(value: object) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return date.fromisoformat(text[:10])

    return [
        {
            "date": parse_date(row[0]),
            "index_code": str(row[1]).zfill(6),
            "code": str(row[4]).zfill(6),
        }
        for row in frame.itertuples(index=False, name=None)
    ]


def _quotes(codes: Sequence[str]) -> list[Mapping[str, object]]:
    params = urlencode({
        "fltt": "2",
        "invt": "2",
        "fields": "f12,f14,f2,f21,f38,f124",
        "secids": ",".join(
            f"{'1' if code.startswith(('5', '6')) else '0'}.{code}" for code in codes
        ),
    })
    payload = json.loads(_request(f"{QUOTES_URL}?{params}"))
    try:
        rows = payload["data"]["diff"]
    except (KeyError, TypeError) as error:
        raise ValueError("Eastmoney quote data unavailable") from error
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("unexpected Eastmoney quote schema")
    return rows


class Csi300EtfProvider:
    name = "csindex-eastmoney-etf"

    def __init__(
        self,
        *,
        membership_fetcher: Callable[[], Sequence[Mapping[str, object]]] | None = None,
        quote_fetcher: Callable[[Sequence[str]], Sequence[Mapping[str, object]]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._membership = membership_fetcher or _membership
        self._quotes = quote_fetcher or _quotes
        self._now = now or (lambda: datetime.now(UTC))

    def fetch(self, report_date: date) -> EtfSnapshot:
        try:
            members = self._membership()
            codes = [str(row["code"]).zfill(6) for row in members]
            membership_dates = {row["date"] for row in members}
            if (
                len(codes) != 300 or len(set(codes)) != 300
                or len(membership_dates) != 1
                or any(
                    str(row["index_code"]).zfill(6) != "000300"
                    for row in members
                )
            ):
                raise ValueError("CSI 300 membership incomplete or inconsistent")
            membership_date = next(iter(membership_dates))
            if not isinstance(membership_date, date) or membership_date > report_date:
                raise ValueError("CSI 300 membership effective date is in the future")
            requested = [*ETF_CODES, *sorted(codes)]
            quotes = {}
            for start in range(0, len(requested), 100):
                batch = requested[start:start + 100]
                rows = self._quotes(batch)
                if (
                    len(rows) != len(batch)
                    or {row["f12"] for row in rows} != set(batch)
                ):
                    raise ValueError("missing, duplicate or unexpected quote codes")
                quotes.update({row["f12"]: row for row in rows})

            def stamp(row: Mapping[str, object]) -> datetime:
                return datetime.fromtimestamp(float(row["f124"]), UTC)

            return EtfSnapshot(
                report_date=report_date,
                membership_date=membership_date,
                generated_at=self._now(),
                etfs=tuple(EtfQuote(
                    code=code,
                    name=quotes[code]["f14"],
                    price=quotes[code]["f2"],
                    shares=quotes[code]["f38"],
                    source_timestamp=stamp(quotes[code]),
                ) for code in ETF_CODES),
                constituents=tuple(ConstituentCap(
                    code=code,
                    circulating_market_cap=quotes[code]["f21"],
                    source_timestamp=stamp(quotes[code]),
                ) for code in sorted(codes)),
            )
        except (KeyError, TypeError, OverflowError) as error:
            raise ValueError(f"invalid CSI300 ETF source data: {error}") from error
