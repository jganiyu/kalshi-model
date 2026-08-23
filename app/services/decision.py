from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.domain import (
    expected_value,
    kalshi_fee,
    market_probability,
    position_size,
)


@dataclass(frozen=True)
class Decision:
    signal: str
    reason_code: str
    confidence: str
    explanation: str
    model_probability: float | None
    market_probability: float | None
    edge: float | None
    expected_value: float | None
    executable_price: float | None
    fee_per_contract: float | None
    suggested_fraction: float
    suggested_dollars: float
    suggested_contracts: int
    side: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_decision(
    *,
    model_probability: float | None,
    market: dict[str, Any],
    settings: dict[str, Any],
    bankroll: float,
    data_quality: dict[str, Any],
    calibration: dict[str, Any],
    model_variant_spread: float,
) -> Decision:
    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")
    implied = market_probability(yes_bid, yes_ask)
    missing = model_probability is None or implied is None or yes_ask is None or no_ask is None
    if missing or not data_quality.get("reliable", False):
        detail = data_quality.get("reason", "Critical market data is incomplete.")
        return Decision(
            "NO TRADE", "DATA_UNRELIABLE", "Low", f"Data unreliable: {detail}",
            model_probability, implied, None, None, None, None, 0.0, 0.0, 0, None,
        )
    if not data_quality.get("trade_allowed", True):
        edge = float(model_probability) - float(implied)
        return Decision(
            "NO TRADE",
            str(data_quality.get("reason_code") or "MODEL_UNCERTAINTY"),
            "Low",
            str(data_quality.get("reason") or "Hold: model uncertainty is too high."),
            model_probability,
            implied,
            edge,
            None,
            None,
            None,
            0.0,
            0.0,
            0,
            None,
        )

    slippage = float(settings.get("slippage_cents", 0.5)) / 100
    executable_yes = min(0.999, float(yes_ask) + slippage)
    executable_no = min(0.999, float(no_ask) + slippage)
    yes_probability = float(model_probability)
    no_probability = 1.0 - yes_probability
    yes_ev = expected_value(yes_probability, executable_yes)
    no_ev = expected_value(no_probability, executable_no)
    side = "YES" if yes_ev >= no_ev else "NO"
    side_label = "UP" if side == "YES" else "DOWN"
    probability = yes_probability if side == "YES" else no_probability
    price = executable_yes if side == "YES" else executable_no
    ev = yes_ev if side == "YES" else no_ev
    edge = (yes_probability - implied) if side == "YES" else (implied - yes_probability)
    executable_edge = probability - price
    min_edge = float(settings.get("min_edge", 0.05))

    if ev <= 0 or executable_edge < min_edge:
        return Decision(
            "NO TRADE", "NO_EDGE", "Low",
            "The apparent probability advantage is not large enough after spread, fees, and slippage.",
            model_probability, implied, edge, ev, price, kalshi_fee(price), 0.0, 0.0, 0, side,
        )

    available = market.get(f"{side.lower()}_ask_size")
    sizing = position_size(
        bankroll=bankroll,
        probability=probability,
        price=price,
        fractional_kelly=float(settings.get("fractional_kelly", 0.25)),
        max_position_pct=float(settings.get("max_position_pct", 0.05)),
        max_risk_pct=float(settings.get("max_risk_per_trade_pct", 0.02)),
        available_contracts=available,
    )
    if sizing.contracts < 1:
        return Decision(
            "NO TRADE", "SIZE_TOO_SMALL", "Low",
            "The conservative risk allocation is too small for one executable contract.",
            model_probability, implied, edge, ev, price, kalshi_fee(price), 0.0, 0.0, 0, side,
        )

    spread = float(market.get("spread") or 1.0)
    sample_size = int(calibration.get("sample_size") or 0)
    calibration_error = calibration.get("calibration_error")
    confidence = "Low"
    if edge >= 0.08 and spread <= 0.03 and model_variant_spread <= 0.07:
        confidence = "Moderate"
    if (
        edge >= 0.12
        and spread <= 0.02
        and model_variant_spread <= 0.04
        and sample_size >= 150
        and calibration_error is not None
        and calibration_error <= 0.07
    ):
        confidence = "High"
    explanation = (
        f"The model prices {side_label} above its executable cost by "
        f"{executable_edge * 100:.1f} points after a conservative slippage allowance."
    )
    return Decision(
        f"TRADE {side}", "POSITIVE_EV", confidence, explanation,
        model_probability, implied, edge, ev, price, kalshi_fee(price),
        sizing.bankroll_fraction, sizing.dollar_amount, sizing.contracts, side,
    )


def material_change(previous: dict[str, Any] | None, current: Decision, min_edge: float) -> str | None:
    if previous is None:
        return "initial signal"
    if previous.get("signal") != current.signal:
        return f"signal changed: {previous.get('signal')} -> {current.signal}"
    if previous.get("confidence") != current.confidence and current.confidence in {"Moderate", "High"}:
        return f"confidence changed: {previous.get('confidence')} -> {current.confidence}"
    previous_edge = previous.get("edge")
    if previous_edge is not None and current.edge is not None:
        if (previous_edge < min_edge <= current.edge) or (previous_edge >= min_edge > current.edge):
            return "edge crossed the configured threshold"
    if previous.get("reason_code") != current.reason_code and current.reason_code == "RISK_LIMIT":
        return "risk control blocked a trade"
    return None
