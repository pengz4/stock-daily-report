import sys
from dataclasses import FrozenInstanceError
from datetime import date
from types import ModuleType

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


def _expected_codes(*codes):
    return [{"code": code} for code in codes]


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
        expected_codes_fetcher=lambda: _expected_codes(
            "600519", "000001", "300750", "688981", "920002"
        ),
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
        expected_codes_fetcher=lambda: _expected_codes("000001"),
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
        expected_codes_fetcher=lambda: _expected_codes("600519"),
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


def test_universe_request_availability_failure_uses_injected_fallback():
    calls = []

    def unavailable():
        calls.append("primary")
        raise ProviderAvailabilityError(
            "akshare",
            "network_error",
            "eastmoney disconnected",
        )

    def fallback():
        calls.append("fallback")
        return [_row("sh600519", "贵州茅台")]

    quotes = AkShareUniverseProvider(
        fetcher=unavailable,
        fallback_fetcher=fallback,
        expected_codes_fetcher=lambda: _expected_codes("600519"),
    ).get_quotes()

    assert [quote.code for quote in quotes] == ["600519"]
    assert calls == ["primary", "fallback"]


def test_universe_request_reports_both_unavailable_attempts():
    calls = []

    def unavailable_primary():
        calls.append("primary")
        raise OSError("eastmoney disconnected")

    def unavailable_fallback():
        calls.append("fallback")
        raise OSError("sina disconnected")

    provider = AkShareUniverseProvider(
        fetcher=unavailable_primary,
        fallback_fetcher=unavailable_fallback,
    )

    with pytest.raises(ProviderAvailabilityError) as raised:
        provider.get_quotes()

    assert raised.value.provider == "akshare"
    assert raised.value.code == "all_endpoints_unavailable"
    assert "stock_zh_a_spot_em" in raised.value.detail
    assert "eastmoney disconnected" in raised.value.detail
    assert "stock_zh_a_spot" in raised.value.detail
    assert "sina disconnected" in raised.value.detail
    assert calls == ["primary", "fallback"]


@pytest.mark.parametrize(
    "fallback_error",
    [
        pytest.param(
            pytest.importorskip("akshare.utils.demjson").JSONDecodeError(
                "rate-limit HTML"
            ),
            id="demjson-decode-error",
        ),
        pytest.param(IndexError("empty Sina count response"), id="index-error"),
    ],
)
def test_sina_invalid_remote_responses_report_both_unavailable_attempts(
    fallback_error,
):
    def unavailable_primary():
        raise OSError("eastmoney disconnected")

    def unavailable_fallback():
        raise fallback_error

    provider = AkShareUniverseProvider(
        fetcher=unavailable_primary,
        fallback_fetcher=unavailable_fallback,
    )

    with pytest.raises(ProviderAvailabilityError) as raised:
        provider.get_quotes()

    assert raised.value.code == "all_endpoints_unavailable"
    assert "stock_zh_a_spot_em" in raised.value.detail
    assert "eastmoney disconnected" in raised.value.detail
    assert "stock_zh_a_spot" in raised.value.detail
    assert "invalid_remote_response" in raised.value.detail
    assert str(fallback_error) in raised.value.detail


def test_fallback_data_error_preserves_primary_failure_and_both_sources():
    def unavailable_primary():
        raise OSError("eastmoney disconnected")

    provider = AkShareUniverseProvider(
        fetcher=unavailable_primary,
        fallback_fetcher=lambda: [{"代码": "sh600519"}],
    )

    with pytest.raises(ProviderDataError) as raised:
        provider.get_quotes()

    assert raised.value.code == "provider_schema_invalid"
    assert "stock_zh_a_spot_em" in raised.value.detail
    assert "eastmoney disconnected" in raised.value.detail
    assert "stock_zh_a_spot" in raised.value.detail
    assert "missing fields" in raised.value.detail


def test_universe_request_data_error_does_not_use_fallback():
    calls = []
    primary_error = ProviderDataError(
        "akshare",
        "provider_schema_invalid",
        "malformed primary response",
    )

    def malformed_primary():
        calls.append("primary")
        raise primary_error

    def fallback():
        calls.append("fallback")
        return [_row("600519", "贵州茅台")]

    provider = AkShareUniverseProvider(
        fetcher=malformed_primary,
        fallback_fetcher=fallback,
    )

    with pytest.raises(ProviderDataError) as raised:
        provider.get_quotes()

    assert raised.value is primary_error
    assert calls == ["primary"]


@pytest.mark.parametrize(
    "error",
    [
        KeyError("代码"),
        TypeError("unexpected schema"),
        ValueError("could not parse"),
        ZeroDivisionError("division by zero"),
    ],
)
def test_fetch_time_parsing_failures_raise_provider_data_error(error):
    def malformed_response():
        raise error

    provider = AkShareUniverseProvider(fetcher=malformed_response)

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\]",
    ) as raised:
        provider.get_quotes()

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"
    assert str(error) in str(raised.value)


@pytest.mark.parametrize("response", [[], FakeFrame([])])
def test_empty_universe_snapshots_raise_provider_data_error(response):
    provider = AkShareUniverseProvider(
        fetcher=lambda: response,
        expected_codes_fetcher=lambda: _expected_codes("600519"),
    )

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*empty",
    ) as raised:
        provider.get_quotes()

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"


