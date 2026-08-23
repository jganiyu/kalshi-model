from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import AppConfig
from app.db import Database
from app.domain import (
    calibration_metrics,
    iso_now,
    momentum_return,
    parse_time,
    realized_volatility,
    settlement_probability,
)
from app.services.bootstrap import HistoricalBootstrapService
from app.services.decision import Decision, make_decision, material_change
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
from app.services.paper import PaperTradingService
from app.services.streaming import BitcoinWebSocketFeeds, KalshiWebSocketFeed
from app.services.training import ModelManager


logger = logging.getLogger(__name__)


class AnalysisEngine:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.http: httpx.AsyncClient | None = None
        self.bitcoin: BitcoinCompositeFeed | None = None
        self.kalshi: KalshiPublicClient | None = None
        self.paper = PaperTradingService(db)
        self.models = ModelManager(db)
        self.dashboard: dict[str, Any] = {
            "system": {"status": "starting", "message": "Connecting to public data feeds."},
            "current": None,
            "next": None,
            "btc": None,
            "notification": None,
        }
        self._runner: asyncio.Task[None] | None = None
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._publish_task: asyncio.Task[None] | None = None
        self._live_refresh_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._update_lock = asyncio.Lock()
        self._previous_ticker: str | None = None
        self._pending_settlements: set[str] = set()
        self._last_settlement_check = 0.0
        self._last_live_update = 0.0
        self._last_btc_persist = 0.0
        self._last_kalshi_persist = 0.0
        self._latest_quotes: dict[str, ExchangeQuote] = {}
        self._latest_btc: dict[str, Any] | None = None
        self._current_market: dict[str, Any] | None = None
        self._next_market: dict[str, Any] | None = None
        self._market_state: dict[str, Any] | None = None
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
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers={"User-Agent": "kalshi-model/0.1 local-analysis-only"},
        )
        self.bitcoin = BitcoinCompositeFeed(self.http)
        self.kalshi = KalshiPublicClient(
            self.http, self.config.kalshi_api_base, self.config.kalshi_series
        )
        await self.collect_once()
        self._runner = asyncio.create_task(self._run_loop())
        bitcoin_streams = BitcoinWebSocketFeeds(
            self._handle_stream_quote, self._handle_stream_status
        )
        self._stream_tasks.extend(
            [
                asyncio.create_task(bitcoin_streams.run_coinbase()),
                asyncio.create_task(bitcoin_streams.run_kraken()),
            ]
        )
        if self.config.kalshi_api_key_id and self.config.kalshi_private_key_path:
            kalshi_stream = KalshiWebSocketFeed(
                self.config.kalshi_ws_url,
                self.config.kalshi_api_key_id,
                self.config.kalshi_private_key_path,
                lambda: str(self._current_market["ticker"]) if self._current_market else None,
                self._handle_kalshi_message,
                self._handle_stream_status,
            )
            self._stream_tasks.append(asyncio.create_task(kalshi_stream.run()))
        existing = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM signal_snapshots WHERE material_reason='historical bootstrap'"
        )
        if not existing or existing["count"] == 0:
            self._bootstrap_task = asyncio.create_task(self._bootstrap())

    async def stop(self) -> None:
        self._stopping.set()
        for task in (
            self._runner,
            self._bootstrap_task,
            self._publish_task,
            self._live_refresh_task,
            *self._stream_tasks,
        ):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in self._stream_tasks if task), return_exceptions=True
        )
        if self.http:
            await self.http.aclose()

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
            report = self.models.evaluate_and_retrain("automatic historical bootstrap")
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
        report = self.models.evaluate_and_retrain("manual historical bootstrap")
        self.dashboard["bootstrap"] = {"status": "complete", **result, "report": report["tldr"]}
        return self.dashboard["bootstrap"]

    async def collect_once(self) -> None:
        assert self.bitcoin and self.kalshi
        composite, market_pair = await asyncio.gather(
            self.bitcoin.fetch(), self.kalshi.active_markets()
        )
        current_market, next_market = market_pair
        observed_at = iso_now()
        btc_state = self._save_bitcoin(composite, observed_at)
        self._latest_quotes = {quote.exchange: quote for quote in composite.quotes}
        self._latest_btc = btc_state
        self._current_market = current_market
        self._next_market = next_market
        settings = self.db.settings()
        current_payload = None
        notification = None

        if current_market and btc_state.get("price") and market_strike(current_market):
            ticker = str(current_market["ticker"])
            self._save_market(current_market, observed_at)
            orderbook_payload = await self.kalshi.orderbook(ticker)
            market_state = self._save_kalshi_snapshot(
                current_market, orderbook_payload, observed_at
            )
            self._market_state = market_state
            self.paper.process_open_orders(ticker, market_state)
            current_payload, notification = self._analyze(
                current_market, market_state, btc_state, settings, observed_at
            )
            if self._previous_ticker and self._previous_ticker != ticker:
                self._pending_settlements.add(self._previous_ticker)
            self._previous_ticker = ticker
        elif current_market:
            self._market_state = None
            self._save_market(current_market, observed_at)
            reason = (
                "The contract threshold is missing from the Kalshi response."
                if not market_strike(current_market)
                else "Fewer than two reliable BTC exchange feeds are available."
            )
            current_payload = self._unreliable_current(
                current_market, reason
            )
        elif self._previous_ticker:
            self._market_state = None
            self._pending_settlements.add(self._previous_ticker)
            self._previous_ticker = None

        if next_market:
            self._save_market(next_market, observed_at)
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
            "notification": notification,
            "paper": self._portfolio_summary(),
            "calibration": self.calibration_summary(),
            "model": self.models.active(),
        }
        self._schedule_publish()

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

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
                },
                "kalshi": {
                    "connected": bool(kalshi_status.get("connected")),
                    "configured": bool(kalshi_status.get("configured")),
                    "error": kalshi_status.get("error"),
                },
            },
        }

    async def _handle_stream_status(
        self, source: str, connected: bool, error: str | None
    ) -> None:
        status = self._stream_status.setdefault(source, {})
        status["connected"] = connected
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
        self._schedule_publish()

    async def _handle_stream_quote(self, quote: ExchangeQuote) -> None:
        self._latest_quotes[quote.exchange] = quote
        self._schedule_live_refresh()

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
            composite = live_composite_quote(quotes, preferred)
            if composite.price is not None:
                now = time.monotonic()
                persist = now - self._last_btc_persist >= 1.0
                self._latest_btc = self._save_bitcoin(
                    composite, observed_at, persist=persist
                )
                if persist:
                    self._last_btc_persist = now
            self._refresh_cached_dashboard(observed_at)
            self._last_live_update = time.monotonic()

    async def _handle_kalshi_message(
        self, message: dict[str, Any], book_metrics: dict[str, Any] | None
    ) -> None:
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
                "rapid_repricing": (
                    new_mid - old_mid
                    if new_mid is not None and old_mid is not None
                    else 0.0
                ),
            }
        )
        return state

    def _refresh_cached_dashboard(self, observed_at: str) -> None:
        if not (self._current_market and self._market_state and self._latest_btc):
            return
        self.paper.process_open_orders(
            str(self._current_market["ticker"]), self._market_state
        )
        current, notification = self._analyze(
            self._current_market,
            self._market_state,
            self._latest_btc,
            self.db.settings(),
            observed_at,
        )
        reliable = current["decision"]["reason_code"] != "DATA_UNRELIABLE"
        self.dashboard = {
            **self.dashboard,
            "system": self._system_state(reliable, observed_at),
            "btc": self._latest_btc,
            "current": current,
            "next": self._market_summary(self._next_market) if self._next_market else None,
            "notification": notification,
            "paper": self._portfolio_summary(),
        }
        self._schedule_publish()

    def _save_bitcoin(
        self, composite: CompositeQuote, observed_at: str, *, persist: bool = True
    ) -> dict[str, Any]:
        if composite.price is None:
            return {
                "price": None,
                "exchange_count": len(composite.quotes),
                "errors": composite.errors,
            }
        if persist:
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
        recent = self.db.fetch_all(
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
        samples.append((datetime.now(UTC).timestamp(), composite.price))
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
        volume_points: list[tuple[float, float]] = []
        for row in recent[-3:]:
            try:
                previous_source = json.loads(row["source_json"])
                total_volume = sum(
                    float(quote.get("volume") or 0)
                    for quote in previous_source.get("quotes", [])
                )
                timestamp = parse_time(row["observed_at"])
                if timestamp:
                    volume_points.append((timestamp.timestamp(), total_volume))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        current_total_volume = sum(float(quote.volume or 0) for quote in composite.quotes)
        volume_points.append((samples[-1][0], current_total_volume))
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

    def _save_kalshi_snapshot(
        self,
        market: dict[str, Any],
        book_payload: dict[str, Any],
        observed_at: str,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        metrics = orderbook_metrics(book_payload)
        # The list-markets response can lag the dedicated order book. Executability
        # must come from the freshest resting levels, with summary fields as fallback.
        yes_bid = metrics["yes_bids"][0][0] if metrics["yes_bids"] else as_float(market.get("yes_bid_dollars"))
        yes_ask = metrics["yes_asks"][0][0] if metrics["yes_asks"] else as_float(market.get("yes_ask_dollars"))
        no_bid = metrics["no_bids"][0][0] if metrics["no_bids"] else as_float(market.get("no_bid_dollars"))
        no_ask = metrics["no_asks"][0][0] if metrics["no_asks"] else as_float(market.get("no_ask_dollars"))
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
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": spread,
            "liquidity": as_float(market.get("liquidity_dollars")) or 0.0,
            "open_interest": as_float(market.get("open_interest_fp")) or 0.0,
            "volume": as_float(market.get("volume_fp")) or 0.0,
            "yes_bid_size": metrics["yes_bids"][0][1] if metrics["yes_bids"] else as_float(market.get("yes_bid_size_fp")),
            "yes_ask_size": metrics["yes_asks"][0][1] if metrics["yes_asks"] else as_float(market.get("yes_ask_size_fp")),
            "no_ask_size": metrics["no_asks"][0][1] if metrics["no_asks"] else None,
            "imbalance": metrics["imbalance"],
            "rapid_repricing": rapid,
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
                    rapid, json.dumps(metrics),
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
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        close = parse_time(market.get("close_time"))
        seconds_remaining = max(0.0, (close - datetime.now(UTC)).total_seconds()) if close else 0.0
        strike = market_strike(market)
        baseline = settlement_probability(
            float(btc["price"]), float(strike), seconds_remaining,
            btc.get("volatility_15m") or btc.get("volatility_5m"),
            btc.get("momentum_5m") or 0.0,
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
        }
        probability, model_version = self.models.predict(features, baseline.probability)
        variants = []
        for vol in (btc.get("volatility_5m"), btc.get("volatility_60m")):
            if vol:
                variants.append(
                    settlement_probability(
                        float(btc["price"]), float(strike), seconds_remaining, vol,
                        btc.get("momentum_5m") or 0.0,
                    ).probability
                )
        variant_spread = max(variants) - min(variants) if len(variants) > 1 else 0.0
        calibration = self.calibration_summary()
        portfolio = self.paper.portfolio()
        quality = self._data_quality(btc, market_state, seconds_remaining, settings)
        decision = make_decision(
            model_probability=probability,
            market=market_state,
            settings=settings,
            bankroll=portfolio["available_cash"],
            drawdown_pct=portfolio["session_drawdown_pct"],
            data_quality=quality,
            calibration=calibration,
            model_variant_spread=variant_spread,
        )
        previous = self.db.fetch_one(
            "SELECT * FROM signal_snapshots WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (market["ticker"],),
        )
        reason = material_change(previous, decision, float(settings["min_edge"]))
        notification = None
        if reason:
            signal_id = self._save_signal(
                market["ticker"], decision, model_version, features, btc,
                market_state, reason, observed_at,
            )
            if settings.get("paper_trading_enabled", True):
                self.paper.open_from_decision(market["ticker"], decision, model_version)
            if previous and previous.get("signal") != decision.signal:
                notification = {
                    "title": f"Signal changed: {previous.get('signal')} -> {decision.signal}",
                    "detail": f"Edge is {decision.edge * 100:+.1f} points." if decision.edge is not None else decision.explanation,
                    "signal_id": signal_id,
                }
        summary = self._market_summary(market)
        summary.update(
            {
                **market_state,
                "time_remaining_seconds": seconds_remaining,
                "model_probability": probability,
                "model_version": model_version,
                "z_distance": baseline.z_distance,
                "annualized_volatility": baseline.annualized_volatility,
                "model_variant_spread": variant_spread,
                "data_quality": quality,
                "decision": decision.as_dict(),
            }
        )
        return summary, notification

    def _data_quality(
        self,
        btc: dict[str, Any],
        market: dict[str, Any],
        seconds_remaining: float,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        if int(btc.get("exchange_count") or 0) < 2:
            return {"reliable": False, "reason": "fewer than two exchange feeds responded"}
        if float(btc.get("dispersion_pct") or 0) > float(settings["max_exchange_dispersion_pct"]):
            return {"reliable": False, "reason": "cross-exchange prices disagree beyond the configured limit"}
        if seconds_remaining <= 10:
            return {"reliable": False, "reason": "the contract is closing or transitioning"}
        if market.get("yes_ask") is None or market.get("no_ask") is None:
            return {"reliable": False, "reason": "an executable Kalshi ask is missing"}
        return {"reliable": True, "reason": "critical feeds are current and mutually consistent"}

    def _save_signal(
        self,
        ticker: str,
        decision: Decision,
        model_version: str,
        features: dict[str, Any],
        btc: dict[str, Any],
        market: dict[str, Any],
        reason: str,
        observed_at: str,
    ) -> int:
        return self.db.execute(
            """
            INSERT INTO signal_snapshots(
                observed_at,ticker,signal,reason_code,confidence,explanation,
                model_probability,market_probability,edge,expected_value,
                suggested_fraction,suggested_dollars,suggested_contracts,model_version,
                input_json,btc_state_json,kalshi_state_json,material_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observed_at, ticker, decision.signal, decision.reason_code,
                decision.confidence, decision.explanation, decision.model_probability,
                decision.market_probability, decision.edge, decision.expected_value,
                decision.suggested_fraction, decision.suggested_dollars,
                decision.suggested_contracts, model_version,
                json.dumps({"features": features}), json.dumps(btc), json.dumps(market), reason,
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
            self._pending_settlements.discard(ticker)
            if inserted:
                count = self.db.fetch_one("SELECT COUNT(*) AS count FROM settlements")["count"]
                latest = self.db.fetch_one(
                    "SELECT created_at FROM calibration_reports ORDER BY created_at DESC LIMIT 1"
                )
                needs_daily = not latest or str(latest["created_at"])[:10] != datetime.now(UTC).date().isoformat()
                if count <= 20 or needs_daily:
                    self.models.evaluate_and_retrain(f"settlement: {ticker}")

    def _unreliable_current(self, market: dict[str, Any], reason: str) -> dict[str, Any]:
        summary = self._market_summary(market)
        summary["decision"] = Decision(
            "NO TRADE", "DATA_UNRELIABLE", "Low", f"Data unreliable: {reason}",
            None, None, None, None, None, None, 0, 0, 0, None,
        ).as_dict()
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

    def _portfolio_summary(self) -> dict[str, Any]:
        portfolio = self.paper.portfolio()
        return {
            key: value
            for key, value in portfolio.items()
            if key not in {"trades", "orders"}
        }

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
            order = self.paper.place_order(
                ticker=str(current["ticker"]),
                side=str(payload.get("side", "")),
                action=str(payload.get("action", "")),
                order_type=order_type,
                market=current,
                dollars=payload.get("dollars"),
                contracts=payload.get("contracts"),
                limit_price=limit_price,
            )
            self.dashboard["paper"] = self._portfolio_summary()
            self._schedule_publish()
            return {"order": order, "portfolio": self.paper.portfolio()}

    async def cancel_manual_paper_order(self, order_id: int) -> dict[str, Any]:
        async with self._update_lock:
            if not self.paper.cancel_order(order_id):
                raise ValueError("That limit order is no longer open.")
            self.dashboard["paper"] = self._portfolio_summary()
            self._schedule_publish()
            return {"canceled": order_id, "portfolio": self.paper.portfolio()}

    def calibration_summary(self) -> dict[str, Any]:
        observations = self.models.observations()
        return calibration_metrics(
            (row["model_probability"], row["result"]) for row in observations
        )

    def chart(self, minutes: int) -> list[dict[str, Any]]:
        minutes = max(5, min(360, int(minutes)))
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        return self.db.fetch_all(
            """
            SELECT observed_at, composite_price AS price, dispersion_pct,
                   volatility_15m FROM btc_ticks
            WHERE observed_at >= ? ORDER BY observed_at ASC
            """,
            (since,),
        )

    def _degrade(self, error: str) -> None:
        current = self.dashboard.get("current")
        if current:
            current = dict(current)
            current["decision"] = Decision(
                "NO TRADE", "DATA_UNRELIABLE", "Low",
                "Data unreliable: the most recent refresh failed.",
                current.get("model_probability"), None, None, None, None, None,
                0, 0, 0, None,
            ).as_dict()
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
