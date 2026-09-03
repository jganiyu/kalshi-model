from __future__ import annotations

import abc
import asyncio
import json
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Database
from app.domain import iso_now, kalshi_fee, parse_time, settlement_margin
from app.services.kalshi_trading import (
    AmbiguousSubmissionError,
    KalshiTradingClient,
    KalshiTradingError,
    book_to_outcome,
    normalize_order_price,
)
from app.services.trade_review import broker_trade_ref, review_metadata
from app.services.paper import PaperTradingService


TRADING_MODES = {"PAPER", "DEMO", "LIVE"}
OPEN_ORDER_STATES = {
    "INTENT_CREATED",
    "SUBMITTING",
    "ACKNOWLEDGED",
    "RESTING",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
    "RECONCILIATION_REQUIRED",
}
FINAL_ORDER_STATES = {
    "FILLED", "CANCELED", "REJECTED", "EXPIRED", "SETTLED",
    # Exchange evidence says the position is flat, but a manual close may
    # mean the fill cannot be attributed to this timed-out client ID.
    "RESOLVED_EXTERNALLY",
}
NON_COUNTED_DAILY_INTENT_STATES = {"INTENT_CREATED", "REJECTED"}


def normalize_mode(value: Any) -> str:
    mode = str(value or "PAPER").upper()
    if mode not in TRADING_MODES:
        raise ValueError("Trading mode must be Paper, Demo, or Live.")
    return mode


@dataclass(frozen=True)
class OrderIntent:
    mode: str
    ticker: str
    side: str
    action: str
    contracts: int
    limit_price: float
    strategy: str
    source: str
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    post_only: bool = False
    time_in_force: str = "good_till_canceled"
    cancel_order_on_pause: bool = True
    price_ranges: list[dict[str, Any]] | None = None
    stop_loss_price: float | None = None
    target_exit_price: float | None = None
    fallback_exit_mode: str | None = None
    fallback_exit_seconds: float | None = None
    cancel_after_seconds: float | None = None
    decision_snapshot: dict[str, Any] = field(default_factory=dict)
    risk_snapshot: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_mode(self.mode))
        object.__setattr__(self, "side", str(self.side).upper())
        object.__setattr__(self, "action", str(self.action).upper())
        object.__setattr__(self, "time_in_force", str(self.time_in_force).lower())
        if self.mode == "PAPER":
            return
        if self.side not in {"YES", "NO"}:
            raise ValueError("Choose Up or Down.")
        if self.action not in {"BUY", "SELL"}:
            raise ValueError("Choose Buy or Sell.")
        if self.time_in_force not in {
            "good_till_canceled", "immediate_or_cancel", "fill_or_kill"
        }:
            raise ValueError("Choose a supported order time in force.")
        if isinstance(self.contracts, bool) or int(self.contracts) < 1:
            raise ValueError("Contracts must be a positive whole number.")
        object.__setattr__(
            self,
            "limit_price",
            normalize_order_price(
                float(self.limit_price),
                self.action,
                side=self.side,
                price_ranges=self.price_ranges,
            ),
        )


