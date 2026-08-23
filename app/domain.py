from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from statistics import median
from typing import Iterable, Sequence


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60
SETTLEMENT_WINDOW_SECONDS = 60.0
DEFAULT_BENCHMARK_UNCERTAINTY_PCT = 0.00015
MIN_BENCHMARK_CALIBRATION_SAMPLES = 20


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def kalshi_fee(price: float, contracts: float = 1.0, multiplier: float = 1.0) -> float:
    """Current general taker fee, rounded up to the nearest centicent ($0.0001)."""
    p = Decimal(str(clamp(price, 0.0, 1.0)))
    count = Decimal(str(max(0.0, contracts)))
    raw = Decimal(str(multiplier)) * Decimal("0.07") * count * p * (Decimal("1") - p)
    return float(raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def robust_composite(prices: Sequence[float]) -> tuple[float | None, float | None]:
    clean = [float(price) for price in prices if price and price > 0 and math.isfinite(price)]
    if not clean:
        return None, None
    center = median(clean)
    dispersion_pct = ((max(clean) - min(clean)) / center) * 100 if len(clean) > 1 else 0.0
    return center, dispersion_pct


def realized_volatility(
    samples: Sequence[tuple[float, float]], window_seconds: float
) -> float | None:
    """Annualized log-return volatility from timestamp/price samples."""
    if len(samples) < 3:
        return None
    latest = samples[-1][0]
    filtered = [(ts, px) for ts, px in samples if latest - ts <= window_seconds and px > 0]
    if len(filtered) < 3:
        return None
    returns: list[float] = []
    intervals: list[float] = []
    for (ts_a, px_a), (ts_b, px_b) in zip(filtered, filtered[1:]):
        delta = ts_b - ts_a
        if delta > 0:
            returns.append(math.log(px_b / px_a))
            intervals.append(delta)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    mean_interval = sum(intervals) / len(intervals)
    return math.sqrt(max(variance, 0.0) * (SECONDS_PER_YEAR / mean_interval))


def momentum_return(samples: Sequence[tuple[float, float]], window_seconds: float) -> float:
    if len(samples) < 2:
        return 0.0
    latest_ts, latest_px = samples[-1]
    eligible = [(ts, px) for ts, px in samples if latest_ts - ts <= window_seconds and px > 0]
    if len(eligible) < 2:
        return 0.0
    return math.log(latest_px / eligible[0][1])


@dataclass(frozen=True)
class ProbabilityEstimate:
    probability: float
    raw_probability: float
    z_distance: float
    annualized_volatility: float
    reference_price: float
    effective_horizon_seconds: float
    basis_uncertainty_pct: float
    model_version: str
    explanation: str


def settlement_probability(
    spot: float,
    strike: float,
    seconds_remaining: float,
    annualized_volatility: float | None,
    momentum_5m: float = 0.0,
    basis_uncertainty_pct: float = DEFAULT_BENCHMARK_UNCERTAINTY_PCT,
    model_version: str = "baseline-1.1",
    observed_window_average: float | None = None,
    observed_window_seconds: float = 0.0,
    benchmark_bias_pct: float = 0.0,
) -> ProbabilityEstimate:
    """Interpretable distance/volatility model for P(final BRTI average >= strike)."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    horizon = max(0.0, seconds_remaining)
    bias = clamp(benchmark_bias_pct, -0.0005, 0.0005)
    bias_multiplier = math.exp(bias)
    corrected_spot = spot * bias_multiplier
    elapsed = clamp(observed_window_seconds, 0.0, SETTLEMENT_WINDOW_SECONDS)
    has_observed_window = bool(
        horizon < SETTLEMENT_WINDOW_SECONDS
        and observed_window_average is not None
        and observed_window_average > 0
        and elapsed > 0
    )
    if has_observed_window:
        remaining_window = SETTLEMENT_WINDOW_SECONDS - elapsed
        corrected_average = float(observed_window_average) * bias_multiplier
        reference_price = (
            corrected_average * elapsed + corrected_spot * remaining_window
        ) / SETTLEMENT_WINDOW_SECONDS
        effective_horizon = max(
            1.0,
            remaining_window**3
            / (3.0 * SETTLEMENT_WINDOW_SECONDS**2),
        )
    else:
        reference_price = corrected_spot
        effective_horizon = max(
            1.0,
            horizon - (2.0 * SETTLEMENT_WINDOW_SECONDS / 3.0)
            if horizon >= SETTLEMENT_WINDOW_SECONDS
            else horizon**3 / (3.0 * SETTLEMENT_WINDOW_SECONDS**2),
        )
    vol = clamp(annualized_volatility or 0.55, 0.15, 2.50)
    horizon_sigma = vol * math.sqrt(effective_horizon / SECONDS_PER_YEAR)
    basis_sigma = max(basis_uncertainty_pct, 0.0)
    total_sigma = math.sqrt(horizon_sigma**2 + basis_sigma**2)
    distance = math.log(reference_price / strike)
    # Only a small, decaying fraction of recent momentum is carried forward.
    momentum_weight = clamp(horizon / 900.0, 0.0, 1.0) * 0.12
    adjusted_distance = distance + momentum_weight * momentum_5m
    z_distance = adjusted_distance / max(total_sigma, 1e-8)
    raw = normal_cdf(z_distance)
    probability = clamp(raw, 0.01, 0.99)
    direction = "above" if distance >= 0 else "below"
    if has_observed_window:
        explanation = (
            f"The projected 60-second settlement proxy is {direction} the threshold; "
            f"{int(elapsed)} seconds of the closing window are already averaged."
        )
    else:
        explanation = (
            f"The settlement proxy is {direction} the threshold with {int(horizon)} seconds left; "
            "the estimate includes closing-average volatility and benchmark uncertainty."
        )
    return ProbabilityEstimate(
        probability=probability,
        raw_probability=raw,
        z_distance=z_distance,
        annualized_volatility=vol,
        reference_price=reference_price,
        effective_horizon_seconds=effective_horizon,
        basis_uncertainty_pct=basis_sigma,
        model_version=model_version,
        explanation=explanation,
    )


def benchmark_error_summary(
    observations: Iterable[tuple[float, float]],
    *,
    uncertainty_floor_pct: float = DEFAULT_BENCHMARK_UNCERTAINTY_PCT,
    minimum_samples: int = MIN_BENCHMARK_CALIBRATION_SAMPLES,
) -> dict[str, float | int | bool | None]:
    residuals = [
        math.log(float(official) / float(proxy))
        for proxy, official in observations
        if proxy > 0 and official > 0 and abs(math.log(float(official) / float(proxy))) <= 0.005
    ]
    sample_size = len(residuals)
    mean_absolute_error = (
        sum(abs(value) for value in residuals) / sample_size if sample_size else None
    )
    if sample_size < minimum_samples:
        return {
            "sample_size": sample_size,
            "minimum_samples": minimum_samples,
            "calibrated": False,
            "bias_pct": 0.0,
            "residual_sigma_pct": None,
            "mean_absolute_error_pct": mean_absolute_error,
            "uncertainty_pct": uncertainty_floor_pct,
        }
    bias = clamp(float(median(residuals)), -0.0005, 0.0005)
    deviations = [value - bias for value in residuals]
    robust_sigma = 1.4826 * float(median(abs(value) for value in deviations))
    standard_sigma = math.sqrt(sum(value**2 for value in deviations) / sample_size)
    residual_sigma = max(robust_sigma, standard_sigma)
    return {
        "sample_size": sample_size,
        "minimum_samples": minimum_samples,
        "calibrated": True,
        "bias_pct": bias,
        "residual_sigma_pct": residual_sigma,
        "mean_absolute_error_pct": mean_absolute_error,
        "uncertainty_pct": max(uncertainty_floor_pct, residual_sigma * 2.0),
    }


def market_probability(yes_bid: float | None, yes_ask: float | None) -> float | None:
    if yes_bid is not None and yes_ask is not None and yes_bid > 0 and yes_ask > 0:
        return (yes_bid + yes_ask) / 2
    return yes_ask or yes_bid


def expected_value(probability: float, price: float, contracts: float = 1.0) -> float:
    return probability * contracts - price * contracts - kalshi_fee(price, contracts)


def fractional_kelly_fraction(probability: float, price: float, fraction: float) -> float:
    if not 0 < price < 1:
        return 0.0
    full_kelly = (probability - price) / (1.0 - price)
    return clamp(full_kelly * max(0.0, fraction), 0.0, 1.0)


@dataclass(frozen=True)
class PositionSize:
    bankroll_fraction: float
    dollar_amount: float
    contracts: int


def position_size(
    bankroll: float,
    probability: float,
    price: float,
    fractional_kelly: float,
    max_position_pct: float,
    max_risk_pct: float,
    available_contracts: float | None = None,
) -> PositionSize:
    if bankroll <= 0 or price <= 0:
        return PositionSize(0.0, 0.0, 0)
    kelly = fractional_kelly_fraction(probability, price, fractional_kelly)
    cap = min(max(0.0, max_position_pct), max(0.0, max_risk_pct))
    target_fraction = min(kelly, cap)
    max_cost = bankroll * target_fraction
    unit_cost = price + kalshi_fee(price)
    contracts = int(max_cost // max(unit_cost, 1e-8))
    if available_contracts is not None:
        contracts = min(contracts, int(max(0.0, available_contracts)))
    amount = contracts * unit_cost
    return PositionSize(
        bankroll_fraction=(amount / bankroll) if bankroll else 0.0,
        dollar_amount=amount,
        contracts=contracts,
    )


def calibration_metrics(observations: Iterable[tuple[float, int]]) -> dict[str, object]:
    rows = [(clamp(float(p), 0.0, 1.0), int(y)) for p, y in observations]
    if not rows:
        return {"sample_size": 0, "brier_score": None, "calibration_error": None, "buckets": []}
    brier = sum((p - y) ** 2 for p, y in rows) / len(rows)
    boundaries = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.000001)]
    buckets: list[dict[str, object]] = []
    weighted_error = 0.0
    for low, high in boundaries:
        values = [(p, y) for p, y in rows if low <= p < high]
        if not values:
            continue
        predicted = sum(p for p, _ in values) / len(values)
        actual = sum(y for _, y in values) / len(values)
        error = abs(predicted - actual)
        weighted_error += error * len(values)
        buckets.append(
            {
                "label": f"{round(low * 100):d}-{round(min(high, 1) * 100):d}%",
                "count": len(values),
                "predicted": predicted,
                "actual": actual,
                "error": error,
            }
        )
    return {
        "sample_size": len(rows),
        "brier_score": brier,
        "calibration_error": weighted_error / len(rows),
        "buckets": buckets,
    }
