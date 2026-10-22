from datetime import UTC, date, datetime
from http.client import HTTPException

import pytest

from stock_daily_report.models import DailyBar
from stock_daily_report.providers.base import (
    ProviderAvailabilityError,
    ProviderDataError,
)
from stock_daily_report.providers.index import AkShareIndexProvider
from stock_daily_report.providers.service import (
    AllProvidersFailedError,
    IndexHistoryService,
)


class FakeFrame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        assert orient == "records"
        return self.records


def _row(day="2026-09-11", close=3200.0):
    return {
        "日期": day,
        "开盘": close - 10,
        "最高": close + 10,
        "最低": close - 20,
        "收盘": close,
        "成交量": 1_000_000,
        "成交额": 3_200_000_000,
    }


def test_akshare_index_provider_maps_index_history_fields():
    bars = AkShareIndexProvider(
        fetcher=lambda **_: FakeFrame([_row()]),
        now=lambda: datetime(2026, 9, 11, 8, tzinfo=UTC),
    ).get_daily_bars("000001", end=date(2026, 9, 11))

    assert bars == [
        DailyBar(
            trade_date=date(2026, 9, 11),
            open=3190.0,
            high=3210.0,
            low=3180.0,
            close=3200.0,
            volume=1_000_000.0,
            amount=3_200_000_000.0,
            turnover_rate=0.0,
            adjustment_mode="none",
            provider_name="akshare",
            source_timestamp=datetime(2026, 9, 11, 8, tzinfo=UTC),
        )
    ]


def test_akshare_index_provider_truncates_rows_after_end_date():
    request = {}

    def fetcher(**kwargs):
        request.update(kwargs)
        return [_row("2026-09-10"), _row("2026-09-11"), _row("2026-09-12")]

    bars = AkShareIndexProvider(fetcher=fetcher).get_daily_bars(
        "399001", end=date(2026, 9, 11)
    )

    assert request["symbol"] == "399001"
    assert request["end_date"] == "20260911"
    assert [bar.trade_date for bar in bars] == [
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]


def test_akshare_index_provider_filters_rows_before_start_date():
    bars = AkShareIndexProvider(
        fetcher=lambda **_: [
            _row("2026-09-09"),
            _row("2026-09-10"),
            _row("2026-09-11"),
            _row("2026-09-12"),
        ]
    ).get_daily_bars(
        "399001",
        start=date(2026, 9, 10),
        end=date(2026, 9, 11),
    )

    assert [bar.trade_date for bar in bars] == [
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 10, tzinfo=UTC), None),
        ("2026-09-10", None),
        (None, datetime(2026, 9, 11, tzinfo=UTC)),
        (None, "2026-09-11"),
        (date(2026, 9, 12), date(2026, 9, 11)),
    ],
)
def test_akshare_index_provider_rejects_invalid_date_arguments(start, end):
    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[invalid_date_range\]",
    ):
        AkShareIndexProvider(fetcher=lambda **_: []).get_daily_bars(
            "000001",
            start=start,
            end=end,
        )


@pytest.mark.parametrize(
    ("record", "detail"),
    [
        ({"日期": "2026-09-11"}, "missing"),
        ({**_row(), "收盘": "not-a-number"}, "invalid"),
    ],
)
def test_akshare_index_provider_rejects_missing_or_invalid_rows(record, detail):
    provider = AkShareIndexProvider(fetcher=lambda **_: [record])

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*000001",
    ) as raised:
        provider.get_daily_bars("000001")

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"
    assert detail in str(raised.value).lower()


def test_akshare_index_provider_surfaces_availability_error():
    provider = AkShareIndexProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
    )

    with pytest.raises(
        ProviderAvailabilityError,
        match=r"akshare\[network_error\]: offline",
    ):
        provider.get_daily_bars("000001")


@pytest.mark.parametrize("failure", [ValueError("bad response"), TypeError("bad type")])
def test_akshare_index_provider_contextualizes_fetcher_schema_errors(failure):
    provider = AkShareIndexProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(failure)
    )

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*bad",
    ) as raised:
        provider.get_daily_bars("000001")

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"


@pytest.mark.parametrize(
    "failure",
    [KeyError("records"), AttributeError("malformed response")],
)
def test_akshare_index_provider_contextualizes_to_dict_schema_errors(failure):
    class MalformedResponse:
        def to_dict(self, orient):
            assert orient == "records"
            raise failure

    provider = AkShareIndexProvider(fetcher=lambda **_: MalformedResponse())

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*000001",
    ) as raised:
        provider.get_daily_bars("000001")

    assert raised.value.provider == "akshare"
    assert raised.value.code == "provider_schema_invalid"


def test_akshare_index_provider_contextualizes_transport_errors_for_fallback():
    primary = AkShareIndexProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(HTTPException("offline"))
    )
    fallback = AkShareIndexProvider(fetcher=lambda **_: [_row()])

    fetched = IndexHistoryService(
        {"primary": primary, "fallback": fallback},
        primary_provider="primary",
        fallback_provider="fallback",
    ).fetch("000001")

    assert fetched.provider_name == "fallback"


