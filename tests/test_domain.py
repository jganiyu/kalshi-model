from __future__ import annotations

import math

import pytest

from app.domain import (
    calibration_metrics,
    expected_value,
    fractional_kelly_fraction,
    kalshi_fee,
    position_size,
    robust_composite,
    settlement_probability,
)


def test_kalshi_fee_uses_current_quadratic_formula_and_centicent_rounding() -> None:
    assert kalshi_fee(0.50, 100) == pytest.approx(1.75)
    assert kalshi_fee(0.055, 1) == pytest.approx(0.0037)
    assert kalshi_fee(0.01, 1) == pytest.approx(0.0007)


def test_expected_value_includes_entry_price_and_fee() -> None:
    assert expected_value(0.72, 0.61) == pytest.approx(0.72 - 0.61 - kalshi_fee(0.61))


def test_probability_is_monotonic_in_distance_and_time() -> None:
    below = settlement_probability(99_900, 100_000, 300, 0.55).probability
    above = settlement_probability(100_100, 100_000, 300, 0.55).probability
    near_expiry = settlement_probability(100_100, 100_000, 30, 0.55).probability
    assert below < 0.5 < above
    assert near_expiry > above


def test_probability_stays_finite_at_expiry() -> None:
    estimate = settlement_probability(100_000, 100_000, 0, None)
    assert math.isfinite(estimate.probability)
    assert 0.01 <= estimate.probability <= 0.99


def test_robust_composite_uses_median_and_reports_dispersion() -> None:
    price, dispersion = robust_composite([100.0, 101.0, 1_000.0])
    assert price == 101.0
    assert dispersion == pytest.approx((1_000.0 - 100.0) / 101.0 * 100)


def test_fractional_kelly_and_position_caps() -> None:
    fraction = fractional_kelly_fraction(0.70, 0.50, 0.25)
    assert fraction == pytest.approx(0.10)
    size = position_size(
        bankroll=1_000,
        probability=0.70,
        price=0.50,
        fractional_kelly=0.25,
        max_position_pct=0.05,
        max_risk_pct=0.02,
        available_contracts=100,
    )
    assert size.bankroll_fraction <= 0.02
    assert size.contracts > 0
    assert size.dollar_amount <= 20


def test_calibration_metrics_are_probability_metrics() -> None:
    metrics = calibration_metrics([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)])
    assert metrics["sample_size"] == 4
    assert metrics["brier_score"] == pytest.approx(0.025)
    assert metrics["calibration_error"] == pytest.approx(0.15)
