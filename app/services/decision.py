from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.domain import (
    expected_value,
    kalshi_fee,
    market_probability,
    position_size,
)
from app.services.kalshi_trading import normalize_order_price


def _executable_price(
    raw_price: float,
    slippage: float,
    action: str,
    settings: dict[str, Any],
    side: str,
    price_ranges: list[dict[str, Any]] | None,
) -> float:
    candidate = (
        min(0.999, raw_price + slippage)
        if action == "BUY"
        else max(0.001, raw_price - slippage)
    )
    if str(settings.get("trading_mode") or "PAPER").upper() in {"DEMO", "LIVE"}:
        return normalize_order_price(
            candidate, action, side=side, price_ranges=price_ranges
        )
    return candidate


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


def make_trade_assessment(
    *,
    up_probability: float | None,
    market: dict[str, Any],
    settings: dict[str, Any],
    side: str,
    data_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return action-specific economics without choosing a trading action."""
    normalized_side = "NO" if str(side).upper() == "NO" else "YES"
    probability = (
        None
        if up_probability is None
        else float(up_probability)
        if normalized_side == "YES"
        else 1.0 - float(up_probability)
    )
    bid = market.get(f"{normalized_side.lower()}_bid")
    ask = market.get(f"{normalized_side.lower()}_ask")
    bid_value = float(bid) if bid is not None else None
    ask_value = float(ask) if ask is not None else None
    implied = market_probability(bid_value, ask_value)
    slippage = float(settings.get("slippage_cents", 0.5)) / 100

    def action_values(action: str) -> dict[str, Any]:
        raw_price = ask_value if action == "BUY" else bid_value
        if raw_price is None or probability is None:
            return {
                "action": action,
                "executable_price": None,
                "raw_price": raw_price,
                "fee_per_contract": None,
                "slippage": slippage,
                "net_edge": None,
                "expected_value": None,
            }
        executable = _executable_price(
            raw_price,
            slippage,
            action,
            settings,
            normalized_side,
            market.get("price_ranges"),
        )
        fee = kalshi_fee(executable)
        net = (
            probability - executable - fee
            if action == "BUY"
            else executable - fee - probability
        )
        return {
            "action": action,
            "executable_price": executable,
            "raw_price": raw_price,
            "fee_per_contract": fee,
            "slippage": slippage,
            "net_edge": net,
            "expected_value": net,
        }

    quality = data_quality or {}
    return {
        "side": normalized_side,
        "model_probability": probability,
        "market_probability": implied,
        "spread": (
            max(0.0, ask_value - bid_value)
            if ask_value is not None and bid_value is not None
            else None
        ),
        "ask_size": market.get(f"{normalized_side.lower()}_ask_size"),
        "price_ranges": market.get("price_ranges"),
        "data_reliable": bool(quality.get("reliable", False)),
        "trade_allowed": bool(
            quality.get("reliable", False) and quality.get("trade_allowed", True)
        ),
        "quality_reason": quality.get("reason"),
        "buy": action_values("BUY"),
        "sell": action_values("SELL"),
    }


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
    entry_gates_released: bool = False,
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
    buy_price = _executable_price(
        float(ask), slippage, "BUY", settings, side, market.get("price_ranges")
    )
    sell_price = _executable_price(
        float(bid), slippage, "SELL", settings, side, market.get("price_ranges")
    )
    probability = float(selected_probability)
    buy_fee = kalshi_fee(buy_price)
    sell_fee = kalshi_fee(sell_price)
    buy_ev = expected_value(probability, buy_price)
    sell_ev = sell_price - sell_fee - probability
    buy_edge = probability - buy_price - buy_fee
    sell_edge = sell_price - sell_fee - probability
    configured_buy_edge = float(settings.get("buy_edge", settings.get("min_edge", 0.05)))
    minimum_buy_probability = float(settings.get("minimum_buy_probability", 0.55))
    configured_sell_edge = float(settings.get("sell_edge", 0.03))
    hold_buffer = float(settings.get("hold_buffer", 0.005))
    action = "HOLD"
    if buy_edge >= configured_buy_edge + hold_buffer:
        action = "BUY" if probability >= minimum_buy_probability else "SPECULATIVE"
    elif sell_edge >= configured_sell_edge + hold_buffer:
        action = "SELL"

    spread = max(0.0, float(ask) - float(bid))
    action_edge = buy_edge if action in {"BUY", "SPECULATIVE"} else sell_edge if action == "SELL" else probability - float(implied)
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

    if action == "SPECULATIVE":
        return Decision(
            "SPECULATIVE", "LOW_WIN_PROBABILITY", confidence,
            f"Speculative {side_label} value: the estimated win chance is "
            f"{probability * 100:.1f}%, with a {(1.0 - probability) * 100:.1f}% chance "
            f"of expiring worthless. Net edge is {buy_edge * 100:.1f} points after "
            "fees and slippage, but this is not a Buy signal.",
            probability, implied, buy_edge, buy_ev, buy_price, buy_fee,
            0.0, 0.0, 0, side,
        )

    available = market.get(f"{side.lower()}_ask_size")
    minimum_liquidity = (
        1
        if entry_gates_released
        else int(settings.get("minimum_liquidity_contracts", 1))
    )
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
        max_risk_pct=float(settings.get("max_risk_per_trade_pct", 0.03)),
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
        f"Buy {side_label}: the estimated win chance is {probability * 100:.1f}%, "
        f"with a {(1.0 - probability) * 100:.1f}% chance of expiring worthless. "
        f"Net edge is {buy_edge * 100:.1f} points after fees and slippage."
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
