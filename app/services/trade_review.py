from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from app.db import Database
from app.domain import iso_now, parse_time


SAMPLE_SECONDS = 5
CALCULATION_VERSION = "trade-review-1"


def _synchronized(method):
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def paper_trade_ref(trade_id: Any) -> str:
    return f"PAPER:{int(trade_id)}"


def broker_trade_ref(mode: str, ticker: str, side: str) -> str:
    return f"{str(mode).upper()}:{ticker}:{str(side).upper()}"


def review_metadata(
    db: Database, environment: str, trade_ref: str, trade_status: str | None
) -> dict[str, Any]:
    link = db.fetch_one(
        """
        SELECT s.id,s.status,s.coverage,s.gap_count,s.regular_point_count
        FROM trade_review_links l
        JOIN trade_review_sessions s ON s.id=l.session_id
        WHERE l.environment=? AND l.trade_ref=?
        """,
        (str(environment).upper(), trade_ref),
    )
    historical_status = str(trade_status or "").upper()
    available = bool(
        link
        and link.get("status") in {"FINALIZED", "PARTIAL"}
        and historical_status not in {"OPEN", "UNSETTLED", "PARTIALLY CLOSED"}
    )
    return {
        "review_ref": trade_ref,
        "review_session_id": link.get("id") if link else None,
        "review_available": available,
        "review_status": link.get("status") if link else "UNAVAILABLE",
        "review_coverage": link.get("coverage") if link else None,
        "review_gap_count": link.get("gap_count") if link else None,
    }


