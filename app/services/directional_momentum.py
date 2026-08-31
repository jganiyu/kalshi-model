from __future__ import annotations

import math
from typing import Any, Iterable


def regression_momentum(
    samples: Iterable[tuple[float, float]],
    *,
    lookback_seconds: float,
    now: float | None = None,
) -> dict[str, Any]:
    """Measure BTC-proxy direction with a least-squares slope over a recent window."""
    window = max(1.0, float(lookback_seconds))
    cleaned: dict[float, float] = {}
    for raw_timestamp, raw_price in samples:
        try:
            timestamp = float(raw_timestamp)
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(timestamp) and math.isfinite(price):
            cleaned[timestamp] = price
    ordered = sorted(cleaned.items())
    if not ordered:
        return _unavailable(window, "Waiting for BTC-proxy history.")

    end = float(now) if now is not None else ordered[-1][0]
    start = end - window
    points = [(timestamp, price) for timestamp, price in ordered if start <= timestamp <= end]
    if len(points) < 3:
        return _unavailable(
            window,
            "Building the BTC directional lookback window.",
            sample_count=len(points),
        )

    coverage = max(0.0, points[-1][0] - points[0][0])
    required_coverage = max(1.0, window * 0.80)
    maximum_gap = max(2.5, window / 3.0)
    gaps = [current[0] - previous[0] for previous, current in zip(points, points[1:])]
    if coverage + 1e-9 < required_coverage or (gaps and max(gaps) > maximum_gap):
        return _unavailable(
            window,
            "Building a continuous BTC directional lookback window.",
            sample_count=len(points),
            coverage_seconds=coverage,
        )

    origin = points[0][0]
    x_values = [timestamp - origin for timestamp, _ in points]
    y_values = [price for _, price in points]
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    if denominator <= 1e-12:
        return _unavailable(
            window,
            "BTC directional samples do not span enough time.",
            sample_count=len(points),
            coverage_seconds=coverage,
        )
    slope = sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values)
    ) / denominator
    movement = slope * window
    direction = "UP" if movement > 1e-9 else "DOWN" if movement < -1e-9 else "FLAT"
    return {
        "reliable": True,
        "lookback_seconds": window,
        "sample_count": len(points),
        "coverage_seconds": coverage,
        "slope_dollars_per_second": slope,
        "regression_movement_dollars": movement,
        "direction": direction,
        "detail": f"BTC regression moved {movement:+.2f} dollars over {window:g} seconds.",
    }


def directional_gate(
    settings: dict[str, Any],
    momentum: dict[str, Any] | None,
    *,
    side: str,
) -> dict[str, Any]:
    enabled = bool(settings.get("directional_momentum_gate_enabled", True))
    lookback = max(
        1.0, float(settings.get("directional_momentum_lookback_seconds", 15.0))
    )
    minimum = max(
        0.0,
        float(settings.get("directional_momentum_minimum_movement_dollars", 1.0)),
    )
    normalized_side = str(side or "YES").upper()
    required = minimum if normalized_side == "YES" else -minimum
    state = dict(momentum or {})
    movement = state.get("regression_movement_dollars")
    reliable = bool(state.get("reliable"))
    if not enabled:
        passed = True
        detail = "The BTC Directional Momentum Gate is off."
    elif not reliable or movement is None:
        passed = False
        detail = str(state.get("detail") or "Waiting for BTC directional momentum.")
    else:
        current = float(movement)
        passed = (
            current + 1e-9 >= minimum
            if normalized_side == "YES"
            else current - 1e-9 <= -minimum
        )
        label = "Up" if normalized_side == "YES" else "Down"
        direction = "rise" if normalized_side == "YES" else "fall"
        article = "an" if normalized_side == "YES" else "a"
        detail = (
            f"BTC momentum confirms the {label} entry."
            if passed
            else (
                f"BTC must show a regression {direction} of at least "
                f"${minimum:,.2f} over {lookback:g} seconds for {article} {label} entry."
            )
        )
    return {
        "enabled": enabled,
        "passed": passed,
        "current": float(movement) if movement is not None else None,
        "required": required,
        "lookback_seconds": lookback,
        "minimum_movement_dollars": minimum,
        "slope_dollars_per_second": state.get("slope_dollars_per_second"),
        "direction": state.get("direction"),
        "sample_count": state.get("sample_count", 0),
        "coverage_seconds": state.get("coverage_seconds", 0.0),
        "reliable": reliable,
        "detail": detail,
    }


def _unavailable(
    lookback_seconds: float,
    detail: str,
    *,
    sample_count: int = 0,
    coverage_seconds: float = 0.0,
) -> dict[str, Any]:
    return {
        "reliable": False,
        "lookback_seconds": lookback_seconds,
        "sample_count": sample_count,
        "coverage_seconds": coverage_seconds,
        "slope_dollars_per_second": None,
        "regression_movement_dollars": None,
        "direction": None,
        "detail": detail,
    }
