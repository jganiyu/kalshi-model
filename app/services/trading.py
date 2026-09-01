from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from app.config import AppConfig
from app.db import Database
from app.domain import kalshi_fee, parse_time, threshold_breach_exit_state
from app.domain import texas_holdem_exit_reason
from app.services.broker import (
    fill_aggregate,
    KalshiBroker,
    KalshiDemoBroker,
    KalshiLiveBroker,
    OrderIntent,
    PaperBroker,
    _exchange_error_detail,
    normalize_mode,
)
from app.services.credentials import resolve_trading_credentials
from app.services.kalshi_trading import (
    KalshiTradingClient,
    KalshiTradingError,
    normalize_order_price,
)
from app.services.paper import PaperTradingService
from app.services.streaming import KalshiPrivateWebSocketFeed


_USE_DEFAULT_STOP = object()
_MARKET_STYLE_EXIT_FLOOR = 0.0001


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def manual_market_quality(
    current: dict[str, Any], assessment: dict[str, Any]
) -> tuple[bool, str]:
    """Keep feed reliability separate from immediate order-book executability."""
    quality = current.get("data_quality")
    if isinstance(quality, dict):
        reliable = bool(quality.get("reliable"))
    else:
        reliable = bool(assessment.get("data_reliable"))
    return reliable, "High" if reliable else "Low"


def protective_exit_reason(
    position: dict[str, Any],
    bid: float,
    seconds_remaining: float,
    settings: dict[str, Any],
    *,
    btc_proxy: float | None = None,
    threshold: float | None = None,
    data_reliable: bool = True,
) -> tuple[str | None, int | None]:
    if position.get("strategy") == "TEXAS_HOLDEM":
        texas_reason, _ = texas_holdem_exit_reason(bid, seconds_remaining, settings)
        if texas_reason:
            return texas_reason, 0
        # Texas positions intentionally begin on the opposite side of To Beat;
        # their dedicated River stop replaces generic stops and breach exits.
        return None, None
    if settings.get("global_profit_take_enabled", True) and bid + 1e-12 >= float(
        settings.get("global_profit_take_price", 0.99)
    ):
        return "GLOBAL_PROFIT_TAKE", 0
    threshold_exit = threshold_breach_exit_state(
        str(position.get("side") or ""),
        btc_proxy,
        threshold,
        enabled=bool(settings.get("threshold_breach_exit_enabled", True)),
        buffer_dollars=float(
            settings.get("threshold_breach_exit_buffer_dollars", 0.0)
        ),
        data_reliable=data_reliable,
    )
    if threshold_exit["breached"] and threshold_exit["status"] == "Breached":
        return "THRESHOLD_BREACH_EXIT", 1
    if position.get("stop_loss_price") is not None and bid <= float(
        position["stop_loss_price"]
    ):
        return "STOP_LOSS", 1
    if (
        position.get("strategy") == "SWING"
        and position.get("target_exit_price") is not None
        and bid >= float(position["target_exit_price"])
    ):
        return "SWING_TARGET", 2
    if (
        position.get("strategy") == "SWING"
        and position.get("fallback_exit_mode") == "Exit"
        and position.get("fallback_exit_seconds") is not None
        and seconds_remaining <= float(position["fallback_exit_seconds"])
    ):
        return "SWING_FALLBACK", 3
    return None, None


