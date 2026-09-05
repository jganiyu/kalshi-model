from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import AppConfig
from app.db import Database
from app.domain import (
    DEFAULT_BENCHMARK_UNCERTAINTY_PCT,
    NextThresholdForecast,
    SETTLEMENT_WINDOW_SECONDS,
    TEXAS_HOLDEM_V2,
    TEXAS_V2_MVI_BOOST_MULTIPLIER,
    TEXAS_V2_MVI_BOOST_THRESHOLD,
    TEXAS_V2_RULE_VERSION,
    TEXAS_V2_THESIS_CHECKPOINT_SECONDS,
    TEXAS_V2_THESIS_UNFAVORABLE_DISTANCE,
    calibration_metrics,
    clamp,
    iso_now,
    momentum_return,
    parse_time,
    realized_volatility,
    settlement_probability,
)
from app.services.bootstrap import HistoricalBootstrapService
from app.services.broker import KalshiBroker
from app.services.decision import (
    Decision,
    make_decision,
    make_trade_assessment,
    material_change,
)
from app.services.directional_momentum import regression_momentum
from app.services.forecast import Forecast, forecast_label, make_forecast
from app.services.kalshi import (
    KalshiPublicClient,
    as_float,
    market_strike,
    orderbook_metrics,
)
from app.services.market_data import (
    BitcoinCompositeFeed,
    CompositeQuote,
    ExchangeQuote,
    live_composite_quote,
)
from app.services.margin_volatility import MarginVolatilityService
from app.services.paper import PaperTradingService
from app.services.streaming import BitcoinWebSocketFeeds, KalshiWebSocketFeed
from app.services.training import ModelManager
from app.services.trading import TradingCoordinator
from app.services.trade_review import TradeReviewService
from app.services.volume_signals import VolumeSignalService


logger = logging.getLogger(__name__)

# Kalshi REST is used for both ordinary account reads and time-critical
# reduce-only exits.  Keep authenticated connections around across the quiet
# gaps between 15-minute markets instead of falling back to a new TCP/TLS
# handshake for the next protective request.  The cap still leaves headroom
# for public market-data reads without allowing a reconciliation burst to
# create an unbounded connection pool.
KALSHI_HTTP_LIMITS = httpx.Limits(
    max_connections=12,
    max_keepalive_connections=8,
    keepalive_expiry=120.0,
)

# Public price discovery must remain independent from authenticated account
# recovery.  In particular, a slow /portfolio request must never consume the
# connection pool that keeps the displayed BTC/contract price and order book
# current.
PUBLIC_MARKET_HTTP_LIMITS = httpx.Limits(
    max_connections=8,
    max_keepalive_connections=4,
    keepalive_expiry=30.0,
)
PUBLIC_MARKET_HTTP_TIMEOUT = httpx.Timeout(
    connect=2.5,
    read=3.5,
    write=3.5,
    pool=1.0,
)

