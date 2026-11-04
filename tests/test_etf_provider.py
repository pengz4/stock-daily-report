import logging
from datetime import UTC, date, datetime
from http.client import IncompleteRead
from io import BytesIO
from urllib.error import HTTPError

import pytest

DAY = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 8, tzinfo=UTC)


def members():
    return [
        {"date": DAY, "index_code": "000300", "code": str(600000 + i)}
        for i in range(300)
    ]


def quotes(codes):
    return [
        {
            "f12": code,
            "f14": f"Name {code}",
            "f2": 4,
            "f38": 100_000_000,
            "f21": 1_000_000_000,
            "f124": int(datetime(2026, 9, 4, 7, tzinfo=UTC).timestamp()),
        }
        for code in codes
    ]


def test_provider_joins_exact_members_and_preserves_quote_units():
    from stock_daily_report.providers.etf import Csi300EtfProvider

    requested = []

    def fetch(codes):
        requested.extend(codes)
        return quotes(codes)

    snapshot = Csi300EtfProvider(
        membership_fetcher=members, quote_fetcher=fetch, now=lambda: NOW
    ).fetch(DAY)
    assert len(snapshot.constituents) == 300
    assert len(requested) == 304
    assert snapshot.etfs[0].shares == 100_000_000
    assert snapshot.constituents[0].circulating_market_cap == 1_000_000_000
    assert snapshot.etfs[0].price == 4
    assert snapshot.membership_date == DAY


def test_provider_accepts_the_latest_effective_membership_before_report_date():
    from stock_daily_report.providers.etf import Csi300EtfProvider

    effective_date = date(2026, 9, 1)
    historical_members = [
        {**row, "date": effective_date} for row in members()
    ]
    snapshot = Csi300EtfProvider(
        membership_fetcher=lambda: historical_members,
        quote_fetcher=lambda codes: quotes(codes),
        now=lambda: NOW,
    ).fetch(DAY)

    assert snapshot.report_date == DAY
    assert snapshot.membership_date == effective_date


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "stale", "wrong-index", "mixed-date"]
)
def test_provider_rejects_incomplete_or_misdated_sources(kind):
    from stock_daily_report.providers.etf import Csi300EtfProvider

    def fetch(codes):
        rows = quotes(codes)
        if kind == "missing":
            rows.pop()
        if kind == "duplicate":
            rows.append(rows[0])
        if kind == "stale":
            rows[0]["f124"] -= 86400
        return rows

    membership = members()
    if kind == "wrong-index":
        membership[0]["index_code"] = "000905"
    if kind == "mixed-date":
        membership[0]["date"] = date(2026, 9, 3)
    with pytest.raises(ValueError):
        Csi300EtfProvider(
            membership_fetcher=lambda: membership,
            quote_fetcher=fetch,
            now=lambda: NOW,
        ).fetch(DAY)


def test_provider_rejects_membership_effective_after_report_date():
    from stock_daily_report.providers.etf import Csi300EtfProvider

    future_members = [
        {**row, "date": date(2026, 10, 30)} for row in members()
    ]
    with pytest.raises(ValueError, match="future"):
        Csi300EtfProvider(
            membership_fetcher=lambda: future_members,
            quote_fetcher=lambda codes: quotes(codes),
            now=lambda: NOW,
        ).fetch(DAY)


def test_quote_transport_requests_explicit_codes_fields_and_timeout(monkeypatch):
    import json
    from urllib.parse import parse_qs, urlparse

    import stock_daily_report.providers.etf as module

    captured = []

    def open_url(request, *, timeout):
        captured.append((parse_qs(urlparse(request.full_url).query), timeout))
        return BytesIO(json.dumps({"data": {"diff": quotes(["510300"])}}).encode())

    monkeypatch.setattr(module, "urlopen", open_url)
    assert module._quotes(
        ["510300"], source="Eastmoney closing quotes batch 1/4"
    )[0]["f38"] == 100_000_000
    assert captured[0][0]["secids"] == ["1.510300"]
    assert "f21" in captured[0][0]["fields"][0]
    assert "f124" in captured[0][0]["fields"][0]
    assert captured[0][1] == 15


