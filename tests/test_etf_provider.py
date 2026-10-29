from datetime import UTC, date, datetime

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


@pytest.mark.parametrize("kind", ["missing", "duplicate", "stale", "wrong-index"])
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
    with pytest.raises(ValueError):
        Csi300EtfProvider(
            membership_fetcher=lambda: membership,
            quote_fetcher=fetch,
            now=lambda: NOW,
        ).fetch(DAY)


def test_quote_transport_requests_explicit_codes_fields_and_timeout(monkeypatch):
    import json
    from io import BytesIO
    from urllib.parse import parse_qs, urlparse

    import stock_daily_report.providers.etf as module

    captured = []

    def open_url(request, *, timeout):
        captured.append((parse_qs(urlparse(request.full_url).query), timeout))
        return BytesIO(json.dumps({"data": {"diff": quotes(["510300"])}}).encode())

    monkeypatch.setattr(module, "urlopen", open_url)
    assert module._quotes(["510300"])[0]["f38"] == 100_000_000
    assert captured[0][0]["secids"] == ["1.510300"]
    assert "f21" in captured[0][0]["fields"][0]
    assert "f124" in captured[0][0]["fields"][0]
    assert captured[0][1] == 15


def test_membership_reader_uses_official_dated_excel_layout(monkeypatch):
    import sys
    from types import SimpleNamespace

    import stock_daily_report.providers.etf as module

    frame = SimpleNamespace(
        columns=range(9),
        itertuples=lambda **kwargs: [
            (20260904, 300, "HS300", "CSI300", 1, "Bank", "Bank", "SZ", "SZ")
        ],
    )
    monkeypatch.setitem(
        sys.modules, "pandas", SimpleNamespace(read_excel=lambda data: frame)
    )
    monkeypatch.setattr(module, "_request", lambda url: b"excel-fixture")
    assert module._membership() == [
        {"date": DAY, "index_code": "000300", "code": "000001"}
    ]


def test_transport_retries_only_twice_and_reports_truncated_responses(monkeypatch):
    from http.client import IncompleteRead

    import stock_daily_report.providers.etf as module

    attempts = []

    def broken(*args, **kwargs):
        attempts.append(kwargs)
        raise IncompleteRead(b"truncated")

    monkeypatch.setattr(module, "urlopen", broken)
    monkeypatch.setattr(module, "sleep", lambda seconds: None)
    with pytest.raises(OSError):
        module._request("https://example.test/quotes")
    assert len(attempts) == 2