class AnalysisEngine:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.http: httpx.AsyncClient | None = None
        self.trading_http: httpx.AsyncClient | None = None
        self.bitcoin: BitcoinCompositeFeed | None = None
        self.kalshi: KalshiPublicClient | None = None
        self.kalshi_demo: KalshiPublicClient | None = None
        self.paper = PaperTradingService(db)
        self.margin_volatility = MarginVolatilityService(db)
        self.trade_reviews = TradeReviewService(db)
        self.volume_signals = VolumeSignalService(db)
        self.trading = TradingCoordinator(config, db, self.paper)
        self.models = ModelManager(db)
        self._benchmark_calibration: dict[str, Any] = {
            "sample_size": 0,
            "calibrated": False,
            "bias_pct": 0.0,
            "residual_sigma_pct": None,
            "uncertainty_pct": DEFAULT_BENCHMARK_UNCERTAINTY_PCT,
        }
        # Startup must show the last evaluated calibration, not an empty
        # placeholder or a synchronous historical retraining scan.
        self._calibration_summary: dict[str, Any] = self.models.latest_calibration_summary()
        self._recent_btc_samples: list[tuple[float, float]] = []
        self._recent_btc_volume_points: list[tuple[float, float]] = []
        self._btc_samples_loaded = False
        self._next_threshold_forecast = NextThresholdForecast()
        self.dashboard: dict[str, Any] = {
            "system": {"status": "starting", "message": "Connecting to public data feeds."},
            "current": None,
            "next": None,
            "next_threshold_forecast": None,
            "btc": None,
            "notification": None,
            "strategy": {"texas_holdem": self._texas_recovery_state()},
        }
        self._runner: asyncio.Task[None] | None = None
        self._initial_collect_task: asyncio.Task[None] | None = None
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._kalshi_stream_task: asyncio.Task[None] | None = None
        self._kalshi_book_fallback_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._publish_task: asyncio.Task[None] | None = None
        self._trade_review_task: asyncio.Task[None] | None = None
        self._pending_trade_review: tuple[dict[str, Any], str] | None = None
        self._last_trade_review_key: tuple[str, int, str] | None = None
        self._live_refresh_task: asyncio.Task[None] | None = None
        self._trading_summary_task: asyncio.Task[None] | None = None
        self._volume_history_task: asyncio.Task[None] | None = None
        self._volume_flush_task: asyncio.Task[None] | None = None
        self._trading_summary: dict[str, Any] = {
            "selected_mode": "PAPER", "selected": {}, "modes": {}
        }
        self._stopping = asyncio.Event()
        self._update_lock = asyncio.Lock()
        self._previous_ticker: str | None = None
        self._pending_settlements: set[str] = set()
        self._last_settlement_check = 0.0
        self._last_live_update = 0.0
        self._last_btc_persist = 0.0
        self._last_kalshi_persist = 0.0
        self._last_kalshi_ws_book = 0.0
        self._latest_quotes: dict[str, ExchangeQuote] = {}
        self._latest_btc: dict[str, Any] | None = None
        self._current_market: dict[str, Any] | None = None
        self._next_market: dict[str, Any] | None = None
        self._market_state: dict[str, Any] | None = None
        self._execution_market_state: dict[str, Any] | None = None
        self._stream_status: dict[str, dict[str, Any]] = {
            "Coinbase": {"connected": False},
            "Kraken": {"connected": False},
            "Kalshi": {
                "connected": False,
                "configured": bool(
                    config.kalshi_api_key_id and config.kalshi_private_key_path
                ),
            },
        }

    async def start(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=PUBLIC_MARKET_HTTP_TIMEOUT,
            headers={"User-Agent": "kalshi-model/0.1 local-analysis-only"},
            limits=PUBLIC_MARKET_HTTP_LIMITS,
        )
        self.trading_http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers={"User-Agent": "kalshi-model/0.1 local-analysis-only"},
            limits=KALSHI_HTTP_LIMITS,
        )
        self.bitcoin = BitcoinCompositeFeed(self.http)
        self.kalshi = KalshiPublicClient(
            self.http, self.config.kalshi_api_base, self.config.kalshi_series
        )
        self.kalshi_demo = KalshiPublicClient(
            self.http, self.config.kalshi_demo_api_base, self.config.kalshi_series
        )
        await self.trading.start(self.trading_http)
        self._runner = asyncio.create_task(self._run_loop())
        self._initial_collect_task = asyncio.create_task(self._collect_initial())
        # A large trading archive is cold state.  Feature hydration is bounded
        # and off-loop; quote handling starts immediately without it.
        self._volume_history_task = asyncio.create_task(self._load_volume_history())
        bitcoin_streams = BitcoinWebSocketFeeds(
            self._handle_stream_quote, self._handle_stream_status,
            self._handle_stream_trade,
        )
        self._stream_tasks.extend(
            [
                asyncio.create_task(bitcoin_streams.run_coinbase()),
                asyncio.create_task(bitcoin_streams.run_kraken()),
            ]
        )
        self._start_kalshi_stream()
        self._kalshi_book_fallback_task = asyncio.create_task(
            self._run_kalshi_book_fallback()
        )
        # Historical bootstrap can read a large local archive.  It is manual
        # work, never a startup prerequisite for live connectivity.
        self.dashboard["bootstrap"] = {
            "status": "not_started",
            "message": "Historical bootstrap is available on demand in Settings.",
        }

    async def stop(self) -> None:
        self._stopping.set()
        tasks = [task for task in (
            self._runner,
            self._initial_collect_task,
            self._bootstrap_task,
            self._publish_task,
            self._trade_review_task,
            self._live_refresh_task,
            self._trading_summary_task,
            self._volume_history_task,
            self._volume_flush_task,
            self._kalshi_stream_task,
            self._kalshi_book_fallback_task,
            *self._stream_tasks,
        ) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.trading.stop()
        if self.http:
            await self.http.aclose()
        if self.trading_http:
            await self.trading_http.aclose()

    async def _collect_initial(self) -> None:
        try:
            await self.collect_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Initial collection failed")
            self._degrade(str(exc))

    def _start_kalshi_stream(self) -> None:
        key_id = self.config.kalshi_api_key_id
        key_path = self.config.kalshi_private_key_path
        configured = bool(key_id and key_path)
        self._stream_status["Kalshi"] = {
            "connected": False,
            "configured": configured,
        }
        if not configured:
            self._kalshi_stream_task = None
            return
        kalshi_stream = KalshiWebSocketFeed(
            self.config.kalshi_ws_url,
            str(key_id),
            Path(key_path),
            lambda: str(self._current_market["ticker"]) if self._current_market else None,
            self._handle_kalshi_message,
            self._handle_stream_status,
        )
        self._kalshi_stream_task = asyncio.create_task(kalshi_stream.run())

    async def set_kalshi_credentials(
        self,
        key_id: str | None,
        private_key_path: Path | None,
        source: str,
    ) -> None:
        if self._kalshi_stream_task:
            self._kalshi_stream_task.cancel()
            await asyncio.gather(self._kalshi_stream_task, return_exceptions=True)
            self._kalshi_stream_task = None
        self.config = replace(
            self.config,
            kalshi_api_key_id=key_id,
            kalshi_private_key_path=private_key_path,
            kalshi_credentials_source=source,
        )
        self._start_kalshi_stream()
        current = self.dashboard.get("current") or {}
        reliable = bool(
            current
            and current.get("decision", {}).get("reason_code") != "DATA_UNRELIABLE"
        )
        self.dashboard["system"] = self._system_state(reliable, iso_now())
        self.dashboard["strategy"] = {"texas_holdem": self._texas_recovery_state()}
        self._schedule_publish()

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self.config.poll_seconds)
                await self.collect_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # Keep the local monitor alive through transient outages.
                logger.exception("Collection loop failed")
                self._degrade(str(exc))

    async def _bootstrap(self) -> None:
        assert self.kalshi and self.bitcoin
        try:
            result = await HistoricalBootstrapService(
                self.db, self.kalshi, self.bitcoin
            ).run()
            self._benchmark_calibration = self.models.benchmark_calibration()
            report = self.models.evaluate_and_retrain("automatic historical bootstrap")
            self._calibration_summary = dict(report["current"])
            self.dashboard["bootstrap"] = {"status": "complete", **result, "report": report["tldr"]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Historical bootstrap failed")
            self.dashboard["bootstrap"] = {
                "status": "failed",
                "error": str(exc),
                "message": "Live analysis continues; bootstrap can be retried from Settings.",
            }

    async def run_bootstrap(self) -> dict[str, Any]:
        assert self.kalshi and self.bitcoin
        result = await HistoricalBootstrapService(self.db, self.kalshi, self.bitcoin).run()
        self._benchmark_calibration = self.models.benchmark_calibration()
        report = self.models.evaluate_and_retrain("manual historical bootstrap")
        self._calibration_summary = dict(report["current"])
        self.dashboard["bootstrap"] = {"status": "complete", **result, "report": report["tldr"]}
        return self.dashboard["bootstrap"]

    async def collect_once(self) -> None:
        assert self.bitcoin and self.kalshi
        composite, market_pair = await asyncio.gather(
            self.bitcoin.fetch(), self.kalshi.active_markets()
        )
        current_market, next_market = market_pair
        observed_at = iso_now()
        btc_state = await self._save_bitcoin(composite, observed_at)
        self._latest_quotes = {quote.exchange: quote for quote in composite.quotes}
        self._latest_btc = btc_state
        self._current_market = current_market
        self._next_market = next_market
        self._update_next_threshold_forecast(observed_at)
        settings = self.db.settings()
        current_payload = None
        notification = None

        if current_market and btc_state.get("price") and market_strike(current_market):
            ticker = str(current_market["ticker"])
            self._save_market(current_market, observed_at)
            self._record_threshold_observation(
                current_market, observed_at, source="REST", event_type="poll"
            )
            orderbook_payload, kalshi_trades = await asyncio.gather(
                self.kalshi.orderbook(ticker),
                self.kalshi.trades(
                    ticker,
                    min_ts=int((datetime.now(UTC) - timedelta(minutes=10)).timestamp()),
                ),
                return_exceptions=True,
            )
            if isinstance(orderbook_payload, BaseException):
                raise orderbook_payload
            if not isinstance(kalshi_trades, BaseException):
                self.volume_signals.record_kalshi_trades(ticker, kalshi_trades)
            market_state = self._save_kalshi_snapshot(
                current_market, orderbook_payload, observed_at
            )
            self._market_state = market_state
            execution_market_state = await self._execution_state_for(
                current_market, market_state, observed_at
            )
            self._execution_market_state = execution_market_state
            self.paper.process_open_orders(ticker, market_state)
            current_payload, notification = self._analyze(
                current_market,
                market_state,
                btc_state,
                settings,
                observed_at,
                execution_market_state=execution_market_state,
            )
            current_payload["btc_proxy"] = btc_state.get("price")
            current_payload["btc_state"] = btc_state
            self.paper.process_texas_holdem_exits(ticker, current_payload)
            self.paper.process_threshold_breach_exits(ticker, current_payload)
            self._schedule_trade_review(current_payload, observed_at)
            if self._previous_ticker and self._previous_ticker != ticker:
                self._pending_settlements.add(self._previous_ticker)
            self._previous_ticker = ticker
        elif current_market:
            self._market_state = None
            self._execution_market_state = None
            self._save_market(current_market, observed_at)
            self._record_threshold_observation(
                current_market, observed_at, source="REST", event_type="poll"
            )
            reason = (
                "The contract threshold is missing from the Kalshi response."
                if not market_strike(current_market)
                else "Fewer than two reliable BTC exchange feeds are available."
            )
            current_payload = self._unreliable_current(
                current_market, reason
            )
            current_payload["btc_proxy"] = btc_state.get("price")
            current_payload["btc_state"] = btc_state
            self._schedule_trade_review(current_payload, observed_at)
        elif self._previous_ticker:
            self._market_state = None
            self._execution_market_state = None
            self._pending_settlements.add(self._previous_ticker)
            self._previous_ticker = None

        if next_market:
            self._save_market(next_market, observed_at)
            self._record_threshold_observation(
                next_market, observed_at, source="REST", event_type="poll"
            )
        if time.monotonic() - self._last_settlement_check >= 30:
            await self._check_pending_settlements()
            self._last_settlement_check = time.monotonic()

        reliability = bool(current_payload and current_payload["decision"]["reason_code"] != "DATA_UNRELIABLE")
        self.dashboard = {
            **self.dashboard,
            "system": self._system_state(reliability, observed_at),
            "btc": btc_state,
            "current": current_payload,
            "next": self._market_summary(next_market) if next_market else None,
            "next_threshold_forecast": self._next_threshold_forecast_state(),
            "notification": notification,
            "strategy": {"texas_holdem": self._texas_recovery_state()},
            "paper": self._portfolio_summary(),
            "trading": self._trading_summary,
            "calibration": self.calibration_summary(),
            "model": self.models.active(),
        }
        self._schedule_publish()
        self.trading.schedule_process(current_payload)
        self._schedule_trading_summary()

    async def _execution_state_for(
        self,
        market: dict[str, Any],
        live_state: dict[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        """Return quotes from the exchange that will execute the selected mode."""
        if self.trading.selected_mode != "DEMO":
            state = dict(live_state)
            state["execution_market_mode"] = "LIVE"
            state["execution_data_error"] = None
            return state

        ticker = str(market["ticker"])
        if self.kalshi_demo is None:
            return self._empty_execution_state(
                market, observed_at, "Demo order book is unavailable."
            )
        try:
            demo_market, demo_book = await asyncio.gather(
                self.kalshi_demo.market(ticker),
                self.kalshi_demo.orderbook(ticker),
            )
            state = self._save_kalshi_snapshot(
                {**market, **demo_market},
                demo_book,
                observed_at,
                persist=False,
                allow_summary_fallback=False,
            )
            state["execution_market_mode"] = "DEMO"
            state["execution_data_error"] = None
            return state
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            logger.warning("Demo execution book unavailable for %s", ticker, exc_info=True)
            return self._empty_execution_state(
                market, observed_at, "Demo order book is unavailable."
            )

    def _empty_execution_state(
        self, market: dict[str, Any], observed_at: str, reason: str
    ) -> dict[str, Any]:
        state = self._save_kalshi_snapshot(
            market,
            {"orderbook_fp": {}},
            observed_at,
            persist=False,
            allow_summary_fallback=False,
        )
        state["execution_market_mode"] = "DEMO"
        state["execution_data_error"] = reason
        return state

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    def refresh_trading_dashboard(self) -> None:
        """Publish current broker safety state without waiting for a market tick."""
        self._schedule_trading_summary()
        self.dashboard["strategy"] = {"texas_holdem": self._texas_recovery_state()}
        self._schedule_publish()

    def _schedule_trading_summary(self) -> None:
        if self._trading_summary_task and not self._trading_summary_task.done():
            return

        async def refresh() -> None:
            summary = await asyncio.to_thread(
                self.trading.summary, self.dashboard.get("current")
            )
            self._trading_summary = summary
            self.dashboard["trading"] = summary
            self._schedule_publish()

        self._trading_summary_task = asyncio.create_task(refresh())

    def _texas_recovery_state(self) -> dict[str, Any]:
        """Keep the Texas HUD visible while public market data reconnects."""
        settings = self.db.settings()
        targets = {
            "flop": float(settings.get("texas_holdem_flop_target", .60)),
            "flop_stop": float(settings.get("texas_holdem_flop_stop", .60)),
            "turn": float(settings.get("texas_holdem_turn_target", .50)),
            "turn_stop": float(settings.get("texas_holdem_turn_stop", .60)),
            "river": float(settings.get("texas_holdem_river_target", .95)),
            "river_stop": float(settings.get("texas_holdem_river_stop", .60)),
        }
        return {
            "enabled": bool(settings.get("texas_holdem_enabled", False)),
            "strategy": TEXAS_HOLDEM_V2,
            "display_name": "Texas Hold’em 2.0",
            "status": "WAITING_FOR_MARKET_DATA",
            "phase": {"key": "FLOP", "label": "The Flop"},
            "side": None,
            "attempt_count": 0,
            "maximum_attempts": 1 + int(settings.get("texas_holdem_additional_retries", 2)),
            "filled_contracts": 0,
            "target_contracts": None,
            "executable_bid": None,
            "entry_price_cap": float(settings.get("texas_holdem_max_entry_price", .50)),
            "targets": targets,
            "active_target": targets["flop"],
            "blocker": "Waiting for Kalshi market data.",
            "market_open_time": None,
            "pass": {"passed": False, "scheduled": False, "next_open_time": None},
            "threshold_breach_exempt": True,
            "rules": {
                "version": TEXAS_V2_RULE_VERSION,
                "mvi_minimum": self.paper._texas_v2_mvi_minimum(
                    settings, str(settings.get("trading_mode") or "PAPER")
                ),
                "mvi_boost_threshold": TEXAS_V2_MVI_BOOST_THRESHOLD,
                "mvi_boost_multiplier": TEXAS_V2_MVI_BOOST_MULTIPLIER,
                "thesis_checkpoint_seconds": TEXAS_V2_THESIS_CHECKPOINT_SECONDS,
                "thesis_unfavorable_distance": TEXAS_V2_THESIS_UNFAVORABLE_DISTANCE,
            },
            "thesis": {"enabled": True, "status": "WAITING"},
        }

    def _schedule_publish(self) -> None:
        if self._publish_task and not self._publish_task.done():
            return
        self._publish_task = asyncio.create_task(self._publish_after_delay())

    async def _publish_after_delay(self) -> None:
        await asyncio.sleep(self.config.live_update_seconds)
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(None)

    def _system_state(self, reliable: bool, observed_at: str) -> dict[str, Any]:
        bitcoin_sources = [
            name
            for name in ("Coinbase", "Kraken")
            if self._stream_status[name].get("connected")
        ]
        kalshi_status = self._stream_status["Kalshi"]
        # Streaming market data and authenticated order REST are independent
        # paths. A live websocket must not be presented as proof that orders
        # can currently be submitted.
        selected_broker = self.trading.broker()
        execution_readiness = (
            selected_broker.readiness()
            if hasattr(selected_broker, "readiness")
            else None
        )
        streaming = bool(bitcoin_sources or kalshi_status.get("connected"))
        return {
            "status": "live" if reliable else "degraded",
            "message": (
                "Live WebSocket feeds"
                if reliable and streaming
                else "Live REST fallback"
                if reliable
                else "Signal withheld until critical data recovers"
            ),
            "updated_at": observed_at,
            "poll_seconds": self.config.poll_seconds,
            "live_update_seconds": self.config.live_update_seconds,
            "read_only": True,
            "streams": {
                "bitcoin": {
                    "connected": bool(bitcoin_sources),
                    "sources": bitcoin_sources,
                    "status": {
                        name: dict(self._stream_status.get(name, {}))
                        for name in ("Coinbase", "Kraken", "Bitstamp")
                    },
                },
                "kalshi": {
                    "connected": bool(kalshi_status.get("connected")),
                    "configured": bool(kalshi_status.get("configured")),
                    "error": kalshi_status.get("error"),
                    "fallback": dict(kalshi_status.get("fallback") or {}),
                },
                "kalshi_execution": {
                    "last_rest_available": bool(
                        execution_readiness
                        and execution_readiness.get("connected")
                        and execution_readiness.get("authenticated")
                    ),
                    "reconciled": bool(execution_readiness and execution_readiness.get("reconciled")),
                    "error": execution_readiness.get("last_error") if execution_readiness else None,
                },
            },
        }

    async def _handle_stream_status(
        self, source: str, connected: bool, error: str | None
    ) -> None:
        status = self._stream_status.setdefault(source, {})
        status["connected"] = connected
        status["updated_at"] = iso_now()
        if error:
            status["error"] = error
        else:
            status.pop("error", None)
        current = self.dashboard.get("current") or {}
        reliable = bool(
            current
            and current.get("decision", {}).get("reason_code") != "DATA_UNRELIABLE"
        )
        self.dashboard["system"] = self._system_state(reliable, iso_now())
        self.dashboard["strategy"] = {"texas_holdem": self._texas_recovery_state()}
        self._schedule_publish()

    async def _handle_stream_quote(self, quote: ExchangeQuote) -> None:
        self._latest_quotes[quote.exchange] = quote
        self._schedule_live_refresh()

    async def _handle_stream_trade(self, trade: Any) -> None:
        self.volume_signals.add_trade(trade)
        self._schedule_volume_flush()

    async def _load_volume_history(self) -> None:
        try:
            await asyncio.to_thread(self.volume_signals.load_rolling_history)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Trade-flow features stay unavailable rather than blocking prices.
            logger.warning("Could not hydrate rolling volume history", exc_info=True)

    def _schedule_volume_flush(self) -> None:
        if self._volume_flush_task and not self._volume_flush_task.done():
            return
        self._volume_flush_task = asyncio.create_task(self._flush_volume_after())

    async def _flush_volume_after(self) -> None:
        await asyncio.sleep(0.5)
        try:
            await asyncio.to_thread(self.volume_signals.flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not persist BTC trade-flow batch", exc_info=True)

    def _schedule_live_refresh(self) -> None:
        if self._live_refresh_task and not self._live_refresh_task.done():
            return
        elapsed = time.monotonic() - self._last_live_update
        delay = max(0.0, self.config.live_update_seconds - elapsed)
        self._live_refresh_task = asyncio.create_task(self._refresh_live_after(delay))

    async def _refresh_live_after(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        async with self._update_lock:
            quotes = list(self._latest_quotes.values())
            observed_at = iso_now()
            preferred = {
                name
                for name in ("Coinbase", "Kraken")
                if self._stream_status[name].get("connected")
            }
            composite = live_composite_quote(
                quotes, preferred,
                maximum_age_seconds=float(self.db.settings().get("max_data_age_seconds", 20)),
            )
            if composite.price is not None:
                now = time.monotonic()
                persist = now - self._last_btc_persist >= 1.0
                self._latest_btc = await self._save_bitcoin(
                    composite, observed_at, persist=persist
                )
                if persist:
                    self._last_btc_persist = now
            self._update_next_threshold_forecast(observed_at)
            self._refresh_cached_dashboard(observed_at)
            self._last_live_update = time.monotonic()

    async def _handle_kalshi_message(
        self, message: dict[str, Any], book_metrics: dict[str, Any] | None
    ) -> None:
        message_type = str(message.get("type") or "")
        if message_type in {"market_lifecycle", "market_lifecycle_v2"}:
            await self._handle_market_lifecycle(message)
            return
        if not self._current_market:
            return
        payload = message.get("msg") or {}
        if payload.get("market_ticker") != self._current_market.get("ticker"):
            return
        async with self._update_lock:
            for key in (
                "yes_bid_dollars",
                "yes_ask_dollars",
                "yes_bid_size_fp",
                "yes_ask_size_fp",
                "volume_fp",
                "open_interest_fp",
            ):
                if payload.get(key) is not None:
                    self._current_market[key] = payload[key]
            if book_metrics is not None:
                self._last_kalshi_ws_book = time.monotonic()
                now = time.monotonic()
                persist = now - self._last_kalshi_persist >= 1.0
                book_payload = {
                    "orderbook_fp": {
                        "yes_dollars": book_metrics["yes_bids"],
                        "no_dollars": book_metrics["no_bids"],
                    }
                }
                self._market_state = self._save_kalshi_snapshot(
                    self._current_market,
                    book_payload,
                    iso_now(),
                    persist=persist,
                )
                if persist:
                    self._last_kalshi_persist = now
            elif message.get("type") == "ticker":
                self._market_state = self._ticker_market_state(
                    self._current_market, self._market_state
                )
        self._schedule_live_refresh()

    def _kalshi_book_needs_fallback(self) -> bool:
        """A connected socket is not proof that its book is still moving."""
        status = self._stream_status["Kalshi"]
        return (
            not status.get("connected")
            or self._last_kalshi_ws_book <= 0
            or time.monotonic() - self._last_kalshi_ws_book
            > self.config.kalshi_book_stale_seconds
        )

    async def _run_kalshi_book_fallback(self) -> None:
        """Fetch only the cached active book while the market socket is stale.

        No BTC, account, analysis, trade-history, reconciliation, or database
        path is awaited here.  A fresh websocket book wins immediately.
        """
        delay = self.config.kalshi_book_fallback_seconds
        while not self._stopping.is_set():
            try:
                if not self.kalshi or not self._kalshi_book_needs_fallback():
                    delay = self.config.kalshi_book_fallback_seconds
                    await asyncio.sleep(delay)
                    continue
                market = self._current_market
                ticker = str(market.get("ticker") or "") if market else ""
                if not ticker:
                    await asyncio.sleep(delay)
                    continue
                started = time.monotonic()
                payload = await self.kalshi.fallback_orderbook(ticker)
                received_at = iso_now()
                receive_ms = round((time.monotonic() - started) * 1000, 1)
                await self._apply_kalshi_fallback_book(
                    ticker, payload, received_at, receive_ms
                )
                delay = self.config.kalshi_book_fallback_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep diagnostics actionable without leaking request content.
                fallback = self._stream_status["Kalshi"].setdefault("fallback", {})
                fallback.update({
                    "active": True,
                    "error": type(exc).__name__,
                    "last_error_at": iso_now(),
                })
                delay = min(
                    8.0,
                    max(delay * 2, self.config.kalshi_book_fallback_seconds),
                )
            await asyncio.sleep(delay + random.uniform(0.0, min(0.2, delay * 0.2)))

    async def _apply_kalshi_fallback_book(
        self,
        ticker: str,
        payload: dict[str, Any],
        received_at: str,
        receive_ms: float,
    ) -> None:
        """Apply a REST book only when it still belongs to the active ticker."""
        applied_started = time.monotonic()
        protective_current: dict[str, Any] | None = None
        async with self._update_lock:
            market = self._current_market
            if not market or str(market.get("ticker") or "") != ticker:
                return
            # Do not overwrite a newer websocket book received in flight.
            if not self._kalshi_book_needs_fallback():
                return
            state = self._save_kalshi_snapshot(market, payload, received_at, persist=False)
            state.update({
                "quote_source": "REST_FALLBACK",
                "received_at": received_at,
                "fallback_receive_ms": receive_ms,
            })
            self._market_state = state
            # The order-book UI gets the new executable levels immediately,
            # without synchronously rebuilding analysis or touching SQLite.
            current = self.dashboard.get("current")
            if current and str(current.get("ticker") or "") == ticker:
                updated = dict(current)
                updated.update({
                    key: state[key] for key in (
                        "observed_at", "executable_quote_at", "yes_bid", "yes_ask",
                        "no_bid", "no_ask", "spread", "yes_bid_size", "yes_ask_size",
                        "no_bid_size", "no_ask_size", "imbalance", "orderbook",
                        "quote_source", "received_at", "fallback_receive_ms",
                    )
                })
                # The Texas HUD's price is the executable bid for its held
                # outcome, not the stale value from the last full analysis.
                # A Live public book is never substituted into a Demo/Paper
                # execution view.
                if str(updated.get("execution_market_mode") or "") == "LIVE":
                    texas = updated.get("texas_holdem")
                    side = str((texas or {}).get("side") or "").upper()
                    bid = state.get(f"{side.lower()}_bid") if side in {"YES", "NO"} else None
                    if isinstance(texas, dict) and bid is not None:
                        updated_texas = dict(texas)
                        updated_texas["executable_bid"] = bid
                        updated["texas_holdem"] = updated_texas
                    # Phase boundaries are time based.  Keep an exit-only pass
                    # accurate even if a BTC/full-analysis update is delayed.
                    close = parse_time(updated.get("close_time") or market.get("close_time"))
                    received = parse_time(received_at)
                    if close and received:
                        updated["time_remaining_seconds"] = max(
                            0.0, (close - received).total_seconds()
                        )
                    # This is a snapshot, not an entry signal.  The trading
                    # coordinator coalesces it into one Live exit-only pass.
                    protective_current = updated
                self.dashboard["current"] = updated
            fallback = self._stream_status["Kalshi"].setdefault("fallback", {})
            fallback.update({
                "active": True,
                "last_received_at": received_at,
                "receive_ms": receive_ms,
                "apply_ms": round((time.monotonic() - applied_started) * 1000, 1),
            })
            fallback.pop("error", None)
        fallback["published_at"] = iso_now()
        self._schedule_publish()
        if protective_current is not None:
            self.trading.schedule_protective_exits("LIVE", protective_current)

    async def _handle_market_lifecycle(self, message: dict[str, Any]) -> None:
        payload = message.get("msg") or {}
        ticker = str(payload.get("market_ticker") or payload.get("ticker") or "")
        if not ticker:
            return
        observed_at = iso_now()
        async with self._update_lock:
            market = None
            for candidate in (self._current_market, self._next_market):
                if candidate and str(candidate.get("ticker")) == ticker:
                    market = candidate
                    break
            additional = payload.get("additional_metadata") or {}
            floor_strike = payload.get("floor_strike", additional.get("floor_strike"))
            if (
                market is None
                and ticker.startswith(self.config.kalshi_series)
                and floor_strike is not None
            ):
                def timestamp(value: Any) -> str | None:
                    try:
                        return datetime.fromtimestamp(float(value), UTC).isoformat()
                    except (TypeError, ValueError, OSError):
                        return None

                market = {
                    "ticker": ticker,
                    "event_ticker": additional.get("event_ticker"),
                    "status": "initialized",
                    "title": additional.get("title") or additional.get("name"),
                    "floor_strike": floor_strike,
                    "open_time": timestamp(payload.get("open_ts")),
                    "close_time": timestamp(payload.get("close_ts")),
                    "expected_expiration_time": timestamp(
                        additional.get("expected_expiration_ts")
                    ),
                    "rules_primary": additional.get("rules_primary"),
                    "rules_secondary": additional.get("rules_secondary"),
                }
                new_open = parse_time(market.get("open_time"))
                existing_open = parse_time(
                    self._next_market.get("open_time") if self._next_market else None
                )
                if self._next_market is None or (
                    new_open is not None
                    and (existing_open is None or new_open < existing_open)
                ):
                    self._next_market = market
            if market is None:
                return
            if floor_strike is not None:
                market["floor_strike"] = floor_strike
            if payload.get("status") is not None:
                market["status"] = payload["status"]
            else:
                lifecycle_status = {
                    "created": "initialized",
                    "activated": "active",
                    "deactivated": "inactive",
                    "determined": "determined",
                    "settled": "finalized",
                }.get(str(payload.get("event_type") or ""))
                if lifecycle_status:
                    market["status"] = lifecycle_status
            self._save_market(market, observed_at)
            self._record_threshold_observation(
                market,
                observed_at,
                source="WEBSOCKET",
                event_type=str(payload.get("event_type") or "lifecycle"),
            )
            if self._next_market is market:
                self.dashboard["next"] = self._market_summary(market)
            self._update_next_threshold_forecast(observed_at)
        self._schedule_live_refresh()

    @staticmethod
    def _ticker_market_state(
        market: dict[str, Any], previous: dict[str, Any] | None
    ) -> dict[str, Any]:
        state = dict(previous or {})
        old_mid = None
        if state.get("yes_bid") is not None and state.get("yes_ask") is not None:
            old_mid = (float(state["yes_bid"]) + float(state["yes_ask"])) / 2
        yes_bid = as_float(market.get("yes_bid_dollars"))
        yes_ask = as_float(market.get("yes_ask_dollars"))
        if yes_bid is None:
            yes_bid = state.get("yes_bid")
        if yes_ask is None:
            yes_ask = state.get("yes_ask")
        new_mid = (
            (float(yes_bid) + float(yes_ask)) / 2
            if yes_bid is not None and yes_ask is not None
            else None
        )
        state.update(
            {
                "ticker": market["ticker"],
                "observed_at": iso_now(),
                "executable_quote_at": iso_now(),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": 1.0 - float(yes_ask) if yes_ask is not None else None,
                "no_ask": 1.0 - float(yes_bid) if yes_bid is not None else None,
                "spread": (
                    float(yes_ask) - float(yes_bid)
                    if yes_bid is not None and yes_ask is not None
                    else None
                ),
                "liquidity": as_float(market.get("liquidity_dollars")) or state.get("liquidity", 0.0),
                "open_interest": as_float(market.get("open_interest_fp")) or state.get("open_interest", 0.0),
                "volume": as_float(market.get("volume_fp")) or state.get("volume", 0.0),
                "yes_bid_size": as_float(market.get("yes_bid_size_fp")) or state.get("yes_bid_size"),
                "yes_ask_size": as_float(market.get("yes_ask_size_fp")) or state.get("yes_ask_size"),
                "no_ask_size": as_float(market.get("yes_bid_size_fp")) or state.get("no_ask_size"),
                "no_bid_size": as_float(market.get("yes_ask_size_fp")) or state.get("no_bid_size"),
                "rapid_repricing": (
                    new_mid - old_mid
                    if new_mid is not None and old_mid is not None
                    else 0.0
                ),
                "price_level_structure": market.get("price_level_structure")
                or state.get("price_level_structure"),
                "price_ranges": market.get("price_ranges")
                or state.get("price_ranges"),
            }
        )
        return state

    def _refresh_cached_dashboard(self, observed_at: str) -> None:
        if not (self._current_market and self._market_state and self._latest_btc):
            return
        # Market lifecycle messages can replace the active contract with a shell
        # before Kalshi publishes its threshold. Ignore queued quote refreshes until
        # the REST poll has a complete, matching market again.
        if (
            market_strike(self._current_market) is None
            or self._latest_btc.get("price") is None
            or self._market_state.get("ticker") != self._current_market.get("ticker")
        ):
            return
        self.paper.process_open_orders(
            str(self._current_market["ticker"]), self._market_state
        )
        if self.trading.selected_mode == "DEMO":
            execution_state = self._execution_market_state
            if (
                execution_state is None
                or execution_state.get("ticker") != self._current_market.get("ticker")
                or execution_state.get("execution_market_mode") != "DEMO"
            ):
                execution_state = self._empty_execution_state(
                    self._current_market,
                    observed_at,
                    "Waiting for the Demo order book.",
                )
        else:
            execution_state = self._market_state
        current, notification = self._analyze(
            self._current_market,
            self._market_state,
            self._latest_btc,
            self.db.settings(),
            observed_at,
            execution_market_state=execution_state,
        )
        current["btc_proxy"] = self._latest_btc.get("price")
        current["btc_state"] = self._latest_btc
        self.paper.process_texas_holdem_exits(
            str(self._current_market["ticker"]), current
        )
        self.paper.process_threshold_breach_exits(
            str(self._current_market["ticker"]), current
        )
        self._schedule_trade_review(current, observed_at)
        # Protection and order evaluation must not wait for summary assembly,
        # which can involve local history reads for all account modes.
        self.trading.schedule_process(current)
        reliable = current["decision"]["reason_code"] != "DATA_UNRELIABLE"
        self.dashboard = {
            **self.dashboard,
            "system": self._system_state(reliable, observed_at),
            "btc": self._latest_btc,
            "current": current,
            "next": self._market_summary(self._next_market) if self._next_market else None,
            "notification": notification,
            "strategy": {"texas_holdem": self._texas_recovery_state()},
            "paper": self._portfolio_summary(),
            "trading": self._trading_summary,
        }
        self._schedule_publish()
        self._schedule_trading_summary()

    def _schedule_trade_review(
        self, current: dict[str, Any], observed_at: str
    ) -> None:
        parsed = parse_time(observed_at) or datetime.now(UTC)
        bucket = int(parsed.timestamp()) // 5
        readiness = current.get("standard_edge_readiness") or {}
        signature = json.dumps(
            {
                "forecast": (current.get("forecast") or {}).get("signal"),
                "side": readiness.get("side"),
                "status": readiness.get("status"),
                "gates": {
                    key: bool((value or {}).get("passed"))
                    for key, value in (readiness.get("gates") or {}).items()
                },
                "model": current.get("model_version"),
                "entered": (current.get("automatic_entry") or {}).get("entered"),
            },
            sort_keys=True,
        )
        key = (str(current.get("ticker") or ""), bucket, signature)
        if key == self._last_trade_review_key:
            return
        self._last_trade_review_key = key
        payload = dict(current)
        payload["btc_proxy"] = (self._latest_btc or {}).get("price")
        payload["btc_state"] = self._latest_btc or {}
        self._pending_trade_review = (payload, observed_at)
        if not self._trade_review_task or self._trade_review_task.done():
            self._trade_review_task = asyncio.create_task(
                self._drain_trade_reviews()
            )

    async def _drain_trade_reviews(self) -> None:
        while self._pending_trade_review is not None:
            payload, observed_at = self._pending_trade_review
            self._pending_trade_review = None
            await asyncio.to_thread(
                self.trade_reviews.observe, payload, observed_at
            )

    async def _save_bitcoin(
        self, composite: CompositeQuote, observed_at: str, *, persist: bool = True
    ) -> dict[str, Any]:
        if composite.price is None:
            return {
                "price": None,
                "exchange_count": len(composite.quotes),
                "errors": composite.errors,
            }
        if persist:
            self.volume_signals.audit_cumulative(composite, observed_at)
            for quote in composite.quotes:
                self.db.execute(
                    """
                    INSERT INTO exchange_quotes(
                        observed_at,exchange,price,bid,ask,volume,latency_ms
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        observed_at, quote.exchange, quote.price, quote.bid, quote.ask,
                        quote.volume, quote.latency_ms,
                    ),
                )
        now_epoch = datetime.now(UTC).timestamp()
        recent: list[dict[str, Any]] = []
        if not self._btc_samples_loaded:
            recent = await asyncio.to_thread(
                self.db.fetch_all,
                """
                SELECT observed_at, composite_price, source_json FROM btc_ticks
                WHERE observed_at >= ? ORDER BY observed_at ASC
                """,
                ((datetime.now(UTC) - timedelta(minutes=65)).isoformat(),),
            )
            samples = [
                (parse_time(row["observed_at"]).timestamp(), float(row["composite_price"]))
                for row in recent
                if parse_time(row["observed_at"])
            ]
            for row in recent:
                try:
                    timestamp = parse_time(row["observed_at"])
                    source = json.loads(row["source_json"])
                    if timestamp:
                        self._recent_btc_volume_points.append((
                            timestamp.timestamp(),
                            sum(float(quote.get("volume") or 0) for quote in source.get("quotes", [])),
                        ))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            self._btc_samples_loaded = True
        else:
            samples = list(self._recent_btc_samples)
        samples.append((now_epoch, composite.price))
        samples = [
            (timestamp, price) for timestamp, price in samples
            if now_epoch - timestamp <= 65 * 60
        ]
        latest_timestamp = samples[-1][0]
        self._recent_btc_samples = [
            (timestamp, price)
            for timestamp, price in samples
            if latest_timestamp - timestamp <= SETTLEMENT_WINDOW_SECONDS + 5
        ]
        vol_5m = realized_volatility(samples, 300)
        vol_15m = realized_volatility(samples, 900)
        vol_60m = realized_volatility(samples, 3600)
        momentum_1m = momentum_return(samples, 60)
        momentum_5m = momentum_return(samples, 300)
        recent_5m = [
            price for timestamp, price in samples if samples[-1][0] - timestamp <= 300
        ]
        high_low_5m_pct = (
            (max(recent_5m) - min(recent_5m)) / composite.price if recent_5m else 0.0
        )
        current_total_volume = sum(float(quote.volume or 0) for quote in composite.quotes)
        self._recent_btc_volume_points.append((samples[-1][0], current_total_volume))
        self._recent_btc_volume_points = [
            (timestamp, volume)
            for timestamp, volume in self._recent_btc_volume_points
            if now_epoch - timestamp <= 65 * 60
        ]
        volume_points = self._recent_btc_volume_points[-3:]
        volume_acceleration = None
        if len(volume_points) >= 3:
            first_dt = volume_points[-2][0] - volume_points[-3][0]
            second_dt = volume_points[-1][0] - volume_points[-2][0]
            if first_dt > 0 and second_dt > 0:
                first_rate = (volume_points[-2][1] - volume_points[-3][1]) / first_dt
                second_rate = (volume_points[-1][1] - volume_points[-2][1]) / second_dt
                volume_acceleration = second_rate - first_rate
        source = composite.as_dict()
        if persist:
            self.db.execute(
                """
                INSERT INTO btc_ticks(
                    observed_at,composite_price,dispersion_pct,exchange_count,
                    volatility_5m,volatility_15m,volatility_60m,momentum_1m,
                    momentum_5m,volume_acceleration,source_json,high_low_5m_pct
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observed_at, composite.price, composite.dispersion_pct or 0.0,
                    len(composite.quotes), vol_5m, vol_15m, vol_60m,
                    momentum_1m, momentum_5m, volume_acceleration, json.dumps(source),
                    high_low_5m_pct,
                ),
            )
        return {
            **source,
            "observed_at": observed_at,
            "volatility_5m": vol_5m,
            "volatility_15m": vol_15m,
            "volatility_60m": vol_60m,
            "momentum_1m": momentum_1m,
            "momentum_5m": momentum_5m,
            "high_low_5m_pct": high_low_5m_pct,
            "volume_acceleration": volume_acceleration,
        }

    def _settlement_window_state(
        self,
        close: datetime | None,
        observed_at: str,
    ) -> dict[str, float | int | None]:
        observed = parse_time(observed_at) or datetime.now(UTC)
        if close is None:
            return {
                "average": None,
                "elapsed_seconds": 0.0,
                "sample_seconds": 0,
                "coverage": 1.0,
            }
        seconds_remaining = max(0.0, (close - observed).total_seconds())
        elapsed = clamp(
            SETTLEMENT_WINDOW_SECONDS - seconds_remaining,
            0.0,
            SETTLEMENT_WINDOW_SECONDS,
        )
        if elapsed <= 0:
            return {
                "average": None,
                "elapsed_seconds": 0.0,
                "sample_seconds": 0,
                "coverage": 1.0,
            }
        window_start = close.timestamp() - SETTLEMENT_WINDOW_SECONDS
        observed_end = min(observed.timestamp(), close.timestamp())
        per_second: dict[int, float] = {}
        for timestamp, price in self._recent_btc_samples:
            if window_start <= timestamp <= observed_end:
                per_second[int(timestamp)] = price
        sample_seconds = len(per_second)
        expected_seconds = max(1, math.ceil(elapsed))
        return {
            "average": (
                sum(per_second.values()) / sample_seconds if sample_seconds else None
            ),
            "elapsed_seconds": elapsed,
            "sample_seconds": sample_seconds,
            "coverage": min(1.0, sample_seconds / expected_seconds),
        }

    def _save_market(self, market: dict[str, Any], observed_at: str) -> None:
        self.db.execute(
            """
            INSERT INTO markets(
                ticker,event_ticker,status,title,strike,open_time,close_time,
                expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
                first_seen_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                status=excluded.status,title=excluded.title,strike=excluded.strike,
                close_time=excluded.close_time,expected_expiration_time=excluded.expected_expiration_time,
                result=excluded.result,rules_primary=excluded.rules_primary,
                rules_secondary=excluded.rules_secondary,raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                market["ticker"], market.get("event_ticker"), market.get("status", "unknown"),
                market.get("title"), market_strike(market), market.get("open_time"),
                market.get("close_time"), market.get("expected_expiration_time"),
                market.get("result"), market.get("rules_primary"), market.get("rules_secondary"),
                json.dumps(market), observed_at, observed_at,
            ),
        )

    def _record_threshold_observation(
        self,
        market: dict[str, Any],
        observed_at: str,
        *,
        source: str,
        event_type: str,
    ) -> dict[str, Any] | None:
        threshold = market_strike(market)
        ticker = market.get("ticker")
        if threshold is None or not ticker:
            return None
        latest = self.db.fetch_one(
            """
            SELECT * FROM threshold_observations
            WHERE ticker=? ORDER BY revision DESC,id DESC LIMIT 1
            """,
            (str(ticker),),
        )
        changed = bool(
            latest is not None
            and not math.isclose(
                float(latest["threshold"]), float(threshold), rel_tol=0.0, abs_tol=1e-9
            )
        )
        if latest is None or changed:
            self.db.execute(
                """
                INSERT INTO threshold_observations(
                    ticker,threshold,observed_at,market_status,open_time,
                    source,event_type,revision,changed
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(ticker), float(threshold), observed_at, market.get("status"),
                    market.get("open_time"), source, event_type,
                    int(latest["revision"]) + 1 if latest else 1,
                    int(changed),
                ),
            )
        return self._threshold_state(str(ticker))

    def _threshold_state(self, ticker: str) -> dict[str, Any] | None:
        first = self.db.fetch_one(
            """
            SELECT * FROM threshold_observations
            WHERE ticker=? ORDER BY revision ASC,id ASC LIMIT 1
            """,
            (ticker,),
        )
        latest = self.db.fetch_one(
            """
            SELECT * FROM threshold_observations
            WHERE ticker=? ORDER BY revision DESC,id DESC LIMIT 1
            """,
            (ticker,),
        )
        if not first or not latest:
            return None
        return {
            "ticker": ticker,
            "threshold": float(latest["threshold"]),
            "first_observed_at": first["observed_at"],
            "latest_observed_at": latest["observed_at"],
            "revision": int(latest["revision"]),
            "changed": bool(latest["changed"]),
            "source": latest["source"],
            "event_type": latest["event_type"],
        }

    def _save_kalshi_snapshot(
        self,
        market: dict[str, Any],
        book_payload: dict[str, Any],
        observed_at: str,
        *,
        persist: bool = True,
        allow_summary_fallback: bool = True,
    ) -> dict[str, Any]:
        metrics = orderbook_metrics(book_payload)
        # The list-markets response can lag the dedicated order book. Executability
        # must come from the freshest resting levels, with summary fields as fallback.
        yes_bid = (
            metrics["yes_bids"][0][0]
            if metrics["yes_bids"]
            else as_float(market.get("yes_bid_dollars"))
            if allow_summary_fallback
            else None
        )
        yes_ask = (
            metrics["yes_asks"][0][0]
            if metrics["yes_asks"]
            else as_float(market.get("yes_ask_dollars"))
            if allow_summary_fallback
            else None
        )
        no_bid = (
            metrics["no_bids"][0][0]
            if metrics["no_bids"]
            else as_float(market.get("no_bid_dollars"))
            if allow_summary_fallback
            else None
        )
        no_ask = (
            metrics["no_asks"][0][0]
            if metrics["no_asks"]
            else as_float(market.get("no_ask_dollars"))
            if allow_summary_fallback
            else None
        )
        spread = (yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else None
        previous_bid = as_float(market.get("previous_yes_bid_dollars"))
        previous_ask = as_float(market.get("previous_yes_ask_dollars"))
        rapid = 0.0
        if (
            None not in (yes_bid, yes_ask, previous_bid, previous_ask)
            and previous_bid > 0
            and previous_ask > 0
        ):
            rapid = ((yes_bid + yes_ask) - (previous_bid + previous_ask)) / 2
        state = {
            "ticker": market["ticker"],
            "observed_at": observed_at,
            # Separate executable-book freshness from BTC proxy freshness.
            # Exit and entry paths may use only this timestamp for Kalshi
            # price validity.
            "executable_quote_at": observed_at,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": spread,
            "liquidity": as_float(market.get("liquidity_dollars")) or 0.0,
            "open_interest": as_float(market.get("open_interest_fp")) or 0.0,
            "volume": as_float(market.get("volume_fp")) or 0.0,
            "yes_bid_size": (
                metrics["yes_bids"][0][1]
                if metrics["yes_bids"]
                else as_float(market.get("yes_bid_size_fp"))
                if allow_summary_fallback
                else None
            ),
            "yes_ask_size": (
                metrics["yes_asks"][0][1]
                if metrics["yes_asks"]
                else as_float(market.get("yes_ask_size_fp"))
                if allow_summary_fallback
                else None
            ),
            "no_ask_size": metrics["no_asks"][0][1] if metrics["no_asks"] else None,
            "no_bid_size": metrics["no_bids"][0][1] if metrics["no_bids"] else None,
            "imbalance": metrics["imbalance"],
            "rapid_repricing": rapid,
            "price_level_structure": market.get("price_level_structure"),
            "price_ranges": market.get("price_ranges"),
            "orderbook": metrics,
        }
        if persist:
            self.db.execute(
                """
                INSERT INTO kalshi_snapshots(
                    observed_at,ticker,yes_bid,yes_ask,no_bid,no_ask,spread,liquidity,
                    open_interest,volume,yes_bid_size,yes_ask_size,imbalance,
                    rapid_repricing,orderbook_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observed_at, market["ticker"], yes_bid, yes_ask, no_bid, no_ask, spread,
                    state["liquidity"], state["open_interest"], state["volume"],
                    state["yes_bid_size"], state["yes_ask_size"], state["imbalance"],
                    # Normalized columns contain every value consumed by the
                    # model and historical review; avoid a redundant full book.
                    rapid, "{}",
                ),
            )
        return state

    def _analyze(
        self,
        market: dict[str, Any],
        market_state: dict[str, Any],
        btc: dict[str, Any],
        settings: dict[str, Any],
        observed_at: str,
        *,
        execution_market_state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        close = parse_time(market.get("close_time"))
        observed = parse_time(observed_at) or datetime.now(UTC)
        seconds_remaining = max(0.0, (close - observed).total_seconds()) if close else 0.0
        strike = market_strike(market)
        settlement_window = self._settlement_window_state(close, observed_at)
        benchmark_bias = float(self._benchmark_calibration.get("bias_pct") or 0.0)
        benchmark_uncertainty = float(
            self._benchmark_calibration.get("uncertainty_pct")
            or DEFAULT_BENCHMARK_UNCERTAINTY_PCT
        )
        baseline = settlement_probability(
            float(btc["price"]), float(strike), seconds_remaining,
            btc.get("volatility_15m") or btc.get("volatility_5m"),
            btc.get("momentum_5m") or 0.0,
            basis_uncertainty_pct=benchmark_uncertainty,
            observed_window_average=settlement_window["average"],
            observed_window_seconds=float(settlement_window["elapsed_seconds"] or 0.0),
            benchmark_bias_pct=benchmark_bias,
        )
        settlement_fraction = (
            float(settlement_window["elapsed_seconds"] or 0.0)
            / SETTLEMENT_WINDOW_SECONDS
        )
        volume_signals = self.volume_signals.snapshot(
            observed_at=observed_at,
            ticker=str(market["ticker"]),
            btc_price=float(btc["price"]),
            momentum_1m=float(btc.get("momentum_1m") or 0.0),
            momentum_5m=float(btc.get("momentum_5m") or 0.0),
            open_interest=market_state.get("open_interest"),
            seconds_remaining=seconds_remaining,
            threshold_margin=float(btc["price"]) - float(strike),
            annualized_volatility=baseline.annualized_volatility,
            settlement_window_fraction=settlement_fraction,
            # The fast quote/protection lane reads its in-memory rolling window.
            # SQL persistence and cold history loading run independently.
            persist=False,
            use_rolling_cache=True,
        )
        market_mid = (
            (market_state["yes_bid"] + market_state["yes_ask"]) / 2
            if market_state["yes_bid"] is not None and market_state["yes_ask"] is not None
            else 0.5
        )
        features = {
            "z_distance": baseline.z_distance,
            "time_fraction": seconds_remaining / 900,
            "volatility_5m": btc.get("volatility_5m") or baseline.annualized_volatility,
            "volatility_15m": btc.get("volatility_15m") or baseline.annualized_volatility,
            "momentum_1m": btc.get("momentum_1m") or 0.0,
            "momentum_5m": btc.get("momentum_5m") or 0.0,
            "high_low_5m_pct": btc.get("high_low_5m_pct") or 0.0,
            "volume_acceleration": btc.get("volume_acceleration") or 0.0,
            "dispersion_pct": btc.get("dispersion_pct") or 0.0,
            "orderbook_imbalance": market_state.get("imbalance") or 0.0,
            "market_probability": market_mid,
            "settlement_window_fraction": (
                settlement_fraction
            ),
            "benchmark_uncertainty_pct": benchmark_uncertainty,
            "threshold_margin_dollars": float(btc["price"]) - float(strike),
            **volume_signals.get("features", {}),
        }
        probability, model_version = self.models.predict(features, baseline.probability)
        forecast = make_forecast(probability)
        variants = []
        for vol in (btc.get("volatility_5m"), btc.get("volatility_60m")):
            if vol:
                variants.append(
                    settlement_probability(
                        float(btc["price"]), float(strike), seconds_remaining, vol,
                        btc.get("momentum_5m") or 0.0,
                        basis_uncertainty_pct=benchmark_uncertainty,
                        observed_window_average=settlement_window["average"],
                        observed_window_seconds=float(
                            settlement_window["elapsed_seconds"] or 0.0
                        ),
                        benchmark_bias_pct=benchmark_bias,
                    ).probability
                )
        variant_spread = max(variants) - min(variants) if len(variants) > 1 else 0.0
        calibration = self.calibration_summary()
        trading_mode = self.trading.selected_mode
        trade_market_state = execution_market_state or market_state
        portfolio = self.trading.broker(trading_mode).portfolio()
        selected_side = str(settings.get("selected_side", "YES"))
        held_by_side = {
            side: sum(
                int(position["contracts"])
                for position in portfolio.get("positions", [])
                if position["ticker"] == market["ticker"]
                and position["side"] == side
            )
            for side in ("YES", "NO")
        }
        quality = self._data_quality(
            btc,
            market_state,
            seconds_remaining,
            settings,
            reference_price=baseline.reference_price,
            strike=float(strike),
            benchmark_uncertainty_pct=benchmark_uncertainty,
            settlement_window=settlement_window,
        )
        btc_observed = parse_time(btc.get("observed_at"))
        mvi_source_reliable = bool(
            int(btc.get("exchange_count") or 0)
            >= int(settings.get("minimum_exchange_feeds", 2))
            and float(btc.get("dispersion_pct") or 0)
            <= float(settings.get("max_exchange_dispersion_pct", 0.40))
            and btc_observed is not None
            and (datetime.now(UTC) - btc_observed).total_seconds()
            <= float(settings.get("max_data_age_seconds", 20))
        )
        margin_volatility = self.margin_volatility.observe(
            observed_at=observed_at,
            ticker=str(market["ticker"]),
            threshold=float(strike),
            btc_proxy=float(btc["price"]),
            seconds_remaining=seconds_remaining,
            source_reliable=mvi_source_reliable,
        )
        directional_momentum = regression_momentum(
            self._recent_btc_samples,
            lookback_seconds=float(
                settings.get("directional_momentum_lookback_seconds", 15)
            ),
        )
        gates_released = bool(
            self.paper.gate_release_state(str(market["ticker"]))["released"]
        )
        execution_quality_by_side: dict[str, dict[str, Any]] = {}
        for side in ("YES", "NO"):
            execution_quality = dict(quality)
            execution_error = trade_market_state.get("execution_data_error")
            if execution_error:
                execution_quality.update(
                    {"reliable": False, "trade_allowed": False, "reason": execution_error}
                )
            elif trading_mode == "DEMO" and trade_market_state.get(
                f"{side.lower()}_ask"
            ) is None:
                execution_quality.update(
                    {
                        "reliable": False,
                        "trade_allowed": False,
                        "reason": (
                            "The Demo order book has no executable "
                            f"{'Up' if side == 'YES' else 'Down'} ask."
                        ),
                    }
                )
            execution_quality_by_side[side] = execution_quality
        decisions = {
            side: make_decision(
                model_probability=probability,
                market=trade_market_state,
                settings=settings,
                bankroll=portfolio["available_cash"],
                data_quality=execution_quality_by_side[side],
                calibration=calibration,
                model_variant_spread=variant_spread,
                selected_side=side,
                held_contracts=held_by_side[side],
                entry_gates_released=gates_released,
            )
            for side in ("YES", "NO")
        }
        assessments = {
            side: make_trade_assessment(
                up_probability=probability,
                market=trade_market_state,
                settings=settings,
                side=side,
                data_quality=execution_quality_by_side[side],
            )
            for side in ("YES", "NO")
        }
        for side, assessment in assessments.items():
            assessment["decision_confidence"] = decisions[side].confidence
            assessment["exchange_index"] = market.get("exchange_index")
            assessment["margin_volatility"] = margin_volatility
        decision = decisions.get(selected_side, decisions["YES"])
        previous = self.db.fetch_one(
            "SELECT * FROM signal_snapshots WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (market["ticker"],),
        )
        previous_side = None
        if previous:
            try:
                previous_side = json.loads(previous["input_json"]).get("selected_side")
            except (TypeError, json.JSONDecodeError):
                previous_side = None
        trade_reason = (
            "selected side changed"
            if previous and previous_side != selected_side
            else material_change(previous, decision, float(settings.get("buy_edge", 0.05)))
        )
        previous_forecast = previous.get("forecast_signal") if previous else None
        forecast_reason = (
            "initial forecast"
            if not previous_forecast
            else (
                f"forecast changed: {previous_forecast} -> {forecast.signal}"
                if previous_forecast and previous_forecast != forecast.signal
                else None
            )
        )
        reason = forecast_reason or trade_reason
        notification = None
        if reason:
            signal_id = self._save_signal(
                market["ticker"], forecast, decision, model_version, features, btc,
                market_state, reason, observed_at, probability, selected_side,
                margin_volatility,
            )
            if previous_forecast and previous_forecast != forecast.signal:
                notification = {
                    "title": (
                        f"Forecast changed: {forecast_label(previous_forecast)} "
                        f"→ {forecast_label(forecast.signal)}"
                    ),
                    "detail": forecast.explanation,
                    "signal_id": signal_id,
                }
        threshold_state = self._threshold_state(str(market["ticker"]))
        execution_kwargs: dict[str, Any] = {}
        if trading_mode != "PAPER":
            broker = self.trading.broker(trading_mode)
            readiness = broker.readiness()
            automatic_enabled = bool(
                settings.get(f"{trading_mode.lower()}_automatic_trading_enabled", False)
            ) and bool(readiness.get("automatic_armed"))
            texas_enabled = bool(settings.get("texas_holdem_enabled", False))
            try:
                texas_mvi = float((margin_volatility or {}).get("mvi"))
            except (TypeError, ValueError):
                texas_mvi = float("nan")
            texas_boost = (
                TEXAS_V2_MVI_BOOST_MULTIPLIER
                if texas_enabled
                and bool((margin_volatility or {}).get("reliable"))
                and math.isfinite(texas_mvi)
                and texas_mvi >= TEXAS_V2_MVI_BOOST_THRESHOLD
                else 1.0
            )
            execution_risk_by_side = {
                side: self.trading.preview_automatic_risk(
                    strategy=TEXAS_HOLDEM_V2 if texas_enabled else "STANDARD_EDGE",
                    ticker=str(market["ticker"]),
                    assessment=assessments[side],
                    bankroll_fraction=(
                        float(settings.get("max_risk_per_trade_pct", 0.05)) * texas_boost
                        if texas_enabled else float(decisions[side].suggested_fraction or 0.0)
                    ),
                    model_version=model_version,
                    reason=(
                        "Texas Hold'em opening-play risk preview."
                        if texas_enabled else decisions[side].explanation
                    ),
                    stop_loss_cents=(
                        None if texas_enabled else settings.get("default_stop_loss_cents")
                    ),
                    time_in_force=("immediate_or_cancel" if texas_enabled else None),
                    maximum_entry_price=(
                        settings.get("texas_holdem_max_entry_price")
                        if texas_enabled else None
                    ),
                )
                for side in ("YES", "NO")
            }

            def standard_entry_handler(
                entry_ticker: str,
                entry_decision: Decision,
                entry_model_version: str,
            ) -> bool:
                side_assessment = assessments.get(str(entry_decision.side)) or {}
                entered, _ = self.trading.submit_automatic(
                    strategy="STANDARD_EDGE",
                    ticker=entry_ticker,
                    assessment=side_assessment,
                    bankroll_fraction=float(entry_decision.suggested_fraction or 0.0),
                    model_version=entry_model_version,
                    reason=entry_decision.explanation,
                    stop_loss_cents=settings.get("default_stop_loss_cents"),
                    strategy_metadata={
                        "margin_volatility_index": margin_volatility.get("mvi"),
                        "margin_cushion_ratio": margin_volatility.get("cushion_ratio"),
                        "margin_volatility_version": margin_volatility.get(
                            "calculation_version"
                        ),
                    },
                )
                return entered

            execution_kwargs = {
                "execution_mode": trading_mode,
                "automatic_enabled": automatic_enabled,
                "execution_block_reason": (
                    None
                    if readiness.get("ready_for_automatic")
                    else readiness.get("automatic_blocker")
                ),
                "execution_risk_by_side": execution_risk_by_side,
                "entry_exists_override": self.trading.has_automatic_entry(
                    trading_mode,
                    str(market["ticker"]),
                    strategy=(
                        TEXAS_HOLDEM_V2
                        if settings.get("texas_holdem_enabled", False)
                        else None
                    ),
                ),
                "standard_entry_handler": standard_entry_handler,
                "fixed_entry_handler": self.trading.submit_automatic,
            }
        automatic_entry = self.paper.consider_strategies(
            ticker=str(market["ticker"]),
            assessments=assessments,
            standard_decisions=decisions,
            seconds_remaining=seconds_remaining,
            market_status=market.get("status"),
            market_open_time=market.get("open_time"),
            market_observed_at=trade_market_state.get("observed_at"),
            threshold_state=threshold_state,
            settlement_window=settlement_window,
            z_distance=baseline.z_distance,
            threshold_margin_dollars=float(btc["price"]) - float(strike),
            margin_volatility=margin_volatility,
            directional_momentum=directional_momentum,
            model_version=model_version,
            portfolio=portfolio,
            **execution_kwargs,
        )
        summary = self._market_summary(market)
        summary.update(
            {
                **trade_market_state,
                "time_remaining_seconds": seconds_remaining,
                "model_probability": decision.model_probability,
                "up_probability": probability,
                "selected_side": selected_side,
                "trading_mode": trading_mode,
                "execution_market_mode": trade_market_state.get(
                    "execution_market_mode", "LIVE"
                ),
                "model_version": model_version,
                "z_distance": baseline.z_distance,
                "annualized_volatility": baseline.annualized_volatility,
                "settlement_proxy_price": baseline.reference_price,
                "settlement_window": settlement_window,
                "benchmark_calibration": self._benchmark_calibration,
                "benchmark_uncertainty_dollars": (
                    baseline.reference_price * benchmark_uncertainty
                ),
                "btc_proxy": btc.get("price"),
                "model_variant_spread": variant_spread,
                "data_quality": quality,
                "forecast": forecast.as_dict(),
                "trade_decision": decision.as_dict(),
                "decision": decision.as_dict(),
                "trade_decisions": {
                    side: value.as_dict() for side, value in decisions.items()
                },
                "trade_assessments": assessments,
                "threshold_state": threshold_state,
                "margin_volatility": margin_volatility,
                "directional_momentum": directional_momentum,
                "volume_signals": volume_signals,
                "automatic_entry": automatic_entry,
                "standard_edge_readiness": automatic_entry.get(
                    "standard_edge_readiness"
                ),
                "texas_holdem": automatic_entry.get("texas_holdem"),
                "swing_readiness": automatic_entry.get("swing_readiness"),
            }
        )
        return summary, notification

    @staticmethod
    def _data_quality(
        btc: dict[str, Any],
        market: dict[str, Any],
        seconds_remaining: float,
        settings: dict[str, Any],
        *,
        reference_price: float,
        strike: float,
        benchmark_uncertainty_pct: float,
        settlement_window: dict[str, float | int | None],
    ) -> dict[str, Any]:
        if int(btc.get("exchange_count") or 0) < int(
            settings.get("minimum_exchange_feeds", 2)
        ):
            return {"reliable": False, "reason": "fewer than two exchange feeds responded"}
        now = datetime.now(UTC)
        maximum_age = float(settings.get("max_data_age_seconds", 20))
        for label, timestamp in (("BTC", btc.get("observed_at")),):
            parsed = parse_time(timestamp)
            if parsed is not None and (now - parsed).total_seconds() > maximum_age:
                return {"reliable": False, "reason": f"the {label} feed is stale"}
        executable_quote_at = parse_time(market.get("executable_quote_at"))
        if executable_quote_at is None:
            return {
                "reliable": False,
                "reason": "the Kalshi executable quote timestamp is unavailable",
            }
        if (now - executable_quote_at).total_seconds() > maximum_age:
            return {
                "reliable": False,
                "reason": "the Kalshi executable quote is stale",
            }
        if float(btc.get("dispersion_pct") or 0) > float(settings["max_exchange_dispersion_pct"]):
            return {"reliable": False, "reason": "cross-exchange prices disagree beyond the configured limit"}
        if seconds_remaining <= float(settings.get("closing_guard_seconds", 10)):
            return {"reliable": False, "reason": "the contract is closing or transitioning"}
        if any(market.get(key) is None for key in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
            return {"reliable": False, "reason": "an executable Kalshi bid or ask is missing"}
        elapsed = float(settlement_window.get("elapsed_seconds") or 0.0)
        coverage = float(settlement_window.get("coverage") or 0.0)
        if elapsed >= 5.0 and coverage < float(
            settings.get("settlement_min_coverage_pct", 0.50)
        ):
            return {
                "reliable": True,
                "trade_allowed": False,
                "reason_code": "SETTLEMENT_WINDOW_INCOMPLETE",
                "reason": (
                    "Hold: final-minute proxy coverage is too sparse to estimate "
                    "Kalshi's 60-second settlement average."
                ),
            }
        uncertainty_dollars = reference_price * benchmark_uncertainty_pct
        if abs(math.log(reference_price / strike)) <= benchmark_uncertainty_pct:
            return {
                "reliable": True,
                "trade_allowed": False,
                "reason_code": "BENCHMARK_UNCERTAINTY",
                "reason": (
                    "Hold: the projected settlement proxy is inside the learned "
                    f"BRTI uncertainty band (+/-${uncertainty_dollars:,.2f})."
                ),
            }
        return {
            "reliable": True,
            "trade_allowed": True,
            "reason": "critical feeds are current and mutually consistent",
        }

    def _save_signal(
        self,
        ticker: str,
        forecast: Forecast,
        decision: Decision,
        model_version: str,
        features: dict[str, Any],
        btc: dict[str, Any],
        market: dict[str, Any],
        reason: str,
        observed_at: str,
        model_up_probability: float,
        selected_side: str,
        margin_volatility: dict[str, Any] | None = None,
    ) -> int:
        return self.db.execute(
            """
            INSERT INTO signal_snapshots(
                observed_at,ticker,signal,reason_code,confidence,explanation,
                model_probability,market_probability,edge,expected_value,
                suggested_fraction,suggested_dollars,suggested_contracts,model_version,
                input_json,btc_state_json,kalshi_state_json,material_reason,
                forecast_signal,forecast_explanation,margin_volatility_index,
                margin_cushion_ratio,margin_volatility_max
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observed_at, ticker, decision.signal, decision.reason_code,
                decision.confidence, decision.explanation, model_up_probability,
                decision.market_probability, decision.edge, decision.expected_value,
                decision.suggested_fraction, decision.suggested_dollars,
                decision.suggested_contracts, model_version,
                json.dumps({"features": features, "selected_side": selected_side}),
                json.dumps(btc), json.dumps(market), reason,
                forecast.signal, forecast.explanation,
                (margin_volatility or {}).get("mvi"),
                (margin_volatility or {}).get("cushion_ratio"),
                float(self.db.settings().get("maximum_margin_volatility", 0)),
            ),
        )

    async def _check_pending_settlements(self) -> None:
        assert self.kalshi
        for ticker in list(self._pending_settlements):
            try:
                market = await self.kalshi.market(ticker)
            except Exception:
                continue
            if market.get("result") not in {"yes", "no"}:
                continue
            result = 1 if market["result"] == "yes" else 0
            settled_at = market.get("settlement_ts") or iso_now()
            inserted = not self.db.fetch_one("SELECT ticker FROM settlements WHERE ticker=?", (ticker,))
            self.db.execute(
                """
                INSERT OR IGNORE INTO settlements(
                    ticker,settled_at,result,settlement_value,raw_json,processed_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    ticker, settled_at, result, as_float(market.get("settlement_value_dollars")),
                    json.dumps(market), iso_now(),
                ),
            )
            self.db.execute(
                "UPDATE markets SET status=?, result=?, raw_json=?, updated_at=? WHERE ticker=?",
                (market.get("status", "finalized"), market["result"], json.dumps(market), iso_now(), ticker),
            )
            self.paper.settle(ticker, result, settled_at)
            await asyncio.to_thread(
                self.trade_reviews.finalize,
                ticker,
                result=result,
                settled_at=settled_at,
                settlement_value=as_float(market.get("settlement_value_dollars")),
            )
            self._pending_settlements.discard(ticker)
            if inserted:
                self._benchmark_calibration = self.models.benchmark_calibration()

    def _unreliable_current(self, market: dict[str, Any], reason: str) -> dict[str, Any]:
        summary = self._market_summary(market)
        side = str(self.db.settings().get("selected_side", "YES"))
        trade_decision = Decision(
            "HOLD", "DATA_UNRELIABLE", "Low", f"Hold: {reason}",
            None, None, None, None, None, None, 0, 0, 0, side,
        ).as_dict()
        summary["trade_decision"] = trade_decision
        summary["decision"] = trade_decision
        summary["forecast"] = None
        summary["selected_side"] = side
        summary["data_quality"] = {"reliable": False, "reason": reason}
        return summary

    def _market_summary(self, market: dict[str, Any] | None) -> dict[str, Any] | None:
        if not market:
            return None
        return {
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "title": market.get("title"),
            "status": market.get("status"),
            "exchange_index": market.get("exchange_index"),
            "strike": market_strike(market),
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "yes_bid": as_float(market.get("yes_bid_dollars")),
            "yes_ask": as_float(market.get("yes_ask_dollars")),
            "no_bid": as_float(market.get("no_bid_dollars")),
            "no_ask": as_float(market.get("no_ask_dollars")),
            "liquidity": as_float(market.get("liquidity_dollars")) or 0,
            "open_interest": as_float(market.get("open_interest_fp")) or 0,
            "volume": as_float(market.get("volume_fp")) or 0,
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
        }

    def _persist_next_threshold_forecast(self, evidence: dict[str, Any]) -> None:
        """Persist only terminal forecast evidence, never on the quote hot path."""
        ticker = str(evidence.get("ticker") or "")
        open_time = str(evidence.get("target_open_time") or "")
        if not ticker or not open_time:
            return
        comparison = evidence.get("comparison") or {}
        self.db.execute(
            """
            INSERT INTO next_threshold_forecasts(
                ticker,target_open_time,status,estimate,samples_collected,coverage,
                sample_dispersion_dollars,official_threshold,error_dollars,
                evidence_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,target_open_time) DO UPDATE SET
                status=excluded.status,estimate=excluded.estimate,
                samples_collected=excluded.samples_collected,coverage=excluded.coverage,
                sample_dispersion_dollars=excluded.sample_dispersion_dollars,
                official_threshold=excluded.official_threshold,
                error_dollars=excluded.error_dollars,evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                ticker, open_time, str(evidence.get("final_state") or evidence.get("status") or "FROZEN"),
                evidence.get("estimate"), int(evidence.get("samples_collected") or 0),
                float(evidence.get("coverage") or 0.0), evidence.get("sample_dispersion_dollars"),
                evidence.get("official_threshold"), comparison.get("error_dollars"),
                json.dumps(evidence), str(evidence.get("frozen_at") or iso_now()), iso_now(),
            ),
        )

    def _next_threshold_forecast_state(self) -> dict[str, Any] | None:
        value = self.dashboard.get("next_threshold_forecast")
        return dict(value) if isinstance(value, dict) else None

    def _update_next_threshold_forecast(self, observed_at: str) -> None:
        """Maintain the display-only final-minute threshold estimate.

        It consumes the already-available BTC composite and market lifecycle
        snapshots.  It does not schedule requests, analysis, trading, or gates.
        """
        frozen, frozen_evidence = self._next_threshold_forecast.freeze_if_due(observed_at)
        if frozen_evidence:
            self._persist_next_threshold_forecast(frozen_evidence)
        state, comparison_evidence = self._next_threshold_forecast.observe(
            next_market=self._next_market,
            known_markets=(self._current_market, self._next_market),
            proxy_price=(self._latest_btc or {}).get("price"),
            observed_at=observed_at,
            official_threshold=market_strike,
        )
        if comparison_evidence:
            self._persist_next_threshold_forecast(comparison_evidence)
        self.dashboard["next_threshold_forecast"] = state or frozen

    def _portfolio_summary(self) -> dict[str, Any]:
        portfolio = self.paper.portfolio()
        summary = {
            key: value
            for key, value in portfolio.items()
            if key not in {"trades", "orders"}
        }
        summary["recent_paper_trades"] = [
            {
                "id": trade.get("id"),
                "opened_at": trade.get("opened_at"),
                "ticker": trade.get("ticker"),
                "side": trade.get("side"),
                "entry_price": trade.get("entry_price"),
                "contracts": trade.get("contracts"),
                "strategy": trade.get("strategy") or trade.get("source"),
                "source": trade.get("source"),
                "status": trade.get("status"),
                "realized_pnl": trade.get("realized_pnl"),
                "available_cash_after": trade.get("available_cash_after"),
                "settlement_margin": trade.get("settlement_margin"),
                "margin_volatility_index": (
                    trade.get("entries") or [{}]
                )[-1].get("margin_volatility_index"),
                "margin_cushion_ratio": (
                    trade.get("entries") or [{}]
                )[-1].get("margin_cushion_ratio"),
                "exit_reason": next(
                    (
                        entry.get("exit_reason")
                        for entry in reversed(trade.get("entries") or [])
                        if entry.get("exit_reason")
                    ),
                    None,
                ),
            }
            for trade in portfolio.get("trades", [])[:10]
        ]
        return summary

    async def place_manual_paper_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._update_lock:
            current = self.dashboard.get("current")
            if not current or not current.get("ticker"):
                raise ValueError("There is no active Kalshi contract.")
            quality = current.get("data_quality") or {}
            if not quality.get("reliable", False):
                raise ValueError(
                    quality.get("reason") or "Live market data is not reliable enough to trade."
                )
            order_type = str(payload.get("order_type", "market")).upper()
            limit_price = None
            if order_type == "LIMIT":
                try:
                    limit_price = float(payload.get("limit_price_cents")) / 100
                except (TypeError, ValueError) as exc:
                    raise ValueError("Enter a valid limit price in cents.") from exc
            stop_loss_price = None
            raw_stop = payload.get("stop_loss_cents")
            if raw_stop not in (None, ""):
                try:
                    stop_loss_price = float(raw_stop) / 100
                except (TypeError, ValueError) as exc:
                    raise ValueError("Enter a valid stop-loss price in cents.") from exc
            order = self.paper.place_order(
                ticker=str(current["ticker"]),
                side=str(payload.get("side", "")),
                action=str(payload.get("action", "")),
                order_type=order_type,
                market=current,
                dollars=payload.get("dollars"),
                contracts=payload.get("contracts"),
                limit_price=limit_price,
                stop_loss_price=stop_loss_price,
            )
            self.dashboard["paper"] = self._portfolio_summary()
            self._schedule_publish()
            return {"order": order, "portfolio": self.paper.portfolio()}

    async def apply_settings(
        self,
        updates: dict[str, Any],
        *,
        restored_from_id: int | None = None,
    ) -> dict[str, Any]:
        async with self._update_lock:
            current_settings = self.db.settings()
            changed_live_limits = [
                key
                for key, value in updates.items()
                if key.startswith("live_")
                and key not in {"live_automatic_trading_enabled"}
                and current_settings.get(key) != value
            ]
            settings = self.db.update_settings(
                updates, restored_from_id=restored_from_id
            )
            if changed_live_limits:
                live_broker = self.trading.broker("LIVE")
                if isinstance(live_broker, KalshiBroker):
                    live_broker._audit(
                        "LIVE_LIMITS_CHANGED",
                        {
                            "settings": sorted(changed_live_limits),
                            "session_remains_armed": live_broker.session_armed,
                            "automatic_remains_armed": live_broker.automatic_armed,
                        },
                    )
            self._refresh_cached_dashboard(iso_now())
            return settings

    async def restore_settings(self, snapshot_id: int) -> dict[str, Any]:
        snapshot = self.db.fetch_one(
            "SELECT settings_json FROM configuration_snapshots WHERE id=?",
            (snapshot_id,),
        )
        if not snapshot:
            raise ValueError("Configuration snapshot not found.")
        return await self.apply_settings(
            json.loads(snapshot["settings_json"]), restored_from_id=snapshot_id
        )

    async def cancel_manual_paper_order(self, order_id: int) -> dict[str, Any]:
        async with self._update_lock:
            if not self.paper.cancel_order(order_id):
                raise ValueError("That limit order is no longer open.")
            self.dashboard["paper"] = self._portfolio_summary()
            self._schedule_publish()
            return {"canceled": order_id, "portfolio": self.paper.portfolio()}

    async def reset_paper_round(self) -> dict[str, Any]:
        async with self._update_lock:
            reset = self.paper.reset_round()
            portfolio = self.paper.portfolio()
            self.dashboard["paper"] = {
                key: value
                for key, value in portfolio.items()
                if key not in {"trades", "orders"}
            }
            self._schedule_publish()
            return {"reset": reset, "portfolio": portfolio}

    def calibration_summary(self) -> dict[str, Any]:
        return dict(self._calibration_summary)

    def chart(self, minutes: int) -> dict[str, Any]:
        minutes = max(5, min(360, int(minutes)))
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        points = self.db.fetch_all(
            """
            SELECT observed_at, composite_price AS price, dispersion_pct,
                   volatility_15m FROM btc_ticks
            WHERE observed_at >= ? ORDER BY observed_at ASC
            """,
            (since,),
        )
        return {
            "points": points,
            "volatility_points": self.margin_volatility.chart(since),
            "maximum_margin_volatility": float(
                self.db.settings().get("maximum_margin_volatility", 0)
            ),
        }

    def _degrade(self, error: str) -> None:
        current = self.dashboard.get("current")
        if current:
            current = dict(current)
            trade_decision = Decision(
                "HOLD", "DATA_UNRELIABLE", "Low",
                "Hold: the most recent refresh failed.",
                current.get("model_probability"), None, None, None, None, None,
                0, 0, 0, current.get("selected_side"),
            ).as_dict()
            current["trade_decision"] = trade_decision
            current["decision"] = trade_decision
        self.dashboard = {
            **self.dashboard,
            "current": current,
            "system": {
                "status": "degraded",
                "message": "A public data source is unavailable; signal withheld.",
                "updated_at": iso_now(),
                "error": error,
                "read_only": True,
            },
        }
