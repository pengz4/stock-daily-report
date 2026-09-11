from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from stock_daily_report.providers.base import (
    ProviderAvailabilityError,
    ProviderDataError,
)
from stock_daily_report.providers.universe import (
    AkShareUniverseProvider,
    UniverseQuote,
)


class FakeFrame:
    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        assert orient == "records"
        return self._records


def _row(code, name, price=10.5, volume=1_000, amount=10_500):
    return {
        "代码": code,
        "名称": name,
        "最新价": price,
        "成交量": volume,
        "成交额": amount,
    }


def test_akshare_universe_provider_normalizes_supported_a_share_classes():
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return FakeFrame(
            [
                _row("600519", "贵州茅台", 1500.0, 2_000, 3_000_000.0),
                _row("000001", "平安银行"),
                _row("300750", "宁德时代"),
                _row("688981", "中芯国际"),
                _row("920002", "万达轴承"),
            ]
        )

    quotes = AkShareUniverseProvider(
        fetcher=fetcher,
        clock=lambda: date(2026, 9, 11),
    ).get_quotes()

    assert calls == 1
    assert [(quote.code, quote.market) for quote in quotes] == [
        ("600519", "SH"),
        ("000001", "SZ"),
        ("300750", "SZ"),
        ("688981", "SH"),
        ("920002", "BJ"),
    ]
    assert quotes[0] == UniverseQuote(
        code="600519",
        name="贵州茅台",
        market="SH",
        latest_price=1500.0,
        volume=2_000.0,
        amount=3_000_000.0,
        quote_date=date(2026, 9, 11),
    )
    assert quotes[0].exchange == "SH"


def test_universe_quotes_are_immutable():
    quote = AkShareUniverseProvider(
        fetcher=lambda: [_row("000001", "平安银行")],
        clock=lambda: date(2026, 9, 11),
    ).get_quotes()[0]

    with pytest.raises(FrozenInstanceError):
        quote.name = "changed"


@pytest.mark.parametrize(
    ("row", "context"),
    [
        ({"代码": "600519"}, "code=600519"),
        (_row("600519", " "), "code=600519"),
        (_row("600519", "贵州茅台", price="not-a-price"), "code=600519"),
    ],
)
def test_malformed_universe_rows_raise_data_error_with_context(row, context):
    provider = AkShareUniverseProvider(
        fetcher=lambda: [row],
        clock=lambda: date(2026, 9, 11),
    )

    with pytest.raises(
        ProviderDataError,
        match=rf"akshare\[provider_schema_invalid\].*{context}",
    ) as raised:
        provider.get_quotes()

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"


def test_universe_request_failures_raise_availability_error():
    def unavailable():
        raise OSError("offline")

    provider = AkShareUniverseProvider(fetcher=unavailable)

    with pytest.raises(
        ProviderAvailabilityError,
        match=r"akshare\[network_error\]: offline",
    ):
        provider.get_quotes()


def test_duplicate_universe_codes_are_rejected():
    provider = AkShareUniverseProvider(
        fetcher=lambda: [
            _row("600519", "贵州茅台"),
            _row("600519", "贵州茅台"),
        ],
        clock=lambda: date(2026, 9, 11),
    )

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[duplicate_symbol\].*600519",
    ):
        provider.get_quotes()


@pytest.mark.parametrize("code", ["510300", "430047", "AAPL", "600519.SH"])
def test_unsupported_instruments_and_codes_are_rejected(code):
    provider = AkShareUniverseProvider(
        fetcher=lambda: [_row(code, "unsupported")],
        clock=lambda: date(2026, 9, 11),
    )

    with pytest.raises(
        ProviderDataError,
        match=rf"akshare\[unsupported_symbol\].*{code}",
    ):
        provider.get_quotes()
