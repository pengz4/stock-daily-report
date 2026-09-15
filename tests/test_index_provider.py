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
