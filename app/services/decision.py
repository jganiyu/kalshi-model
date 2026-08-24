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
    selected_side: str | None = None,
    held_contracts: int = 0,
) -> Decision:
    side = str(selected_side or settings.get("selected_side", "YES")).upper()
    if side not in {"YES", "NO"}:
        side = "YES"
    side_label = "UP" if side == "YES" else "DOWN"
    bid = market.get(f"{side.lower()}_bid")
    ask = market.get(f"{side.lower()}_ask")
    implied = market_probability(bid, ask)
    selected_probability = (
        None
        if model_probability is None
        else float(model_probability) if side == "YES" else 1.0 - float(model_probability)
    )
    missing = selected_probability is None or implied is None or ask is None or bid is None
    if missing or not data_quality.get("reliable", False):
        detail = data_quality.get("reason", "Critical market data is incomplete.")
        return Decision(
            "HOLD", "DATA_UNRELIABLE", "Low", f"Hold {side_label}: {detail}",
            selected_probability, implied, None, None, None, None,
            0.0, 0.0, 0, side,
        )
    if not data_quality.get("trade_allowed", True):
        edge = float(selected_probability) - float(implied)
        return Decision(
            "HOLD",
            str(data_quality.get("reason_code") or "MODEL_UNCERTAINTY"),
            "Low",
            str(data_quality.get("reason") or f"Hold {side_label}: model uncertainty is too high."),
            selected_probability,
            implied,
            edge,
            None,
            None,
            None,
            0.0,
            0.0,
            0,
            side,
        )

    slippage = float(settings.get("slippage_cents", 0.5)) / 100
    buy_price = min(0.999, float(ask) + slippage)
    sell_price = max(0.001, float(bid) - slippage)
    probability = float(selected_probability)
    buy_fee = kalshi_fee(buy_price)
    sell_fee = kalshi_fee(sell_price)
    buy_ev = expected_value(probability, buy_price)
    sell_ev = sell_price - sell_fee - probability
    buy_edge = probability - buy_price - buy_fee
    sell_edge = sell_price - sell_fee - probability
    configured_buy_edge = float(settings.get("buy_edge", settings.get("min_edge", 0.05)))
    configured_sell_edge = float(settings.get("sell_edge", 0.03))
    hold_buffer = float(settings.get("hold_buffer", 0.005))
    action = "HOLD"
    if buy_edge >= configured_buy_edge + hold_buffer:
        action = "BUY"
    elif sell_edge >= configured_sell_edge + hold_buffer:
        action = "SELL"

    spread = max(0.0, float(ask) - float(bid))
    action_edge = buy_edge if action == "BUY" else sell_edge if action == "SELL" else probability - float(implied)
    confidence_basis = max(buy_edge, sell_edge, 0.0)
    sample_size = int(calibration.get("sample_size") or 0)
    calibration_error = calibration.get("calibration_error")
    confidence = "Low"
    if (
        confidence_basis >= float(settings.get("confidence_moderate_edge", 0.08))
        and spread <= float(settings.get("confidence_moderate_max_spread", 0.03))
        and model_variant_spread
        <= float(settings.get("confidence_moderate_max_variant_spread", 0.07))
    ):
        confidence = "Moderate"
    if (
        confidence_basis >= float(settings.get("confidence_high_edge", 0.12))
        and spread <= float(settings.get("confidence_high_max_spread", 0.02))
        and model_variant_spread
        <= float(settings.get("confidence_high_max_variant_spread", 0.04))
        and sample_size >= int(settings.get("confidence_high_min_samples", 150))
        and calibration_error is not None
        and calibration_error
        <= float(settings.get("confidence_high_max_calibration_error", 0.07))
    ):
        confidence = "High"

    if action == "SELL":
        holding_note = (
            f" The {held_contracts} held contract{'s' if held_contracts != 1 else ''} may be sold manually."
            if held_contracts
            else " This is informational; no paper sale can execute without holdings."
        )
        return Decision(
            "SELL", "SELL_EDGE", confidence,
            f"Sell {side_label}: the executable bid exceeds the model value by "
            f"{sell_edge * 100:.1f} points after fees and slippage.{holding_note}",
            probability, implied, sell_edge, sell_ev, sell_price, sell_fee,
            0.0, 0.0, held_contracts, side,
        )

    if action == "HOLD":
        return Decision(
            "HOLD", "NO_EDGE", confidence,
            f"Hold {side_label}: neither the ask nor bid clears the configured edge "
            "and hold buffer after fees and slippage.",
            probability, implied, action_edge, max(buy_ev, sell_ev), None, None,
            0.0, 0.0, 0, side,
        )

    available = market.get(f"{side.lower()}_ask_size")
    minimum_liquidity = int(settings.get("minimum_liquidity_contracts", 1))
    if available is not None and float(available) < minimum_liquidity:
        return Decision(
            "HOLD", "LIQUIDITY", "Low",
            f"Hold {side_label}: only {int(float(available))} contracts are available at the ask.",
            probability, implied, buy_edge, buy_ev, buy_price, buy_fee,
            0.0, 0.0, 0, side,
        )

    sizing = position_size(
        bankroll=bankroll,
        probability=probability,
        price=buy_price,
        fractional_kelly=float(settings.get("fractional_kelly", 0.25)),
        max_position_pct=float(settings.get("max_position_pct", 0.05)),
        max_risk_pct=float(settings.get("max_risk_per_trade_pct", 0.02)),
        available_contracts=available,
    )
    if sizing.contracts < 1:
        return Decision(
            "HOLD", "SIZE_TOO_SMALL", "Low",
            f"Hold {side_label}: the conservative risk allocation is too small for one executable contract.",
            probability, implied, buy_edge, buy_ev, buy_price, buy_fee,
            0.0, 0.0, 0, side,
        )
    explanation = (
        f"Buy {side_label}: the model value exceeds the executable ask by "
        f"{buy_edge * 100:.1f} points after fees and slippage."
    )
    return Decision(
        "BUY", "BUY_EDGE", confidence, explanation,
        probability, implied, buy_edge, buy_ev, buy_price, buy_fee,
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