class TradingCoordinator:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        paper: PaperTradingService,
    ):
        self.config = config
        self.db = db
        self.paper = paper
        self.brokers: dict[str, PaperBroker | KalshiBroker] = {
            "PAPER": PaperBroker(paper),
            "DEMO": KalshiDemoBroker(db),
            "LIVE": KalshiLiveBroker(db),
        }
        self.http: httpx.AsyncClient | None = None
        self._private_streams: dict[str, asyncio.Task[None]] = {}
        self._reconciliation_tasks: dict[str, asyncio.Task[None]] = {}
        self._confirmations: dict[str, tuple[float, OrderIntent]] = {}
        self._submission_tasks: set[asyncio.Task[Any]] = set()
        self._pending_automatic_keys: set[tuple[str, str]] = set()
        self._pending_exit_keys: set[tuple[str, str, str]] = set()

    @property
    def selected_mode(self) -> str:
        return normalize_mode(self.db.settings().get("trading_mode", "PAPER"))

    def broker(self, mode: str | None = None) -> PaperBroker | KalshiBroker:
        return self.brokers[normalize_mode(mode or self.selected_mode)]

    async def start(self, http: httpx.AsyncClient) -> None:
        self.http = http
        for mode in ("DEMO", "LIVE"):
            self._configure_broker(mode)
        # Live arming is intentionally memory-only and always begins off.
        for mode in ("DEMO", "LIVE"):
            broker = self.brokers[mode]
            assert isinstance(broker, KalshiBroker)
            if broker.client:
                try:
                    await broker.reconcile()
                except (KalshiTradingError, ValueError):
                    pass
                self._start_private_stream(mode)
            self._start_reconciliation_loop(mode)

    async def stop(self) -> None:
        for task in self._private_streams.values():
            task.cancel()
        for task in self._reconciliation_tasks.values():
            task.cancel()
        for task in self._submission_tasks:
            task.cancel()
        await asyncio.gather(
            *self._private_streams.values(), *self._reconciliation_tasks.values(),
            *self._submission_tasks,
            return_exceptions=True,
        )
        self._private_streams.clear()
        self._reconciliation_tasks.clear()
        self._submission_tasks.clear()

    def _configure_broker(self, mode: str) -> None:
        broker = self.brokers[mode]
        assert isinstance(broker, KalshiBroker)
        key_id, key_path, _ = resolve_trading_credentials(mode)
        if not (self.http and key_id and key_path and key_path.exists()):
            broker.set_client(None)
            return
        base = (
            self.config.kalshi_demo_api_base
            if mode == "DEMO"
            else self.config.kalshi_live_api_base
        )
        broker.set_client(
            KalshiTradingClient(
                self.http, base, key_id, Path(key_path), environment=mode
            )
        )

    async def credentials_changed(self, mode: str) -> None:
        normalized = normalize_mode(mode)
        if normalized == "PAPER":
            return
        if normalized == "DEMO":
            self.db.execute(
                "UPDATE broker_mode_state SET demo_verified_at=NULL,updated_at=? WHERE mode IN ('DEMO','LIVE')",
                (datetime_now(),),
            )
            live_broker = self.brokers["LIVE"]
            assert isinstance(live_broker, KalshiBroker)
            live_broker.disarm("Demo verification was invalidated by a credential change.")
        task = self._private_streams.pop(normalized, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._configure_broker(normalized)
        broker = self.brokers[normalized]
        assert isinstance(broker, KalshiBroker)
        if broker.client:
            try:
                await broker.reconcile()
            except (KalshiTradingError, ValueError):
                pass
            self._start_private_stream(normalized)

    def _start_private_stream(self, mode: str) -> None:
        broker = self.brokers[mode]
        assert isinstance(broker, KalshiBroker)
        if not broker.client or mode in self._private_streams:
            return
        _, key_path, _ = resolve_trading_credentials(mode)
        if not key_path:
            return
        ws_url = (
            self.config.kalshi_demo_ws_url
            if mode == "DEMO"
            else self.config.kalshi_live_ws_url
        )

        async def on_message(_: dict[str, Any]) -> None:
            try:
                await broker.reconcile()
            except (KalshiTradingError, ValueError):
                # Reconciliation itself blocks trading. Keep the user's in-memory
                # arm state so a later successful refresh can resume it.
                pass

        async def on_status(_: str, connected: bool, error: str | None) -> None:
            await self._handle_private_stream_status(mode, connected, error)

        feed = KalshiPrivateWebSocketFeed(
            ws_url,
            broker.client.key_id,
            Path(key_path),
            mode,
            on_message,
            on_status,
        )
        self._private_streams[mode] = asyncio.create_task(feed.run())

    async def _handle_private_stream_status(
        self,
        mode: str,
        connected: bool,
        error: str | None,
    ) -> None:
        broker = self.brokers[mode]
        assert isinstance(broker, KalshiBroker)
        broker._update_mode_state(  # One controlled owner for connection state.
            connected=connected,
            reconciliation_required=True,
            reconciled=False,
            last_error=error,
        )
        if not connected:
            broker._audit(
                "PRIVATE_STREAM_PAUSED",
                {
                    "reason": error or "Private trading stream disconnected.",
                    "automatic_will_resume": broker.automatic_armed,
                },
            )
            return
        # A new socket is not proof of complete state. REST reconciliation keeps
        # submissions blocked until the exchange account is authoritative again.
        try:
            await broker.reconcile()
        except (KalshiTradingError, ValueError):
            return
        if broker.session_armed:
            broker._audit(
                "PRIVATE_STREAM_RESUMED",
                {"automatic_resumed": broker.automatic_armed},
            )

    def _start_reconciliation_loop(self, mode: str) -> None:
        if mode in self._reconciliation_tasks:
            return

        async def run() -> None:
            while True:
                await asyncio.sleep(30)
                broker = self.brokers[mode]
                assert isinstance(broker, KalshiBroker)
                if not broker.client:
                    continue
                try:
                    await broker.reconcile()
                except (KalshiTradingError, ValueError):
                    # A later successful reconciliation restores readiness while
                    # retaining the user's arm and automatic-trading choices.
                    pass

        self._reconciliation_tasks[mode] = asyncio.create_task(run())

    def summary(self, current: dict[str, Any] | None = None) -> dict[str, Any]:
        modes = {
            mode: self.brokers[mode].portfolio() for mode in ("PAPER", "DEMO", "LIVE")
        }
        for mode, portfolio in modes.items():
            self._annotate_threshold_breach_exits(mode, portfolio, current)
        return {
            "selected_mode": self.selected_mode,
            "selected": modes[self.selected_mode],
            "modes": modes,
        }

    def selected_summary(
        self, current: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return only the account currently shown on the Trading page.

        The dashboard still needs all three mode summaries for its environment
        switcher. The dedicated Trading page does not, so avoiding those two
        extra portfolio builds keeps page navigation responsive without
        changing account, risk, or reconciliation data.
        """
        mode = self.selected_mode
        portfolio = self.brokers[mode].portfolio()
        self._annotate_threshold_breach_exits(mode, portfolio, current)
        # These raw activity streams are used internally to build the ledger,
        # but the Trading page renders the already-aggregated ledger instead.
        # Omitting them materially reduces the local response size.
        for key in ("fills", "intents", "settlements"):
            portfolio.pop(key, None)
        return {"selected_mode": mode, "selected": portfolio}

    def _annotate_threshold_breach_exits(
        self,
        mode: str,
        portfolio: dict[str, Any],
        current: dict[str, Any] | None,
    ) -> None:
        settings = self.db.settings()
        enabled = bool(settings.get("threshold_breach_exit_enabled", True))
        buffer_dollars = float(
            settings.get("threshold_breach_exit_buffer_dollars", 0.0)
        )
        current = current or {}
        quality = current.get("data_quality") or {}
        data_reliable = bool(quality.get("reliable"))
        pending = {
            (str(order.get("ticker")), str(order.get("side")))
            for order in portfolio.get("open_orders") or []
            if str(order.get("action")).upper() == "SELL"
            and str(order.get("source") or "").lower()
            == "threshold_breach_exit"
        }
        readiness = portfolio.get("readiness") or {}
        for position in portfolio.get("positions") or []:
            entry = (position.get("entries") or [{}])[0]
            ticker = str(position.get("ticker") or "")
            side = str(position.get("side") or "")
            blocked_reason = None
            if ticker != str(current.get("ticker") or ""):
                blocked_reason = "This position is not the active Kalshi market."
            state = threshold_breach_exit_state(
                side,
                current.get("btc_proxy"),
                current.get("strike"),
                enabled=enabled,
                buffer_dollars=buffer_dollars,
                data_reliable=data_reliable,
                pending=(ticker, side) in pending
                or str(
                    position.get("threshold_exit_status")
                    or entry.get("threshold_exit_status")
                    or ""
                )
                == "Exit pending",
                blocked_reason=blocked_reason,
            )
            if (
                str(position.get("threshold_exit_status") or "") == "Blocked"
                and position.get("threshold_triggered_at")
            ):
                state["status"] = "Blocked"
                state["reason"] = position.get("threshold_exit_block_reason") or (
                    "A prior exit attempt was rejected."
                )
            if (
                mode != "PAPER"
                and state["breached"]
                and state["status"] == "Breached"
                and not readiness.get("ready_for_manual")
            ):
                state["status"] = "Blocked"
                state["reason"] = readiness.get("blocker") or (
                    f"The {mode.title()} session is not ready for an exit."
                )
            state["last_attempt_at"] = position.get("threshold_exit_last_attempt_at")
            state["last_attempt_bid"] = position.get("threshold_exit_last_attempt_bid")
            state["error_code"] = position.get("threshold_exit_error_code")
            try:
                state["error_details"] = json.loads(
                    position.get("threshold_exit_error_details_json") or "null"
                )
            except (TypeError, json.JSONDecodeError):
                state["error_details"] = None
            state["remaining_contracts"] = position.get("contracts")
            position["threshold_breach_exit"] = state

    async def reconcile(self, mode: str) -> dict[str, Any]:
        broker = self.broker(mode)
        result = await broker.reconcile()
        readiness = result.get("readiness") or {}
        if not readiness.get("reconciled"):
            raise ValueError(
                str(
                    readiness.get("last_error")
                    or readiness.get("blocker")
                    or "Account reconciliation is still incomplete."
                )
            )
        return result

    async def verify_demo(self, current: dict[str, Any], confirmation: str) -> dict[str, Any]:
        if confirmation.strip() != "VERIFY DEMO TRADING":
            raise ValueError('Type "VERIFY DEMO TRADING" to run the Demo check.')
        broker = self.broker("DEMO")
        assert isinstance(broker, KalshiBroker)
        await broker.reconcile()
        try:
            exchange_index = int(current.get("exchange_index"))
        except (TypeError, ValueError):
            raise ValueError(
                "Kalshi market routing data is unavailable. Wait for the active market "
                "to refresh, then retry Verify Demo."
            ) from None
        verification_exposure = 0.01 + kalshi_fee(0.01)
        if (
            broker.balance_breakdown()
            and broker.available_balance_for_exchange(exchange_index)
            + 1e-9 < verification_exposure
        ):
            raise ValueError(
                f"Demo funds are not allocated to this market's Kalshi exchange shard "
                f"({exchange_index}). Move mock funds to shard {exchange_index} in "
                "Kalshi Demo, then retry Verify Demo."
            )
        broker.arm(confirmation="ARM DEMO TRADING", automatic=False)
        side = "YES" if float(current.get("yes_ask") or 1.0) > 0.01 else "NO"
        intent = OrderIntent(
            mode="DEMO",
            ticker=str(current.get("ticker") or ""),
            side=side,
            action="BUY",
            contracts=1,
            limit_price=0.01,
            strategy="DEMO_VERIFICATION",
            source="verification",
            post_only=True,
            decision_snapshot={"purpose": "create, acknowledge, cancel, and reconcile"},
            risk_snapshot={"exchange_index": exchange_index},
        )
        result = await broker.submit(intent)
        exchange_id = result.get("exchange_order_id")
        if not exchange_id:
            broker.disarm("Demo verification did not receive an order acknowledgement.")
            raise ValueError("Demo did not acknowledge the verification order.")
        try:
            await broker.cancel(str(exchange_id))
        except (ValueError, KalshiTradingError):
            # Reconciliation below decides whether cancellation actually won.
            pass
        await broker.reconcile()
        verified_order = self.db.fetch_one(
            "SELECT status FROM broker_orders WHERE mode='DEMO' AND exchange_order_id=?",
            (str(exchange_id),),
        ) or {}
        if verified_order.get("status") != "CANCELED":
            broker.disarm("Demo verification could not confirm order cancellation.")
            raise ValueError("Demo order cancellation could not be confirmed.")
        restart_broker = KalshiDemoBroker(self.db, broker.client)
        restart_state = await restart_broker.reconcile()
        if not (restart_state.get("readiness") or restart_broker.readiness()).get(
            "reconciled"
        ):
            broker.disarm("Demo restart reconciliation did not complete.")
            raise ValueError("Demo restart reconciliation did not complete.")
        partial = fill_aggregate(
            [
                {"contracts": 1, "price": 0.04, "fee": 0.01},
                {"contracts": 2, "price": 0.05, "fee": 0.02},
            ],
            4,
        )
        simulation_settings = dict(self.db.settings())
        simulation_settings.update(
            {"global_profit_take_enabled": True, "global_profit_take_price": 0.99}
        )
        swing_position = {
            "strategy": "SWING",
            "stop_loss_price": 0.03,
            "target_exit_price": 0.10,
            "fallback_exit_mode": "Exit",
            "fallback_exit_seconds": 120,
        }
        profit_reason, _ = protective_exit_reason(
            swing_position, 0.99, 300, simulation_settings
        )
        stop_settings = {**simulation_settings, "global_profit_take_enabled": False}
        stop_reason, _ = protective_exit_reason(
            swing_position, 0.02, 300, stop_settings
        )
        if not (
            partial["status"] == "PARTIALLY_FILLED"
            and partial["remaining_contracts"] == 1
            and profit_reason == "GLOBAL_PROFIT_TAKE"
            and stop_reason == "STOP_LOSS"
        ):
            broker.disarm("Demo safety simulation failed.")
            raise ValueError("The Demo safety simulation did not pass.")
        broker.mark_demo_verified()
        broker.disarm("Demo verification completed.")
        broker._audit(
            "DEMO_EXIT_SIMULATIONS_VERIFIED",
            {
                "stop_loss": True,
                "profit_take": True,
                "partial_fill": True,
                "restart_reconciliation": True,
            },
        )
        return {
            "verified": True,
            "order_created": True,
            "order_acknowledged": True,
            "cancel_attempted": bool(exchange_id),
            "reconciled": True,
            "simulations": {
                "stop_loss": "passed (local safety simulation)",
                "profit_take": "passed (local safety simulation)",
                "partial_fill": "passed (local exchange-event simulation)",
            },
            "readiness": broker.readiness(),
        }

    def preview_manual(
        self,
        mode: str,
        payload: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = normalize_mode(mode)
        if normalized == "PAPER":
            raise ValueError("Paper orders do not require a broker confirmation preview.")
        broker = self.broker(normalized)
        assert isinstance(broker, KalshiBroker)
        side = str(payload.get("side") or "").upper()
        action = str(payload.get("action") or "").upper()
        if side not in {"YES", "NO"} or action not in {"BUY", "SELL"}:
            raise ValueError("Choose a valid side and action.")
        assessment = (current.get("trade_assessments") or {}).get(side) or {}
        economics = assessment.get(action.lower()) or {}
        raw_limit = payload.get("limit_price_cents")
        custom_limit = raw_limit not in (None, "")
        if custom_limit:
            try:
                limit_price = float(raw_limit) / 100.0
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a valid limit price in cents.") from exc
        else:
            limit_price = economics.get("executable_price")
        if limit_price is None or not 0 < float(limit_price) < 1:
            raise ValueError("An explicit worst acceptable price is required.")
        limit_price = normalize_order_price(
            float(limit_price),
            action,
            side=side,
            price_ranges=assessment.get("price_ranges"),
        )
        raw_contracts = payload.get("contracts")
        if raw_contracts not in (None, ""):
            try:
                contract_value = float(raw_contracts)
            except (TypeError, ValueError) as exc:
                raise ValueError("Contracts must be a positive whole number.") from exc
            if not contract_value.is_integer() or contract_value < 1:
                raise ValueError("Contracts must be a positive whole number.")
            contracts = int(contract_value)
        else:
            try:
                dollars = float(payload.get("dollars") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Enter a valid dollar amount.") from exc
            unit = float(limit_price) + (kalshi_fee(float(limit_price)) if action == "BUY" else 0)
            contracts = math.floor(dollars / unit) if unit > 0 else 0
            if contracts < 1:
                raise ValueError("The amount is too small for one contract.")
        stop = payload.get("stop_loss_cents")
        stop_price = None if stop in (None, "", 0, "0") else float(stop) / 100
        data_reliable, data_quality = manual_market_quality(current, assessment)
        intent = OrderIntent(
            mode=normalized,
            ticker=str(current.get("ticker") or ""),
            side=side,
            action=action,
            contracts=contracts,
            limit_price=float(limit_price),
            strategy="MANUAL",
            source="manual",
            price_ranges=assessment.get("price_ranges"),
            stop_loss_price=stop_price,
            decision_snapshot={
                "forecast": current.get("forecast"),
                "assessment": assessment,
                "observed_at": current.get("observed_at"),
                "custom_limit": custom_limit,
            },
            risk_snapshot={
                "spread": None if custom_limit else assessment.get("spread"),
                "liquidity": None if custom_limit else assessment.get("ask_size"),
                "data_quality": data_quality,
                "data_reliable": data_reliable,
                "market_open": str(current.get("status") or "").lower()
                in {"active", "open"},
                "exchange_index": current.get("exchange_index"),
            },
        )
        risk = broker.risk_check(
            intent,
            spread=None if custom_limit else assessment.get("spread"),
            liquidity=None if custom_limit else assessment.get("ask_size"),
            data_quality=data_quality,
            data_reliable=data_reliable,
            market_open=str(current.get("status") or "").lower() in {"active", "open"},
        )
        token = secrets.token_urlsafe(24)
        self._confirmations[token] = (time.monotonic() + 120.0, intent)
        portfolio = broker.portfolio()
        account_hint = broker.client.key_id[-6:] if broker.client else "unconfigured"
        return {
            "confirmation_token": token,
            "expires_in_seconds": 120,
            "environment": normalized,
            "account": f"Key …{account_hint}",
            "market": current.get("ticker"),
            "contract": "Up" if side == "YES" else "Down",
            "action": action.title(),
            "quantity": contracts,
            "limit_price": float(limit_price),
            "maximum_cash_exposure": risk["order_exposure"],
            "estimated_fees": kalshi_fee(float(limit_price), contracts),
            "slippage_allowance": float(self.db.settings().get("slippage_cents", 0.5)) / 100,
            "stop_loss": "Off" if stop_price is None else stop_price,
            "global_profit_take": (
                float(self.db.settings().get("global_profit_take_price", 0.99))
                if self.db.settings().get("global_profit_take_enabled", True)
                else "Off"
            ),
            "remaining_allocation": portfolio["remaining_allocation"],
            "risk": risk,
        }

    async def confirm_manual(
        self,
        token: str,
        expected_mode: str | None = None,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        confirmation = self._confirmations.pop(str(token), None)
        if not confirmation or confirmation[0] < time.monotonic():
            raise ValueError("The order confirmation expired. Review it again.")
        intent = confirmation[1]
        if expected_mode and intent.mode != normalize_mode(expected_mode):
            raise ValueError("The confirmation belongs to a different environment.")
        if current is not None:
            if str(current.get("ticker") or "") != intent.ticker:
                raise ValueError("The market changed. Review the order again.")
            assessment = (current.get("trade_assessments") or {}).get(intent.side) or {}
            data_reliable, data_quality = manual_market_quality(current, assessment)
            custom_limit = bool(intent.decision_snapshot.get("custom_limit"))
            intent = replace(
                intent,
                risk_snapshot={
                    "spread": None if custom_limit else assessment.get("spread"),
                    "liquidity": None if custom_limit else assessment.get("ask_size"),
                    "data_quality": data_quality,
                    "data_reliable": data_reliable,
                    "market_open": str(current.get("status") or "").lower()
                    in {"active", "open"},
                    "exchange_index": current.get("exchange_index"),
                },
            )
        broker = self.broker(intent.mode)
        return await broker.submit(intent)

    def _prepare_automatic_intent(
        self,
        *,
        strategy: str,
        ticker: str,
        assessment: dict[str, Any],
        bankroll_fraction: float,
        model_version: str,
        reason: str,
        stop_loss_cents: Any = _USE_DEFAULT_STOP,
        target_exit_price: float | None = None,
        fallback_exit_mode: str | None = None,
        fallback_exit_seconds: float | None = None,
        strategy_metadata: dict[str, Any] | None = None,
        enforce_duplicate_guard: bool = True,
        requested_contracts: int | None = None,
        time_in_force: str | None = None,
        maximum_entry_price: float | None = None,
    ) -> tuple[KalshiBroker | None, OrderIntent | None, float, str | None]:
        mode = self.selected_mode
        if mode == "PAPER":
            return None, None, 0.0, "Paper uses the existing simulated order path."
        broker = self.broker(mode)
        assert isinstance(broker, KalshiBroker)
        side = str(assessment.get("side") or "")
        buy = assessment.get("buy") or {}
        price = buy.get("executable_price")
        key = (mode, ticker)
        if side not in {"YES", "NO"} or price is None:
            return broker, None, 0.0, "An executable automatic candidate is unavailable."
        if enforce_duplicate_guard and (
            key in self._pending_automatic_keys
            or broker.has_automatic_entry(
                ticker,
                exclude_strategy="TEXAS_HOLDEM" if strategy == "TEXAS_HOLDEM" else None,
            )
        ):
            return broker, None, 0.0, "An automatic entry already exists for this market."
        settings = self.db.settings()
        effective = max(0.0, float(bankroll_fraction))
        if settings.get("risk_controls_enabled", True):
            effective = min(
                effective,
                float(settings.get("max_risk_per_trade_pct", 0.03)),
                float(settings.get("max_position_pct", 0.05)),
            )
        account = self.db.fetch_one(
            "SELECT available_balance,portfolio_value FROM broker_account_snapshots WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode,),
        ) or {}
        available_cash = float(account.get("available_balance") or 0)
        portfolio_value = float(account.get("portfolio_value") or 0)
        exchange_index_raw = assessment.get("exchange_index")
        try:
            exchange_index = (
                int(exchange_index_raw) if exchange_index_raw is not None else None
            )
        except (TypeError, ValueError):
            exchange_index = None
        exchange_available = broker.available_balance_for_exchange(exchange_index)
        if exchange_index is not None and broker.balance_breakdown():
            available_cash = min(available_cash, exchange_available)
        current_bankroll = max(available_cash, portfolio_value)
        remaining_allocation = max(
            0.0,
            broker.allocation_cap(available_cash, portfolio_value)
            - broker.allocated_capital(),
        )
        target = min(
            available_cash,
            current_bankroll * effective,
            remaining_allocation,
        )
        price = normalize_order_price(
            float(price),
            "BUY",
            side=side,
            price_ranges=assessment.get("price_ranges"),
        )
        if maximum_entry_price is not None and price > float(maximum_entry_price) + 1e-12:
            return broker, None, effective, "The executable entry exceeds the Texas Hold'em price cap."
        unit = float(price) + kalshi_fee(float(price))
        if (
            exchange_index is not None
            and broker.balance_breakdown()
            and exchange_available + 1e-9 < unit
        ):
            return (
                broker,
                None,
                effective,
                f"Move funds to Kalshi exchange shard {exchange_index} before "
                "placing this order.",
            )
        contracts = math.floor(target / unit) if unit > 0 else 0
        ask_size = assessment.get("ask_size")
        if ask_size is not None:
            contracts = min(contracts, math.floor(max(0.0, float(ask_size))))
        if requested_contracts is not None:
            contracts = min(contracts, max(0, int(requested_contracts)))
        if contracts < 1:
            return broker, None, effective, "The allocation is too small for one contract."
        configured_stop = (
            settings.get("default_stop_loss_cents")
            if stop_loss_cents is _USE_DEFAULT_STOP else stop_loss_cents
        )
        stop_price = None
        if configured_stop not in (None, "", 0, "0"):
            stop_price = float(configured_stop) / 100
        intent = OrderIntent(
            mode=mode,
            ticker=ticker,
            side=side,
            action="BUY",
            contracts=contracts,
            limit_price=float(price),
            strategy=strategy,
            source="automatic",
            time_in_force=time_in_force or "good_till_canceled",
            cancel_order_on_pause=(time_in_force != "immediate_or_cancel"),
            price_ranges=assessment.get("price_ranges"),
            stop_loss_price=stop_price,
            target_exit_price=target_exit_price,
            fallback_exit_mode=fallback_exit_mode,
            fallback_exit_seconds=fallback_exit_seconds,
            cancel_after_seconds=float(
                settings.get(f"{mode.lower()}_entry_timeout_seconds", 15)
            ) if time_in_force != "immediate_or_cancel" else None,
            decision_snapshot={
                "model_version": model_version,
                "reason": reason,
                "assessment": assessment,
                "strategy_metadata": strategy_metadata or {},
                "margin_volatility_index": (strategy_metadata or {}).get(
                    "margin_volatility_index"
                ),
                "margin_cushion_ratio": (strategy_metadata or {}).get(
                    "margin_cushion_ratio"
                ),
            },
            risk_snapshot={
                "spread": assessment.get("spread"),
                "liquidity": assessment.get("ask_size"),
                "data_quality": str(
                    assessment.get("decision_confidence") or "Low"
                ),
                "data_reliable": bool(
                    assessment.get("data_reliable")
                    and assessment.get("trade_allowed")
                ),
                "market_open": True,
                "exchange_index": exchange_index,
            },
        )
        if strategy == "TEXAS_HOLDEM":
            self.db.execute(
                """
                UPDATE texas_holdem_rounds SET target_contracts=COALESCE(target_contracts,?),
                    updated_at=? WHERE environment=? AND ticker=?
                """,
                (contracts, datetime_now(), mode, ticker),
            )
        return broker, intent, effective, None

    def preview_automatic_risk(self, **kwargs: Any) -> dict[str, Any]:
        broker, intent, effective, blocker = self._prepare_automatic_intent(
            **kwargs, enforce_duplicate_guard=False
        )
        if not broker or not intent:
            return {
                "passed": False,
                "primary_blocker": blocker,
                "failures": [blocker] if blocker else [],
                "effective_bankroll_allocation": effective,
            }
        assessment = kwargs.get("assessment") or {}
        texas = kwargs.get("strategy") == "TEXAS_HOLDEM"
        risk = broker.risk_check(
            intent,
            spread=assessment.get("spread"),
            liquidity=assessment.get("ask_size"),
            data_quality=(
                "High" if texas and assessment.get("data_reliable")
                else str(assessment.get("decision_confidence") or "Low")
            ),
            data_reliable=bool(
                assessment.get("data_reliable")
                and (texas or assessment.get("trade_allowed"))
            ),
            market_open=True,
        )
        return {**risk, "effective_bankroll_allocation": effective}

    def submit_automatic(
        self,
        *,
        strategy: str,
        ticker: str,
        assessment: dict[str, Any],
        bankroll_fraction: float,
        model_version: str,
        reason: str,
        stop_loss_cents: Any = _USE_DEFAULT_STOP,
        target_exit_price: float | None = None,
        fallback_exit_mode: str | None = None,
        fallback_exit_seconds: float | None = None,
        strategy_metadata: dict[str, Any] | None = None,
        requested_contracts: int | None = None,
        time_in_force: str | None = None,
        maximum_entry_price: float | None = None,
        **_: Any,
    ) -> tuple[bool, float]:
        broker, intent, effective, _ = self._prepare_automatic_intent(
            strategy=strategy,
            ticker=ticker,
            assessment=assessment,
            bankroll_fraction=bankroll_fraction,
            model_version=model_version,
            reason=reason,
            stop_loss_cents=stop_loss_cents,
            target_exit_price=target_exit_price,
            fallback_exit_mode=fallback_exit_mode,
            fallback_exit_seconds=fallback_exit_seconds,
            strategy_metadata=strategy_metadata,
            requested_contracts=requested_contracts,
            time_in_force=time_in_force,
            maximum_entry_price=maximum_entry_price,
        )
        if not broker or not intent:
            return False, effective
        risk = broker.risk_check(
            intent,
            spread=assessment.get("spread"),
            liquidity=assessment.get("ask_size"),
            data_quality=(
                "High" if strategy == "TEXAS_HOLDEM" and assessment.get("data_reliable")
                else str(assessment.get("decision_confidence") or "Low")
            ),
            data_reliable=bool(
                assessment.get("data_reliable")
                and (strategy == "TEXAS_HOLDEM" or assessment.get("trade_allowed"))
            ),
            market_open=True,
        )
        if not risk["passed"]:
            return False, effective
        key = (intent.mode, ticker)
        self._pending_automatic_keys.add(key)
        task = asyncio.create_task(self._submit_and_attach_position(intent))
        self._track_task(task)
        return True, effective

    async def _submit_and_attach_position(self, intent: OrderIntent) -> None:
        broker = self.broker(intent.mode)
        assert isinstance(broker, KalshiBroker)
        try:
            result = await broker.submit(intent)
            await broker.reconcile()
            if intent.strategy == "TEXAS_HOLDEM" and intent.action == "BUY":
                attempt_number = int(
                    (intent.decision_snapshot.get("strategy_metadata") or {}).get(
                        "attempt_number", 0
                    ) or 0
                )
                fill = self.db.fetch_one(
                    """
                    SELECT COALESCE(SUM(contracts),0) amount
                    FROM broker_fills
                    WHERE mode=? AND client_order_id=? AND action='BUY'
                    """,
                    (intent.mode, intent.client_order_id),
                ) or {}
                if attempt_number:
                    self.db.execute(
                        """
                        UPDATE texas_holdem_attempts SET broker_client_order_id=?,
                            requested_contracts=?,filled_contracts=?,status=?,blocker=NULL
                        WHERE round_id=(
                            SELECT id FROM texas_holdem_rounds
                            WHERE environment=? AND ticker=?
                        ) AND attempt_number=?
                        """,
                        (
                            intent.client_order_id,
                            intent.contracts,
                            float(fill.get("amount") or 0.0),
                            str(result.get("status") or "ACKNOWLEDGED"),
                            intent.mode,
                            intent.ticker,
                            attempt_number,
                        ),
                    )
            if (
                intent.action == "SELL"
                and intent.source == "threshold_breach_exit"
                and str(result.get("status") or "").upper()
                in {"CANCELED", "EXPIRED"}
            ):
                self.db.execute(
                    """
                    UPDATE broker_positions SET threshold_exit_status='Blocked',
                        threshold_exit_block_reason=?,threshold_exit_last_attempt_at=?,
                        threshold_exit_last_attempt_bid=?
                    WHERE mode=? AND ticker=? AND side=? AND status='open'
                    """,
                    (
                        "Kalshi accepted the protective exit but it did not fill; "
                        "waiting for a new executable quote.",
                        datetime_now(),
                        intent.decision_snapshot.get("executable_bid"),
                        intent.mode,
                        intent.ticker,
                        intent.side,
                    ),
                )
            if intent.action == "SELL" and intent.source.startswith("texas_"):
                position = self.db.fetch_one(
                    "SELECT contracts,status FROM broker_positions WHERE mode=? AND ticker=? AND side=?",
                    (intent.mode, intent.ticker, intent.side),
                ) or {}
                remaining = float(position.get("contracts") or 0.0)
                exited = remaining <= 1e-9 or position.get("status") == "closed"
                status = "EXITED" if exited else "EXIT_FAILED"
                reason = None if exited else (
                    "Kalshi accepted the Texas exit but it did not fully fill; "
                    "waiting for a new executable quote."
                )
                self.db.execute(
                    """
                    UPDATE broker_positions SET texas_exit_status=?,texas_exit_reason=COALESCE(?,texas_exit_reason)
                    WHERE mode=? AND ticker=? AND side=?
                    """,
                    ("Exited" if exited else "Exit failed", reason, intent.mode, intent.ticker, intent.side),
                )
                self.db.execute(
                    """
                    UPDATE texas_holdem_rounds SET status=?,fold_reason=?,
                        filled_contracts=?,exited_at=CASE WHEN ? THEN ? ELSE exited_at END,
                        updated_at=? WHERE environment=? AND ticker=?
                    """,
                    (
                        status, reason, remaining, int(exited), datetime_now(),
                        datetime_now(), intent.mode, intent.ticker,
                    ),
                )
            if intent.action == "BUY":
                self.db.execute(
                    """
                    UPDATE broker_positions SET strategy=?,source=?,stop_loss_price=?,
                        target_exit_price=?,fallback_exit_mode=?,fallback_exit_seconds=?,
                        strategy_metadata_json=?,threshold_breach_enabled=CASE
                            WHEN ?='TEXAS_HOLDEM' THEN 0 ELSE threshold_breach_enabled END,
                        threshold_exit_status=CASE WHEN ?='TEXAS_HOLDEM' THEN 'Watching'
                            ELSE threshold_exit_status END,
                        threshold_exit_block_reason=CASE WHEN ?='TEXAS_HOLDEM'
                            THEN 'Threshold Breach Exit is inactive for Texas Hold''em positions.'
                            ELSE threshold_exit_block_reason END
                    WHERE mode=? AND ticker=? AND side=? AND status='open'
                    """,
                    (
                        intent.strategy, intent.source, intent.stop_loss_price,
                        intent.target_exit_price, intent.fallback_exit_mode,
                        intent.fallback_exit_seconds,
                        json.dumps(intent.decision_snapshot.get("strategy_metadata") or {}, sort_keys=True),
                        intent.strategy, intent.strategy, intent.strategy,
                        intent.mode, intent.ticker, intent.side,
                    ),
                )
                if intent.strategy == "TEXAS_HOLDEM":
                    position = self.db.fetch_one(
                        "SELECT contracts,average_price,fees FROM broker_positions WHERE mode=? AND ticker=? AND side=? AND status='open'",
                        (intent.mode, intent.ticker, intent.side),
                    ) or {}
                    filled = float(position.get("contracts") or 0.0)
                    target = self.db.fetch_one(
                        "SELECT target_contracts FROM texas_holdem_rounds WHERE environment=? AND ticker=?",
                        (intent.mode, intent.ticker),
                    ) or {}
                    target_contracts = float(target.get("target_contracts") or 0.0)
                    round_status = (
                        "ENTERED" if filled > 0 and (target_contracts <= 0 or filled + 1e-9 >= target_contracts)
                        else "PARTIALLY_FILLED" if filled > 0 else "WAITING"
                    )
                    self.db.execute(
                        """
                        UPDATE texas_holdem_rounds SET filled_contracts=?,entry_price=?,
                            entry_fees=?,status=?,updated_at=? WHERE environment=? AND ticker=?
                        """,
                        (
                            filled, position.get("average_price"), position.get("fees") or 0,
                            round_status, datetime_now(), intent.mode, intent.ticker,
                        ),
                    )
        except (KalshiTradingError, ValueError) as exc:
            if intent.strategy == "TEXAS_HOLDEM" and intent.action == "BUY":
                attempt_number = int(
                    (intent.decision_snapshot.get("strategy_metadata") or {}).get(
                        "attempt_number", 0
                    ) or 0
                )
                if attempt_number:
                    self.db.execute(
                        """
                        UPDATE texas_holdem_attempts SET broker_client_order_id=?,
                            requested_contracts=?,status='REJECTED',blocker=?
                        WHERE round_id=(
                            SELECT id FROM texas_holdem_rounds
                            WHERE environment=? AND ticker=?
                        ) AND attempt_number=?
                        """,
                        (
                            intent.client_order_id,
                            intent.contracts,
                            str(exc),
                            intent.mode,
                            intent.ticker,
                            attempt_number,
                        ),
                    )
                    self.db.execute(
                        """
                        UPDATE texas_holdem_rounds SET fold_reason=?,updated_at=?
                        WHERE environment=? AND ticker=?
                        """,
                        (str(exc), datetime_now(), intent.mode, intent.ticker),
                    )
            if intent.action == "SELL":
                if intent.source == "threshold_breach_exit":
                    error = _exchange_error_detail(exc)
                    self.db.execute(
                        """
                        UPDATE broker_positions SET threshold_exit_status='Blocked',
                            threshold_exit_block_reason=?,threshold_exit_last_attempt_at=?,
                            threshold_exit_last_attempt_bid=?,threshold_exit_error_code=?,
                            threshold_exit_error_details_json=?
                        WHERE mode=? AND ticker=? AND side=? AND status='open'
                        """,
                        (
                            str(exc),
                            datetime_now(),
                            intent.decision_snapshot.get("executable_bid"),
                            error.get("code"),
                            json.dumps(error, sort_keys=True),
                            intent.mode,
                            intent.ticker,
                            intent.side,
                        ),
                    )
                elif intent.source.startswith("texas_"):
                    self.db.execute(
                        """
                        UPDATE broker_positions SET texas_exit_status='Exit failed',
                            texas_exit_reason=?,texas_exit_last_attempt_at=?
                        WHERE mode=? AND ticker=? AND side=? AND status='open'
                        """,
                        (str(exc), datetime_now(), intent.mode, intent.ticker, intent.side),
                    )
                    self.db.execute(
                        """
                        UPDATE texas_holdem_rounds SET status='EXIT_FAILED',
                            fold_reason=?,updated_at=? WHERE environment=? AND ticker=?
                        """,
                        (str(exc), datetime_now(), intent.mode, intent.ticker),
                    )
                try:
                    await broker.reconcile()
                except (KalshiTradingError, ValueError):
                    pass
            return
        finally:
            if intent.source == "automatic":
                self._pending_automatic_keys.discard((intent.mode, intent.ticker))

    def has_automatic_entry(
        self, mode: str, ticker: str, *, strategy: str | None = None
    ) -> bool:
        normalized = normalize_mode(mode)
        broker = self.broker(normalized)
        return (
            (normalized, ticker) in self._pending_automatic_keys
            or (
                isinstance(broker, KalshiBroker)
                and broker.has_automatic_entry(
                    ticker,
                    exclude_strategy="TEXAS_HOLDEM"
                    if strategy == "TEXAS_HOLDEM" else None,
                )
            )
        )

    async def process(self, current: dict[str, Any]) -> None:
        now = datetime_now()
        for mode in ("DEMO", "LIVE"):
            broker = self.broker(mode)
            assert isinstance(broker, KalshiBroker)
            if not broker.client:
                continue
            current_ticker = str(current.get("ticker") or "")
            observed = parse_time(current.get("observed_at"))
            quote_fresh = bool(
                observed
                and (time.time() - observed.timestamp())
                <= float(self.db.settings().get("max_data_age_seconds", 20))
            )
            market_open = str(current.get("status") or "").lower() in {
                "active", "open"
            }
            automatic_orders = self.db.fetch_all(
                """
                SELECT o.exchange_order_id,o.ticker,o.side,o.strategy
                FROM broker_orders o
                WHERE o.mode=? AND o.source='automatic' AND o.action='BUY'
                  AND o.status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED')
                """,
                (mode,),
            )
            for order in automatic_orders:
                assessment = (current.get("trade_assessments") or {}).get(
                    str(order.get("side"))
                ) or {}
                decision = (current.get("trade_decisions") or {}).get(
                    str(order.get("side"))
                ) or {}
                invalid = bool(
                    not broker.readiness().get("ready_for_automatic")
                    or str(order.get("ticker")) != current_ticker
                    or not quote_fresh
                    or not market_open
                    or not assessment.get("data_reliable")
                    or not assessment.get("trade_allowed")
                    or (
                        order.get("strategy") == "STANDARD_EDGE"
                        and decision.get("signal") != "BUY"
                    )
                )
                if invalid:
                    try:
                        await broker.cancel(str(order["exchange_order_id"]))
                    except (KalshiTradingError, ValueError):
                        pass
            # Timeouts and protective exits remain managed even while another mode
            # is selected. New automatic entries still use selected_mode only.
            timed_out = self.db.fetch_all(
                """
                SELECT o.exchange_order_id FROM broker_orders o
                JOIN broker_order_intents i
                  ON i.mode=o.mode AND i.client_order_id=o.client_order_id
                WHERE o.mode=? AND o.source='automatic'
                  AND o.status IN ('ACKNOWLEDGED','RESTING','PARTIALLY_FILLED')
                  AND i.cancel_deadline_at IS NOT NULL AND i.cancel_deadline_at<=?
                """,
                (mode, now),
            )
            for row in timed_out:
                try:
                    await broker.cancel(str(row["exchange_order_id"]))
                except (KalshiTradingError, ValueError):
                    pass
            await self._process_exits(broker, current)

    def schedule_process(self, current: dict[str, Any] | None) -> None:
        if not current:
            return
        self._track_task(asyncio.create_task(self.process(current)))

    async def _process_exits(self, broker: KalshiBroker, current: dict[str, Any]) -> None:
        ticker = str(current.get("ticker") or "")
        observed = parse_time(current.get("observed_at"))
        quote_fresh = bool(
            observed
            and (time.time() - observed.timestamp())
            <= float(self.db.settings().get("max_data_age_seconds", 20))
        )
        market_open = str(current.get("status") or "").lower() in {"active", "open"}
        if not ticker or not broker.session_armed or not quote_fresh or not market_open:
            return
        settings = self.db.settings()
        slippage = float(settings.get("slippage_cents", 0.5)) / 100
        seconds_remaining = float(current.get("time_remaining_seconds") or 0)
        btc_proxy = current.get("btc_proxy")
        threshold = current.get("strike")
        quality = current.get("data_quality") or {}
        data_reliable = bool(quality.get("reliable"))
        for position in broker.portfolio().get("positions", []):
            if position.get("ticker") != ticker:
                continue
            side = str(position.get("side"))
            texas_position = position.get("strategy") == "TEXAS_HOLDEM"
            threshold_exit = threshold_breach_exit_state(
                side,
                btc_proxy,
                threshold,
                enabled=(
                    bool(settings.get("threshold_breach_exit_enabled", True))
                    and not texas_position
                ),
                buffer_dollars=float(
                    settings.get("threshold_breach_exit_buffer_dollars", 0.0)
                ),
                data_reliable=data_reliable,
            )
            previous_status = str(position.get("threshold_exit_status") or "")
            previous_reason = position.get("threshold_exit_block_reason")
            if (
                previous_status == "Blocked"
                and position.get("threshold_triggered_at")
            ):
                threshold_exit["status"] = "Blocked"
                threshold_exit["reason"] = previous_reason or "A prior exit attempt was rejected."
            self.db.execute(
                """
                UPDATE broker_positions SET threshold_breach_enabled=?,
                    threshold_exit_buffer=?,threshold_exit_level=?,
                    threshold_exit_status=?,threshold_exit_block_reason=?
                WHERE mode=? AND ticker=? AND side=? AND status='open'
                """,
                (
                    int(bool(threshold_exit["enabled"])),
                    threshold_exit["buffer_dollars"],
                    threshold_exit["exit_level"],
                    threshold_exit["status"],
                    threshold_exit["reason"],
                    broker.mode,
                    ticker,
                    side,
                ),
            )
            bid = current.get(f"{side.lower()}_bid")
            if bid is None:
                if threshold_exit["breached"]:
                    self.db.execute(
                        """
                        UPDATE broker_positions SET threshold_exit_status='Blocked',
                            threshold_exit_block_reason='No executable bid is available.'
                        WHERE mode=? AND ticker=? AND side=? AND status='open'
                        """,
                        (broker.mode, ticker, side),
                    )
                continue
            bid = float(bid)
            reason, priority = protective_exit_reason(
                position,
                bid,
                seconds_remaining,
                settings,
                btc_proxy=float(btc_proxy) if btc_proxy is not None else None,
                threshold=float(threshold) if threshold is not None else None,
                data_reliable=data_reliable,
            )
            if reason is None:
                continue
            # Kalshi has no unpriced market order. A threshold-breach exit uses
            # the lowest valid outcome price as a reduce-only IOC limit, which
            # sweeps every executable bid without leaving a resting order.
            market_style_exit = reason == "THRESHOLD_BREACH_EXIT" or reason.startswith("TEXAS_")
            candidate_limit = normalize_order_price(
                _MARKET_STYLE_EXIT_FLOOR
                if market_style_exit else max(0.01, bid - slippage),
                "SELL",
                side=side,
                price_ranges=current.get("price_ranges"),
            )
            rejected_exits = self.db.fetch_all(
                """
                SELECT status,updated_at,limit_price,decision_snapshot_json
                FROM broker_order_intents
                WHERE mode=? AND ticker=? AND side=? AND action='SELL' AND source=?
                ORDER BY id DESC LIMIT 7
                """,
                (broker.mode, ticker, side, reason.lower()),
            )
            consecutive_unfilled = 0
            for row in rejected_exits:
                if row.get("status") not in {"REJECTED", "CANCELED", "EXPIRED"}:
                    break
                consecutive_unfilled += 1
            if consecutive_unfilled:
                # Repeating an unfilled protective order at an unchanged price
                # cannot improve execution and can churn a thin market.
                latest_rejected = rejected_exits[0]
                latest_snapshot = _json_object(
                    latest_rejected.get("decision_snapshot_json")
                )
                latest_bid = latest_snapshot.get("executable_bid")
                unchanged_market = bool(
                    market_style_exit
                    and latest_bid is not None
                    and abs(float(latest_bid) - bid) < 1e-9
                )
                unchanged_limit = bool(
                    not market_style_exit
                    and abs(
                        float(latest_rejected.get("limit_price") or 0)
                        - candidate_limit
                    ) < 1e-9
                )
                if unchanged_market or unchanged_limit:
                    continue
            pending_key = (broker.mode, ticker, side)
            if pending_key in self._pending_exit_keys:
                if reason == "THRESHOLD_BREACH_EXIT":
                    self._mark_threshold_exit_pending(
                        broker.mode, ticker, side, threshold_exit
                    )
                continue
            existing = self.db.fetch_one(
                """
                SELECT id FROM broker_order_intents WHERE mode=? AND ticker=? AND side=?
                  AND action='SELL' AND status NOT IN ('CANCELED','REJECTED','EXPIRED','SETTLED')
                LIMIT 1
                """,
                (broker.mode, ticker, side),
            )
            if existing:
                if reason == "THRESHOLD_BREACH_EXIT":
                    self._mark_threshold_exit_pending(
                        broker.mode, ticker, side, threshold_exit
                    )
                continue
            contracts = math.floor(float(position.get("contracts") or 0))
            if contracts < 1:
                continue
            decision_snapshot: dict[str, Any] = {
                "trigger": reason,
                "executable_bid": bid,
                "priority": priority,
                "market_style_ioc": market_style_exit,
                "submitted_limit_floor": candidate_limit
                if market_style_exit else None,
            }
            if reason.startswith("TEXAS_"):
                _, texas_state = texas_holdem_exit_reason(bid, seconds_remaining, settings)
                decision_snapshot["texas_holdem"] = texas_state
            if reason == "THRESHOLD_BREACH_EXIT":
                decision_snapshot.update(
                    {
                        "threshold_breach_enabled": threshold_exit["enabled"],
                        "threshold_exit_buffer": threshold_exit["buffer_dollars"],
                        "threshold_exit_level": threshold_exit["exit_level"],
                        "threshold_trigger_btc_proxy": btc_proxy,
                        "threshold_trigger_threshold": threshold,
                        "threshold_triggered_at": datetime_now(),
                    }
                )
            elif reason.startswith("TEXAS_"):
                self.db.execute(
                    """
                    UPDATE broker_positions SET texas_exit_status='Exit pending',
                        texas_exit_reason=?,texas_exit_last_attempt_at=?,
                        texas_exit_last_attempt_bid=?
                    WHERE mode=? AND ticker=? AND side=? AND status='open'
                    """,
                    (reason, datetime_now(), bid, broker.mode, ticker, side),
                )
                self.db.execute(
                    """
                    UPDATE texas_holdem_rounds SET status='EXIT_PENDING',exit_reason=?,
                        exit_trigger_bid=?,updated_at=? WHERE environment=? AND ticker=?
                    """,
                    (reason, bid, datetime_now(), broker.mode, ticker),
                )
            intent = OrderIntent(
                mode=broker.mode,
                ticker=ticker,
                side=side,
                action="SELL",
                contracts=contracts,
                limit_price=candidate_limit,
                strategy=str(position.get("strategy") or "MANUAL"),
                source=reason.lower(),
                time_in_force="immediate_or_cancel",
                cancel_order_on_pause=False,
                price_ranges=current.get("price_ranges"),
                decision_snapshot=decision_snapshot,
                risk_snapshot={"exchange_index": current.get("exchange_index")},
            )
            if reason == "THRESHOLD_BREACH_EXIT":
                self._mark_threshold_exit_pending(
                    broker.mode,
                    ticker,
                    side,
                    threshold_exit,
                    triggered=True,
                    executable_bid=bid,
                )
            self._pending_exit_keys.add(pending_key)
            task = asyncio.create_task(self._submit_exit(intent, pending_key))
            self._track_task(task)

    async def _submit_exit(
        self, intent: OrderIntent, pending_key: tuple[str, str, str]
    ) -> None:
        try:
            await self._submit_and_attach_position(intent)
        finally:
            self._pending_exit_keys.discard(pending_key)

    def _mark_threshold_exit_pending(
        self,
        mode: str,
        ticker: str,
        side: str,
        state: dict[str, object],
        *,
        triggered: bool = False,
        executable_bid: float | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE broker_positions SET threshold_breach_enabled=?,
                threshold_exit_buffer=?,threshold_exit_level=?,
                threshold_trigger_btc_proxy=CASE WHEN ? THEN ?
                    ELSE threshold_trigger_btc_proxy END,
                threshold_trigger_threshold=CASE WHEN ? THEN ?
                    ELSE threshold_trigger_threshold END,
                threshold_triggered_at=CASE WHEN ? THEN COALESCE(
                    threshold_triggered_at,?) ELSE threshold_triggered_at END,
                threshold_exit_status='Exit pending',threshold_exit_block_reason=NULL,
                threshold_exit_last_attempt_at=CASE WHEN ? THEN ?
                    ELSE threshold_exit_last_attempt_at END,
                threshold_exit_last_attempt_bid=CASE WHEN ? THEN ?
                    ELSE threshold_exit_last_attempt_bid END,
                threshold_exit_error_code=NULL,threshold_exit_error_details_json=NULL
            WHERE mode=? AND ticker=? AND side=? AND status='open'
            """,
            (
                int(bool(state["enabled"])),
                state["buffer_dollars"],
                state["exit_level"],
                int(triggered),
                state["btc_proxy"],
                int(triggered),
                state["threshold"],
                int(triggered),
                datetime_now(),
                int(triggered),
                datetime_now(),
                int(triggered),
                executable_bid,
                mode,
                ticker,
                side,
            ),
        )

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._submission_tasks.add(task)
        task.add_done_callback(self._submission_tasks.discard)


def datetime_now() -> str:
    from app.domain import iso_now

    return iso_now()