def test_index_history_service_does_not_fallback_for_schema_fetch_errors():
    primary = AkShareIndexProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(ValueError("bad response"))
    )
    fallback = AkShareIndexProvider(fetcher=lambda **_: [_row()])

    with pytest.raises(
        ProviderDataError,
        match=r"akshare\[provider_schema_invalid\].*bad response",
    ):
        IndexHistoryService(
            {"primary": primary, "fallback": fallback},
            primary_provider="primary",
            fallback_provider="fallback",
        ).fetch("000001")


def test_index_history_service_falls_back_only_for_availability_errors():
    primary = AkShareIndexProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
    )
    fallback = AkShareIndexProvider(fetcher=lambda **_: [_row()])

    fetched = IndexHistoryService(
        {"primary": primary, "fallback": fallback},
        primary_provider="primary",
        fallback_provider="fallback",
    ).fetch("000001")

    assert fetched.provider_name == "fallback"
    assert fetched.bars[0].trade_date == date(2026, 9, 11)


def test_index_history_service_preserves_all_provider_failures():
    service = IndexHistoryService(
        {
            "primary": AkShareIndexProvider(
                fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
            ),
            "fallback": AkShareIndexProvider(
                fetcher=lambda **_: (_ for _ in ()).throw(OSError("still offline"))
            ),
        },
        primary_provider="primary",
        fallback_provider="fallback",
    )

    with pytest.raises(AllProvidersFailedError, match="offline"):
        service.fetch("000001")


def test_sina_index_prices_do_not_fabricate_amount_and_use_index_symbols():
    from stock_daily_report.providers.index import SinaIndexProvider

    calls = []

    def fetcher(symbol):
        calls.append(symbol)
        return [
            {"day": "2026-09-11", "close": "3200.0"},
            {"day": "2026-09-10", "close": "3190.0"},
        ]

    provider = SinaIndexProvider(fetcher=fetcher)
    for code, symbol in [
        ("000001", "sh000001"), ("399001", "sz399001"),
        ("399006", "sz399006"), ("000300", "sh000300"),
        ("000852", "sh000852"),
    ]:
        bars = provider.get_daily_bars(code, end=date(2026, 9, 11))
        assert calls[-1] == symbol
        assert [b.trade_date for b in bars] == [date(2026, 9, 10), date(2026, 9, 11)]
        assert bars[-1].close == 3200
        assert bars[-1].provider_name == "sina-index"
        assert "amount" not in bars[-1].model_dump()


@pytest.mark.parametrize("records", [
    [], [{"day": "2026-09-11", "close": "nan"}],
    [{"day": "2026-09-11", "close": True}],
    [{"day": "2026-09-11", "close": -1}],
    [{"day": "2026-09-11", "close": 3200}] * 2,
    [{"day": "2026-09-10", "close": 3200}],
])
def test_sina_index_rejects_invalid_duplicate_or_stale_history(records):
    from stock_daily_report.providers.index import SinaIndexProvider

    with pytest.raises(ProviderDataError):
        SinaIndexProvider(fetcher=lambda _: records).get_daily_bars(
            "000001", end=date(2026, 9, 11)
        )


def test_sina_index_retries_transport_and_reuses_valid_same_date_cache(tmp_path):
    from stock_daily_report.providers.index import SinaIndexProvider

    calls = []
    def fetcher(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise OSError("offline")
        return [{"day": "2026-09-11", "close": 3200}]

    provider = SinaIndexProvider(
        fetcher=fetcher, cache_directory=tmp_path,
        now=lambda: datetime(2026, 9, 11, 9, tzinfo=UTC),
        sleep=lambda _: None,
    )
    first = provider.get_daily_bars("000001", end=date(2026, 9, 11))
    assert provider.get_daily_bars("000001", end=date(2026, 9, 11)) == first
    assert len(calls) == 2


def test_sina_index_refuses_stock_codes_before_request():
    from stock_daily_report.providers.index import SinaIndexProvider

    with pytest.raises(ProviderDataError, match="invalid_index_code"):
        SinaIndexProvider(fetcher=lambda _: pytest.fail("must not fetch")).get_daily_bars(
            "600000"
        )


def test_price_only_index_history_builds_available_market_state():
    from datetime import timedelta

    from stock_daily_report.market_scan.state import calculate_index_state
    from stock_daily_report.providers.index import SinaIndexProvider

    bars = SinaIndexProvider(fetcher=lambda _: [
        {"day": (date(2026, 9, 11) - timedelta(days=offset)).isoformat(),
         "close": 3200 - offset}
        for offset in range(60)
    ]).get_daily_bars("000001", end=date(2026, 9, 11))
    state = calculate_index_state(
        name="Shanghai", code="000001", bars=bars, report_date=date(2026, 9, 11),
    )
    assert state.status == "available"
    assert state.provider == "sina-index"
    assert state.close_vs_ma60 == "above"


def test_sina_index_cache_is_not_read_or_written_intraday(tmp_path):
    from stock_daily_report.providers.index import SinaIndexProvider

    calls = []
    provider = SinaIndexProvider(
        fetcher=lambda symbol: calls.append(symbol) or [
            {"day": "2026-09-11", "close": 3200}
        ],
        cache_directory=tmp_path,
        now=lambda: datetime(2026, 9, 11, 6, tzinfo=UTC),
    )
    provider.get_daily_bars("000001", end=date(2026, 9, 11))
    provider.get_daily_bars("000001", end=date(2026, 9, 11))
    assert len(calls) == 2
    assert not list(tmp_path.glob("*.json"))