class Broker(abc.ABC):
    mode: str

    @abc.abstractmethod
    def portfolio(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def submit(self, intent: OrderIntent) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def cancel(self, order_id: str | int) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def reconcile(self) -> dict[str, Any]: ...


class PaperBroker(Broker):
    mode = "PAPER"

    def __init__(self, service: PaperTradingService):
        self.service = service

    def portfolio(self) -> dict[str, Any]:
        return {"mode": self.mode, **self.service.portfolio()}

    async def submit(self, intent: OrderIntent) -> dict[str, Any]:
        raise ValueError("Paper orders use the existing paper order path.")

    async def cancel(self, order_id: str | int) -> dict[str, Any]:
        if not self.service.cancel_order(int(order_id)):
            raise ValueError("That paper order is no longer open.")
        return {"canceled": int(order_id)}

    async def reconcile(self) -> dict[str, Any]:
        return {"mode": self.mode, "reconciled": True}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dollars(payload: dict[str, Any], fixed_key: str, cents_key: str) -> float:
    if payload.get(fixed_key) not in (None, ""):
        return _number(payload.get(fixed_key))
    return _number(payload.get(cents_key)) / 100.0


def _safe_json(payload: dict[str, Any]) -> str:
    blocked = {"private_key", "signature", "authorization", "auth", "api_key"}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("[redacted]" if key.lower() in blocked else scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(payload), sort_keys=True, default=str)


def _exchange_error_detail(exc: Exception) -> dict[str, Any]:
    """Persist useful exchange diagnostics without retaining secrets."""
    detail: dict[str, Any] = {"message": str(exc)}
    if isinstance(exc, KalshiTradingError):
        detail["transport"] = bool(exc.transport)
        if exc.status_code is not None:
            detail["status_code"] = exc.status_code
        if exc.code:
            detail["code"] = exc.code
        if exc.details not in (None, ""):
            detail["details"] = exc.details
    return json.loads(_safe_json(detail))


def _threshold_exit_record(position: dict[str, Any]) -> dict[str, Any]:
    """Return safe threshold-exit evidence for the ledger and review APIs."""
    try:
        details = json.loads(position.get("threshold_exit_error_details_json") or "null")
    except (TypeError, json.JSONDecodeError):
        details = None
    return {
        "enabled": bool(position.get("threshold_breach_enabled")),
        "buffer_dollars": position.get("threshold_exit_buffer"),
        "exit_level": position.get("threshold_exit_level"),
        "trigger_btc_proxy": position.get("threshold_trigger_btc_proxy"),
        "trigger_threshold": position.get("threshold_trigger_threshold"),
        "triggered_at": position.get("threshold_triggered_at"),
        "status": position.get("threshold_exit_status"),
        "reason": position.get("threshold_exit_block_reason"),
        "last_attempt_at": position.get("threshold_exit_last_attempt_at"),
        "last_attempt_bid": position.get("threshold_exit_last_attempt_bid"),
        "error_code": position.get("threshold_exit_error_code"),
        "error_details": details,
        "remaining_contracts": position.get("contracts"),
    }


def fill_aggregate(
    fills: list[dict[str, Any]], requested_contracts: float
) -> dict[str, float | str | None]:
    filled = sum(_number(row.get("contracts")) for row in fills)
    value = sum(
        _number(row.get("contracts")) * _number(row.get("price")) for row in fills
    )
    fees = sum(_number(row.get("fee")) for row in fills)
    remaining = max(0.0, float(requested_contracts) - filled)
    return {
        "filled_contracts": filled,
        "remaining_contracts": remaining,
        "average_fill_price": value / filled if filled else None,
        "fees": fees,
        "status": (
            "FILLED"
            if requested_contracts > 0 and remaining <= 1e-9
            else "PARTIALLY_FILLED"
        ),
    }


class KalshiBroker(Broker):
    def __init__(
        self,
        mode: str,
        db: Database,
        client: KalshiTradingClient | None = None,
    ):
        normalized = normalize_mode(mode)
        if normalized == "PAPER":
            raise ValueError("KalshiBroker only supports Demo or Live.")
        self.mode = normalized
        self.db = db
        self.client = client
        self.session_armed = False
        self.automatic_armed = False
        # Dashboard snapshots are produced independently of the arm/disarm
        # endpoints.  This monotonically increasing revision lets clients
        # reject an older snapshot after a confirmed local safety action.
        self._arming_generation = 0
        self._last_connection_at: float | None = None
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_generation = 0
        self._reconciliation_paused = False
        # A private stream is independent evidence that the authenticated
        # Kalshi connection is alive. A single slow REST reconciliation
        # endpoint must not falsely label that healthy stream "unreachable".
        self._private_stream_connected = False

    def set_client(self, client: KalshiTradingClient | None) -> None:
        self.client = client
        self._private_stream_connected = False
        self.disarm("Credentials changed.")
        self._update_mode_state(
            connected=False,
            authenticated=False,
            reconciled=False,
            reconciliation_required=True,
            last_error=None if client else "Credentials are not configured.",
        )

    def _latest_account_snapshot(self) -> dict[str, Any]:
        return self.db.fetch_one(
            "SELECT * FROM broker_account_snapshots WHERE mode=? ORDER BY id DESC LIMIT 1",
            (self.mode,),
        ) or {}

    def balance_breakdown(self) -> list[dict[str, Any]]:
        account = self._latest_account_snapshot()
        raw = account.get("raw_json")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, json.JSONDecodeError):
            payload = {}
        rows: list[dict[str, Any]] = []
        for item in payload.get("balance_breakdown") or []:
            if not isinstance(item, dict):
                continue
            try:
                exchange_index = int(item.get("exchange_index"))
            except (TypeError, ValueError):
                continue
            balance = (
                _number(item.get("balance_dollars"))
                if item.get("balance_dollars") not in (None, "")
                else _number(item.get("balance"))
            )
            rows.append({"exchange_index": exchange_index, "balance": balance})
        return rows

    def available_balance_for_exchange(self, exchange_index: int | None) -> float:
        account = self._latest_account_snapshot()
        total = _number(account.get("available_balance"))
        if exchange_index is None:
            return total
        breakdown = self.balance_breakdown()
        if not breakdown:
            return total
        return next(
            (
                _number(item.get("balance"))
                for item in breakdown
                if item.get("exchange_index") == int(exchange_index)
            ),
            0.0,
        )

    def disarm(self, reason: str | None = None) -> None:
        self.session_armed = False
        self.automatic_armed = False
        self._arming_generation += 1
        if reason:
            self._audit("DISARMED", {"reason": reason})

    def arm(self, *, confirmation: str, automatic: bool = False) -> dict[str, Any]:
        state = self.mode_state()
        if self.mode == "LIVE":
            if confirmation.strip() != "ARM LIVE TRADING":
                raise ValueError('Type "ARM LIVE TRADING" to continue.')
            if not state.get("demo_verified_at"):
                raise ValueError("Complete Demo readiness verification before arming Live.")
            if not state.get("limits_reviewed_at"):
                raise ValueError("Review and save the Live hard limits before first arming.")
        elif confirmation.strip() != "ARM DEMO TRADING":
            raise ValueError('Type "ARM DEMO TRADING" to continue.')
        if not state.get("authenticated") or not state.get("reconciled"):
            raise ValueError("Authenticate and reconcile this account before arming.")
        if state.get("kill_switch"):
            raise ValueError("Release the kill switch before arming.")
        self.session_armed = True
        self.automatic_armed = bool(automatic)
        self._arming_generation += 1
        self._audit("ARMED", {"automatic": self.automatic_armed})
        return self.readiness()

    def set_automatic_armed(self, enabled: bool) -> dict[str, Any]:
        if enabled and not self.session_armed:
            raise ValueError(f"Arm the {self.mode.title()} session first.")
        requested = bool(enabled)
        if self.automatic_armed != requested:
            self._arming_generation += 1
        self.automatic_armed = requested
        self._audit("AUTOMATIC_ARMING_CHANGED", {"enabled": self.automatic_armed})
        return self.readiness()

    def mark_limits_reviewed(self) -> None:
        self._update_mode_state(limits_reviewed_at=iso_now())

    def mark_demo_verified(self) -> None:
        if self.mode != "DEMO":
            raise ValueError("Demo verification can only be completed in Demo.")
        verified_at = iso_now()
        self._update_mode_state(demo_verified_at=verified_at)
        self.db.execute(
            "UPDATE broker_mode_state SET demo_verified_at=?,updated_at=? WHERE mode='LIVE'",
            (verified_at, iso_now()),
        )
        self._audit("DEMO_VERIFIED", {"verified_at": verified_at})

    def mode_state(self) -> dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT * FROM broker_mode_state WHERE mode=?", (self.mode,)
        )
        return row or {
            "mode": self.mode,
            "connected": 0,
            "authenticated": 0,
            "reconciled": 0,
            "reconciliation_required": 1,
            "kill_switch": 0,
        }

    def readiness(self) -> dict[str, Any]:
        state = self.mode_state()
        credentials = self.client is not None
        reasons: list[str] = []
        if not credentials:
            reasons.append(f"{self.mode.title()} trading credentials are not configured.")
        elif not state.get("connected"):
            reasons.append("Reconnecting to Kalshi.")
        if not state.get("authenticated"):
            reasons.append("Account authentication is not verified.")
        if not state.get("reconciled") or state.get("reconciliation_required"):
            reasons.append(self._reconciliation_blocker_message())
        if state.get("kill_switch"):
            reasons.append("The kill switch is active.")
        if not self.session_armed:
            reasons.append(f"The {self.mode.title()} session is disarmed.")
        protective_reasons: list[str] = []
        if not credentials:
            protective_reasons.append(
                f"{self.mode.title()} trading credentials are not configured."
            )
        # A reconciliation request can be delayed long enough for its
        # signature to expire.  That stale 401 is not evidence that a new,
        # priority-signed reduce-only request cannot authenticate.  Keep
        # entries behind the account-auth gate above, but let confirmed
        # exposure use the protective lane and judge its fresh request.
        if state.get("kill_switch"):
            protective_reasons.append("The kill switch is active.")
        if not self.session_armed:
            protective_reasons.append(f"The {self.mode.title()} session is disarmed.")
        return {
            "mode": self.mode,
            "credentials_configured": credentials,
            "connected": bool(state.get("connected")),
            "authenticated": bool(state.get("authenticated")),
            "reconciled": bool(state.get("reconciled")),
            "reconciliation_required": bool(state.get("reconciliation_required")),
            "session_armed": self.session_armed,
            "automatic_armed": self.automatic_armed,
            "arming_generation": self._arming_generation,
            "kill_switch": bool(state.get("kill_switch")),
            "demo_verified": bool(state.get("demo_verified_at")),
            "limits_reviewed": bool(state.get("limits_reviewed_at")),
            "ready_for_manual": not reasons,
            "ready_for_automatic": not reasons and self.automatic_armed,
            # Protective exits intentionally use a narrower lane.  A failed
            # account-wide reconciliation blocks new exposure, but must not
            # strand a known position when a reduce-only order can be sized
            # from a targeted exchange read or durable fill ledger.
            "ready_for_protective_exit": not protective_reasons,
            "protective_exit_degraded": bool(
                not state.get("reconciled") or state.get("reconciliation_required")
            ),
            "protective_exit_blocker": (
                protective_reasons[0] if protective_reasons else None
            ),
            "blocker": reasons[0] if reasons else None,
            "automatic_blocker": (
                reasons[0]
                if reasons else None if self.automatic_armed
                else f"Automatic {self.mode.title()} trading is disarmed."
            ),
            "warnings": reasons,
            "last_reconciled_at": state.get("last_reconciled_at"),
            "last_error": state.get("last_error"),
            "connection_diagnostic": self._connection_diagnostic(state),
        }

    def _connection_diagnostic(self, state: dict[str, Any]) -> str | None:
        """Return the current, sanitized REST failure class for the Trading UI."""
        if not state.get("reconciliation_required") or not state.get("last_error"):
            return None
        row = self.db.fetch_one(
            """
            SELECT detail_json FROM broker_audit_events
            WHERE mode=? AND event_type='RECONCILIATION_REQUEST_FAILED'
            ORDER BY id DESC LIMIT 1
            """,
            (self.mode,),
        )
        try:
            detail = json.loads((row or {}).get("detail_json") or "{}")
            exchange = detail.get("exchange_error") or {}
            details = exchange.get("details") or {}
            failure = str(details.get("failure_kind") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            failure = ""
        labels = {
            "connect_timeout": "Kalshi connection timed out",
            "connect_error": "Kalshi connection failed",
            "read_timeout": "Kalshi response timed out",
            "read_error": "Kalshi response failed",
            "write_timeout": "Kalshi request timed out",
            "write_error": "Kalshi request failed",
            "pool_timeout": "Kalshi request queue timed out",
        }
        return labels.get(failure)

    def _reconciliation_blocker_message(self) -> str:
        """Give an actionable ETA only when an uncertain current market exists."""
        pending = self.db.fetch_one(
            """
            SELECT i.ticker,m.status AS market_status
            FROM broker_order_intents i
            LEFT JOIN markets m ON m.ticker=i.ticker
            WHERE i.mode=? AND i.status='RECONCILIATION_REQUIRED'
            ORDER BY i.updated_at DESC LIMIT 1
            """,
            (self.mode,),
        ) or {}
        if str(pending.get("market_status") or "").lower() == "active":
            return "Verifying timed-out order · entries resume next round."
        return "Reconciling Kalshi account activity."

    def portfolio(self) -> dict[str, Any]:
        account = self._latest_account_snapshot()
        positions = self.db.fetch_all(
            "SELECT * FROM broker_positions WHERE mode=? AND status='open' ORDER BY updated_at DESC",
            (self.mode,),
        )
        for position in positions:
            # A non-zero Kalshi position is filled exposure awaiting an exit or
            # exchange settlement; it is not a resting order.
            position["display_status"] = "UNSETTLED"
        orders = self.db.fetch_all(
            "SELECT * FROM broker_orders WHERE mode=? ORDER BY updated_at DESC LIMIT 100",
            (self.mode,),
        )
        fills = self.db.fetch_all(
            "SELECT * FROM broker_fills WHERE mode=? ORDER BY filled_at DESC LIMIT 100",
            (self.mode,),
        )
        fee_total = self.db.fetch_one(
            "SELECT COALESCE(SUM(fee),0) AS amount FROM broker_fills WHERE mode=?",
            (self.mode,),
        ) or {}
        intents = self.db.fetch_all(
            "SELECT * FROM broker_order_intents WHERE mode=? ORDER BY created_at DESC LIMIT 100",
            (self.mode,),
        )
        settlements = self.db.fetch_all(
            "SELECT * FROM broker_settlements WHERE mode=? ORDER BY settled_at DESC LIMIT 100",
            (self.mode,),
        )
        available = _number(account.get("available_balance"))
        portfolio_value = _number(account.get("portfolio_value"))
        allocated = self.allocated_capital()
        cap = self.allocation_cap(available, portfolio_value)
        open_orders = [order for order in orders if order.get("status") in OPEN_ORDER_STATES]
        settings = self.db.settings()
        profit_take_enabled = bool(settings.get("global_profit_take_enabled", True))
        threshold_breach_enabled = bool(
            settings.get("threshold_breach_exit_enabled", True)
        )
        stop_positions = [
            position for position in positions
            if position.get("stop_loss_price") is not None
        ]
        managed_positions = [
            position for position in positions if position.get("contracts")
        ]
        readiness = self.readiness()
        strategy_results = self.strategy_results()
        realized_row = self.db.fetch_one(
            "SELECT COALESCE(SUM(realized_pnl),0) AS amount FROM broker_positions WHERE mode=?",
            (self.mode,),
        ) or {}
        realized_pnl = _number(realized_row.get("amount"))
        account_equity = max(available, portfolio_value)
        return {
            "mode": self.mode,
            "available_cash": available,
            "current_bankroll": account_equity,
            "portfolio_value": portfolio_value,
            "balance_breakdown": self.balance_breakdown(),
            "allocated_capital": allocated,
            "allocation_cap": cap,
            "remaining_allocation": max(0.0, cap - allocated),
            "positions": positions,
            "orders": orders,
            "open_orders": open_orders,
            "fills": fills,
            "ledger": self.trade_ledger(),
            "intents": intents,
            "settlements": settlements,
            "open_positions": len(positions),
            "open_order_count": len(open_orders),
            # Activity is bounded to 100 rows; the account fee total is not.
            "actual_fees": _number(fee_total.get("amount")),
            "realized_pnl": realized_pnl,
            "strategy_results": strategy_results,
            "automatic_trading_enabled": self.automatic_armed,
            "automatic_trade_allowed": readiness["ready_for_automatic"],
            "automatic_trade_block_reason": readiness["automatic_blocker"],
            "readiness": readiness,
            "risk_state": self.risk_state(),
            "reconciliation_state": {
                "reconciled": readiness["reconciled"],
                "required": readiness["reconciliation_required"],
                "last_reconciled_at": readiness["last_reconciled_at"],
                "last_error": readiness["last_error"],
            },
            "protective_exit_state": {
                "ready": readiness["ready_for_protective_exit"],
                "degraded": readiness["protective_exit_degraded"],
                "blocker": readiness["protective_exit_blocker"],
                "warning": (
                    "New entries are blocked until reconciliation completes. "
                    "Protective exits use only confirmed position evidence."
                    if readiness["protective_exit_degraded"] else None
                ),
            },
            "stop_loss_state": {
                "active_positions": len(stop_positions),
                "positions": [
                    {
                        "ticker": row.get("ticker"),
                        "side": row.get("side"),
                        "trigger_price": row.get("stop_loss_price"),
                    }
                    for row in stop_positions
                ],
                "warning": (
                    "Stop-loss execution requires the Kalshi Model to remain running and connected."
                ),
            },
            "profit_take_state": {
                "enabled": profit_take_enabled,
                "trigger_price": (
                    float(settings.get("global_profit_take_price", 0.99))
                    if profit_take_enabled else None
                ),
                "managed_positions": len(managed_positions),
                "warning": (
                    "Profit taking requires the app to remain running, connected, "
                    "authenticated, reconciled, and armed."
                ),
            },
            "threshold_breach_exit_state": {
                "enabled": threshold_breach_enabled,
                "buffer_dollars": float(
                    settings.get("threshold_breach_exit_buffer_dollars", 0.0)
                ),
                "managed_positions": len(managed_positions),
                "warning": (
                    "This is a side-aware exit based on the BTC proxy versus To Beat. "
                    "It does not use contract price as the trigger."
                ),
            },
        }

    def trade_ledger(self) -> list[dict[str, Any]]:
        fills = self.db.fetch_all(
            "SELECT * FROM broker_fills WHERE mode=? ORDER BY filled_at ASC,id ASC",
            (self.mode,),
        )
        settlements = {
            str(row.get("ticker")): row
            for row in self.db.fetch_all(
                "SELECT * FROM broker_settlements WHERE mode=?", (self.mode,)
            )
        }
        entry_evidence_rows = self.db.fetch_all(
            """
            SELECT ticker,side,margin_volatility_index,margin_cushion_ratio
            FROM broker_order_intents
            WHERE mode=? AND action='BUY' AND source='automatic'
            ORDER BY created_at ASC,id ASC
            """,
            (self.mode,),
        )
        entry_evidence = {
            (str(row.get("ticker")), str(row.get("side"))): row
            for row in entry_evidence_rows
        }
        settlement_margin_rows = self.db.fetch_all(
            """
            SELECT s.ticker,
                   COALESCE(
                     json_extract(s.raw_json,'$.expiration_value'),
                     json_extract(m.raw_json,'$.expiration_value')
                   ) AS settlement_price,
                   m.strike
            FROM settlements s LEFT JOIN markets m ON m.ticker=s.ticker
            """
        )
        settlement_margins: dict[str, float] = {}
        for row in settlement_margin_rows:
            margin = settlement_margin(
                row.get("settlement_price"), row.get("strike")
            )
            if margin is not None:
                settlement_margins[str(row["ticker"])] = margin
        protection_rows = self.db.fetch_all(
            """
            SELECT * FROM broker_positions
            WHERE mode=? AND threshold_breach_enabled IS NOT NULL
            """,
            (self.mode,),
        )
        protections = {
            (str(row.get("ticker")), str(row.get("side"))): _threshold_exit_record(row)
            for row in protection_rows
        }
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for fill in fills:
            grouped[(str(fill.get("ticker")), str(fill.get("side")))].append(fill)

        ledger: list[dict[str, Any]] = []
        for key, rows in grouped.items():
            buys = [row for row in rows if row.get("action") == "BUY"]
            if not buys:
                continue
            total_contracts = sum(_number(row.get("contracts")) for row in buys)
            if total_contracts <= 0:
                continue
            entry_value = sum(
                _number(row.get("contracts")) * _number(row.get("price"))
                for row in buys
            )
            lots: deque[list[float]] = deque()
            realized = 0.0
            last_exit: dict[str, Any] | None = None
            for row in rows:
                quantity = max(0.0, _number(row.get("contracts")))
                if quantity <= 0:
                    continue
                fee = _number(row.get("fee"))
                if row.get("action") == "BUY":
                    lots.append(
                        [quantity, _number(row.get("price")) + fee / quantity]
                    )
                    continue
                remaining = quantity
                matched_cost = 0.0
                matched = 0.0
                while remaining > 1e-9 and lots:
                    lot = lots[0]
                    take = min(remaining, lot[0])
                    matched += take
                    matched_cost += take * lot[1]
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 1e-9:
                        lots.popleft()
                if matched:
                    realized += (
                        matched * _number(row.get("price"))
                        - fee * matched / quantity
                        - matched_cost
                    )
                    last_exit = row

            remaining_contracts = sum(lot[0] for lot in lots)
            settlement = settlements.get(key[0])
            settlement_result = str(
                (settlement or {}).get("market_result") or ""
            ).upper()
            latest_buy = buys[-1]
            status = "OPEN"
            realized_pnl: float | None = None
            activity_at = latest_buy.get("filled_at")
            available_after = latest_buy.get("available_cash_after")
            position_won: int | None = None
            if (
                settlement and remaining_contracts > 1e-9
                and settlement_result in {"YES", "NO"}
            ):
                position_won = int(key[1] == settlement_result)
                remaining_cost = sum(lot[0] * lot[1] for lot in lots)
                payout = remaining_contracts if position_won else 0.0
                realized += payout - remaining_cost
                status = "SETTLED"
                realized_pnl = realized
                activity_at = settlement.get("settled_at")
                available_after = settlement.get("available_cash_after")
            elif remaining_contracts <= 1e-9:
                status = "CLOSED"
                realized_pnl = realized
                if last_exit:
                    activity_at = last_exit.get("filled_at")
                    available_after = last_exit.get("available_cash_after")
            elif remaining_contracts < total_contracts:
                status = "PARTIALLY CLOSED"
                realized_pnl = realized
                if last_exit:
                    activity_at = last_exit.get("filled_at")
                    available_after = last_exit.get("available_cash_after")

            strategies = {
                str(row.get("strategy")) for row in buys if row.get("strategy")
            }
            sources = {str(row.get("source")) for row in buys if row.get("source")}
            strategy = (
                next(iter(strategies)) if len(strategies) == 1
                else "MIXED" if strategies else "EXTERNAL"
            )
            source = (
                next(iter(sources)) if len(sources) == 1
                else "mixed" if sources else "external"
            )
            trade_ref = broker_trade_ref(self.mode, key[0], key[1])
            ledger.append(
                {
                    "ticker": key[0],
                    "side": key[1],
                    "opened_at": buys[0].get("filled_at"),
                    "activity_at": activity_at,
                    "price": entry_value / total_contracts,
                    "contracts": total_contracts,
                    "strategy": strategy,
                    "source": source,
                    "exit_reason": (
                        str(last_exit.get("source") or "").upper()
                        if last_exit else None
                    ),
                    "status": status,
                    "display_status": "UNSETTLED" if status == "OPEN" else status,
                    "realized_pnl": realized_pnl,
                    "available_cash_after": available_after,
                    "settlement_margin": settlement_margins.get(key[0]),
                    "market_result": (settlement or {}).get("market_result"),
                    "position_won": position_won,
                    "margin_volatility_index": (
                        entry_evidence.get(key) or {}
                    ).get("margin_volatility_index"),
                    "margin_cushion_ratio": (
                        entry_evidence.get(key) or {}
                    ).get("margin_cushion_ratio"),
                    "threshold_breach_exit": protections.get(key),
                    **review_metadata(self.db, self.mode, trade_ref, status),
                }
            )
        return sorted(
            ledger,
            key=lambda row: str(row.get("activity_at") or row.get("opened_at") or ""),
            reverse=True,
        )[:100]

    def risk_state(self) -> dict[str, Any]:
        settings = self.db.settings()
        prefix = self.mode.lower()
        today = datetime.now(UTC).date().isoformat()
        first = self.db.fetch_one(
            """
            SELECT available_balance,portfolio_value FROM broker_account_snapshots
            WHERE mode=? AND observed_at LIKE ? ORDER BY observed_at ASC,id ASC LIMIT 1
            """,
            (self.mode, f"{today}%"),
        ) or {}
        latest = self.db.fetch_one(
            """
            SELECT available_balance,portfolio_value FROM broker_account_snapshots
            WHERE mode=? ORDER BY observed_at DESC,id DESC LIMIT 1
            """,
            (self.mode,),
        ) or {}
        starting_equity = max(
            _number(first.get("available_balance")),
            _number(first.get("portfolio_value")),
        )
        current_equity = max(
            _number(latest.get("available_balance")),
            _number(latest.get("portfolio_value")),
        )
        daily_pnl = current_equity - starting_equity if first and latest else 0.0
        open_orders = self.db.fetch_one(
            """
            SELECT COUNT(*) AS count FROM broker_orders WHERE mode=?
            AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
            """,
            (self.mode,),
        ) or {}
        limits = {
            "bankroll_cap_pct": float(settings[f"{prefix}_bankroll_cap_pct"]),
            "max_total_allocated_capital": float(settings[f"{prefix}_max_total_allocated_capital"]),
            "max_amount_per_order": float(settings[f"{prefix}_max_amount_per_order"]),
            "max_exposure_per_market": float(settings[f"{prefix}_max_exposure_per_market"]),
            "max_total_open_exposure": float(settings[f"{prefix}_max_total_open_exposure"]),
            "max_open_orders": int(settings[f"{prefix}_max_open_orders"]),
            "max_daily_loss": float(settings[f"{prefix}_max_daily_loss"]),
            "max_daily_order_count": int(settings[f"{prefix}_max_daily_order_count"]),
            "max_entry_price": float(settings[f"{prefix}_max_entry_price"]),
            "max_spread": float(settings[f"{prefix}_max_spread"]),
            "min_liquidity": int(settings[f"{prefix}_min_liquidity"]),
            "min_data_quality": str(settings[f"{prefix}_min_data_quality"]),
        }
        order_count = self.daily_order_count(today)
        open_order_count = int(open_orders.get("count") or 0)
        allocated = self.allocated_capital()
        failures: list[str] = []
        if allocated > limits["max_total_allocated_capital"] + 1e-9:
            failures.append("The total allocated-capital limit is active.")
        if allocated > limits["max_total_open_exposure"] + 1e-9:
            failures.append("The total open-exposure limit is active.")
        if open_order_count >= limits["max_open_orders"]:
            failures.append("The maximum open-order count is active.")
        if daily_pnl < -limits["max_daily_loss"] - 1e-9:
            failures.append("The daily realized and unrealized loss limit is active.")
        if order_count >= limits["max_daily_order_count"]:
            failures.append("The daily order-count limit is active.")
        return {
            "passed": not failures,
            "primary_blocker": failures[0] if failures else None,
            "failures": failures,
            "allocated_capital": allocated,
            "open_orders": open_order_count,
            "daily_order_count": order_count,
            "daily_realized_and_unrealized_pnl": daily_pnl,
            "limits": limits,
            "checked_at": iso_now(),
        }

    def daily_order_count(self, day: str | None = None) -> int:
        """Count orders that reached submission or exchange handling.

        Locally created intents and rejected attempts are retained for auditability,
        but they do not consume the user's accepted-order safety allowance.
        """
        target_day = day or datetime.now(UTC).date().isoformat()
        placeholders = ",".join("?" for _ in NON_COUNTED_DAILY_INTENT_STATES)
        row = self.db.fetch_one(
            f"""
            SELECT COUNT(*) AS count FROM broker_order_intents
            WHERE mode=? AND created_at LIKE ? AND status NOT IN ({placeholders})
            """,
            (
                self.mode,
                f"{target_day}%",
                *sorted(NON_COUNTED_DAILY_INTENT_STATES),
            ),
        ) or {}
        return int(row.get("count") or 0)

    def audit_history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM broker_audit_events WHERE mode=? ORDER BY id DESC LIMIT ?",
            (self.mode, max(1, min(1000, int(limit)))),
        )
        for row in rows:
            try:
                row["detail"] = json.loads(row.pop("detail_json"))
            except (TypeError, json.JSONDecodeError):
                row["detail"] = {}
                row.pop("detail_json", None)
        return rows

    def allocation_cap(self, available: float, portfolio_value: float) -> float:
        settings = self.db.settings()
        fraction = float(settings.get(f"{self.mode.lower()}_bankroll_cap_pct", 1.0))
        # Kalshi's portfolio_value is total account equity, including cash.
        # max() preserves compatibility with responses where it is unavailable.
        eligible = max(0.0, available, portfolio_value)
        hard = float(
            settings.get(f"{self.mode.lower()}_max_total_allocated_capital", 1000000.0)
        )
        return min(eligible * max(0.0, min(1.0, fraction)), hard)

    def allocated_capital(self, *, exclude_client_order_id: str | None = None) -> float:
        position = self.db.fetch_one(
            "SELECT COALESCE(SUM(ABS(market_exposure)+fees),0) AS amount FROM broker_positions WHERE mode=? AND status='open'",
            (self.mode,),
        )
        resting = self.db.fetch_all(
            """
            SELECT limit_price,remaining_contracts
            FROM broker_orders WHERE mode=? AND action='BUY'
              AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
            """,
            (self.mode,),
        )
        pending_sql = """
            SELECT limit_price,requested_contracts
            FROM broker_order_intents WHERE mode=? AND action='BUY'
              AND status IN ('INTENT_CREATED','SUBMITTING','RECONCILIATION_REQUIRED')
        """
        pending_params: tuple[Any, ...] = (self.mode,)
        if exclude_client_order_id:
            pending_sql += " AND client_order_id<>?"
            pending_params = (self.mode, exclude_client_order_id)
        pending = self.db.fetch_all(pending_sql, pending_params)

        def reserved(rows: list[dict[str, Any]], quantity_key: str) -> float:
            total = 0.0
            for row in rows:
                price = _number(row.get("limit_price"))
                contracts = max(0, math.floor(_number(row.get(quantity_key))))
                total += price * contracts + kalshi_fee(price, contracts)
            return total

        return (
            _number((position or {}).get("amount"))
            + reserved(resting, "remaining_contracts")
            + reserved(pending, "requested_contracts")
        )

    def has_automatic_entry(
        self, ticker: str, *, exclude_strategy: str | None = None
    ) -> bool:
        strategy_clause = ""
        params: list[Any] = [self.mode, ticker]
        if exclude_strategy:
            strategy_clause = " AND strategy<>?"
            params.append(str(exclude_strategy))
        row = self.db.fetch_one(
            f"""
            SELECT id FROM broker_order_intents
            WHERE mode=? AND ticker=? AND source='automatic'
              AND action='BUY' AND status NOT IN ('CANCELED','REJECTED','EXPIRED','SETTLED')
              {strategy_clause}
            LIMIT 1
            """,
            tuple(params),
        )
        return row is not None

    @staticmethod
    def _is_protective_exit(intent: OrderIntent) -> bool:
        return bool(
            intent.action == "SELL"
            and intent.decision_snapshot.get("protective_exit")
        )

    async def _safe_protective_exit_intent(self, intent: OrderIntent) -> OrderIntent:
        """Size an exit from evidence that remains safe during a bad full refresh.

        An unresolved sell has an unknown exchange outcome, so it is a hard
        stop rather than an invitation to submit a second client id.  A healthy
        targeted position read is preferred; confirmed, persisted fills are a
        conservative fallback when only the account-wide refresh is down.
        """
        readiness = self.readiness()
        if not readiness["ready_for_protective_exit"]:
            raise ValueError(str(readiness["protective_exit_blocker"]))
        unresolved = self.db.fetch_one(
            """
            SELECT client_order_id FROM broker_order_intents
            WHERE mode=? AND ticker=? AND side=? AND action='SELL'
              AND client_order_id<>?
              AND status IN ('INTENT_CREATED','SUBMITTING','RECONCILIATION_REQUIRED',
                             'ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
            LIMIT 1
            """,
            (self.mode, intent.ticker, intent.side, intent.client_order_id),
        )
        if unresolved:
            raise ValueError(
                "A prior protective exit has an unresolved client ID; waiting "
                "for its exchange outcome to avoid an oversell."
            )

        quantity = 0
        evidence = ""
        lookup_error: Exception | None = None
        targeted_lookup_succeeded = False
        try:
            assert self.client is not None
            payload = await self.client.positions(ticker=intent.ticker)
            targeted_lookup_succeeded = True
            for row in payload.get("market_positions") or payload.get("positions") or []:
                if str(row.get("ticker") or row.get("market_ticker") or "") != intent.ticker:
                    continue
                signed = _number(row.get("position_fp") or row.get("position"))
                if (intent.side == "YES" and signed > 0) or (
                    intent.side == "NO" and signed < 0
                ):
                    quantity = math.floor(abs(signed))
                    evidence = "targeted_exchange_position"
                    break
        except Exception as exc:  # The durable ledger fallback below is intentional.
            lookup_error = exc

        # A successful zero-position response is authoritative.  Fall back to
        # ledger fills only when the targeted exchange read itself is unavailable.
        if not quantity and not targeted_lookup_succeeded:
            fills = self.db.fetch_one(
                """
                SELECT COALESCE(SUM(CASE WHEN action='BUY' THEN contracts
                                         WHEN action='SELL' THEN -contracts ELSE 0 END),0)
                       AS contracts
                FROM broker_fills WHERE mode=? AND ticker=? AND side=?
                """,
                (self.mode, intent.ticker, intent.side),
            ) or {}
            signed_fills = _number(fills.get("contracts"))
            if signed_fills > 0:
                quantity = math.floor(signed_fills)
                evidence = "durable_confirmed_fills"

        if quantity < 1:
            detail = (
                f" Targeted lookup failed: {lookup_error}." if lookup_error else ""
            )
            raise ValueError(
                "Protective exit is deferred: no confirmed reducible contracts are "
                f"available for {intent.ticker}/{intent.side}.{detail}"
            )
        safe_quantity = min(intent.contracts, quantity)
        snapshot = {
            **intent.decision_snapshot,
            "protective_exit_quantity": safe_quantity,
            "protective_exit_quantity_source": evidence,
            "protective_exit_degraded": readiness["protective_exit_degraded"],
        }
        self._audit(
            "PROTECTIVE_EXIT_EVIDENCE",
            {"contracts": safe_quantity, "source": evidence,
             "degraded": readiness["protective_exit_degraded"]},
            intent=intent,
        )
        return replace(intent, contracts=safe_quantity, decision_snapshot=snapshot)

    def risk_check(
        self,
        intent: OrderIntent,
        *,
        spread: float | None = None,
        liquidity: float | None = None,
        data_quality: str | None = None,
        data_reliable: bool | None = None,
        market_open: bool | None = None,
        allow_unarmed: bool = False,
    ) -> dict[str, Any]:
        settings = self.db.settings()
        prefix = self.mode.lower()
        readiness = self.readiness()
        failures: list[str] = []
        protective_exit = self._is_protective_exit(intent)
        ready_key = (
            "ready_for_automatic" if intent.source == "automatic" else "ready_for_manual"
        )
        if protective_exit:
            if not readiness["ready_for_protective_exit"]:
                failures.append(str(readiness["protective_exit_blocker"] or "Protective exit is not ready."))
        elif not allow_unarmed and not readiness[ready_key]:
            failures.append(
                str(
                    readiness.get(
                        "automatic_blocker" if ready_key == "ready_for_automatic" else "blocker"
                    )
                    or "Trading is not ready."
                )
            )
        exposure = intent.contracts * intent.limit_price + kalshi_fee(
            intent.limit_price, intent.contracts
        )
        account = self._latest_account_snapshot()
        available = _number(account.get("available_balance"))
        portfolio_value = _number(account.get("portfolio_value"))
        exchange_index_raw = intent.risk_snapshot.get("exchange_index")
        try:
            exchange_index = (
                int(exchange_index_raw) if exchange_index_raw is not None else None
            )
        except (TypeError, ValueError):
            exchange_index = None
        exchange_available = self.available_balance_for_exchange(exchange_index)
        allocated_before = self.allocated_capital(
            exclude_client_order_id=intent.client_order_id
        )
        remaining_allocation = max(
            0.0, self.allocation_cap(available, portfolio_value) - allocated_before
        )
        if intent.action == "BUY":
            if (
                exchange_index is not None
                and self.balance_breakdown()
                and exposure > exchange_available + 1e-9
            ):
                failures.append(
                    f"Move funds to Kalshi exchange shard {exchange_index} before "
                    "placing this order."
                )
            if data_reliable is False:
                failures.append("Market data is stale or unreliable.")
            if market_open is False:
                failures.append("The market is not open for new exposure.")
            if exposure > float(settings[f"{prefix}_max_amount_per_order"]) + 1e-9:
                failures.append("The order exceeds the maximum amount per order.")
            if exposure > available + 1e-9:
                failures.append("The order exceeds the available account balance.")
            if exposure > remaining_allocation + 1e-9:
                failures.append("The order exceeds the remaining mode allocation.")
            positions = self.db.fetch_all(
                "SELECT * FROM broker_positions WHERE mode=? AND status='open'",
                (self.mode,),
            )
            market_exposure = sum(
                abs(_number(position.get("market_exposure")))
                for position in positions
                if position.get("ticker") == intent.ticker
            )
            market_resting = self.db.fetch_all(
                """
                SELECT limit_price,remaining_contracts FROM broker_orders
                WHERE mode=? AND ticker=? AND action='BUY'
                  AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
                """,
                (self.mode, intent.ticker),
            )
            market_pending = self.db.fetch_all(
                """
                SELECT limit_price,requested_contracts FROM broker_order_intents
                WHERE mode=? AND ticker=? AND action='BUY'
                  AND status IN ('INTENT_CREATED','SUBMITTING','RECONCILIATION_REQUIRED')
                  AND client_order_id<>?
                """,
                (self.mode, intent.ticker, intent.client_order_id),
            )
            reserved_market_exposure = sum(
                _number(row.get("limit_price"))
                * math.floor(max(0.0, _number(row.get("remaining_contracts"))))
                + kalshi_fee(
                    _number(row.get("limit_price")),
                    math.floor(max(0.0, _number(row.get("remaining_contracts")))),
                )
                for row in market_resting
            ) + sum(
                _number(row.get("limit_price"))
                * math.floor(max(0.0, _number(row.get("requested_contracts"))))
                + kalshi_fee(
                    _number(row.get("limit_price")),
                    math.floor(max(0.0, _number(row.get("requested_contracts")))),
                )
                for row in market_pending
            )
            if market_exposure + reserved_market_exposure + exposure > float(
                settings[f"{prefix}_max_exposure_per_market"]
            ) + 1e-9:
                failures.append("The order exceeds the per-market exposure limit.")
            if allocated_before + exposure > float(
                settings[f"{prefix}_max_total_open_exposure"]
            ) + 1e-9:
                failures.append("The total open-exposure limit would be exceeded.")
            open_orders = self.db.fetch_one(
                """
                SELECT COUNT(*) AS count FROM broker_orders WHERE mode=?
                AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
                """,
                (self.mode,),
            ) or {}
            if int(open_orders.get("count") or 0) >= int(settings[f"{prefix}_max_open_orders"]):
                failures.append("The maximum open-order count is active.")
            if intent.limit_price > float(settings[f"{prefix}_max_entry_price"]) + 1e-9:
                failures.append("The entry price exceeds the hard limit.")
            if spread is not None and spread > float(settings[f"{prefix}_max_spread"]) + 1e-9:
                failures.append("The spread exceeds the hard limit.")
            if liquidity is not None and liquidity + 1e-9 < int(settings[f"{prefix}_min_liquidity"]):
                failures.append("Liquidity is below the hard minimum.")
            quality_rank = {"Low": 0, "Moderate": 1, "High": 2}
            required = str(settings[f"{prefix}_min_data_quality"])
            if data_quality is not None and quality_rank.get(data_quality, -1) < quality_rank.get(required, 1):
                failures.append("Data quality is below the hard minimum.")
            today = datetime.now(UTC).date().isoformat()
            if self.daily_order_count(today) >= int(
                settings[f"{prefix}_max_daily_order_count"]
            ):
                failures.append("The daily order-count limit is active.")
            first_account = self.db.fetch_one(
                """
                SELECT available_balance,portfolio_value FROM broker_account_snapshots
                WHERE mode=? AND observed_at LIKE ? ORDER BY observed_at ASC,id ASC LIMIT 1
                """,
                (self.mode, f"{today}%"),
            ) or {}
            if first_account:
                start_equity = max(
                    _number(first_account.get("available_balance")),
                    _number(first_account.get("portfolio_value")),
                )
                current_equity = max(available, portfolio_value)
                if current_equity - start_equity < -float(
                    settings[f"{prefix}_max_daily_loss"]
                ) - 1e-9:
                    failures.append(
                        "The daily realized and unrealized loss limit is active."
                    )
        else:
            if protective_exit:
                return {
                    "passed": not failures,
                    "failures": failures,
                    "primary_blocker": failures[0] if failures else None,
                    "order_exposure": exposure,
                    "remaining_allocation": remaining_allocation,
                    "checked_at": iso_now(),
                }
            position = self.db.fetch_one(
                "SELECT contracts FROM broker_positions WHERE mode=? AND ticker=? AND side=? AND status='open'",
                (self.mode, intent.ticker, intent.side),
            )
            reserved = self.db.fetch_one(
                """
                SELECT COALESCE(SUM(remaining_contracts),0) AS contracts
                FROM broker_orders WHERE mode=? AND ticker=? AND side=? AND action='SELL'
                  AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','CANCEL_PENDING')
                """,
                (self.mode, intent.ticker, intent.side),
            )
            available_contracts = max(
                0,
                math.floor(_number((position or {}).get("contracts")) - _number((reserved or {}).get("contracts"))),
            )
            if intent.contracts > available_contracts:
                failures.append(f"Only {available_contracts} contracts are available to sell.")
        return {
            "passed": not failures,
            "failures": failures,
            "primary_blocker": failures[0] if failures else None,
            "order_exposure": exposure,
            "remaining_allocation": remaining_allocation,
            "checked_at": iso_now(),
        }

    async def submit(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.mode != self.mode:
            raise ValueError("Order intent belongs to a different trading mode.")
        if self.client is None:
            raise ValueError(f"{self.mode.title()} credentials are not configured.")
        existing = self.db.fetch_one(
            "SELECT * FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, intent.client_order_id),
        )
        if existing:
            return existing
        if self._is_protective_exit(intent):
            intent = await self._safe_protective_exit_intent(intent)
        now = iso_now()
        deadline = None
        if intent.cancel_after_seconds:
            deadline = (datetime.now(UTC) + timedelta(seconds=intent.cancel_after_seconds)).isoformat()
        self.db.execute(
            """
            INSERT INTO broker_order_intents(
                mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
                status,strategy,source,created_at,updated_at,stop_loss_price,
                target_exit_price,fallback_exit_mode,fallback_exit_seconds,
                cancel_deadline_at,decision_snapshot_json,risk_snapshot_json
                ,margin_volatility_index,margin_cushion_ratio
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.mode, intent.client_order_id, intent.ticker, intent.side,
                intent.action, intent.contracts, intent.limit_price, "INTENT_CREATED",
                intent.strategy, intent.source, now, now, intent.stop_loss_price,
                intent.target_exit_price, intent.fallback_exit_mode,
                intent.fallback_exit_seconds, deadline,
                _safe_json(intent.decision_snapshot), _safe_json(intent.risk_snapshot),
                intent.decision_snapshot.get("margin_volatility_index"),
                intent.decision_snapshot.get("margin_cushion_ratio"),
            ),
        )
        self._audit("INTENT_CREATED", asdict(intent), intent=intent)
        risk = self.risk_check(
            intent,
            spread=intent.risk_snapshot.get("spread"),
            liquidity=intent.risk_snapshot.get("liquidity"),
            data_quality=intent.risk_snapshot.get("data_quality"),
            data_reliable=intent.risk_snapshot.get("data_reliable"),
            market_open=intent.risk_snapshot.get("market_open"),
        )
        self.db.execute(
            "UPDATE broker_order_intents SET risk_snapshot_json=?,updated_at=? WHERE mode=? AND client_order_id=?",
            (_safe_json(risk), iso_now(), self.mode, intent.client_order_id),
        )
        self._audit("RISK_CHECKED", risk, intent=intent)
        if not risk["passed"]:
            self._set_intent(intent.client_order_id, "REJECTED", error=risk["primary_blocker"])
            if self.mode == "LIVE" and intent.action == "BUY":
                self._audit(
                    "AUTOMATIC_ENTRY_BLOCKED_BY_RISK",
                    {"reason": risk["primary_blocker"]},
                    intent=intent,
                )
            raise ValueError(str(risk["primary_blocker"]))
        self._set_intent(intent.client_order_id, "SUBMITTING")
        try:
            response = await self.client.create_order(
                ticker=intent.ticker,
                client_order_id=intent.client_order_id,
                side=intent.side,
                action=intent.action,
                contracts=intent.contracts,
                limit_price=intent.limit_price,
                reduce_only=intent.action == "SELL",
                post_only=intent.post_only,
                live_authorized=self.session_armed,
                price_ranges=intent.price_ranges,
                exchange_index=intent.risk_snapshot.get("exchange_index"),
                time_in_force=intent.time_in_force,
                cancel_order_on_pause=intent.cancel_order_on_pause,
            )
        except AmbiguousSubmissionError as exc:
            self._set_intent(
                intent.client_order_id, "RECONCILIATION_REQUIRED", error=str(exc)
            )
            self._update_mode_state(
                reconciled=False,
                reconciliation_required=True,
                last_error=str(exc),
            )
            self._audit("SUBMISSION_AMBIGUOUS", {"error": str(exc)}, intent=intent)
            if self._is_protective_exit(intent):
                # Never make a safety sell wait on account-wide history. Start
                # a short exact-ID/position check before returning uncertainty.
                try:
                    await asyncio.wait_for(
                        self.recover_ambiguous_protective_exit(intent.client_order_id),
                        timeout=4.0,
                    )
                except asyncio.TimeoutError:
                    self._audit(
                        "PROTECTIVE_EXIT_RECOVERY_DEFERRED",
                        {"reason": "targeted recovery timed out", "timeout_seconds": 4},
                        intent=intent,
                    )
                except Exception as recovery_error:
                    self._audit(
                        "PROTECTIVE_EXIT_RECOVERY_FAILED",
                        {"error": str(recovery_error)}, intent=intent,
                    )
            elif intent.action == "BUY":
                # An uncertain entry must never be re-submitted blindly, but
                # it also must not require a manual account-wide refresh just
                # to learn whether this exact client order reached Kalshi.
                try:
                    await asyncio.wait_for(
                        self.recover_ambiguous_entry(intent), timeout=4.0
                    )
                except asyncio.TimeoutError:
                    self._audit(
                        "ENTRY_RECOVERY_DEFERRED",
                        {"reason": "targeted recovery timed out", "timeout_seconds": 4},
                        intent=intent,
                    )
                except Exception as recovery_error:
                    self._audit(
                        "ENTRY_RECOVERY_FAILED", {"error": str(recovery_error)},
                        intent=intent,
                    )
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            exchange_error = _exchange_error_detail(exc)
            self._set_intent(
                intent.client_order_id,
                "REJECTED",
                error=str(exc),
                error_code=exchange_error.get("code"),
                error_details=exchange_error,
            )
            self._audit(
                "SUBMISSION_REJECTED",
                {"error": str(exc), "exchange_error": exchange_error},
                intent=intent,
            )
            raise
        exchange_order_id = str(response.get("order_id") or "")
        fill_count = _number(response.get("fill_count") or response.get("fill_count_fp"))
        remaining = _number(response.get("remaining_count") or response.get("remaining_count_fp"), intent.contracts)
        # IOC responses report zero remaining contracts even when nothing
        # matched. A zero-fill IOC is not a completed fill.
        status = (
            "FILLED" if fill_count >= intent.contracts and fill_count > 0
            else "PARTIALLY_FILLED" if fill_count > 0
            else "CANCELED" if intent.time_in_force == "immediate_or_cancel"
            else "ACKNOWLEDGED"
        )
        self._set_intent(
            intent.client_order_id,
            status,
            exchange_order_id=exchange_order_id or None,
        )
        self._upsert_order(
            {
                **response,
                "order_id": exchange_order_id,
                "client_order_id": intent.client_order_id,
                "ticker": intent.ticker,
                "status": status,
                "requested_contracts": intent.contracts,
                "filled_contracts": fill_count,
                "remaining_contracts": remaining,
                "limit_price": intent.limit_price,
                "outcome_side": intent.side,
                "action": intent.action,
                "strategy": intent.strategy,
                "source": intent.source,
            }
        )
        # A create-order response with a non-zero fill count is already
        # authenticated exchange evidence.  Do not make an open Texas
        # position wait for the slower, account-wide fills history endpoint.
        # Reconciliation will later replace this conservative local position
        # with the authoritative account snapshot.
        if intent.strategy == "TEXAS_HOLDEM" and intent.action == "BUY" and fill_count > 0:
            self._adopt_texas_acknowledged_fill(intent, fill_count)
        if self._is_protective_exit(intent):
            # A successful, just-in-time signed exit is stronger auth evidence
            # than an older background reconciliation failure.  Do not claim
            # full reconciliation here; new entries remain blocked until it
            # actually succeeds.
            self._update_mode_state(authenticated=True, connected=True)
            self._audit("PROTECTIVE_EXIT_FRESH_AUTH_CONFIRMED", {}, intent=intent)
        self._audit("SUBMISSION_ACKNOWLEDGED", response, intent=intent, exchange_order_id=exchange_order_id)
        return self.db.fetch_one(
            "SELECT * FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, intent.client_order_id),
        ) or response

    async def cancel(self, order_id: str | int) -> dict[str, Any]:
        if self.client is None:
            raise ValueError(f"{self.mode.title()} credentials are not configured.")
        order = self.db.fetch_one(
            "SELECT * FROM broker_orders WHERE mode=? AND (exchange_order_id=? OR id=?)",
            (self.mode, str(order_id), order_id),
        )
        if not order or order.get("status") not in OPEN_ORDER_STATES:
            raise ValueError("That order is no longer resting.")
        self.db.execute(
            "UPDATE broker_orders SET status='CANCEL_PENDING',updated_at=? WHERE id=?",
            (iso_now(), order["id"]),
        )
        try:
            response = await self.client.cancel_order(
                str(order["exchange_order_id"]),
                market_ticker=str(order.get("ticker") or "") or None,
            )
        except KalshiTradingError:
            await self.reconcile()
            raise
        self._audit(
            "CANCEL_REQUESTED", response,
            client_order_id=order.get("client_order_id"),
            exchange_order_id=str(order["exchange_order_id"]),
            ticker=order.get("ticker"),
        )
        await self.reconcile()
        return response

    async def reconcile(self) -> dict[str, Any]:
        """Coalesce overlapping account refreshes into one authoritative result."""
        requested_generation = self._reconcile_generation
        async with self._reconcile_lock:
            if requested_generation != self._reconcile_generation:
                return self.portfolio()
            result = await self._reconcile_once()
            self._reconcile_generation += 1
            return result

    def adopt_private_event(self, event: dict[str, Any]) -> dict[str, str | None]:
        """Durably apply a private-stream fact without requiring account-wide REST.

        WebSocket messages are advisory only for account completeness, but an
        order or fill they contain is exchange evidence.  Applying it here lets
        the UI and protective logic advance during a slow `/portfolio/fills`
        refresh.  The normal upserts make replay safe.
        """
        kind = str(event.get("type") or "").lower()
        payload = event.get("msg") or event.get("data") or event
        if not isinstance(payload, dict):
            return {"kind": kind, "order_id": None, "ticker": None}
        # Some Kalshi envelopes wrap the actual resource one level deeper.
        if kind == "user_order" and isinstance(payload.get("order"), dict):
            payload = payload["order"]
        elif kind == "fill" and isinstance(payload.get("fill"), dict):
            payload = payload["fill"]
        elif kind == "market_position" and isinstance(payload.get("position"), dict):
            payload = payload["position"]

        exchange_id = str(payload.get("order_id") or payload.get("exchange_order_id") or "") or None
        ticker = str(payload.get("ticker") or payload.get("market_ticker") or "") or None
        if kind == "user_order":
            self._upsert_order(payload)
        elif kind == "fill":
            self._upsert_fill(payload)
        elif kind == "market_position":
            self._upsert_position(payload)
        else:
            return {"kind": kind, "order_id": exchange_id, "ticker": ticker}
        self._audit(
            "PRIVATE_EVENT_ADOPTED",
            {"kind": kind, "has_order_id": bool(exchange_id), "has_ticker": bool(ticker)},
            exchange_order_id=exchange_id,
            ticker=ticker,
        )
        return {"kind": kind, "order_id": exchange_id, "ticker": ticker}

    async def recover_private_event(
        self, *, order_id: str | None, ticker: str | None
    ) -> None:
        """Best-effort narrow confirmation after a private event.

        This deliberately does not change reconciliation readiness or wait for
        historical fills.  It is a small correctness upgrade if an event was
        truncated/out of order; full reconciliation remains the account audit.
        """
        if self.client is None:
            return
        calls: list[tuple[str, Any]] = []
        exact_order = getattr(self.client, "order", None)
        if order_id and callable(exact_order):
            calls.append(("order", exact_order(order_id)))
        if ticker:
            calls.append(("position", self.client.positions(ticker=ticker)))
        if not calls:
            return
        results = await asyncio.gather(*(call for _, call in calls), return_exceptions=True)
        for (name, _), result in zip(calls, results):
            if isinstance(result, Exception):
                self._audit("PRIVATE_EVENT_RECOVERY_FAILED", {
                    "resource": name, "error": str(result),
                }, exchange_order_id=order_id, ticker=ticker)
                continue
            if name == "order" and isinstance(result, dict):
                self._upsert_order(result)
            elif name == "position" and isinstance(result, dict):
                for position in result.get("market_positions") or result.get("positions") or []:
                    if isinstance(position, dict):
                        self._upsert_position(position)

    async def recover_ambiguous_protective_exit(
        self, client_order_id: str
    ) -> dict[str, Any]:
        """Recover one uncertain safety sell without full reconciliation.

        Unknown exits retain their client-ID reservation. Only an observed
        order, a confirmed flat position, or a successful exact-ID absence can
        release that hold, so a retry cannot oversell existing exposure.
        """
        intent = self.db.fetch_one(
            "SELECT * FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, client_order_id),
        )
        if not intent or str(intent.get("action") or "") != "SELL" or self.client is None:
            return {"state": "not_protective_exit"}
        try:
            snapshot = json.loads(str(intent.get("decision_snapshot_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot = {}
        if not isinstance(snapshot, dict) or not snapshot.get("protective_exit"):
            return {"state": "not_protective_exit"}
        ticker, side = str(intent.get("ticker") or ""), str(intent.get("side") or "")
        lookup = getattr(self.client, "order_by_client_id", None)

        async def fallback_order_lookup() -> dict[str, Any] | None:
            payload = await self.client.orders(ticker=ticker)
            return next(
                (row for row in payload.get("orders") or []
                 if str(row.get("client_order_id") or "") == client_order_id),
                None,
            )

        order_call = lookup(client_order_id) if callable(lookup) else fallback_order_lookup()
        order_result, position_result = await asyncio.gather(
            order_call, self.client.positions(ticker=ticker), return_exceptions=True
        )
        order_ok = not isinstance(order_result, Exception)
        position_ok = not isinstance(position_result, Exception)
        if isinstance(order_result, dict):
            self._upsert_order(order_result)
        if isinstance(position_result, dict):
            rows = [
                row for row in (position_result.get("market_positions") or position_result.get("positions") or [])
                if isinstance(row, dict)
                and str(row.get("ticker") or row.get("market_ticker") or "") == ticker
            ]
            if rows:
                for row in rows:
                    self._upsert_position(row)
            else:
                # A successful ticker-scoped result without a row is a zero.
                self._upsert_position({"ticker": ticker, "position_fp": "0"})
        latest = self.db.fetch_one(
            "SELECT status FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, client_order_id),
        ) or {}
        status = str(latest.get("status") or "")
        position = self.db.fetch_one(
            "SELECT contracts,status FROM broker_positions WHERE mode=? AND ticker=? AND side=?",
            (self.mode, ticker, side),
        ) or {}
        flat = position_ok and (
            str(position.get("status") or "") == "closed"
            or _number(position.get("contracts")) < 1
        )
        if flat and status in {"SUBMITTING", "RECONCILIATION_REQUIRED"}:
            self._set_intent(
                client_order_id, "RESOLVED_EXTERNALLY",
                error="Position confirmed closed by targeted exchange evidence; uncertain exit no longer reserves exposure.",
            )
            self._resolve_closed_protective_exit(ticker)
            status = "RESOLVED_EXTERNALLY"
        elif order_ok and order_result is None and status in {"SUBMITTING", "RECONCILIATION_REQUIRED"}:
            self._set_intent(
                client_order_id, "REJECTED",
                error="Targeted exchange lookup did not report this exit order; a new reduce-only retry may use confirmed remaining exposure.",
            )
            status = "REJECTED"
        self._audit(
            "PROTECTIVE_EXIT_TARGETED_RECOVERY",
            {"order_lookup_succeeded": order_ok, "position_lookup_succeeded": position_ok,
             "order_found": isinstance(order_result, dict), "position_flat": flat,
             "final_state": status or "RECONCILIATION_REQUIRED"},
            client_order_id=client_order_id, ticker=ticker,
        )
        return {"state": status or "RECONCILIATION_REQUIRED", "position_flat": flat}

    async def recover_ambiguous_entry(self, intent: OrderIntent) -> dict[str, Any]:
        """Resolve one uncertain buy from exact, targeted exchange evidence.

        This never retries the buy. A positive exact-order result is adopted;
        An absent result is not proof that a delayed create cannot still
        appear, so it remains reserved for normal reconciliation.
        """
        if self.client is None or intent.action != "BUY":
            return {"state": "not_entry"}
        lookup = getattr(self.client, "order_by_client_id", None)

        async def fallback_order_lookup() -> dict[str, Any] | None:
            payload = await self.client.orders(ticker=intent.ticker)
            return next(
                (row for row in payload.get("orders") or []
                 if str(row.get("client_order_id") or "") == intent.client_order_id),
                None,
            )

        order_call = lookup(intent.client_order_id) if callable(lookup) else fallback_order_lookup()
        order_result, position_result = await asyncio.gather(
            order_call, self.client.positions(ticker=intent.ticker), return_exceptions=True
        )
        order_ok = not isinstance(order_result, Exception)
        position_ok = not isinstance(position_result, Exception)
        if isinstance(order_result, dict):
            self._upsert_order(order_result)
            filled = _number(
                order_result.get("filled_contracts")
                or order_result.get("fill_count_fp")
                or order_result.get("fill_count")
            )
            if intent.strategy == "TEXAS_HOLDEM" and filled > 0:
                self._adopt_texas_acknowledged_fill(intent, filled)
        position_rows: list[dict[str, Any]] = []
        if isinstance(position_result, dict):
            position_rows = [
                row for row in (position_result.get("market_positions") or position_result.get("positions") or [])
                if isinstance(row, dict)
                and str(row.get("ticker") or row.get("market_ticker") or "") == intent.ticker
            ]
            for row in position_rows:
                self._upsert_position(row)
        latest = self.db.fetch_one(
            "SELECT status FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, intent.client_order_id),
        ) or {}
        status = str(latest.get("status") or "RECONCILIATION_REQUIRED")
        self._audit(
            "ENTRY_TARGETED_RECOVERY",
            {"order_lookup_succeeded": order_ok, "position_lookup_succeeded": position_ok,
             "order_found": isinstance(order_result, dict), "position_found": bool(position_rows),
             "final_state": status},
            intent=intent,
        )
        return {"state": status}

    async def _reconcile_once(self) -> dict[str, Any]:
        if self.client is None:
            self._update_mode_state(
                connected=False, authenticated=False, reconciled=False,
                reconciliation_required=True,
                last_error="Credentials are not configured.",
            )
            return self.readiness()
        requests = [
            asyncio.create_task(call())
            for call in (
                self.client.balance, self.client.orders, self.client.fills,
                self.client.positions, self.client.settlements,
            )
        ]
        try:
            balance, orders, fills, positions, settlements = await asyncio.gather(
                *requests
            )
        except asyncio.CancelledError:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            raise
        except Exception as exc:
            # asyncio.gather does not cancel sibling requests when one fails.
            # Leaving those retries alive caused overlapping reconciliation
            # storms that starved order and quote traffic during an outage.
            for request in requests:
                if not request.done():
                    request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            exchange_error = _exchange_error_detail(exc)
            # An HTTP response (including rate limits and 5xx) confirms that
            # Kalshi is reachable.  Only a transport failure warrants a
            # Reconnecting state; all failures still keep entries blocked
            # until reconciliation is authoritative.
            failed_state: dict[str, Any] = dict(
                connected=(
                    self._private_stream_connected
                    or not bool(getattr(exc, "transport", False))
                ),
                reconciled=False,
                reconciliation_required=True,
                last_error=str(exc),
            )
            if getattr(exc, "status_code", None) in {401, 403}:
                failed_state["authenticated"] = False
            self._update_mode_state(**failed_state)
            if not self._reconciliation_paused:
                self._audit(
                    "RECONCILIATION_PAUSED",
                    {
                        "reason": str(exc),
                        "exchange_error": exchange_error,
                        "private_stream_connected": self._private_stream_connected,
                        "session_will_resume": self.session_armed,
                        "automatic_will_resume": self.automatic_armed,
                    },
                )
            self._audit(
                "RECONCILIATION_REQUEST_FAILED",
                {"exchange_error": exchange_error},
            )
            self._reconciliation_paused = True
            raise
        observed_at = iso_now()
        available = _dollars(balance, "balance_dollars", "balance")
        portfolio_value = _number(balance.get("portfolio_value")) / 100.0
        self.db.execute(
            """
            INSERT INTO broker_account_snapshots(
                mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
            ) VALUES (?,?,?,?,?,?)
            """,
            (self.mode, observed_at, available, portfolio_value, self.allocated_capital(), _safe_json(balance)),
        )
        remote_orders = list(orders.get("orders") or [])
        remote_fills = list(fills.get("fills") or [])
        remote_positions = list(positions.get("market_positions") or positions.get("positions") or [])
        remote_settlements = list(settlements.get("settlements") or [])
        new_fill_ids = {
            str(fill.get("fill_id") or fill.get("trade_id") or "")
            for fill in remote_fills
            if (fill.get("fill_id") or fill.get("trade_id"))
            and not self.db.fetch_one(
                "SELECT 1 FROM broker_fills WHERE mode=? AND fill_id=?",
                (self.mode, str(fill.get("fill_id") or fill.get("trade_id"))),
            )
        }
        new_settlement_tickers = {
            str(settlement.get("ticker") or settlement.get("market_ticker") or "")
            for settlement in remote_settlements
            if (settlement.get("ticker") or settlement.get("market_ticker"))
            and not self.db.fetch_one(
                "SELECT 1 FROM broker_settlements WHERE mode=? AND ticker=?",
                (
                    self.mode,
                    str(settlement.get("ticker") or settlement.get("market_ticker")),
                ),
            )
        }
        single_new_transaction = len(new_fill_ids) + len(new_settlement_tickers) == 1
        for order in remote_orders:
            self._upsert_order(order)
        for fill in remote_fills:
            fill_id = str(fill.get("fill_id") or fill.get("trade_id") or "")
            self._upsert_fill(
                fill,
                available_cash_after=(
                    available
                    if single_new_transaction and fill_id in new_fill_ids
                    else None
                ),
            )
        # A settlement is terminal market evidence, not evidence that an
        # earlier protective order filled.  Keep that distinction when an
        # account snapshot no longer lists the position.
        settlement_tickers = {
            str(item.get("ticker") or item.get("market_ticker") or "")
            for item in remote_settlements
        }
        # A restart may have persisted the settlement on an earlier account
        # snapshot while Kalshi's next response no longer includes it.
        settlement_tickers.update(
            str(row.get("ticker") or "") for row in self.db.fetch_all(
                "SELECT ticker FROM broker_settlements WHERE mode=?", (self.mode,)
            )
        )
        self._replace_positions(remote_positions, settlement_tickers)
        for settlement in remote_settlements:
            ticker = str(
                settlement.get("ticker") or settlement.get("market_ticker") or ""
            )
            self._upsert_settlement(
                settlement,
                available_cash_after=(
                    available
                    if single_new_transaction and ticker in new_settlement_tickers
                    else None
                ),
            )
        # Resolve crash-interrupted and ambiguous submissions by persistent client ID.
        interrupted = self.db.fetch_all(
            "SELECT * FROM broker_order_intents WHERE mode=? AND status='INTENT_CREATED'",
            (self.mode,),
        )
        for intent in interrupted:
            self._set_intent(
                str(intent["client_order_id"]),
                "REJECTED",
                error="The app stopped before this intent was submitted.",
            )
        ambiguous = self.db.fetch_all(
            """
            SELECT * FROM broker_order_intents
            WHERE mode=? AND status IN ('SUBMITTING','RECONCILIATION_REQUIRED')
            """,
            (self.mode,),
        )
        by_client = {
            str(order.get("client_order_id") or ""): order for order in remote_orders
        }
        for intent in ambiguous:
            remote = by_client.get(str(intent["client_order_id"]))
            settled_without_fill = False
            closed_without_fill = False
            confirmed_fill_contracts = 0.0
            market_closed = False
            if not remote:
                confirmed_fill_contracts = self._matching_fill_contracts(intent)
                settled_without_fill = self._settled_without_matching_fill(
                    intent, remote_settlements
                )
                market_closed = await self._closed_without_matching_fill(intent)
                closed_without_fill = not settled_without_fill and not confirmed_fill_contracts and market_closed
            if remote:
                self._set_intent(
                    str(intent["client_order_id"]),
                    self._order_status(remote),
                    exchange_order_id=str(remote.get("order_id") or "") or None,
                    error=None,
                )
            elif (self._market_is_settled(intent, remote_settlements) or market_closed) and (
                confirmed_fill_contracts >= float(intent.get("requested_contracts") or 0)
            ):
                # Kalshi confirmed the requested same-side quantity after the
                # market settled but did not retain the client ID.  It may be a
                # manual close, so resolve the safety hold without attributing
                # that fill to this timed-out request.
                self._set_intent(
                    str(intent["client_order_id"]),
                    "RESOLVED_AFTER_SETTLEMENT",
                    error="A confirmed same-side exchange fill resolved this timed-out request after settlement.",
                )
            elif settled_without_fill or closed_without_fill:
                # A settled or closed market cannot create future exposure. If
                # Kalshi also reports neither the order nor a matching fill, the
                # timed-out request is conclusively absent and the safety hold
                # can be cleared without waiting for settlement publication.
                self._set_intent(
                    str(intent["client_order_id"]),
                    "REJECTED",
                    error=(
                        "Kalshi did not report this timed-out order before the "
                        f"market {'settled' if settled_without_fill else 'closed'}."
                    ),
                )
            elif intent.get("status") == "SUBMITTING":
                self._set_intent(
                    str(intent["client_order_id"]),
                    "RECONCILIATION_REQUIRED",
                    error="Submission outcome remains unknown.",
                )
        unresolved = self.db.fetch_one(
            """
            SELECT COUNT(*) AS count FROM broker_order_intents
            WHERE mode=? AND status='RECONCILIATION_REQUIRED'
            """,
            (self.mode,),
        ) or {}
        reconciliation_required = int(unresolved.get("count") or 0) > 0
        self._update_mode_state(
            connected=True,
            authenticated=True,
            reconciled=not reconciliation_required,
            reconciliation_required=reconciliation_required,
            last_reconciled_at=observed_at,
            last_error=(
                "An order submission still requires reconciliation."
                if reconciliation_required else None
            ),
        )
        self._last_connection_at = time.monotonic()
        was_paused = self._reconciliation_paused
        self._reconciliation_paused = False
        self._audit(
            "RECONCILIATION_INCOMPLETE" if reconciliation_required else "RECONCILED",
            {
                "orders": len(remote_orders),
                "fills": len(remote_fills),
                "positions": len(remote_positions),
                "settlements": len(remote_settlements),
                "unresolved_submissions": int(unresolved.get("count") or 0),
            },
        )
        if was_paused:
            self._audit(
                "RECONCILIATION_RESUMED",
                {
                    "session_resumed": self.session_armed,
                    "automatic_resumed": self.automatic_armed,
                },
            )
        return self.portfolio()

    async def _closed_without_matching_fill(self, intent: dict[str, Any]) -> bool:
        """Resolve an ambiguous request once its market can no longer trade.

        Kalshi can publish settlement after the next contract has already begun.
        The market endpoint closes that gap: once the exchange says the market is
        closed (or its close time has passed), an absent order with no matching
        fill cannot create exposure later.
        """
        if self.client is None:
            return False
        ticker = str(intent.get("ticker") or "")
        if not ticker:
            return False
        try:
            market = await self.client.market(ticker)
        except (KalshiTradingError, ValueError):
            return False
        status = str(market.get("status") or "").lower()
        try:
            close_at = parse_time(
                str(
                    market.get("close_time")
                    or market.get("expected_expiration_time")
                    or ""
                )
            )
        except ValueError:
            close_at = None
        return status in {"closed", "settled", "finalized"} or bool(
            close_at and close_at <= datetime.now(UTC)
        )

    def _has_matching_fill(self, intent: dict[str, Any]) -> bool:
        return self._matching_fill_contracts(intent) > 0

    def _matching_fill_contracts(self, intent: dict[str, Any]) -> float:
        row = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(contracts),0) AS contracts FROM broker_fills
            WHERE mode=? AND ticker=? AND side=? AND action=? AND filled_at>=?
            """,
            (
                self.mode,
                str(intent.get("ticker") or ""),
                str(intent.get("side") or ""),
                str(intent.get("action") or ""),
                str(intent.get("created_at") or ""),
            ),
        ) or {}
        return float(row.get("contracts") or 0)

    def _settled_without_matching_fill(
        self, intent: dict[str, Any], remote_settlements: list[dict[str, Any]]
    ) -> bool:
        """Whether an ambiguous submission is conclusively absent after settlement.

        A missing order alone is not enough to clear an ambiguous request: an
        active market could still hold an order or fill that has not appeared
        in a response yet. Once Kalshi reports the contract settled, however,
        the request cannot create new exposure. We clear it only if the
        reconciled fill history also contains no matching side/action after the
        request began.
        """
        if not self._market_is_settled(intent, remote_settlements):
            return False
        return not self._has_matching_fill(intent)

    def _market_is_settled(
        self, intent: dict[str, Any], remote_settlements: list[dict[str, Any]]
    ) -> bool:
        ticker = str(intent.get("ticker") or "")
        remotely_settled = any(
            str(row.get("ticker") or row.get("market_ticker") or "") == ticker
            for row in remote_settlements
        )
        previously_settled = bool(ticker) and self.db.fetch_one(
            "SELECT 1 FROM broker_settlements WHERE mode=? AND ticker=? LIMIT 1",
            (self.mode, ticker),
        ) is not None
        return bool(ticker) and (remotely_settled or previously_settled)

    async def kill(self) -> dict[str, Any]:
        self.disarm("Kill switch activated.")
        self._update_mode_state(kill_switch=True)
        orders = self.db.fetch_all(
            "SELECT exchange_order_id FROM broker_orders WHERE mode=? AND status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED')",
            (self.mode,),
        )
        canceled = 0
        errors: list[str] = []
        for order in orders:
            try:
                await self.cancel(str(order["exchange_order_id"]))
                canceled += 1
            except (ValueError, KalshiTradingError) as exc:
                errors.append(str(exc))
        self._audit("KILL_SWITCH", {"cancel_attempts": len(orders), "canceled": canceled, "errors": errors})
        return {"mode": self.mode, "active": True, "canceled": canceled, "errors": errors}

    def release_kill_switch(self) -> dict[str, Any]:
        self._update_mode_state(kill_switch=False)
        self._audit("KILL_SWITCH_RELEASED", {})
        return self.readiness()

    def strategy_results(self) -> dict[str, dict[str, Any]]:
        fills = self.db.fetch_all(
            "SELECT * FROM broker_fills WHERE mode=? ORDER BY filled_at ASC", (self.mode,)
        )
        settlements = {
            str(row.get("ticker")): row
            for row in self.db.fetch_all(
                "SELECT * FROM broker_settlements WHERE mode=?", (self.mode,)
            )
        }
        result: dict[str, dict[str, Any]] = {}
        for strategy in (
            "STANDARD_EDGE", "EARLY_THRESHOLD", "LATE_CONVICTION", "SWING",
            "TEXAS_HOLDEM",
        ):
            rows = [row for row in fills if row.get("strategy") == strategy]
            buys = [row for row in rows if row.get("action") == "BUY"]
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                groups[(str(row.get("ticker")), str(row.get("side")))].append(row)
            realized = 0.0
            deployed = 0.0
            completed = 0
            wins = 0
            holding_seconds: list[float] = []
            exit_counts: dict[str, int] = defaultdict(int)
            exit_value = 0.0
            exit_contracts = 0.0
            open_contracts = 0.0
            for key, group_rows in groups.items():
                lots: deque[list[Any]] = deque()
                group_realized = 0.0
                first_entry_at = None
                last_exit_at = None
                group_exit_sources: set[str] = set()
                for row in group_rows:
                    quantity = max(0.0, _number(row.get("contracts")))
                    if quantity <= 0:
                        continue
                    price = _number(row.get("price"))
                    fee = _number(row.get("fee"))
                    timestamp = parse_time(row.get("filled_at"))
                    if row.get("action") == "BUY":
                        first_entry_at = first_entry_at or timestamp
                        lots.append([quantity, price + fee / quantity])
                        continue
                    remaining = quantity
                    matched = 0.0
                    matched_cost = 0.0
                    while remaining > 1e-9 and lots:
                        lot = lots[0]
                        take = min(remaining, float(lot[0]))
                        matched += take
                        matched_cost += take * float(lot[1])
                        lot[0] = float(lot[0]) - take
                        remaining -= take
                        if float(lot[0]) <= 1e-9:
                            lots.popleft()
                    if matched <= 0:
                        continue
                    allocated_fee = fee * matched / quantity
                    net_proceeds = matched * price - allocated_fee
                    group_realized += net_proceeds - matched_cost
                    deployed += matched_cost
                    exit_value += matched * price
                    exit_contracts += matched
                    last_exit_at = timestamp or last_exit_at
                    group_exit_sources.add(str(row.get("source") or "exit").upper())
                remaining_contracts = sum(float(lot[0]) for lot in lots)
                settlement = settlements.get(key[0])
                market_result = str(
                    (settlement or {}).get("market_result") or ""
                ).upper()
                if (
                    settlement and remaining_contracts > 1e-9
                    and market_result in {"YES", "NO"}
                ):
                    won = key[1] == market_result
                    payout_price = 1.0 if won else 0.0
                    remaining_cost = sum(float(lot[0]) * float(lot[1]) for lot in lots)
                    group_realized += remaining_contracts * payout_price - remaining_cost
                    deployed += remaining_cost
                    exit_value += remaining_contracts * payout_price
                    exit_contracts += remaining_contracts
                    group_exit_sources.add("SETTLEMENT")
                    last_exit_at = parse_time(settlement.get("settled_at")) or last_exit_at
                    lots.clear()
                    remaining_contracts = 0.0
                open_contracts += remaining_contracts
                realized += group_realized
                if group_rows and remaining_contracts <= 1e-9 and any(
                    row.get("action") == "BUY" for row in group_rows
                ):
                    completed += 1
                    wins += int(group_realized > 0)
                    if first_entry_at and last_exit_at:
                        holding_seconds.append(
                            max(0.0, (last_exit_at - first_entry_at).total_seconds())
                        )
                    for source in group_exit_sources or {"SETTLEMENT"}:
                        exit_counts[source] += 1
            entry_contracts = sum(_number(row.get("contracts")) for row in buys)
            entry_value = sum(
                _number(row.get("contracts")) * _number(row.get("price"))
                for row in buys
            )
            target_exits = exit_counts.get("SWING_TARGET", 0)
            result[strategy] = {
                "entries": len(
                    {
                        row.get("client_order_id") or row.get("fill_id")
                        for row in buys
                    }
                ),
                "completed_trades": completed,
                "settled_trades": completed,
                "wins": wins,
                "win_rate": wins / completed if completed else None,
                "realized_pnl": realized,
                "actual_fees": sum(_number(row.get("fee")) for row in rows),
                "average_entry_price": (
                    entry_value / entry_contracts if entry_contracts else None
                ),
                "average_exit_price": (
                    exit_value / exit_contracts if exit_contracts else None
                ),
                "average_holding_seconds": (
                    sum(holding_seconds) / len(holding_seconds)
                    if holding_seconds else None
                ),
                "return_on_deployed_capital": (
                    realized / deployed if deployed else None
                ),
                "target_hit_rate": (
                    target_exits / completed if completed else None
                ),
                "exit_counts": dict(exit_counts),
                "open_contracts": open_contracts,
            }
        return result

    def _order_status(self, order: dict[str, Any]) -> str:
        raw = str(order.get("status") or "").lower().replace(" ", "_")
        remaining = _number(order.get("remaining_count_fp") or order.get("remaining_count"))
        filled = _number(order.get("fill_count_fp") or order.get("fill_count"))
        if raw in {"canceled", "cancelled"}:
            return "CANCELED"
        if raw in {"rejected"}:
            return "REJECTED"
        if raw in {"expired"}:
            return "EXPIRED"
        if raw in {"executed", "filled"} or (filled > 0 and remaining <= 0):
            return "FILLED"
        if filled > 0 and remaining > 0:
            return "PARTIALLY_FILLED"
        return "RESTING"

    def _semantic_order(self, order: dict[str, Any]) -> tuple[str, str, float]:
        intent = None
        client_id = str(order.get("client_order_id") or "")
        exchange_id = str(order.get("order_id") or order.get("exchange_order_id") or "")
        if client_id:
            intent = self.db.fetch_one(
                "SELECT side,action,limit_price FROM broker_order_intents WHERE mode=? AND client_order_id=?",
                (self.mode, client_id),
            )
        if not intent and exchange_id:
            intent = self.db.fetch_one(
                "SELECT side,action,limit_price FROM broker_order_intents WHERE mode=? AND exchange_order_id=?",
                (self.mode, exchange_id),
            )
        if intent:
            return str(intent["side"]), str(intent["action"]), _number(intent["limit_price"])
        action = str(order.get("action") or "").upper()
        legacy_side = str(order.get("side") or "").upper()
        if legacy_side in {"YES", "NO"} and action in {"BUY", "SELL"}:
            direct_price = order.get(
                "yes_price_dollars" if legacy_side == "YES" else "no_price_dollars"
            )
            contract_price = _number(
                order.get("outcome_price") or direct_price
            )
            if direct_price in (None, "") and not order.get("outcome_price"):
                book_price = _number(
                    order.get("price")
                    or order.get("price_dollars")
                    or order.get("yes_price_dollars")
                )
                contract_price = (
                    1.0 - book_price if legacy_side == "NO" else book_price
                )
            return legacy_side, action, round(contract_price, 4)
        outcome = str(order.get("outcome_side") or "").upper()
        if outcome in {"YES", "NO"} and action in {"BUY", "SELL"}:
            contract_side = (
                outcome if action == "BUY"
                else "NO" if outcome == "YES" else "YES"
            )
            direct_price = order.get(
                "yes_price_dollars"
                if contract_side == "YES" else "no_price_dollars"
            )
            return contract_side, action, round(_number(direct_price), 4)
        book_side = str(order.get("book_side") or order.get("side") or "bid")
        book_price = _number(
            order.get("price") or order.get("price_dollars") or order.get("yes_price_dollars")
        )
        return book_to_outcome(book_side, book_price, reduce_only=bool(order.get("reduce_only")))

    def _upsert_order(self, order: dict[str, Any]) -> None:
        exchange_id = str(order.get("order_id") or order.get("exchange_order_id") or "")
        if not exchange_id:
            return
        previous = self.db.fetch_one(
            "SELECT * FROM broker_orders WHERE mode=? AND exchange_order_id=?",
            (self.mode, exchange_id),
        )
        side, action, limit_price = self._semantic_order(order)
        client_id = str(order.get("client_order_id") or "") or None
        intent = self.db.fetch_one(
            "SELECT strategy,source,status FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, client_id),
        ) if client_id else None
        requested = _number(
            order.get("requested_contracts")
            or order.get("initial_count_fp")
            or order.get("count_fp")
            or order.get("count")
        )
        filled = _number(
            order.get("filled_contracts") or order.get("fill_count_fp") or order.get("fill_count")
        )
        remaining = _number(
            order.get("remaining_contracts") or order.get("remaining_count_fp") or order.get("remaining_count"),
            max(0.0, requested - filled),
        )
        status = self._order_status(order) if str(order.get("status") or "").upper() not in OPEN_ORDER_STATES | FINAL_ORDER_STATES else str(order["status"]).upper()
        # Private events and REST pages can arrive out of order.  A stale
        # resting/canceled snapshot may not erase a previously confirmed fill.
        previous_filled = _number((previous or {}).get("filled_contracts"))
        if previous_filled > filled:
            filled = previous_filled
            requested = max(requested, _number((previous or {}).get("requested_contracts")))
            remaining = max(0.0, requested - filled)
        if (previous or {}).get("status") in {"FILLED", "SETTLED"}:
            status = str(previous["status"])
            filled = max(filled, previous_filled)
            remaining = _number((previous or {}).get("remaining_contracts"))
        # Kalshi continues returning settled BUY orders as `executed`. Settlement
        # is a stronger local terminal state, so never regress it to FILLED.
        if status == "FILLED" and action == "BUY":
            settlement_exists = self.db.fetch_one(
                "SELECT 1 AS found FROM broker_settlements WHERE mode=? AND ticker=?",
                (self.mode, str(order.get("ticker") or "")),
            )
            if (
                (previous or {}).get("status") == "SETTLED"
                or (intent or {}).get("status") == "SETTLED"
                or settlement_exists
            ):
                status = "SETTLED"
        updated_at = str(order.get("last_update_time") or order.get("updated_at") or iso_now())
        self.db.execute(
            """
            INSERT INTO broker_orders(
                mode,exchange_order_id,client_order_id,ticker,side,action,status,
                requested_contracts,filled_contracts,remaining_contracts,limit_price,
                average_fill_price,fees,strategy,source,created_at,updated_at,raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mode,exchange_order_id) DO UPDATE SET
                client_order_id=excluded.client_order_id,ticker=excluded.ticker,
                side=excluded.side,action=excluded.action,status=excluded.status,
                requested_contracts=excluded.requested_contracts,
                filled_contracts=excluded.filled_contracts,
                remaining_contracts=excluded.remaining_contracts,
                limit_price=excluded.limit_price,average_fill_price=excluded.average_fill_price,
                fees=excluded.fees,strategy=COALESCE(excluded.strategy,broker_orders.strategy),
                source=COALESCE(excluded.source,broker_orders.source),
                updated_at=excluded.updated_at,raw_json=excluded.raw_json
            """,
            (
                self.mode, exchange_id, client_id, str(order.get("ticker") or ""),
                side, action, status, requested, filled, remaining, limit_price,
                _number(order.get("average_fill_price") or order.get("avg_price")) or None,
                _number(order.get("fees") or order.get("fee"))
                or (
                    _number(order.get("taker_fees_dollars"))
                    + _number(order.get("maker_fees_dollars"))
                ),
                order.get("strategy") or (intent or {}).get("strategy"),
                order.get("source") or (intent or {}).get("source"),
                str(order.get("created_time") or order.get("created_at") or iso_now()),
                updated_at, _safe_json(order),
            ),
        )
        if client_id:
            self._set_intent(client_id, status, exchange_order_id=exchange_id)
        if not previous or previous.get("status") != status:
            self._audit(
                "ORDER_STATE_CHANGED",
                {"from": (previous or {}).get("status"), "to": status},
                client_order_id=client_id,
                exchange_order_id=exchange_id,
                ticker=str(order.get("ticker") or ""),
            )

    def _adopt_texas_acknowledged_fill(
        self, intent: OrderIntent, fill_count: float
    ) -> None:
        """Make a confirmed Texas entry protectable before full sync succeeds."""
        rows = self.db.fetch_all(
            """
            SELECT filled_contracts,average_fill_price,limit_price FROM broker_orders
            WHERE mode=? AND ticker=? AND side=? AND action='BUY'
              AND strategy='TEXAS_HOLDEM' AND filled_contracts>0
            """,
            (self.mode, intent.ticker, intent.side),
        )
        confirmed = sum(_number(row.get("filled_contracts")) for row in rows)
        # The just-upserted order is normally included above.  Keep this
        # fallback for a malformed exchange acknowledgement with no order ID.
        confirmed = max(confirmed, fill_count)
        existing = self.db.fetch_one(
            "SELECT * FROM broker_positions WHERE mode=? AND ticker=? AND side=?",
            (self.mode, intent.ticker, intent.side),
        ) or {}
        if confirmed <= _number(existing.get("contracts")):
            return
        weighted_value = sum(
            _number(row.get("filled_contracts"))
            * _number(row.get("average_fill_price") or row.get("limit_price"))
            for row in rows
        )
        average_price = weighted_value / confirmed if confirmed else None
        now = iso_now()
        self.db.execute(
            """
            INSERT INTO broker_positions(
                mode,ticker,side,contracts,average_price,market_exposure,realized_pnl,
                fees,strategy,source,opened_at,updated_at,status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mode,ticker,side) DO UPDATE SET
                contracts=CASE WHEN excluded.contracts>broker_positions.contracts
                    THEN excluded.contracts ELSE broker_positions.contracts END,
                average_price=CASE WHEN excluded.contracts>broker_positions.contracts
                    THEN excluded.average_price ELSE broker_positions.average_price END,
                market_exposure=CASE WHEN excluded.contracts>broker_positions.contracts
                    THEN excluded.market_exposure ELSE broker_positions.market_exposure END,
                strategy=COALESCE(broker_positions.strategy,excluded.strategy),
                source=COALESCE(broker_positions.source,excluded.source),
                updated_at=excluded.updated_at,status='open'
            """,
            (
                self.mode, intent.ticker, intent.side, confirmed, average_price,
                confirmed * _number(average_price), 0, 0, intent.strategy,
                intent.source, now, now, "open",
            ),
        )
        self._audit(
            "ACKNOWLEDGED_FILL_ADOPTED",
            {"filled_contracts": confirmed, "average_fill_price": average_price},
            intent=intent,
        )

    def _upsert_fill(
        self, fill: dict[str, Any], *, available_cash_after: float | None = None
    ) -> None:
        fill_id = str(fill.get("fill_id") or fill.get("trade_id") or "")
        if not fill_id:
            return
        already_recorded = self.db.fetch_one(
            "SELECT id FROM broker_fills WHERE mode=? AND fill_id=?",
            (self.mode, fill_id),
        ) is not None
        exchange_id = str(fill.get("order_id") or "") or None
        order = self.db.fetch_one(
            "SELECT * FROM broker_orders WHERE mode=? AND exchange_order_id=?",
            (self.mode, exchange_id),
        ) if exchange_id else None
        client_id = str(fill.get("client_order_id") or (order or {}).get("client_order_id") or "") or None
        if order:
            side, action, price = str(order["side"]), str(order["action"]), _number(order["limit_price"])
        else:
            side, action, price = self._semantic_order(fill)
        direct_price = fill.get(
            "yes_price_dollars" if side == "YES" else "no_price_dollars"
        )
        if direct_price not in (None, ""):
            fill_price = _number(direct_price, price)
        else:
            book_price = _number(
                fill.get("price")
                or fill.get("price_dollars")
                or fill.get("yes_price_dollars"),
                price,
            )
            fill_price = 1.0 - book_price if side == "NO" else book_price
        contracts = _number(fill.get("count_fp") or fill.get("count") or fill.get("contracts"))
        fee = _number(
            fill.get("fee_cost_dollars")
            or fill.get("fee_cost")
            or fill.get("fee")
            or fill.get("fees")
        )
        intent = self.db.fetch_one(
            "SELECT strategy,source FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, client_id),
        ) if client_id else None
        self.db.execute(
            """
            INSERT INTO broker_fills(
                mode,fill_id,exchange_order_id,client_order_id,ticker,side,action,
                contracts,price,fee,strategy,source,filled_at,raw_json,
                available_cash_after
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mode,fill_id) DO UPDATE SET
                exchange_order_id=excluded.exchange_order_id,
                client_order_id=COALESCE(excluded.client_order_id,broker_fills.client_order_id),
                ticker=excluded.ticker,side=excluded.side,action=excluded.action,
                contracts=excluded.contracts,price=excluded.price,fee=excluded.fee,
                strategy=COALESCE(excluded.strategy,broker_fills.strategy),
                source=COALESCE(excluded.source,broker_fills.source),
                filled_at=excluded.filled_at,raw_json=excluded.raw_json,
                available_cash_after=COALESCE(
                    excluded.available_cash_after,broker_fills.available_cash_after
                )
            """,
            (
                self.mode, fill_id, exchange_id, client_id,
                str(fill.get("ticker") or (order or {}).get("ticker") or ""),
                side, action, contracts, fill_price, fee,
                (intent or {}).get("strategy") or (order or {}).get("strategy"),
                (intent or {}).get("source") or (order or {}).get("source"),
                str(fill.get("created_time") or fill.get("filled_at") or iso_now()),
                _safe_json(fill), available_cash_after,
            ),
        )
        if available_cash_after is not None:
            self.db.execute(
                """
                UPDATE broker_fills SET available_cash_after=COALESCE(available_cash_after,?)
                WHERE mode=? AND fill_id=?
                """,
                (available_cash_after, self.mode, fill_id),
            )
        if exchange_id:
            fill_rows = self.db.fetch_all(
                "SELECT contracts,price,fee FROM broker_fills WHERE mode=? AND exchange_order_id=?",
                (self.mode, exchange_id),
            )
            order_row = self.db.fetch_one(
                """
                SELECT requested_contracts,remaining_contracts,status,updated_at
                FROM broker_orders WHERE mode=? AND exchange_order_id=?
                """,
                (self.mode, exchange_id),
            ) or {}
            requested = _number(order_row.get("requested_contracts"))
            aggregate = fill_aggregate(fill_rows, requested)
            authoritative_status = str(order_row.get("status") or "")
            terminal = authoritative_status in FINAL_ORDER_STATES
            status = authoritative_status if terminal else str(aggregate["status"])
            remaining_contracts = (
                _number(order_row.get("remaining_contracts"))
                if terminal else aggregate["remaining_contracts"]
            )
            order_updated_at = (
                order_row.get("updated_at")
                if terminal
                else fill.get("created_time") or fill.get("filled_at") or iso_now()
            )
            self.db.execute(
                """
                UPDATE broker_orders SET filled_contracts=?,remaining_contracts=?,
                    average_fill_price=?,fees=?,status=?,updated_at=?
                WHERE mode=? AND exchange_order_id=?
                """,
                (
                    aggregate["filled_contracts"], remaining_contracts,
                    aggregate["average_fill_price"], aggregate["fees"], status,
                    order_updated_at,
                    self.mode, exchange_id,
                ),
            )
            if client_id:
                self._set_intent(client_id, status, exchange_order_id=exchange_id)
        if not already_recorded:
            self._audit(
                "FILL_RECORDED",
                {
                    "fill_id": fill_id,
                    "side": side,
                    "action": action,
                    "contracts": contracts,
                    "price": fill_price,
                    "fee": fee,
                },
                client_order_id=client_id,
                exchange_order_id=exchange_id,
                ticker=str(fill.get("ticker") or (order or {}).get("ticker") or ""),
            )

    def _upsert_position(self, position: dict[str, Any]) -> None:
        """Apply one position event without closing unrelated positions."""
        signed = _number(position.get("position_fp") or position.get("position"))
        ticker = str(position.get("ticker") or position.get("market_ticker") or "")
        if not ticker:
            return
        if abs(signed) < 1e-9:
            self.db.execute(
                """
                UPDATE broker_positions SET contracts=0,market_exposure=0,
                    realized_pnl=?,fees=?,updated_at=?,status='closed'
                WHERE mode=? AND ticker=? AND status='open'
                """,
                (
                    _number(position.get("realized_pnl_dollars") or position.get("realized_pnl")),
                    _number(position.get("fees_paid_dollars") or position.get("fees_paid")),
                    str(position.get("last_updated_ts") or iso_now()), self.mode, ticker,
                ),
            )
            self._resolve_closed_protective_exit(ticker)
            return
        side = "YES" if signed > 0 else "NO"
        contracts = abs(signed)
        existing = self.db.fetch_one(
            "SELECT * FROM broker_positions WHERE mode=? AND ticker=? AND side=?",
            (self.mode, ticker, side),
        ) or {}
        exposure = abs(_number(position.get("market_exposure_dollars") or position.get("market_exposure")))
        average_price = exposure / contracts if contracts else None
        self.db.execute(
            """
            INSERT INTO broker_positions(
                mode,ticker,side,contracts,average_price,market_exposure,realized_pnl,
                fees,strategy,source,stop_loss_price,target_exit_price,
                fallback_exit_mode,fallback_exit_seconds,opened_at,updated_at,status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mode,ticker,side) DO UPDATE SET
                contracts=excluded.contracts,average_price=excluded.average_price,
                market_exposure=excluded.market_exposure,realized_pnl=excluded.realized_pnl,
                fees=excluded.fees,updated_at=excluded.updated_at,status='open'
            """,
            (
                self.mode, ticker, side, contracts, average_price, exposure,
                _number(position.get("realized_pnl_dollars") or position.get("realized_pnl")),
                _number(position.get("fees_paid_dollars") or position.get("fees_paid")),
                existing.get("strategy"), existing.get("source"), existing.get("stop_loss_price"),
                existing.get("target_exit_price"), existing.get("fallback_exit_mode"),
                existing.get("fallback_exit_seconds"), existing.get("opened_at") or iso_now(),
                str(position.get("last_updated_ts") or iso_now()), "open",
            ),
        )

    def _resolve_closed_protective_exit(self, ticker: str) -> None:
        """Project an exchange-confirmed flat position as the final exit state.

        This runs for explicit private/targeted position evidence, not for a
        market settlement.  A manual close can otherwise leave an old failed
        or blocked protective-exit message attached to a position that Kalshi
        has already confirmed is flat.
        """
        unresolved = self.db.fetch_all(
            """
            SELECT client_order_id FROM broker_order_intents
            WHERE mode=? AND ticker=? AND action='SELL'
              AND status IN ('SUBMITTING','RECONCILIATION_REQUIRED')
            """,
            (self.mode, ticker),
        )
        for intent in unresolved:
            self._set_intent(
                str(intent["client_order_id"]), "RESOLVED_EXTERNALLY",
                error="Position confirmed closed by exchange evidence; uncertain exit no longer reserves exposure.",
            )
        self.db.execute(
            """
            UPDATE broker_positions SET
                texas_exit_status=CASE WHEN strategy='TEXAS_HOLDEM' OR EXISTS (
                    SELECT 1 FROM texas_holdem_rounds r
                    WHERE r.environment=broker_positions.mode
                      AND r.ticker=broker_positions.ticker
                ) THEN 'Exited' ELSE texas_exit_status END,
                texas_exit_reason=CASE WHEN strategy='TEXAS_HOLDEM' OR EXISTS (
                    SELECT 1 FROM texas_holdem_rounds r
                    WHERE r.environment=broker_positions.mode
                      AND r.ticker=broker_positions.ticker
                ) THEN 'Position confirmed closed by Kalshi.' ELSE texas_exit_reason END,
                threshold_exit_status=CASE
                    WHEN threshold_exit_status IN ('Exit pending','Exit failed','Blocked')
                    THEN 'Closed externally' ELSE threshold_exit_status END,
                threshold_exit_block_reason=CASE
                    WHEN threshold_exit_status IN ('Exit pending','Exit failed','Blocked')
                    THEN NULL ELSE threshold_exit_block_reason END,
                updated_at=?
            WHERE mode=? AND ticker=? AND status='closed'
            """,
            (iso_now(), self.mode, ticker),
        )
        self.db.execute(
            """
            UPDATE texas_holdem_rounds SET status='EXITED',
                fold_reason='Position confirmed closed by Kalshi.',
                exited_at=COALESCE(exited_at,?),updated_at=?
            WHERE environment=? AND ticker=? AND status IN (
                'ENTERED','PARTIALLY_FILLED','EXIT_PENDING','EXIT_FAILED','EXIT_BLOCKED'
            )
            """,
            (iso_now(), iso_now(), self.mode, ticker),
        )

    def _replace_positions(
        self,
        positions: list[dict[str, Any]],
        settlement_tickers: set[str] | None = None,
    ) -> None:
        settlement_tickers = settlement_tickers or set()
        active: set[tuple[str, str]] = set()
        for position in positions:
            signed = _number(position.get("position_fp") or position.get("position"))
            ticker = str(position.get("ticker") or position.get("market_ticker") or "")
            if abs(signed) < 1e-9:
                if ticker:
                    self.db.execute(
                        """
                        UPDATE broker_positions SET contracts=0,market_exposure=0,
                            realized_pnl=?,fees=?,updated_at=?,status='closed'
                            ,threshold_exit_status=CASE
                                WHEN EXISTS (
                                    SELECT 1 FROM broker_fills f
                                    WHERE f.mode=broker_positions.mode
                                      AND f.ticker=broker_positions.ticker
                                      AND f.side=broker_positions.side
                                      AND f.action='SELL'
                                      AND f.source='threshold_breach_exit'
                                ) THEN 'Exited'
                                ELSE threshold_exit_status END
                        WHERE mode=? AND ticker=? AND status='open'
                        """,
                        (
                            _number(
                                position.get("realized_pnl_dollars")
                                or position.get("realized_pnl")
                            ),
                            _number(
                                position.get("fees_paid_dollars")
                                or position.get("fees_paid")
                            ),
                            str(position.get("last_updated_ts") or iso_now()),
                            self.mode,
                            ticker,
                        ),
                    )
                continue
            side = "YES" if signed > 0 else "NO"
            active.add((ticker, side))
            self._upsert_position(position)
        existing_rows = self.db.fetch_all(
            "SELECT ticker,side FROM broker_positions WHERE mode=? AND status='open'", (self.mode,)
        )
        for row in existing_rows:
            key = (str(row["ticker"]), str(row["side"]))
            if key not in active:
                self.db.execute(
                    """UPDATE broker_positions SET status='closed',contracts=0,updated_at=?,
                        threshold_exit_status=CASE WHEN EXISTS (
                            SELECT 1 FROM broker_fills f
                            WHERE f.mode=broker_positions.mode
                              AND f.ticker=broker_positions.ticker
                              AND f.side=broker_positions.side
                              AND f.action='SELL'
                              AND f.source='threshold_breach_exit'
                        ) THEN 'Exited' ELSE threshold_exit_status END
                        WHERE mode=? AND ticker=? AND side=?""",
                    (iso_now(), self.mode, *key),
                )
                if key[0] not in settlement_tickers:
                    self._resolve_closed_protective_exit(key[0])

    def _upsert_settlement(
        self,
        settlement: dict[str, Any],
        *,
        available_cash_after: float | None = None,
    ) -> None:
        ticker = str(settlement.get("ticker") or settlement.get("market_ticker") or "")
        if not ticker:
            return
        previous_settlement = self.db.fetch_one(
            "SELECT * FROM broker_settlements WHERE mode=? AND ticker=?",
            (self.mode, ticker),
        )
        position = self.db.fetch_one(
            "SELECT * FROM broker_positions WHERE mode=? AND ticker=? ORDER BY updated_at DESC LIMIT 1",
            (self.mode, ticker),
        ) or {}
        market_result = str(
            settlement.get("market_result") or settlement.get("result") or ""
        ).upper() or None
        settled_at = str(
            settlement.get("settled_time")
            or settlement.get("settled_at")
            or iso_now()
        )
        side = str(position.get("side") or "") or None
        position_won = None
        if side in {"YES", "NO"} and market_result in {"YES", "NO"}:
            position_won = int(side == market_result)
        fee = _number(
            settlement.get("fee_cost_dollars")
            or settlement.get("fee_cost")
            or settlement.get("fees_paid_dollars")
        )
        revenue = (
            _number(settlement.get("revenue_dollars"))
            if settlement.get("revenue_dollars") not in (None, "")
            else _number(settlement.get("revenue")) / 100.0
        )
        cost = _number(
            settlement.get("yes_total_cost_dollars")
            if side == "YES" else settlement.get("no_total_cost_dollars")
        )
        explicit_realized = settlement.get("realized_pnl_dollars")
        realized = (
            _number(settlement.get("realized_pnl_dollars"))
            if explicit_realized not in (None, "")
            else revenue - cost - fee if side in {"YES", "NO"} else None
        )
        self.db.execute(
            """
            INSERT INTO broker_settlements(
                mode,ticker,side,settled_at,market_result,position_won,realized_pnl,
                fees,raw_json,available_cash_after
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mode,ticker) DO UPDATE SET
                side=excluded.side,settled_at=excluded.settled_at,
                market_result=excluded.market_result,position_won=excluded.position_won,
                realized_pnl=excluded.realized_pnl,fees=excluded.fees,
                raw_json=excluded.raw_json,
                available_cash_after=COALESCE(
                    excluded.available_cash_after,broker_settlements.available_cash_after
                )
            """,
            (
                self.mode, ticker, side,
                settled_at,
                market_result, position_won, realized,
                fee, _safe_json(settlement), available_cash_after,
            ),
        )
        self.db.execute(
            """
            UPDATE broker_positions SET market_result=?,position_won=?,realized_pnl=?,
                status='settled',updated_at=? WHERE mode=? AND ticker=?
            """,
            (market_result, position_won, realized, settled_at, self.mode, ticker),
        )
        self.db.execute(
            """
            UPDATE broker_order_intents SET status='SETTLED',updated_at=?
            WHERE mode=? AND ticker=? AND action='BUY' AND status='FILLED'
            """,
            (settled_at, self.mode, ticker),
        )
        self.db.execute(
            """
            UPDATE broker_orders SET status='SETTLED',remaining_contracts=0,updated_at=?
            WHERE mode=? AND ticker=? AND action='BUY' AND status='FILLED'
            """,
            (settled_at, self.mode, ticker),
        )
        if not previous_settlement or any(
            previous_settlement.get(key) != value
            for key, value in {
                "side": side,
                "market_result": market_result,
                "position_won": position_won,
                "realized_pnl": realized,
                "fees": fee,
            }.items()
        ):
            self._audit(
                "SETTLEMENT_RECORDED",
                {
                    "side": side,
                    "market_result": market_result,
                    "position_won": position_won,
                    "realized_pnl": realized,
                    "fees": fee,
                },
                ticker=ticker,
            )

    def _set_intent(
        self,
        client_order_id: str,
        status: str,
        *,
        exchange_order_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        previous = self.db.fetch_one(
            "SELECT status,ticker,exchange_order_id,error,error_code,error_details_json "
            "FROM broker_order_intents WHERE mode=? AND client_order_id=?",
            (self.mode, client_order_id),
        )
        serialized_details = (
            _safe_json(error_details) if error_details is not None else None
        )
        if previous and all(
            (
                previous.get("status") == status,
                previous.get("exchange_order_id")
                == (exchange_order_id or previous.get("exchange_order_id")),
                previous.get("error") == error,
                previous.get("error_code") == error_code,
                previous.get("error_details_json") == serialized_details,
            )
        ):
            return
        self.db.execute(
            """
            UPDATE broker_order_intents SET status=?,exchange_order_id=COALESCE(?,exchange_order_id),
                error=?,error_code=?,error_details_json=?,updated_at=?
            WHERE mode=? AND client_order_id=?
            """,
            (
                status,
                exchange_order_id,
                error,
                error_code,
                serialized_details,
                iso_now(),
                self.mode,
                client_order_id,
            ),
        )
        if previous and previous.get("status") != status:
            self._audit(
                "INTENT_STATE_CHANGED",
                {
                    "from": previous.get("status"),
                    "to": status,
                    "error": error,
                    "error_code": error_code,
                    "error_details": error_details,
                },
                client_order_id=client_order_id,
                exchange_order_id=(
                    exchange_order_id or previous.get("exchange_order_id")
                ),
                ticker=previous.get("ticker"),
            )

    def _update_mode_state(self, **updates: Any) -> None:
        allowed = {
            "connected", "authenticated", "reconciled", "reconciliation_required",
            "demo_verified_at", "limits_reviewed_at", "kill_switch",
            "last_reconciled_at", "last_error",
        }
        cleaned = {key: value for key, value in updates.items() if key in allowed}
        if not cleaned:
            return
        assignments = ",".join(f"{key}=?" for key in cleaned)
        self.db.execute(
            f"UPDATE broker_mode_state SET {assignments},updated_at=? WHERE mode=?",
            (*cleaned.values(), iso_now(), self.mode),
        )

    def set_private_stream_connected(self, connected: bool) -> None:
        """Record transient stream health without a new persistence field."""
        self._private_stream_connected = bool(connected)

    def _audit(
        self,
        event_type: str,
        detail: dict[str, Any],
        *,
        intent: OrderIntent | None = None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        ticker: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO broker_audit_events(
                mode,created_at,event_type,client_order_id,exchange_order_id,ticker,detail_json
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                self.mode, iso_now(), event_type,
                client_order_id or (intent.client_order_id if intent else None),
                exchange_order_id,
                ticker or (intent.ticker if intent else None),
                _safe_json(detail),
            ),
        )


class KalshiDemoBroker(KalshiBroker):
    def __init__(self, db: Database, client: KalshiTradingClient | None = None):
        super().__init__("DEMO", db, client)


class KalshiLiveBroker(KalshiBroker):
    def __init__(self, db: Database, client: KalshiTradingClient | None = None):
        super().__init__("LIVE", db, client)
