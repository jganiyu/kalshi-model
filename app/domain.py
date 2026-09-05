from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from statistics import median
from typing import Any, Callable, Iterable, Sequence


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60
SETTLEMENT_WINDOW_SECONDS = 60.0
DEFAULT_BENCHMARK_UNCERTAINTY_PCT = 0.00015
MIN_BENCHMARK_CALIBRATION_SAMPLES = 20
NEXT_THRESHOLD_WINDOW_SECONDS = 60
NEXT_THRESHOLD_COMPARISON_SECONDS = 12


class NextThresholdForecast:
    """Read-only, per-second proxy estimate for a forthcoming market threshold.

    This deliberately knows nothing about decisions, orders, or positions.  The
    contract uses CF Benchmarks' BRTI prints, which the app does not receive, so
    this is always labelled as a proxy estimate rather than an official value.
    """

    def __init__(self) -> None:
        self._target_ticker: str | None = None
        self._target_open_time: str | None = None
        self._target_open_epoch: float | None = None
        self._samples: dict[int, float] = {}
        self._frozen: dict[str, Any] | None = None
        self._comparison_until: float | None = None
        self._last_persisted_key: tuple[str, str] | None = None

    @staticmethod
    def _market_identity(market: dict[str, Any] | None) -> tuple[str, str, float] | None:
        if not market:
            return None
        ticker = str(market.get("ticker") or "")
        open_time = str(market.get("open_time") or "")
        parsed = parse_time(open_time)
        if not ticker or not parsed:
            return None
        return ticker, open_time, parsed.timestamp()

    def _payload(self, status: str, observed: datetime) -> dict[str, Any]:
        values = list(self._samples.values())
        estimate = sum(values) / len(values) if values else None
        dispersion = (
            math.sqrt(sum((value - estimate) ** 2 for value in values) / len(values))
            if estimate is not None and values
            else None
        )
        open_epoch = self._target_open_epoch
        elapsed = (
            clamp(observed.timestamp() - (open_epoch - NEXT_THRESHOLD_WINDOW_SECONDS), 0.0,
                  float(NEXT_THRESHOLD_WINDOW_SECONDS))
            if open_epoch is not None
            else 0.0
        )
        expected = max(1, min(NEXT_THRESHOLD_WINDOW_SECONDS, math.ceil(elapsed)))
        return {
            "status": status,
            "ticker": self._target_ticker,
            "target_open_time": self._target_open_time,
            "estimate": estimate,
            "estimate_price": estimate,
            "samples_collected": len(values),
            "sample_target": NEXT_THRESHOLD_WINDOW_SECONDS,
            "coverage": min(1.0, len(values) / expected),
            "sample_dispersion_dollars": dispersion,
            "uncertainty_dollars": dispersion,
            "qualifier": "Proxy estimate · not official CF Benchmarks BRTI",
            "observed_at": observed.isoformat(),
        }

    @staticmethod
    def _inactive_payload(
        observed: datetime, next_market: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep the Dashboard slot honest outside its one-minute sample window."""
        identity = NextThresholdForecast._market_identity(next_market)
        return {
            "status": "inactive",
            "ticker": identity[0] if identity else None,
            "target_open_time": identity[1] if identity else None,
            "estimate": None,
            "estimate_price": None,
            "samples_collected": 0,
            "sample_target": NEXT_THRESHOLD_WINDOW_SECONDS,
            "coverage": 0.0,
            "qualifier": "Starts in the final minute before the next round",
            "observed_at": observed.isoformat(),
        }

    def observe(
        self,
        *,
        next_market: dict[str, Any] | None,
        known_markets: Sequence[dict[str, Any] | None],
        proxy_price: float | None,
        observed_at: str,
        official_threshold: Callable[[dict[str, Any]], float | None] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return (display_state, evidence_to_persist) for this clock tick."""
        observed = parse_time(observed_at) or utc_now()
        now = observed.timestamp()
        evidence: dict[str, Any] | None = None

        if self._frozen is not None:
            target = str(self._frozen.get("ticker") or "")
            official_market = next(
                (market for market in known_markets if market and str(market.get("ticker") or "") == target),
                None,
            )
            threshold = official_threshold(official_market) if official_market and official_threshold else None
            if (
                self._frozen.get("status") != "comparing"
                and threshold is not None
                and self._target_open_epoch is not None
                and now >= self._target_open_epoch
            ):
                comparison = dict(self._frozen)
                comparison.update({
                    "status": "comparing",
                    "official_threshold": float(threshold),
                    "comparison": {
                        "official_threshold": float(threshold),
                        "error_dollars": float(comparison["estimate"]) - float(threshold)
                        if comparison.get("estimate") is not None else None,
                    },
                    "official_observed_at": observed.isoformat(),
                })
                self._frozen = comparison
                self._comparison_until = now + NEXT_THRESHOLD_COMPARISON_SECONDS
                key = (target, "compared")
                if self._last_persisted_key != key:
                    evidence = dict(comparison)
                    evidence["final_state"] = "COMPARED"
                    self._last_persisted_key = key
            # Do not leave a frozen card on screen forever when Kalshi has not
            # yet published (or we never learned) the successor ticker.
            freeze_expired = (
                self._target_open_epoch is not None
                and now >= self._target_open_epoch + NEXT_THRESHOLD_COMPARISON_SECONDS
            )
            if (
                (self._frozen.get("status") == "comparing" and self._comparison_until and now >= self._comparison_until)
                or freeze_expired
            ):
                self._frozen = None
                self._comparison_until = None
                self._target_ticker = None
                self._target_open_time = None
                self._target_open_epoch = None
                self._samples = {}
            elif self._frozen is not None:
                return dict(self._frozen), evidence

        identity = self._market_identity(next_market)
        # Kalshi can publish the successor contract late.  The collection
        # window is still deterministic: it starts 60 seconds before the
        # active contract closes.  Use a deliberately non-tradable placeholder
        # identity until the successor arrives rather than dropping the monitor.
        current = known_markets[0] if known_markets else None
        current_close = parse_time((current or {}).get("close_time"))
        if current_close and 0 <= current_close.timestamp() - now <= NEXT_THRESHOLD_WINDOW_SECONDS:
            successor_is_immediate = (
                identity is not None and abs(identity[2] - current_close.timestamp()) <= 5.0
            )
            if not successor_is_immediate:
                close_key = int(current_close.timestamp())
                identity = (
                    f"PENDING-NEXT-{close_key}",
                    current_close.isoformat(),
                    current_close.timestamp(),
                )
        if identity is None:
            return self._inactive_payload(observed, next_market), evidence
        ticker, open_time, open_epoch = identity
        if not (open_epoch - NEXT_THRESHOLD_WINDOW_SECONDS <= now < open_epoch):
            return self._inactive_payload(observed, next_market), evidence
        if ticker != self._target_ticker or open_time != self._target_open_time:
            self._target_ticker = ticker
            self._target_open_time = open_time
            self._target_open_epoch = open_epoch
            self._samples = {}
            self._last_persisted_key = None
        if proxy_price is not None and math.isfinite(float(proxy_price)) and float(proxy_price) > 0:
            # One equally weighted proxy print per UTC second.  The first fresh
            # price received in that second wins, avoiding update-rate weighting.
            self._samples.setdefault(int(now), float(proxy_price))
        return self._payload("active", observed), evidence

    def freeze_if_due(self, observed_at: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Freeze a completed collection after the target market opens."""
        observed = parse_time(observed_at) or utc_now()
        if self._target_open_epoch is None or observed.timestamp() < self._target_open_epoch:
            return None, None
        if self._frozen is None:
            self._frozen = self._payload("frozen", observed)
            self._frozen["frozen_at"] = observed.isoformat()
            key = (str(self._target_ticker or ""), "frozen")
            evidence = None
            if self._last_persisted_key != key:
                evidence = dict(self._frozen)
                evidence["final_state"] = "FROZEN"
                self._last_persisted_key = key
            return dict(self._frozen), evidence
        return dict(self._frozen), None


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def settlement_margin(settlement_price: object, strike: object) -> float | None:
    """Return settlement price minus strike without trusting external numerics."""
    try:
        price = float(settlement_price)  # type: ignore[arg-type]
        threshold = float(strike)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or not math.isfinite(threshold):
        return None
    return price - threshold


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
    boundaries = [
        (index / 10.0, (index + 1) / 10.0)
        for index in range(10)
    ]
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


def threshold_breach_exit_state(
    side: str,
    btc_proxy: float | None,
    threshold: float | None,
    *,
    enabled: bool = True,
    buffer_dollars: float = 0.0,
    data_reliable: bool = True,
    pending: bool = False,
    exited: bool = False,
    blocked_reason: str | None = None,
) -> dict[str, object]:
    """Describe the side-aware BTC-proxy threshold protection for one position."""
    normalized_side = str(side or "").upper()
    buffer_value = float(buffer_dollars or 0.0)
    proxy_value = float(btc_proxy) if btc_proxy is not None else None
    threshold_value = float(threshold) if threshold is not None else None
    exit_level = None
    if threshold_value is not None and normalized_side in {"YES", "NO"}:
        # The buffer is a signed, side-aware offset. A negative value tolerates
        # an adverse move beyond To Beat; a positive value exits before To Beat.
        exit_level = threshold_value + (buffer_value if normalized_side == "YES" else -buffer_value)

    breached = False
    distance = None
    if proxy_value is not None and exit_level is not None:
        if normalized_side == "YES":
            distance = proxy_value - exit_level
            breached = proxy_value <= exit_level + 1e-9
        elif normalized_side == "NO":
            distance = exit_level - proxy_value
            breached = proxy_value >= exit_level - 1e-9

    reason = blocked_reason
    if not enabled:
        status = "Watching"
        reason = reason or "Threshold Breach Exit is off."
    elif exited:
        status = "Exited"
    elif pending:
        status = "Exit pending"
    elif reason:
        status = "Blocked"
    elif proxy_value is None or exit_level is None:
        status = "Blocked"
        reason = "Current BTC proxy or To Beat threshold is unavailable."
    elif not data_reliable:
        status = "Blocked"
        reason = "BTC proxy data is not reliable enough to trigger an exit."
    elif breached:
        status = "Breached"
    else:
        status = "Watching"

    return {
        "enabled": bool(enabled),
        "buffer_dollars": buffer_value,
        "exit_level": exit_level,
        "btc_proxy": proxy_value,
        "threshold": threshold_value,
        "distance_to_exit": distance,
        "breached": breached,
        "status": status,
        "reason": reason,
    }


TEXAS_HOLDEM_LEGACY = "TEXAS_HOLDEM"
TEXAS_HOLDEM_V2 = "TEXAS_HOLDEM_2_0"
TEXAS_HOLDEM_STRATEGIES = frozenset({TEXAS_HOLDEM_LEGACY, TEXAS_HOLDEM_V2})
# The entry gate is a saved, per-environment setting.  These two values are
# deliberately versioned strategy rules rather than calibration knobs.
TEXAS_V2_MVI_BOOST_THRESHOLD = 8.0
TEXAS_V2_MVI_BOOST_MULTIPLIER = 1.5
TEXAS_V2_THESIS_CHECKPOINT_SECONDS = 300.0
TEXAS_V2_THESIS_UNFAVORABLE_DISTANCE = 50.0
TEXAS_V2_RULE_VERSION = "texas-holdem-2.0.0"


def is_texas_holdem_strategy(value: object) -> bool:
    return str(value or "").upper() in TEXAS_HOLDEM_STRATEGIES


def texas_threshold_breached(side: object, btc_proxy: object, threshold: object) -> bool:
    """A touch counts; the side is the held Kalshi outcome."""
    try:
        proxy = float(btc_proxy)
        strike = float(threshold)
    except (TypeError, ValueError):
        return False
    normalized = str(side or "").upper()
    return proxy + 1e-12 >= strike if normalized == "YES" else (
        proxy <= strike + 1e-12 if normalized == "NO" else False
    )


def texas_unfavorable_distance(side: object, btc_proxy: object, threshold: object) -> float | None:
    """Positive values mean BTC remains on the wrong side for the held outcome."""
    try:
        proxy = float(btc_proxy)
        strike = float(threshold)
    except (TypeError, ValueError):
        return None
    normalized = str(side or "").upper()
    if normalized == "YES":
        return strike - proxy
    if normalized == "NO":
        return proxy - strike
    return None


def texas_holdem_phase(seconds_remaining: float | None) -> dict[str, object]:
    """Return the authoritative five-minute Texas Hold'em market phase."""
    remaining = max(0.0, min(900.0, float(seconds_remaining or 0.0)))
    elapsed = 900.0 - remaining
    if elapsed < 300.0:
        key, label, start = "FLOP", "The Flop", 0.0
    elif elapsed < 600.0:
        key, label, start = "TURN", "The Turn", 300.0
    else:
        key, label, start = "RIVER", "The River", 600.0
    return {
        "key": key,
        "label": label,
        "elapsed_seconds": elapsed,
        "phase_elapsed_seconds": max(0.0, min(300.0, elapsed - start)),
        "progress": max(0.0, min(1.0, (elapsed - start) / 300.0)),
        "market_progress": max(0.0, min(1.0, elapsed / 900.0)),
    }


def texas_holdem_exit_reason(
    bid: float | None,
    seconds_remaining: float | None,
    settings: dict[str, object],
) -> tuple[str | None, dict[str, object]]:
    """Evaluate the phase target and River stop for a Texas position."""
    phase = texas_holdem_phase(seconds_remaining)
    key = str(phase["key"])
    target_key = f"texas_holdem_{key.lower()}_target"
    stop_key = f"texas_holdem_{key.lower()}_stop"
    default_target = {"FLOP": 0.60, "TURN": 0.50, "RIVER": 0.95}[key]
    target = float(settings.get(target_key, default_target))
    stop = float(settings.get(stop_key, 0.60))
    state = {**phase, "target": target, "stop": stop, "bid": bid}
    if bid is None:
        return None, state
    price = float(bid)
    if price + 1e-12 >= target:
        return f"TEXAS_{key}_TARGET", state
    # Zero is the explicit disabled value for a phase stop.  It must never
    # become an implicit 0¢ exit trigger.
    if stop > 0 and price <= stop + 1e-12:
        return f"TEXAS_{key}_STOP", state
    return None, state
