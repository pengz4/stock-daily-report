import json
from datetime import UTC, date, datetime, timedelta

import pytest

DAY = date(2026, 9, 4)
CODES = ("510300", "510310", "510330", "159919")


def snapshot_payload(day=DAY, *, shares=100_000_000.0):
    timestamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=8)
    return {
        "report_date": day,
        "membership_date": day,
        "generated_at": timestamp,
        "etfs": [
            {
                "code": code,
                "name": f"ETF {code}",
                "price": 4.0,
                "shares": shares,
                "source_timestamp": timestamp - timedelta(hours=1),
            }
            for code in CODES
        ],
        "constituents": [
            {
                "code": f"{600000 + index}",
                "circulating_market_cap": 1_000_000_000.0,
                "source_timestamp": timestamp - timedelta(hours=1),
            }
            for index in range(300)
        ],
    }


def make_snapshot(day=DAY, **kwargs):
    from stock_daily_report.etf_tracker import EtfSnapshot

    return EtfSnapshot.model_validate(snapshot_payload(day, **kwargs))


def test_tracker_calculates_ratio_and_changes_against_prior_observation(tmp_path):
    from stock_daily_report.etf_tracker import build_tracker_report, save_snapshot

    previous_day = DAY - timedelta(days=3)
    save_snapshot(tmp_path, make_snapshot(previous_day, shares=90_000_000))
    save_snapshot(tmp_path, make_snapshot())
    report = build_tracker_report(tmp_path, DAY)

    assert report.status == "available"
    assert report.current.etf_market_cap == 1_600_000_000
    assert report.current.circulating_market_cap == 300_000_000_000
    assert report.current.ratio == pytest.approx(1.6 / 300)
    assert report.comparison_date == previous_day
    assert report.ratio_change_pp == pytest.approx((1.6 - 1.44) / 300 * 100)
    assert {fund.shares_change for fund in report.funds} == {10_000_000}
    assert all(
        fund.shares_change_pct == pytest.approx(100 / 9) for fund in report.funds
    )
    assert len(report.history) == 2
    assert not report.membership_changed


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["etfs"].pop(),
        lambda p: p["etfs"].__setitem__(0, p["etfs"][1]),
        lambda p: p["constituents"].pop(),
        lambda p: p["constituents"].__setitem__(0, p["constituents"][1]),
        lambda p: p["etfs"][0].update(shares=float("nan")),
        lambda p: p["etfs"][0].update(price=0),
        lambda p: p["etfs"][0].update(shares=True),
        lambda p: p["constituents"][0].update(circulating_market_cap=-1),
        lambda p: p["constituents"][0].update(
            source_timestamp=datetime(2026, 9, 3, 7, tzinfo=UTC)
        ),
        lambda p: p["etfs"][0].update(
            source_timestamp=datetime(2026, 9, 4, 6, tzinfo=UTC)
        ),
        lambda p: p["etfs"][0].update(
            source_timestamp=datetime(2026, 9, 4, 9, tzinfo=UTC)
        ),
    ],
)
def test_snapshot_rejects_partial_stale_intraday_or_invalid_inputs(change):
    from stock_daily_report.etf_tracker import EtfSnapshot

    payload = snapshot_payload()
    change(payload)
    with pytest.raises(ValueError):
        EtfSnapshot.model_validate(payload)


def test_snapshot_accepts_effective_membership_older_than_closing_observation():
    from stock_daily_report.etf_tracker import EtfSnapshot

    payload = snapshot_payload()
    payload["membership_date"] = date(2026, 9, 1)
    snapshot = EtfSnapshot.model_validate(payload)
    assert snapshot.membership_date == date(2026, 9, 1)


def test_snapshot_rejects_future_membership_effective_date():
    from stock_daily_report.etf_tracker import EtfSnapshot

    payload = snapshot_payload()
    payload["membership_date"] = DAY + timedelta(days=1)
    with pytest.raises(ValueError, match="future"):
        EtfSnapshot.model_validate(payload)


