from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from stock_daily_report.backtest.replay import replay
from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer
from stock_daily_report.models import DailyBar


def _bars() -> list[DailyBar]:
    ranges = [
        [10, 8],
        [15, 12],
        [13, 10],
        [12, 9],
        [8, 5],
        [10, 7],
    ]
    return [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=(high + low) / 2,
            high=high,
            low=low,
            close=(high + low) / 2,
            volume=100,
            amount=1000,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, (high, low) in enumerate(ranges)
    ]


def test_replay_only_exposes_bars_available_on_current_date():
    events = replay(_bars(), analyzer=SimplifiedChanAnalyzer())

    assert events
    assert all(event.max_input_date <= event.observed_at for event in events)
    assert all(event.observed_at in {bar.trade_date for bar in _bars()} for event in events)


def test_replay_records_revision_when_visible_event_changes():
    events = replay(_bars(), analyzer=SimplifiedChanAnalyzer())

    revisions = [event for event in events if event.revision > 0]

    assert revisions
    assert all(event.first_observed_at <= event.observed_at for event in revisions)


def test_replay_coalesces_candidate_and_confirmed_states_per_prefix():
    events = replay(_bars(), analyzer=SimplifiedChanAnalyzer())
    by_observation: dict[date, list[str]] = defaultdict(list)
    for event in events:
        by_observation[event.observed_at].append(event.event_id)

    assert all(len(ids) == len(set(ids)) for ids in by_observation.values())


def test_replay_preserves_descriptive_observations_for_execution_adapter():
    events = replay(_bars(), analyzer=SimplifiedChanAnalyzer())

    assert all(
        event.kind != "signal"
        or event.reason_code != "simplified_breakout_up"
        for event in events
    )
