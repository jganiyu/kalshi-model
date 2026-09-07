"""Causal diagnostic features for a fixed-threshold Texas crossing model.

This module deliberately has no order, sizing, settings, database, or network
dependencies. The mathematical reference is uncalibrated and cannot authorize
an entry. MVI and reversal are recorded without invented fitted coefficients.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


MODEL_VERSION = "texas-breach-reference-1"
DIRECTION_WINDOW_SECONDS = 60.0
MINIMUM_DIRECTION_SPAN_SECONDS = 30.0


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def first_passage_reference(distance: float, sigma: float, horizon: float, drift: float = 0.0) -> float:
    """P(maximum reaches distance) for constant-drift arithmetic Brownian motion.

    ``drift`` is dollars per second toward the threshold. A continued-trend
    scenario is not a calibrated BTC forecast. Dimensionless evaluation avoids
    exp(2*drift*distance/sigma**2) overflowing on strong trends or low sigma.
    """
    if not all(math.isfinite(x) for x in (distance, sigma, horizon, drift)):
        raise ValueError("First-passage inputs must be finite")
    if distance < 0 or sigma <= 0 or horizon <= 0:
        raise ValueError("Invalid first-passage distance, volatility or horizon")
    if distance == 0:
        return 1.0
    scale = sigma * math.sqrt(horizon)
    r, u = distance / scale, drift * math.sqrt(horizon) / sigma
    if not math.isfinite(r) or not math.isfinite(u):
        raise ValueError("First-passage scale exceeds numeric range")
    first = 0.5 * math.erfc((r - u) / math.sqrt(2.0))
    z = r + u
    if z <= 10.0:
        tail = 0.5 * math.erfc(z / math.sqrt(2.0))
        # For positive u, 2*r*u <= (r+u)^2/2 <= 50 here.
        second = math.exp(2.0 * r * u) * tail
    else:
        # Mills-ratio expansion of exp(z*z/2)*Phi(-z). Combining
        # exponents first gives 2*r*u-z*z/2 = -(r-u)^2/2.
        inverse_square = 1.0 / (z * z)
        series = 1.0
        term = 1.0
        for n in range(1, 9):
            term *= -(2 * n - 1) * inverse_square
            series += term
        second = math.exp(-0.5 * (r - u) ** 2) * series / (math.sqrt(2.0 * math.pi) * z)
    return min(1.0, max(0.0, first + second))


def breach_features(
    *,
    now_timestamp: float,
    ticker: str,
    btc_price: object,
    threshold: object,
    seconds_remaining: object,
    margin_volatility: Mapping[str, Any] | None,
    source_reliable: bool,
    price_timestamp: object,
    max_age_seconds: float,
    samples: Sequence[tuple[float, float]] = (),
) -> dict[str, Any]:
    """Describe future proxy touch of this market's fixed To Beat line.

    ``price_timestamp`` belongs to ``btc_price``. ``samples`` must contain only
    individually source-qualified observations. Future samples
    are filtered even during replay. No earlier touch is inferred from history:
    the reference estimates reaching the line from the current location, and
    therefore must not replace the persisted post-fill breach latch.
    """
    metric = margin_volatility or {}
    result: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "ticker": ticker,
        "observed_at_timestamp": _finite(now_timestamp),
        "price_timestamp": _finite(price_timestamp),
        "btc_price": _finite(btc_price),
        "threshold": _finite(threshold),
        "status": "UNAVAILABLE",
        "calibrated": False,
        "execution_enabled": False,
        "reason": None,
        "reference_probability": None,
        "constant_trend_probability": None,
        "distance_dollars": None,
        "expected_remaining_move": None,
        "normalized_distance": None,
        "direction_toward_threshold_dollars_per_second": None,
        "direction_span_seconds": None,
        "direction_max_gap_seconds": None,
        "direction_window_coverage": 0.0,
        "direction_sample_count": 0,
        "direction_status": "UNAVAILABLE",
        "direction_reason": "Insufficient qualified direction samples",
        "mvi": _finite(metric.get("mvi")),
        "reversal_component": _finite(metric.get("reversal_component")),
        "coverage": _finite(metric.get("coverage")),
        "measurement_version": metric.get("calculation_version"),
        "seconds_remaining": _finite(seconds_remaining),
        "quote_age_seconds": None,
    }
    now = _finite(now_timestamp)
    price, strike = _finite(btc_price), _finite(threshold)
    remaining, observed = _finite(seconds_remaining), _finite(price_timestamp)
    age_limit = _finite(max_age_seconds)
    if now is None or observed is None or age_limit is None or age_limit <= 0:
        result["reason"] = "Price timestamp unavailable"
        return result
    age = now - observed
    result["quote_age_seconds"] = age
    if not source_reliable or age < 0 or age > age_limit:
        result["reason"] = "BTC source is stale or unreliable"
        return result
    if price is None or strike is None or min(price, strike) <= 0 or not ticker:
        result["reason"] = "Current market threshold unavailable"
        return result
    if remaining is None or not 0 < remaining <= 900:
        result["reason"] = "Active market clock unavailable"
        return result
    sigma = _finite(metric.get("raw_realized_volatility"))
    if not metric.get("reliable") or sigma is None or sigma <= 0:
        result["reason"] = "Volatility measurement is not reliable"
        return result
    distance = abs(price - strike)
    expected = sigma * math.sqrt(remaining)
    if not math.isfinite(expected) or expected <= 0:
        result["reason"] = "Remaining movement unavailable"
        return result
    normalized = distance / expected
    result.update({
        "status": "REFERENCE",
        "reason": "Uncalibrated mathematical reference; no trading action",
        "distance_dollars": distance,
        "expected_remaining_move": expected,
        "normalized_distance": normalized,
        "reference_probability": math.erfc(normalized / math.sqrt(2.0)),
    })
    # Fixed horizon and timestamps prevent a faster producer from stretching the
    # trend window. Deduplicate timestamps; never use later observations.
    eligible: dict[float, float] = {}
    for sample_timestamp, sample_price in samples:
        ts, px = _finite(sample_timestamp), _finite(sample_price)
        if ts is not None and px is not None and px > 0 and observed - DIRECTION_WINDOW_SECONDS <= ts <= observed:
            eligible[ts] = px
    ordered = sorted(eligible.items())
    result["direction_sample_count"] = len(ordered)
    if len(ordered) < 3:
        return result
    span = ordered[-1][0] - ordered[0][0]
    result["direction_span_seconds"] = span
    maximum_gap = max(right[0] - left[0] for left, right in zip(ordered, ordered[1:]))
    result["direction_max_gap_seconds"] = maximum_gap
    result["direction_window_coverage"] = min(1.0, span / DIRECTION_WINDOW_SECONDS)
    # Require current data and no gap larger than the freshness allowance.
    if span < MINIMUM_DIRECTION_SPAN_SECONDS:
        result["direction_reason"] = "Direction window is too short"
        return result
    if observed - ordered[-1][0] > age_limit:
        result["direction_reason"] = "Direction samples are stale"
        return result
    if maximum_gap > age_limit:
        result["direction_reason"] = "Direction window contains a data gap"
        return result
    xs = [ts - ordered[0][0] for ts, _ in ordered]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(px for _, px in ordered) / len(ordered)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator > 0:
        slope = sum((x - mean_x) * (px - mean_y) for x, (_, px) in zip(xs, ordered)) / denominator
        toward = 1.0 if strike > price else -1.0 if strike < price else 0.0
        result["direction_toward_threshold_dollars_per_second"] = slope * toward
        result["direction_status"] = "AVAILABLE"
        result["direction_reason"] = None
        try:
            result["constant_trend_probability"] = first_passage_reference(distance, sigma, remaining, slope * toward)
        except (ValueError, OverflowError):
            # Extreme invalid scenarios leave the valid driftless reference
            # intact; never leak NaN/Infinity into the dashboard JSON.
            pass
    return result