@pytest.mark.parametrize(
    "source_date",
    [date(2026, 9, 1), datetime(2026, 9, 1, tzinfo=UTC), 20260901, "2026-09-01"],
)
def test_membership_reader_uses_official_dated_excel_layout(monkeypatch, source_date):
    import sys
    from types import SimpleNamespace

    import stock_daily_report.providers.etf as module

    frame = SimpleNamespace(
        columns=range(9),
        itertuples=lambda **kwargs: [
            (source_date, 300, "HS300", "CSI300", 1, "Bank", "Bank", "SZ", "SZ")
        ],
    )
    monkeypatch.setitem(
        sys.modules, "pandas", SimpleNamespace(read_excel=lambda data: frame)
    )
    requested = []

    def request(url, *, source):
        requested.append((url, source))
        return b"excel-fixture"

    monkeypatch.setattr(module, "_request", request)
    assert module._membership() == [
        {"date": date(2026, 9, 1), "index_code": "000300", "code": "000001"}
    ]
    assert requested[0][1] == "CSI 300 membership workbook"


def test_quote_transport_includes_source_and_batch_in_request_label(monkeypatch):
    import json

    import stock_daily_report.providers.etf as module

    requests = []

    def request(url, *, source):
        requests.append((url, source))
        return json.dumps({"data": {"diff": quotes(["510300"])}}).encode()

    monkeypatch.setattr(module, "_request", request)
    assert module._quotes(["510300"], source="Eastmoney closing quotes batch 1/4")
    assert requests[0][1] == "Eastmoney closing quotes batch 1/4"


def test_transient_gateway_errors_retry_with_bounded_backoff_and_source_diagnostics(
    monkeypatch, caplog
):
    import stock_daily_report.providers.etf as module

    attempts = []
    delays = []

    def flaky(request, *, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise HTTPError(request.full_url, 502, "Bad Gateway", {}, None)
        return BytesIO(b"ok")

    monkeypatch.setattr(module, "urlopen", flaky)
    monkeypatch.setattr(module, "sleep", delays.append)
    caplog.set_level(logging.WARNING, logger=module.__name__)

    assert module._request(
        "https://example.test/endpoint?secret=private",
        source="Eastmoney closing quotes batch 2/4",
    ) == b"ok"
    assert len(attempts) == 3
    assert delays == [1, 2]
    assert "Eastmoney closing quotes batch 2/4" in caplog.text
    assert "example.test" in caplog.text
    assert "502" in caplog.text
    assert "secret=private" not in caplog.text


def test_permanent_client_error_is_not_retried_and_identifies_provider(
    monkeypatch,
):
    import stock_daily_report.providers.etf as module

    attempts = []
    delays = []

    def missing(request, *, timeout):
        attempts.append(request.full_url)
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module, "urlopen", missing)
    monkeypatch.setattr(module, "sleep", delays.append)
    with pytest.raises(OSError, match="CSI 300 membership workbook") as raised:
        module._request(
            "https://oss-ch.csindex.com.cn/path?token=private",
            source="CSI 300 membership workbook",
        )
    assert "404" in str(raised.value)
    assert "oss-ch.csindex.com.cn" in str(raised.value)
    assert "token=private" not in str(raised.value)
    assert len(attempts) == 1
    assert delays == []


def test_persistent_gateway_error_reports_final_attempts_without_remote_reason(
    monkeypatch, caplog
):
    import stock_daily_report.providers.etf as module

    attempts = []

    def unavailable(request, *, timeout):
        attempts.append(request.full_url)
        raise HTTPError(
            request.full_url,
            502,
            "Bad Gateway: https://push2.eastmoney.com/?codes=private",
            {},
            None,
        )

    monkeypatch.setattr(module, "urlopen", unavailable)
    monkeypatch.setattr(module, "sleep", lambda _seconds: None)
    caplog.set_level(logging.WARNING, logger=module.__name__)
    with pytest.raises(OSError, match="Eastmoney closing quotes batch 4/4") as raised:
        module._request(
            "https://push2.eastmoney.com/api/qt/ulist.np/get?codes=private",
            source="Eastmoney closing quotes batch 4/4",
        )
    assert "HTTP 502" in str(raised.value)
    assert "push2.eastmoney.com" in str(raised.value)
    assert "private" not in str(raised.value)
    assert "private" not in caplog.text
    assert len(attempts) == 3


def test_transient_transport_errors_retry_three_total_attempts(monkeypatch):
    import stock_daily_report.providers.etf as module

    attempts = []
    delays = []

    def broken(request, *, timeout):
        attempts.append(request.full_url)
        raise IncompleteRead(b"truncated")

    monkeypatch.setattr(module, "urlopen", broken)
    monkeypatch.setattr(module, "sleep", delays.append)
    with pytest.raises(OSError, match="after 3 attempts"):
        module._request("https://example.test/quotes", source="Eastmoney quotes")
    assert len(attempts) == 3
    assert delays == [1, 2]