class TradeReviewService:
    """Persist structured market evidence only for markets that contain a trade."""

    def __init__(self, db: Database, *, buffer_points: int = 190):
        self.db = db
        self._lock = threading.RLock()
        self._buffers: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=buffer_points)
        )
        self._buffer_ticker: str | None = None

    @staticmethod
    def _aligned_time(value: str | None) -> str:
        parsed = parse_time(value) or datetime.now(UTC)
        parsed = parsed.astimezone(UTC).replace(microsecond=0)
        parsed = parsed.replace(second=parsed.second - parsed.second % SAMPLE_SECONDS)
        return parsed.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number

    def _configuration_snapshot_id(self) -> int | None:
        row = self.db.fetch_one(
            "SELECT id FROM configuration_snapshots ORDER BY id DESC LIMIT 1"
        )
        return int(row["id"]) if row else None

    def _point(self, current: dict[str, Any], observed_at: str) -> dict[str, Any]:
        forecast = current.get("forecast") or {}
        readiness = current.get("standard_edge_readiness") or {}
        quality = current.get("data_quality") or {}
        mvi = current.get("margin_volatility") or {}
        threshold = self._number(current.get("strike"))
        btc_proxy = self._number(
            current.get("settlement_proxy_price") or current.get("btc_proxy")
        )
        # settlement_proxy_price is the exact model input; callers also attach the
        # live composite as btc_proxy when it differs.
        live_proxy = self._number(current.get("btc_proxy"))
        if live_proxy is not None:
            btc_proxy = live_proxy
        state = {
            "market": {
                key: current.get(key)
                for key in (
                    "ticker", "open_time", "close_time", "status", "strike",
                    "yes_bid", "yes_ask", "no_bid", "no_ask", "spread",
                    "liquidity", "open_interest", "volume", "yes_bid_size",
                    "yes_ask_size", "no_bid_size", "no_ask_size", "imbalance",
                    "rapid_repricing", "price_level_structure",
                    "price_ranges",
                )
            },
            "btc_state": current.get("btc_state") or {},
            "forecast": forecast,
            "trade_assessments": current.get("trade_assessments") or {},
            "trade_decisions": current.get("trade_decisions") or {},
            "data_quality": quality,
            "standard_edge_readiness": readiness,
            "margin_volatility": mvi,
            "settlement_window": current.get("settlement_window") or {},
            "benchmark_uncertainty_dollars": current.get(
                "benchmark_uncertainty_dollars"
            ),
            "model_variant_spread": current.get("model_variant_spread"),
            "annualized_volatility": current.get("annualized_volatility"),
            "z_distance": current.get("z_distance"),
            "trading_mode": current.get("trading_mode"),
        }
        return {
            "observed_at": self._aligned_time(observed_at),
            "seconds_remaining": self._number(current.get("time_remaining_seconds")),
            "threshold": threshold,
            "btc_proxy": btc_proxy,
            "margin": (
                btc_proxy - threshold
                if btc_proxy is not None and threshold is not None else None
            ),
            "yes_bid": self._number(current.get("yes_bid")),
            "yes_ask": self._number(current.get("yes_ask")),
            "no_bid": self._number(current.get("no_bid")),
            "no_ask": self._number(current.get("no_ask")),
            "spread": self._number(current.get("spread")),
            "liquidity": self._number(current.get("liquidity")),
            "open_interest": self._number(current.get("open_interest")),
            "volume": self._number(current.get("volume")),
            "up_probability": self._number(
                forecast.get("up_probability", current.get("up_probability"))
            ),
            "forecast_signal": forecast.get("signal"),
            "mvi": self._number(mvi.get("mvi")),
            "expected_remaining_move": self._number(
                mvi.get("expected_remaining_move")
            ),
            "cushion_ratio": self._number(mvi.get("cushion_ratio")),
            "data_reliable": int(bool(quality.get("reliable"))),
            "readiness_status": readiness.get("status"),
            "readiness_side": readiness.get("side"),
            "readiness_blocker": readiness.get("blocker"),
            "model_version": current.get("model_version"),
            "configuration_snapshot_id": self._configuration_snapshot_id(),
            "state_json": json.dumps(state, sort_keys=True, separators=(",", ":")),
        }

    def _buffer_point(self, ticker: str, point: dict[str, Any]) -> None:
        if self._buffer_ticker != ticker:
            self._buffers.clear()
            self._buffer_ticker = ticker
        buffer = self._buffers[ticker]
        if buffer and buffer[-1]["observed_at"] == point["observed_at"]:
            buffer[-1] = point
        else:
            buffer.append(point)

    def _trade_links(self, ticker: str) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        paper = self.db.fetch_all(
            """
            SELECT id,ticker,side,opened_at,settled_at,status,strategy,source
            FROM paper_trades WHERE ticker=?
            """,
            (ticker,),
        )
        for trade in paper:
            links.append(
                {
                    "environment": "PAPER",
                    "trade_ref": paper_trade_ref(trade["id"]),
                    "source_type": "paper_trade",
                    "source_id": str(trade["id"]),
                    "ticker": ticker,
                    "side": trade.get("side"),
                    "opened_at": trade.get("opened_at"),
                    "closed_at": trade.get("settled_at"),
                    "status": str(trade.get("status") or "").upper(),
                    "strategy": trade.get("strategy") or trade.get("source"),
                }
            )
        broker_rows = self.db.fetch_all(
            """
            SELECT mode,ticker,side,MIN(filled_at) opened_at,
                   GROUP_CONCAT(DISTINCT strategy) strategies
            FROM broker_fills
            WHERE ticker=? AND action='BUY'
            GROUP BY mode,ticker,side
            """,
            (ticker,),
        )
        for trade in broker_rows:
            mode = str(trade["mode"]).upper()
            settlement = self.db.fetch_one(
                "SELECT settled_at FROM broker_settlements WHERE mode=? AND ticker=?",
                (mode, ticker),
            )
            links.append(
                {
                    "environment": mode,
                    "trade_ref": broker_trade_ref(mode, ticker, trade["side"]),
                    "source_type": "broker_fills",
                    "source_id": f"{ticker}:{trade['side']}",
                    "ticker": ticker,
                    "side": trade.get("side"),
                    "opened_at": trade.get("opened_at"),
                    "closed_at": (settlement or {}).get("settled_at"),
                    "status": "SETTLED" if settlement else "OPEN",
                    "strategy": trade.get("strategies") or "EXTERNAL",
                }
            )
        return links

    def _ensure_session(self, link: dict[str, Any], point: dict[str, Any]) -> int:
        market = self.db.fetch_one(
            "SELECT open_time,close_time FROM markets WHERE ticker=?",
            (link["ticker"],),
        ) or {}
        self.db.execute(
            """
            INSERT OR IGNORE INTO trade_review_sessions(
                environment,ticker,market_open_time,market_close_time,
                recording_started_at,created_at,status,calculation_version
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                link["environment"], link["ticker"], market.get("open_time"),
                market.get("close_time"), point["observed_at"], iso_now(),
                "RECORDING", CALCULATION_VERSION,
            ),
        )
        session = self.db.fetch_one(
            "SELECT id FROM trade_review_sessions WHERE environment=? AND ticker=?",
            (link["environment"], link["ticker"]),
        )
        assert session
        session_id = int(session["id"])
        self.db.execute(
            """
            INSERT INTO trade_review_links(
                session_id,environment,trade_ref,source_type,source_id,ticker,
                side,opened_at,closed_at,status,strategy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(environment,trade_ref) DO UPDATE SET
                closed_at=excluded.closed_at,status=excluded.status,
                strategy=excluded.strategy
            """,
            (
                session_id, link["environment"], link["trade_ref"],
                link["source_type"], link["source_id"], link["ticker"],
                link.get("side"), link.get("opened_at"), link.get("closed_at"),
                link.get("status"), link.get("strategy"),
            ),
        )
        return session_id

    @staticmethod
    def _point_params(session_id: int, point: dict[str, Any], kind: str) -> tuple[Any, ...]:
        return (
            session_id, point["observed_at"], kind,
            point.get("seconds_remaining"), point.get("threshold"),
            point.get("btc_proxy"), point.get("margin"), point.get("yes_bid"),
            point.get("yes_ask"), point.get("no_bid"), point.get("no_ask"),
            point.get("spread"), point.get("liquidity"),
            point.get("open_interest"), point.get("volume"),
            point.get("up_probability"), point.get("forecast_signal"),
            point.get("mvi"), point.get("expected_remaining_move"),
            point.get("cushion_ratio"), point.get("data_reliable"),
            point.get("readiness_status"), point.get("readiness_side"),
            point.get("readiness_blocker"), point.get("model_version"),
            point.get("configuration_snapshot_id"), point["state_json"],
        )

    def _insert_points(
        self, session_id: int, points: list[dict[str, Any]], kind: str = "REGULAR"
    ) -> None:
        self.db.executemany(
            """
            INSERT OR IGNORE INTO trade_review_points(
                session_id,observed_at,sample_kind,seconds_remaining,threshold,
                btc_proxy,margin,yes_bid,yes_ask,no_bid,no_ask,spread,liquidity,
                open_interest,volume,up_probability,forecast_signal,mvi,
                expected_remaining_move,cushion_ratio,data_reliable,
                readiness_status,readiness_side,readiness_blocker,model_version,
                configuration_snapshot_id,state_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [self._point_params(session_id, point, kind) for point in points],
        )

    def _event(
        self,
        session_id: int,
        environment: str,
        event_type: str,
        observed_at: str,
        *,
        trade_ref: str | None = None,
        side: str | None = None,
        action: str | None = None,
        price: Any = None,
        contracts: Any = None,
        fees: Any = None,
        detail: dict[str, Any] | None = None,
        identity: str,
    ) -> bool:
        state_hash = hashlib.sha256(identity.encode()).hexdigest()
        if self.db.fetch_one(
            "SELECT id FROM trade_review_events WHERE session_id=? AND state_hash=?",
            (session_id, state_hash),
        ):
            return False
        self.db.execute(
            """
            INSERT INTO trade_review_events(
                session_id,observed_at,event_type,environment,trade_ref,side,
                action,price,contracts,fees,detail_json,state_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id, observed_at, event_type, environment, trade_ref, side,
                action, self._number(price), self._number(contracts),
                self._number(fees), json.dumps(detail or {}, sort_keys=True), state_hash,
            ),
        )
        return True

    def _sync_source_events(self, session_id: int, link: dict[str, Any]) -> bool:
        environment = link["environment"]
        ticker = link["ticker"]
        added = False
        if environment == "PAPER":
            trade_id = int(link["source_id"])
            entries = self.db.fetch_all(
                "SELECT * FROM paper_entries WHERE trade_id=? ORDER BY opened_at,id",
                (trade_id,),
            )
            for index, entry in enumerate(entries):
                added |= self._event(
                    session_id, environment,
                    "ENTRY" if index == 0 else "PARTIAL_FILL", entry["opened_at"],
                    trade_ref=link["trade_ref"], side=entry.get("side"), action="BUY",
                    price=entry.get("entry_price"), contracts=entry.get("initial_contracts"),
                    fees=entry.get("entry_fees"),
                    detail={"strategy": entry.get("strategy") or entry.get("source")},
                    identity=f"paper-entry:{entry['id']}",
                )
            exits = self.db.fetch_all(
                """
                SELECT o.* FROM paper_orders o JOIN paper_entries e ON e.id=o.entry_id
                WHERE e.trade_id=? AND o.action='SELL' AND o.status='filled'
                ORDER BY o.filled_at,o.id
                """,
                (trade_id,),
            )
            total_entry_contracts = sum(
                float(entry.get("initial_contracts") or 0) for entry in entries
            )
            exited_contracts = 0.0
            for order in exits:
                exited_contracts += float(order.get("filled_contracts") or 0)
                added |= self._event(
                    session_id, environment,
                    "EXIT"
                    if exited_contracts + 1e-9 >= total_entry_contracts
                    else "PARTIAL_EXIT",
                    order.get("filled_at") or order.get("created_at"),
                    trade_ref=link["trade_ref"], side=order.get("side"), action="SELL",
                    price=order.get("filled_price"), contracts=order.get("filled_contracts"),
                    fees=order.get("fees"), detail={"source": order.get("source")},
                    identity=f"paper-exit:{order['id']}",
                )
            trade = self.db.fetch_one("SELECT * FROM paper_trades WHERE id=?", (trade_id,))
            if trade and trade.get("status") == "settled":
                added |= self._event(
                    session_id, environment, "SETTLEMENT", trade.get("settled_at") or iso_now(),
                    trade_ref=link["trade_ref"], side=trade.get("side"),
                    price=1.0 if trade.get("outcome") else 0.0,
                    contracts=trade.get("contracts"),
                    detail={"outcome": trade.get("outcome"), "pnl": trade.get("realized_pnl")},
                    identity=f"paper-settlement:{trade_id}:{trade.get('settled_at')}",
                )
        else:
            fills = self.db.fetch_all(
                """
                SELECT * FROM broker_fills
                WHERE mode=? AND ticker=? AND side=? ORDER BY filled_at,id
                """,
                (environment, ticker, link.get("side")),
            )
            buy_index = 0
            sell_index = 0
            for fill in fills:
                if fill.get("action") == "BUY":
                    event_type = "ENTRY" if buy_index == 0 else "PARTIAL_FILL"
                    buy_index += 1
                else:
                    event_type = "EXIT" if sell_index == 0 else "PARTIAL_EXIT"
                    sell_index += 1
                added |= self._event(
                    session_id, environment, event_type, fill["filled_at"],
                    trade_ref=link["trade_ref"], side=fill.get("side"),
                    action=fill.get("action"), price=fill.get("price"),
                    contracts=fill.get("contracts"), fees=fill.get("fee"),
                    detail={"strategy": fill.get("strategy"), "source": fill.get("source")},
                    identity=f"broker-fill:{environment}:{fill['fill_id']}",
                )
            settlement = self.db.fetch_one(
                "SELECT * FROM broker_settlements WHERE mode=? AND ticker=?",
                (environment, ticker),
            )
            if settlement:
                added |= self._event(
                    session_id, environment, "SETTLEMENT", settlement["settled_at"],
                    trade_ref=link["trade_ref"], side=link.get("side"),
                    detail={
                        "market_result": settlement.get("market_result"),
                        "position_won": settlement.get("position_won"),
                        "pnl": settlement.get("realized_pnl"),
                    },
                    identity=f"broker-settlement:{environment}:{ticker}:{settlement['settled_at']}",
                )
        return added

    def _state_events(
        self, session_id: int, environment: str, point: dict[str, Any]
    ) -> bool:
        previous = self.db.fetch_one(
            """
            SELECT * FROM trade_review_points
            WHERE session_id=? AND sample_kind='REGULAR' AND observed_at<?
            ORDER BY observed_at DESC,id DESC LIMIT 1
            """,
            (session_id, point["observed_at"]),
        )
        if not previous:
            return False
        changed = False
        transitions = (
            ("FORECAST_CHANGE", "forecast_signal", point.get("forecast_signal")),
            ("CANDIDATE_SIDE_CHANGE", "readiness_side", point.get("readiness_side")),
            ("READINESS_CHANGE", "readiness_status", point.get("readiness_status")),
            ("MODEL_CHANGE", "model_version", point.get("model_version")),
            (
                "CONFIGURATION_CHANGE", "configuration_snapshot_id",
                point.get("configuration_snapshot_id"),
            ),
        )
        for event_type, key, value in transitions:
            if previous.get(key) == value:
                continue
            changed |= self._event(
                session_id, environment, event_type, point["observed_at"],
                detail={"from": previous.get(key), "to": value},
                identity=f"{event_type}:{point['observed_at']}:{previous.get(key)}:{value}",
            )
        try:
            old_state = json.loads(previous.get("state_json") or "{}")
            new_state = json.loads(point["state_json"])
        except (TypeError, json.JSONDecodeError):
            return changed
        old_gates = (old_state.get("standard_edge_readiness") or {}).get("gates") or {}
        new_gates = (new_state.get("standard_edge_readiness") or {}).get("gates") or {}
        for gate, detail in new_gates.items():
            old_passed = bool((old_gates.get(gate) or {}).get("passed"))
            new_passed = bool((detail or {}).get("passed"))
            if old_passed == new_passed:
                continue
            changed |= self._event(
                session_id, environment, "GATE_CHANGE", point["observed_at"],
                detail={"gate": gate, "from": old_passed, "to": new_passed},
                identity=f"gate:{gate}:{point['observed_at']}:{old_passed}:{new_passed}",
            )
        return changed

    @_synchronized
    def observe(self, current: dict[str, Any] | None, observed_at: str) -> None:
        if not current or not current.get("ticker"):
            return
        ticker = str(current["ticker"])
        enriched = dict(current)
        if enriched.get("btc_proxy") is None:
            latest_btc = self.db.fetch_one(
                "SELECT composite_price FROM btc_ticks ORDER BY observed_at DESC,id DESC LIMIT 1"
            )
            if latest_btc:
                enriched["btc_proxy"] = latest_btc.get("composite_price")
        point = self._point(enriched, observed_at)
        self._buffer_point(ticker, point)
        links = self._trade_links(ticker)
        if not links:
            return
        sessions: dict[int, dict[str, Any]] = {}
        for link in links:
            session_id = self._ensure_session(link, point)
            sessions[session_id] = link
            count = self.db.fetch_one(
                "SELECT COUNT(*) count FROM trade_review_points WHERE session_id=?",
                (session_id,),
            )
            if not count or int(count["count"]) == 0:
                self._insert_points(session_id, list(self._buffers[ticker]))
            else:
                self._insert_points(session_id, [point])
        for session_id, link in sessions.items():
            event_added = self._sync_source_events(session_id, link)
            state_changed = self._state_events(
                session_id, link["environment"], point
            )
            if event_added or state_changed:
                self._insert_points(session_id, [point], "EVENT")

    @_synchronized
    def finalize(
        self,
        ticker: str,
        *,
        result: int,
        settled_at: str,
        settlement_value: float | None,
    ) -> None:
        market = self.db.fetch_one(
            "SELECT strike,open_time,close_time FROM markets WHERE ticker=?", (ticker,)
        ) or {}
        sessions = self.db.fetch_all(
            "SELECT * FROM trade_review_sessions WHERE ticker=? AND status='RECORDING'",
            (ticker,),
        )
        for session in sessions:
            session_id = int(session["id"])
            for link in self.db.fetch_all(
                "SELECT * FROM trade_review_links WHERE session_id=?", (session_id,)
            ):
                self._sync_source_events(session_id, link)
            regular = self.db.fetch_all(
                """
                SELECT observed_at FROM trade_review_points
                WHERE session_id=? AND sample_kind='REGULAR' ORDER BY observed_at,id
                """,
                (session_id,),
            )
            expected = 180
            opened = parse_time(session.get("market_open_time") or market.get("open_time"))
            closed = parse_time(session.get("market_close_time") or market.get("close_time"))
            if opened and closed:
                expected = max(1, round((closed - opened).total_seconds() / SAMPLE_SECONDS))
            gaps = 0
            times = [parse_time(row["observed_at"]) for row in regular]
            valid_times = [value for value in times if value is not None]
            for previous, current in zip(valid_times, valid_times[1:]):
                if (current - previous).total_seconds() > SAMPLE_SECONDS * 1.5:
                    gaps += 1
            coverage = min(1.0, len(regular) / max(expected, 1))
            status = "FINALIZED" if coverage >= 0.95 and gaps == 0 else "PARTIAL"
            settlement_margin = (
                float(settlement_value) - float(market["strike"])
                if settlement_value is not None and market.get("strike") is not None
                else None
            )
            self.db.execute(
                """
                UPDATE trade_review_sessions SET finalized_at=?,status=?,
                    settlement_result=?,settlement_value=?,settlement_margin=?,
                    expected_regular_points=?,regular_point_count=?,coverage=?,gap_count=?
                WHERE id=?
                """,
                (
                    settled_at, status, "YES" if result else "NO", settlement_value,
                    settlement_margin, expected, len(regular), coverage, gaps, session_id,
                ),
            )
            self._event(
                session_id, session["environment"], "SETTLEMENT", settled_at,
                detail={
                    "result": "YES" if result else "NO",
                    "settlement_value": settlement_value,
                    "settlement_margin": settlement_margin,
                },
                identity=f"public-settlement:{ticker}:{settled_at}",
            )

    def _trade_summary(self, link: dict[str, Any]) -> dict[str, Any]:
        if link["environment"] == "PAPER":
            trade = self.db.fetch_one(
                "SELECT * FROM paper_trades WHERE id=?", (int(link["source_id"]),)
            ) or {}
            exits = self.db.fetch_all(
                """
                SELECT o.* FROM paper_orders o JOIN paper_entries e ON e.id=o.entry_id
                WHERE e.trade_id=? AND o.action='SELL' AND o.status='filled'
                ORDER BY o.filled_at,o.id
                """,
                (int(link["source_id"]),),
            )
            exit_quantity = sum(float(row.get("filled_contracts") or 0) for row in exits)
            exit_value = sum(
                float(row.get("filled_contracts") or 0)
                * float(row.get("filled_price") or 0)
                for row in exits
            )
            return {
                "ticker": trade.get("ticker"), "side": trade.get("side"),
                "opened_at": trade.get("opened_at"),
                "closed_at": trade.get("settled_at")
                or (exits[-1].get("filled_at") if exits else None),
                "entry_price": trade.get("entry_price"), "contracts": trade.get("contracts"),
                "exit_price": exit_value / exit_quantity if exit_quantity else None,
                "fees": float(trade.get("fees") or 0)
                + sum(float(row.get("fees") or 0) for row in exits),
                "strategy": trade.get("strategy") or trade.get("source"),
                "status": trade.get("status"), "realized_pnl": trade.get("realized_pnl"),
                "model_probability": trade.get("model_probability"),
                "edge": trade.get("edge"), "expected_value": trade.get("expected_value"),
                "available_cash_after": trade.get("available_cash_after"),
            }
        fills = self.db.fetch_all(
            """
            SELECT * FROM broker_fills WHERE mode=? AND ticker=? AND side=?
            ORDER BY filled_at,id
            """,
            (link["environment"], link["ticker"], link.get("side")),
        )
        buys = [row for row in fills if row.get("action") == "BUY"]
        quantity = sum(float(row.get("contracts") or 0) for row in buys)
        value = sum(
            float(row.get("contracts") or 0) * float(row.get("price") or 0)
            for row in buys
        )
        settlement = self.db.fetch_one(
            "SELECT * FROM broker_settlements WHERE mode=? AND ticker=?",
            (link["environment"], link["ticker"]),
        ) or {}
        intent = self.db.fetch_one(
            """
            SELECT decision_snapshot_json FROM broker_order_intents
            WHERE mode=? AND ticker=? AND side=? AND action='BUY'
            ORDER BY created_at,id LIMIT 1
            """,
            (link["environment"], link["ticker"], link.get("side")),
        ) or {}
        try:
            decision_snapshot = json.loads(intent.get("decision_snapshot_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            decision_snapshot = {}
        assessment = decision_snapshot.get("assessment") or decision_snapshot
        buy_assessment = assessment.get("buy") or {}
        status = "SETTLED" if settlement else link.get("status")
        sells = [row for row in fills if row.get("action") == "SELL"]
        exit_quantity = sum(float(row.get("contracts") or 0) for row in sells)
        exit_value = sum(
            float(row.get("contracts") or 0) * float(row.get("price") or 0)
            for row in sells
        )
        latest_activity = max(
            fills, key=lambda row: str(row.get("filled_at") or ""), default={}
        )
        return {
            "ticker": link["ticker"], "side": link.get("side"),
            "opened_at": buys[0].get("filled_at") if buys else link.get("opened_at"),
            "closed_at": settlement.get("settled_at") or link.get("closed_at"),
            "entry_price": value / quantity if quantity else None, "contracts": quantity,
            "exit_price": exit_value / exit_quantity if exit_quantity else None,
            "fees": sum(float(row.get("fee") or 0) for row in fills)
            + float(settlement.get("fees") or 0),
            "strategy": link.get("strategy"), "status": status,
            "realized_pnl": settlement.get("realized_pnl"),
            "model_probability": assessment.get("model_probability"),
            "expected_value": buy_assessment.get("expected_value"),
            "edge": buy_assessment.get("net_edge"),
            "available_cash_after": settlement.get("available_cash_after")
            if settlement else latest_activity.get("available_cash_after"),
        }

    def review(self, environment: str, trade_ref: str) -> dict[str, Any]:
        mode = str(environment).upper()
        link = self.db.fetch_one(
            """
            SELECT l.*,s.status session_status,s.market_open_time,s.market_close_time,
                   s.finalized_at,s.settlement_result,s.settlement_value,
                   s.settlement_margin,s.expected_regular_points,
                   s.regular_point_count,s.coverage,s.gap_count,s.calculation_version
            FROM trade_review_links l JOIN trade_review_sessions s ON s.id=l.session_id
            WHERE l.environment=? AND l.trade_ref=?
            """,
            (mode, trade_ref),
        )
        if not link:
            raise ValueError("Historical review is unavailable for this legacy trade.")
        if link.get("session_status") not in {"FINALIZED", "PARTIAL"}:
            raise ValueError("Historical review becomes available after market settlement.")
        rows = self.db.fetch_all(
            "SELECT * FROM trade_review_points WHERE session_id=? ORDER BY observed_at,id",
            (link["session_id"],),
        )
        points = []
        for row in rows:
            row["state"] = json.loads(row.pop("state_json") or "{}")
            points.append(row)
        events = self.db.fetch_all(
            "SELECT * FROM trade_review_events WHERE session_id=? ORDER BY observed_at,id",
            (link["session_id"],),
        )
        for event in events:
            event["detail"] = json.loads(event.pop("detail_json") or "{}")
        regular = [point for point in points if point["sample_kind"] == "REGULAR"]
        gaps: list[dict[str, Any]] = []
        for previous, current in zip(regular, regular[1:]):
            before = parse_time(previous["observed_at"])
            after = parse_time(current["observed_at"])
            if before and after and (after - before).total_seconds() > SAMPLE_SECONDS * 1.5:
                gaps.append(
                    {
                        "from": previous["observed_at"], "to": current["observed_at"],
                        "seconds": (after - before).total_seconds(),
                    }
                )
        return {
            "environment": mode,
            "trade_ref": trade_ref,
            "trade": self._trade_summary(link),
            "session": {
                key: link.get(key)
                for key in (
                    "session_status", "market_open_time", "market_close_time",
                    "finalized_at", "settlement_result", "settlement_value",
                    "settlement_margin", "expected_regular_points",
                    "regular_point_count", "coverage", "gap_count",
                    "calculation_version",
                )
            },
            "points": points,
            "events": events,
            "gaps": gaps,
        }
