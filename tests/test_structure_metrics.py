from dataclasses import replace
from datetime import date

from stock_daily_report.backtest.replay import ReplayEvent
from stock_daily_report.backtest.structure_metrics import compare_structures


def _event(
    event_id: str,
    *,
    kind: str = "fractal",
    formed_at: date = date(2026, 1, 1),
    confirmed_at: date | None = date(2026, 1, 2),
    price: float | None = 10,
) -> ReplayEvent:
    return ReplayEvent(
        analyzer="fixture",
        event_id=event_id,
        kind=kind,
        observed_at=date(2026, 1, 3),
        max_input_date=date(2026, 1, 3),
        formed_at=formed_at,
        confirmed_at=confirmed_at,
        tradable_at=date(2026, 1, 3) if confirmed_at else None,
        status="confirmed" if confirmed_at else "candidate",
        reason_code="fixture",
        price=price,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 1),
    )


def test_compare_structures_reports_precision_recall_and_lag():
    result = compare_structures(
        reference=[_event("strict-1", price=10)],
        candidate=[_event("simple-1", price=10.01)],
        price_tolerance=0.02,
        time_tolerance_days=0,
    )

    assert result.matched == 1
    assert result.precision == 1
    assert result.recall == 1
    assert result.f1 == 1
    assert result.confirmation_lags == (0,)


def test_compare_structures_reports_confirmed_rewrite_rate():
    original = _event("simple-1", price=10)
    revised = _event("simple-1", price=11)
    revised = revised.__class__(**{**revised.__dict__, "revision": 1})

    result = compare_structures(
        reference=[_event("strict-1")],
        candidate=[original, revised],
        price_tolerance=0,
        time_tolerance_days=0,
    )

    assert result.confirmed_count == 1
    assert result.rewritten_confirmed_count == 1
    assert result.rewrite_rate == 1


def test_compare_structures_does_not_count_candidate_confirmation_as_rewrite():
    candidate = _event("simple-1", confirmed_at=None)
    confirmed = replace(
        candidate,
        confirmed_at=date(2026, 1, 4),
        tradable_at=date(2026, 1, 5),
        status="confirmed",
        revision=1,
    )
    unchanged = replace(
        confirmed,
        observed_at=date(2026, 1, 5),
        max_input_date=date(2026, 1, 5),
    )

    result = compare_structures(
        reference=[_event("strict-1")],
        candidate=[candidate, confirmed, unchanged],
    )

    assert result.confirmed_count == 1
    assert result.rewritten_confirmed_count == 0
    assert result.rewrite_rate == 0


def test_compare_structures_ignores_tradeability_enrichment_as_rewrite():
    confirmed = _event("simple-1")
    tradeable = replace(
        confirmed,
        observed_at=date(2026, 1, 4),
        max_input_date=date(2026, 1, 4),
        tradable_at=date(2026, 1, 4),
        revision=1,
    )

    result = compare_structures(
        reference=[_event("strict-1")],
        candidate=[confirmed, tradeable],
    )

    assert result.rewritten_confirmed_count == 0
    assert result.rewrite_rate == 0


def test_compare_structures_uses_maximum_deterministic_matching():
    reference = [
        _event("r1", price=0.0),
        _event("r2", price=0.15),
    ]
    candidate = [
        _event("c1", price=0.08),
        _event("c2", price=0.0),
    ]

    result = compare_structures(
        reference=reference,
        candidate=candidate,
        price_tolerance=0.1,
    )

    assert result.matched == 2


def test_compare_structures_does_not_match_different_symbols():
    reference = replace(_event("r1"), symbol="600519")
    candidate = replace(_event("c1"), symbol="000001")

    result = compare_structures([reference], [candidate])

    assert result.matched == 0


def test_compare_structures_excludes_non_structural_events():
    result = compare_structures(
        reference=[_event("strict-1"), _event("strict-signal", kind="signal")],
        candidate=[_event("simple-1")],
    )

    assert result.reference_count == 1
    assert result.recall == 1


def test_compare_structures_uses_final_visible_snapshot_only():
    stale = replace(
        _event("stale"),
        observed_at=date(2026, 1, 2),
        max_input_date=date(2026, 1, 2),
    )
    current = replace(
        _event("current"),
        observed_at=date(2026, 1, 3),
        max_input_date=date(2026, 1, 3),
    )

    result = compare_structures([], [stale, current])

    assert result.candidate_count == 1


def test_compare_structures_requires_matching_fractal_polarity():
    top = _event("strict-top")
    bottom = replace(top, reason_code="fractal_bottom", event_id="simple-bottom")

    result = compare_structures([top], [bottom])

    assert result.matched == 0


def test_compare_structures_rejects_non_finite_tolerance():
    try:
        compare_structures([], [], price_tolerance=float("nan"))
    except ValueError as error:
        assert "finite" in str(error)
    else:
        raise AssertionError("NaN tolerance must be rejected")


def test_compare_structures_rejects_boolean_tolerance():
    try:
        compare_structures([], [], price_tolerance=True)
    except ValueError as error:
        assert "finite" in str(error)
    else:
        raise AssertionError("boolean tolerance must be rejected")
