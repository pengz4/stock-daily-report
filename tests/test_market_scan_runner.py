import threading
import time
from datetime import UTC, date, datetime, timedelta

from stock_daily_report.models import DailyBar, MarketScanSettings
from stock_daily_report.providers.base import ProviderAvailabilityError
from stock_daily_report.providers.universe import UniverseQuote

REPORT_DATE = date(2026, 9, 11)
GENERATED_AT = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)


def _settings(**changes: object) -> MarketScanSettings:
    values = {
        "rule_version": "market-scan-v1",
        "trend_limit": 30,
        "balanced_limit": 30,
        "minimum_history_bars": 120,
        "minimum_latest_amount": 50_000_000,
        "minimum_coverage_ratio": 0.8,
        "max_workers": 3,
        "max_candidates": 1_200,
    }
    values.update(changes)
    return MarketScanSettings(**values)


def _codes(count: int) -> list[str]:
    return [f"600{index:03d}" for index in range(count)]


def _quote(code: str) -> UniverseQuote:
    return UniverseQuote(
        code=code,
        name=f"Company {code}",
        market="SH",
        latest_price=100.0,
        volume=1_000_000.0,
        amount=100_000_000.0,
        quote_date=REPORT_DATE,
    )


def _bars(code: str) -> list[DailyBar]:
    start = REPORT_DATE - timedelta(days=129)
    offset = int(code[-2:]) / 100_000
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=100.0 * (1.002 + offset) ** index,
            high=100.0 * (1.002 + offset) ** index + 1.0,
            low=100.0 * (1.002 + offset) ** index - 1.0,
            close=100.0 * (1.002 + offset) ** index,
            volume=1_000_000.0 + index,
            amount=(1_000_000.0 + index) * (100.0 * (1.002 + offset) ** index),
            turnover_rate=0.5,
            adjustment_mode="qfq",
            provider_name="fake-history",
            source_timestamp=datetime(2026, 9, 11, tzinfo=UTC),
        )
        for index in range(130)
    ]


class FakeUniverseProvider:
    name = "fake-universe"

    def __init__(self, quotes: list[UniverseQuote]) -> None:
        self._quotes = quotes

    def get_quotes(self) -> list[UniverseQuote]:
        return list(self._quotes)


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(
        self,
        bars_by_code: dict[str, list[DailyBar]],
        *,
        delays: dict[str, float] | None = None,
        failures: set[str] | None = None,
    ) -> None:
        self._bars_by_code = bars_by_code
        self._delays = delays or {}
        self._failures = failures or set()
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.requested_ends: dict[str, date | None] = {}

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        del start
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.requested_ends[code] = end
        try:
            time.sleep(self._delays.get(code, 0.0))
            if code in self._failures:
                raise ProviderAvailabilityError(
                    self.name, "network_error", f"failed {code}"
                )
            return list(self._bars_by_code[code])
        finally:
            with self._lock:
                self.active -= 1


def _scan(
    codes: list[str],
    provider: FakeHistoryProvider,
    **setting_changes: object,
):
    from stock_daily_report.market_scan.runner import scan_market

    return scan_market(
        _settings(**setting_changes),
        FakeUniverseProvider([_quote(code) for code in codes]),
        provider,
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )


def test_scanner_bounds_history_workers_and_passes_report_date():
    codes = _codes(8)
    provider = FakeHistoryProvider(
        {code: _bars(code) for code in codes},
        delays={code: 0.02 for code in codes},
    )

    artifact = _scan(codes, provider, max_workers=2)

    assert provider.maximum_active == 2
    assert provider.requested_ends == dict.fromkeys(codes, REPORT_DATE)
    assert artifact.valid_count == len(codes)


def test_scanner_output_is_deterministic_despite_completion_order():
    codes = _codes(6)
    bars_by_code = {code: _bars(code) for code in codes}
    forward = FakeHistoryProvider(
        bars_by_code,
        delays={code: index * 0.005 for index, code in enumerate(codes)},
    )
    reverse = FakeHistoryProvider(
        bars_by_code,
        delays={code: index * 0.005 for index, code in enumerate(reversed(codes))},
    )

    first = _scan(codes, forward)
    second = _scan(list(reversed(codes)), reverse)

    assert first == second
    assert [row.code for row in first.rankings.trend] == [
        row.code for row in second.rankings.trend
    ]


def test_individual_history_failures_are_counted_without_aborting_scan():
    codes = _codes(4)
    failed_code = codes[1]
    provider = FakeHistoryProvider(
        {code: _bars(code) for code in codes},
        failures={failed_code},
    )

    artifact = _scan(codes, provider, minimum_coverage_ratio=0.5)

    assert artifact.eligible_count == 4
    assert artifact.valid_count == 3
    assert artifact.coverage == 0.75
    assert artifact.failure_counts == {"network_error": 1}
    failed = next(status for status in artifact.statuses if status.code == failed_code)
    assert failed.status == "history_failed"
    assert failed.reason_codes == ("network_error",)
    assert {row.code for row in artifact.rankings.trend} == set(codes) - {failed_code}


def test_coverage_below_minimum_suppresses_both_rankings():
    codes = _codes(5)
    provider = FakeHistoryProvider(
        {code: _bars(code) for code in codes},
        failures=set(codes[1:]),
    )

    artifact = _scan(codes, provider, minimum_coverage_ratio=0.8)

    assert artifact.coverage == 0.2
    assert artifact.rankings.trend == ()
    assert artifact.rankings.balanced == ()
    assert artifact.consensus == ()


def test_rankings_are_limited_unique_and_consensus_is_exact_intersection():
    codes = _codes(35)
    provider = FakeHistoryProvider({code: _bars(code) for code in codes})

    artifact = _scan(codes, provider)

    trend_codes = [row.code for row in artifact.rankings.trend]
    balanced_codes = [row.code for row in artifact.rankings.balanced]
    consensus_codes = {row.code for row in artifact.consensus}
    assert len(trend_codes) <= 30
    assert len(balanced_codes) <= 30
    assert len(trend_codes) == len(set(trend_codes))
    assert len(balanced_codes) == len(set(balanced_codes))
    assert consensus_codes == set(trend_codes) & set(balanced_codes)