def test_partial_snapshot_above_minimum_floor_is_rejected_by_expected_codes():
    snapshot_codes = [
        f"{prefix}{suffix:03d}"
        for prefix in ("600", "601", "603", "605")
        for suffix in range(1_000)
    ]
    missing_codes = [f"000{suffix:03d}" for suffix in range(1, 16)]
    provider = AkShareUniverseProvider(
        fetcher=lambda: [_row(code, code) for code in snapshot_codes],
        expected_codes_fetcher=lambda: _expected_codes(
            *snapshot_codes, *missing_codes
        ),
    )

    with pytest.raises(
        ProviderDataError,
        match=(
            r"akshare\[provider_snapshot_incomplete\].*"
            r"received 4000 supported codes; expected 4015; "
            r"missing 15.*000001.*000010.*\(\+5 more\)"
        ),
    ) as raised:
        provider.get_quotes()

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_snapshot_incomplete"
    assert "000011" not in raised.value.detail


def test_unexpected_supported_codes_are_accepted():
    provider = AkShareUniverseProvider(
        fetcher=lambda: [
            _row("600519", "贵州茅台"),
            _row("000001", "平安银行"),
        ],
        expected_codes_fetcher=lambda: _expected_codes("600519"),
    )

    quotes = provider.get_quotes()

    assert [quote.code for quote in quotes] == ["600519", "000001"]


def test_default_expected_codes_fetcher_uses_full_a_share_code_list(monkeypatch):
    calls = []
    akshare = ModuleType("akshare")

    def fetch_quotes():
        calls.append("quotes")
        return [_row("600519", "贵州茅台")]

    def fetch_expected_codes():
        calls.append("expected")
        return FakeFrame(_expected_codes("600519"))

    akshare.stock_zh_a_spot_em = fetch_quotes
    akshare.stock_info_a_code_name = fetch_expected_codes
    monkeypatch.setitem(sys.modules, "akshare", akshare)

    quotes = AkShareUniverseProvider().get_quotes()

    assert [quote.code for quote in quotes] == ["600519"]
    assert calls == ["quotes", "expected"]


def test_default_universe_fallback_resolves_sina_only_after_primary_failure(
    monkeypatch,
):
    calls = []
    akshare = ModuleType("akshare")

    def fetch_primary():
        calls.append("primary")
        raise OSError("eastmoney disconnected")

    def fetch_fallback():
        calls.append("fallback")
        return [_row("sh600519", "贵州茅台")]

    def fetch_expected_codes():
        calls.append("expected")
        return FakeFrame(_expected_codes("600519"))

    akshare.stock_zh_a_spot_em = fetch_primary
    akshare.stock_zh_a_spot = fetch_fallback
    akshare.stock_info_a_code_name = fetch_expected_codes
    monkeypatch.setitem(sys.modules, "akshare", akshare)

    quotes = AkShareUniverseProvider().get_quotes()

    assert [quote.code for quote in quotes] == ["600519"]
    assert calls == ["primary", "fallback", "expected"]


def test_explicit_metadata_fallback_uses_minimum_universe_size():
    provider = AkShareUniverseProvider(
        fetcher=lambda: [
            _row("600519", "贵州茅台"),
            _row("000001", "平安银行"),
        ],
        use_minimum_size_fallback=True,
        minimum_universe_size=3,
    )

    with pytest.raises(
        ProviderDataError,
        match=(
            r"akshare\[provider_snapshot_incomplete\].*"
            r"received 2 records; expected at least 3"
        ),
    ):
        provider.get_quotes()


def test_minimum_size_fallback_cannot_override_expected_code_fetcher():
    with pytest.raises(
        ValueError,
        match="expected_codes_fetcher.*use_minimum_size_fallback",
    ):
        AkShareUniverseProvider(
            expected_codes_fetcher=lambda: _expected_codes("600519"),
            use_minimum_size_fallback=True,
        )


def test_expected_code_provider_availability_failures_are_not_swallowed():
    def unavailable():
        raise OSError("metadata offline")

    provider = AkShareUniverseProvider(
        fetcher=lambda: [_row("600519", "贵州茅台")],
        expected_codes_fetcher=unavailable,
    )

    with pytest.raises(
        ProviderAvailabilityError,
        match=r"akshare\[network_error\].*expected-code.*metadata offline",
    ):
        provider.get_quotes()


def test_expected_code_provider_schema_failures_are_not_swallowed():
    provider = AkShareUniverseProvider(
        fetcher=lambda: [_row("600519", "贵州茅台")],
        expected_codes_fetcher=lambda: FakeFrame([{"name": "贵州茅台"}]),
    )

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*expected-code",
    ):
        provider.get_quotes()


@pytest.mark.parametrize("minimum_universe_size", [0, -1, True])
def test_minimum_universe_size_must_be_a_positive_integer(
    minimum_universe_size
):
    with pytest.raises(ValueError, match="minimum_universe_size"):
        AkShareUniverseProvider(
            fetcher=lambda: [_row("600519", "贵州茅台")],
            use_minimum_size_fallback=True,
            minimum_universe_size=minimum_universe_size,
        )


def test_duplicate_universe_codes_are_rejected():
    provider = AkShareUniverseProvider(
        fetcher=lambda: [
            _row("600519", "贵州茅台"),
            _row("600519", "贵州茅台"),
        ],
        clock=lambda: date(2026, 9, 11),
        expected_codes_fetcher=lambda: _expected_codes("600519"),
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
        expected_codes_fetcher=lambda: _expected_codes("600519"),
    )

    with pytest.raises(
        ProviderDataError,
        match=rf"akshare\[unsupported_symbol\].*{code}",
    ):
        provider.get_quotes()