def test_persistence_is_immutable_and_detects_tampering(tmp_path):
    from stock_daily_report.etf_tracker import load_snapshot, save_snapshot

    snapshot = make_snapshot()
    path = save_snapshot(tmp_path, snapshot)
    original = path.read_bytes()
    assert save_snapshot(tmp_path, snapshot) == path
    with pytest.raises(ValueError, match="immutable"):
        save_snapshot(tmp_path, make_snapshot(shares=200_000_000))
    assert path.read_bytes() == original
    assert load_snapshot(path) == snapshot
    document = json.loads(original)
    document["snapshot"]["etfs"][0]["shares"] += 1
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        load_snapshot(path)


def test_history_does_not_use_future_and_labels_changed_membership(tmp_path):
    from stock_daily_report.etf_tracker import (
        EtfSnapshot,
        build_tracker_report,
        save_snapshot,
    )

    prior = snapshot_payload(DAY - timedelta(days=1))
    prior["constituents"][0]["code"] = "000001"
    save_snapshot(tmp_path, EtfSnapshot.model_validate(prior))
    save_snapshot(tmp_path, make_snapshot())
    save_snapshot(tmp_path, make_snapshot(DAY + timedelta(days=1), shares=1))
    report = build_tracker_report(tmp_path, DAY)
    assert report.membership_changed
    assert len(report.history) == 2
    assert report.funds[0].shares_change == 0


def test_missing_corrupt_or_misdated_current_data_is_unavailable(tmp_path):
    from stock_daily_report.etf_tracker import build_tracker_report, save_snapshot

    report = build_tracker_report(tmp_path, DAY)
    assert report.status == "unavailable"
    assert report.current is None
    path = save_snapshot(tmp_path, make_snapshot(DAY - timedelta(days=1)))
    target = tmp_path / "etf-tracker" / DAY.isoformat() / "input.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(path.read_bytes())
    assert build_tracker_report(tmp_path, DAY).status == "unavailable"
    target.write_text("{", encoding="utf-8")
    assert build_tracker_report(tmp_path, DAY).status == "unavailable"


def test_collect_reuses_valid_history_but_rejects_backdated_live_capture(tmp_path):
    from stock_daily_report.etf_tracker import collect_tracker, save_snapshot

    class Provider:
        calls = 0

        def fetch(self, report_date):
            self.calls += 1
            return make_snapshot(report_date)

    provider = Provider()
    now = datetime(2026, 9, 4, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="current"):
        collect_tracker(tmp_path, DAY - timedelta(days=1), provider=provider, now=now)
    assert provider.calls == 0
    saved = save_snapshot(tmp_path, make_snapshot(DAY - timedelta(days=1)))
    assert collect_tracker(
        tmp_path, DAY - timedelta(days=1), provider=provider, now=now
    ) == saved
    assert provider.calls == 0
    path = collect_tracker(tmp_path, DAY, provider=provider, now=now)
    assert path.is_file()
    assert provider.calls == 1


def test_cli_collects_or_reuses_snapshot_without_touching_stock_snapshots(tmp_path):
    from stock_daily_report.cli import main
    from stock_daily_report.etf_tracker import save_snapshot

    saved = save_snapshot(tmp_path, make_snapshot())
    assert main([
        "etf-tracker", "--date", DAY.isoformat(), "--output-root", str(tmp_path),
    ]) == 0
    assert saved.is_file()
    assert not (tmp_path / "snapshots").exists()


def test_invalid_prior_snapshot_is_skipped_without_a_fabricated_zero_baseline(tmp_path):
    from stock_daily_report.etf_tracker import build_tracker_report, save_snapshot

    prior = save_snapshot(tmp_path, make_snapshot(DAY - timedelta(days=1)))
    prior.write_text("invalid", encoding="utf-8")
    save_snapshot(tmp_path, make_snapshot())
    report = build_tracker_report(tmp_path, DAY)
    assert report.skipped_history_count == 1
    assert report.comparison_date is None
    assert report.ratio_change_pp is None
    assert all(fund.shares_change is None for fund in report.funds)
