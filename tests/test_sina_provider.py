from datetime import UTC, date, datetime

import pytest

from stock_daily_report.providers.base import ProviderAvailabilityError
from stock_daily_report.providers.sina import SinaMarketDataProvider


def test_sina_provider_maps_symbol_dates_and_daily_fields():
    request = {}

    def fetcher(**kwargs):
        request.update(kwargs)
        return [
            {
                "date": datetime(2026, 9, 10, tzinfo=UTC),
                "open": 1500.0,
                "high": 1510.0,
                "low": 1490.0,
                "close": 1505.0,
                "volume": 1000.0,
                "amount": 1_505_000.0,
                "turnover": 0.1,
            }
        ]

    provider = SinaMarketDataProvider(
        fetcher=fetcher,
        now=lambda: datetime(2026, 9, 10, 16, tzinfo=UTC),
    )

    bars = provider.get_daily_bars("600519", end=date(2026, 9, 10))

    assert request == {
        "symbol": "sh600519",
        "start_date": "20240910",
        "end_date": "20260910",
        "adjust": "qfq",
    }
    assert bars[0]["trade_date"] == date(2026, 9, 10)
    assert bars[0]["provider_name"] == "sina"
    assert bars[0]["close"] == 1505.0


def test_sina_provider_maps_request_failures_to_availability_errors():
    provider = SinaMarketDataProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
    )

    with pytest.raises(ProviderAvailabilityError, match=r"sina\[network_error\]"):
        provider.get_daily_bars("600519", end=date(2026, 9, 10))
