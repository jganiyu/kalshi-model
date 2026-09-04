from __future__ import annotations

import json
import math
import statistics
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.db import Database
from app.domain import clamp, iso_now, parse_time
from app.services.market_data import CompositeQuote, ExchangeTrade


CALCULATION_VERSION = "volume-signals-1"
BTC_TRADE_SOURCES = ("Coinbase", "Kraken")


def normalized_flow(buy_volume: float, sell_volume: float) -> float | None:
    total = buy_volume + sell_volume
    if total <= 0:
        return None
    return clamp((buy_volume - sell_volume) / total, -1.0, 1.0)


def volume_confirmation(momentum: float, relative_volume: float | None) -> float | None:
    if relative_volume is None:
        return None
    return float(momentum) * math.log1p(max(0.0, relative_volume))


def relative_volume(current: float, baseline: Iterable[float]) -> float | None:
    usable = [float(value) for value in baseline if value > 0 and math.isfinite(value)]
    if len(usable) < 3:
        return None
    normal = statistics.median(usable)
    if normal <= 0:
        return None
    return clamp(float(current) / normal, 0.0, 5.0)


def weighted_vwap(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    total = sum(float(row["size"]) for row in rows if float(row["size"]) > 0)
    if total <= 0:
        return None, None
    vwap = sum(float(row["price"]) * float(row["size"]) for row in rows) / total
    variance = sum(
        float(row["size"]) * (float(row["price"]) - vwap) ** 2 for row in rows
    ) / total
    return vwap, math.sqrt(max(0.0, variance))


class VolumeSignalService:
    """Collect and derive point-in-time volume features without future leakage."""

    def __init__(self, db: Database):
        self.db = db
        self._pending: list[ExchangeTrade] = []
        self._lock = threading.RLock()
        # The decision loop reads this bounded rolling window.  SQLite remains
        # the durable audit store, but must never be consulted for every quote.
        self._btc_rows: list[dict[str, Any]] = []
        self._kalshi_rows: dict[str, list[dict[str, Any]]] = {}
        self._rolling_loaded = False
        self._last_audit_bucket: int | None = None
        self._last_snapshot_bucket: tuple[str | None, int] | None = None
        self._latest: dict[str, Any] = self._empty("Collecting actual trade flow.")

    @staticmethod
    def _empty(message: str) -> dict[str, Any]:
        return {
            "status": "UNAVAILABLE",
            "calculation_version": CALCULATION_VERSION,
            "message": message,
            "data_completeness": 0.0,
            "venue_agreement": None,
            "sources": {},
            "metrics": {},
            "features": {},
            "availability": "shadow-only",
        }

    def add_trade(self, trade: ExchangeTrade) -> None:
        side = str(trade.taker_side).upper()
        if side not in {"BUY", "SELL"} or trade.size <= 0 or trade.price <= 0:
            return
        with self._lock:
            self._pending.append(trade)
            self._btc_rows.append({
                "observed_at": trade.observed_at,
                "exchange": trade.exchange,
                "price": float(trade.price),
                "size": float(trade.size),
                "taker_side": side,
                "signed_size": float(trade.size) if side == "BUY" else -float(trade.size),
            })
            self._trim_rolling_locked(parse_time(trade.observed_at) or datetime.now(UTC))

    def load_rolling_history(self) -> None:
        """Cold-load the small feature window; call this off the event loop."""
        now = datetime.now(UTC)
        btc_rows = self.db.fetch_all(
            """
            SELECT observed_at,exchange,price,size,taker_side,signed_size
            FROM btc_trade_ticks
            WHERE observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at,id
            """,
            ((now - timedelta(minutes=65)).isoformat(), now.isoformat()),
        )
        kalshi_rows = self.db.fetch_all(
            """
            SELECT observed_at,ticker,contracts,taker_side FROM kalshi_trade_ticks
            WHERE observed_at >= ? AND observed_at <= ?
            ORDER BY observed_at,id
            """,
            ((now - timedelta(minutes=10)).isoformat(), now.isoformat()),
        )
        with self._lock:
            # Trades arriving while this cold read was in flight are retained.
            pending_btc = list(self._btc_rows)
            self._btc_rows = [dict(row) for row in btc_rows] + pending_btc
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in kalshi_rows:
                grouped.setdefault(str(row["ticker"]), []).append(dict(row))
            for ticker, rows in self._kalshi_rows.items():
                grouped.setdefault(ticker, []).extend(rows)
            self._kalshi_rows = grouped
            self._trim_rolling_locked(now)
            self._rolling_loaded = True

    def _trim_rolling_locked(self, now: datetime) -> None:
        btc_cutoff = now - timedelta(minutes=65)
        self._btc_rows = [
            row for row in self._btc_rows
            if (parsed := parse_time(row.get("observed_at"))) is not None and parsed >= btc_cutoff
        ]
        kalshi_cutoff = now - timedelta(minutes=10)
        self._kalshi_rows = {
            ticker: [
                row for row in rows
                if (parsed := parse_time(row.get("observed_at"))) is not None and parsed >= kalshi_cutoff
            ]
            for ticker, rows in self._kalshi_rows.items()
        }

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        if not pending:
            return
        grouped: dict[tuple[str, int, str], dict[str, Any]] = {}
        for trade in pending:
            parsed = parse_time(trade.observed_at)
            if parsed is None:
                continue
            bucket = int(parsed.timestamp()) // 5
            key = (trade.exchange, bucket, trade.taker_side.upper())
            aggregate = grouped.setdefault(
                key,
                {
                    "observed_at": trade.observed_at,
                    "size": 0.0,
                    "notional": 0.0,
                    "count": 0,
                    "first_trade_id": trade.trade_id,
                    "last_trade_id": trade.trade_id,
                },
            )
            aggregate["observed_at"] = max(aggregate["observed_at"], trade.observed_at)
            aggregate["size"] += trade.size
            aggregate["notional"] += trade.price * trade.size
            aggregate["count"] += 1
            aggregate["last_trade_id"] = trade.trade_id
        rows = []
        for (exchange, bucket, side), aggregate in grouped.items():
            size = float(aggregate["size"])
            if size <= 0:
                continue
            rows.append(
                (
                    aggregate["observed_at"], exchange, f"5s:{bucket}:{side}",
                    aggregate["notional"] / size, size, side,
                    size if side == "BUY" else -size,
                    json.dumps(
                        {
                            "aggregation_seconds": 5,
                            "trade_count": aggregate["count"],
                            "first_trade_id": aggregate["first_trade_id"],
                            "last_trade_id": aggregate["last_trade_id"],
                        },
                        sort_keys=True,
                    ),
                )
            )
        self.db.executemany(
            """
            INSERT INTO btc_trade_ticks(
                observed_at,exchange,trade_id,price,size,taker_side,signed_size,raw_json
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(exchange,trade_id) DO UPDATE SET
                observed_at=MAX(observed_at,excluded.observed_at),
                price=((price*size)+(excluded.price*excluded.size))/(size+excluded.size),
                size=size+excluded.size,
                signed_size=signed_size+excluded.signed_size,
                raw_json=excluded.raw_json
            """,
            rows,
        )

    def audit_cumulative(self, composite: CompositeQuote, observed_at: str) -> None:
        """Persist rolling totals for diagnostics; never use them as interval volume."""
        parsed = parse_time(observed_at)
        audit_bucket = int(parsed.timestamp()) // 30 if parsed is not None else None
        if audit_bucket is not None and audit_bucket == self._last_audit_bucket:
            return
        self._last_audit_bucket = audit_bucket
        rows = []
        for quote in composite.quotes:
            valid = quote.volume is not None and math.isfinite(float(quote.volume))
            rows.append(
                (
                    observed_at, quote.exchange, quote.volume, quote.price,
                    "rolling_24h", int(valid), None if valid else "missing cumulative volume",
                )
            )
        self.db.executemany(
            """
            INSERT OR IGNORE INTO btc_volume_observations(
                observed_at,exchange,cumulative_volume,price,source_window,valid,reason
            ) VALUES (?,?,?,?,?,?,?)
            """,
            rows,
        )

    def record_kalshi_trades(self, ticker: str, trades: list[dict[str, Any]]) -> None:
        rows = []
        rolling_rows = []
        for trade in trades:
            trade_id = str(trade.get("trade_id") or "")
            observed_at = trade.get("created_time")
            side = str(
                trade.get("taker_outcome_side") or trade.get("taker_side") or ""
            ).upper()
            try:
                contracts = float(trade.get("count_fp") or trade.get("count") or 0)
                price = float(trade.get("yes_price_dollars"))
            except (TypeError, ValueError):
                continue
            if not trade_id or not observed_at or contracts <= 0 or side not in {"YES", "NO"}:
                continue
            rows.append(
                (
                    str(observed_at), ticker, trade_id, price, contracts, side,
                    int(bool(trade.get("is_block_trade"))),
                    # Every volume calculation uses the normalized columns.
                    "{}",
                )
            )
            rolling_rows.append({
                "observed_at": str(observed_at), "ticker": ticker,
                "contracts": contracts, "taker_side": side,
            })
        if rolling_rows:
            with self._lock:
                self._kalshi_rows.setdefault(ticker, []).extend(rolling_rows)
                newest = max(
                    (parse_time(row["observed_at"]) for row in rolling_rows),
                    default=None,
                )
                self._trim_rolling_locked(newest or datetime.now(UTC))
        self.db.executemany(
            """
            INSERT OR IGNORE INTO kalshi_trade_ticks(
                observed_at,ticker,trade_id,price,contracts,taker_side,is_block_trade,raw_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    @staticmethod
    def _window(rows: list[dict[str, Any]], now: datetime, seconds: int) -> list[dict[str, Any]]:
        start = now - timedelta(seconds=seconds)
        return [
            row for row in rows
            if (parsed := parse_time(row.get("observed_at"))) is not None
            and start <= parsed <= now
        ]

    @staticmethod
    def _trade_stats(rows: list[dict[str, Any]], seconds: int) -> dict[str, Any]:
        buy = sum(float(row["size"]) for row in rows if row["taker_side"] == "BUY")
        sell = sum(float(row["size"]) for row in rows if row["taker_side"] == "SELL")
        vwap, standard_deviation = weighted_vwap(rows)
        return {
            "total": buy + sell,
            "buy": buy,
            "sell": sell,
            "flow": normalized_flow(buy, sell),
            "cvd_slope": (buy - sell) / max(seconds, 1) if buy + sell > 0 else None,
            "vwap": vwap,
            "price_standard_deviation": standard_deviation,
        }

    @staticmethod
    def _baseline_buckets(
        rows: list[dict[str, Any]], now: datetime, seconds: int, count: int
    ) -> list[float]:
        values = []
        for index in range(1, count + 1):
            end = now - timedelta(seconds=seconds * index)
            start = end - timedelta(seconds=seconds)
            total = sum(
                float(row["size"])
                for row in rows
                if (parsed := parse_time(row.get("observed_at"))) is not None
                and start <= parsed < end
            )
            if total > 0:
                values.append(total)
        return values

    @staticmethod
    def _sign(value: float | None) -> int:
        if value is None or abs(value) < 1e-12:
            return 0
        return 1 if value > 0 else -1

    def snapshot(
        self,
        *,
        observed_at: str,
        ticker: str | None,
        btc_price: float,
        momentum_1m: float,
        momentum_5m: float,
        open_interest: float | None,
        seconds_remaining: float,
        threshold_margin: float,
        annualized_volatility: float,
        settlement_window_fraction: float,
        persist: bool = True,
        use_rolling_cache: bool = False,
    ) -> dict[str, Any]:
        now = parse_time(observed_at) or datetime.now(UTC)
        if use_rolling_cache:
            # New trades are placed in the cache by add_trade immediately;
            # flushing them to SQLite happens independently after this pass.
            with self._lock:
                self._trim_rolling_locked(now)
                btc_rows = list(self._btc_rows)
                kalshi_rows = list(self._kalshi_rows.get(str(ticker), [])) if ticker else []
        else:
            self.flush()
            since = (now - timedelta(minutes=65)).isoformat()
            btc_rows = self.db.fetch_all(
                """
                SELECT observed_at,exchange,price,size,taker_side,signed_size
                FROM btc_trade_ticks
                WHERE julianday(observed_at)>=julianday(?)
                  AND julianday(observed_at)<=julianday(?)
                ORDER BY julianday(observed_at),id
                """,
                (since, now.isoformat()),
            )
            kalshi_rows = []
        one = self._trade_stats(self._window(btc_rows, now, 60), 60)
        five = self._trade_stats(self._window(btc_rows, now, 300), 300)
        rvol_1m = relative_volume(
            one["total"], self._baseline_buckets(btc_rows, now, 60, 30)
        )
        rvol_5m = relative_volume(
            five["total"], self._baseline_buckets(btc_rows, now, 300, 12)
        )
        vwap_distance_1m = (
            (btc_price - one["vwap"]) / one["vwap"] if one["vwap"] else None
        )
        vwap_distance_5m = (
            (btc_price - five["vwap"]) / five["vwap"] if five["vwap"] else None
        )
        vwap_z_1m = (
            (btc_price - one["vwap"]) / max(one["price_standard_deviation"], 1e-8)
            if one["vwap"] and one["price_standard_deviation"] is not None else None
        )
        vwap_z_5m = (
            (btc_price - five["vwap"]) / max(five["price_standard_deviation"], 1e-8)
            if five["vwap"] and five["price_standard_deviation"] is not None else None
        )
        confirmation_1m = volume_confirmation(momentum_1m, rvol_1m)
        confirmation_5m = volume_confirmation(momentum_5m, rvol_5m)

        source_details: dict[str, Any] = {}
        fresh_sources = 0
        source_signs = []
        for source in BTC_TRADE_SOURCES:
            source_rows = [row for row in self._window(btc_rows, now, 60) if row["exchange"] == source]
            stats = self._trade_stats(source_rows, 60)
            latest = max(
                (parse_time(row["observed_at"]) for row in source_rows), default=None
            )
            fresh = bool(latest and (now - latest).total_seconds() <= 20)
            if fresh:
                fresh_sources += 1
            sign = self._sign(stats["flow"])
            if sign:
                source_signs.append(sign)
            source_details[source] = {
                "fresh": fresh,
                "last_trade_at": latest.isoformat() if latest else None,
                "volume_1m": stats["total"],
                "flow_imbalance_1m": stats["flow"],
            }
        completeness = fresh_sources / len(BTC_TRADE_SOURCES)
        venue_agreement = (
            1.0 if len(source_signs) >= 2 and len(set(source_signs)) == 1
            else 0.0 if len(source_signs) >= 2 else None
        )

        if ticker and not use_rolling_cache:
            kalshi_rows = self.db.fetch_all(
                """
                SELECT observed_at,contracts,taker_side FROM kalshi_trade_ticks
                WHERE ticker=? AND julianday(observed_at)>=julianday(?)
                  AND julianday(observed_at)<=julianday(?)
                ORDER BY julianday(observed_at),id
                """,
                (ticker, (now - timedelta(minutes=10)).isoformat(), now.isoformat()),
            )
        kalshi_1m = self._window(kalshi_rows, now, 60)
        kalshi_5m = self._window(kalshi_rows, now, 300)
        prior_5m = [
            row for row in kalshi_rows
            if (parsed := parse_time(row.get("observed_at"))) is not None
            and now - timedelta(minutes=10) <= parsed < now - timedelta(minutes=5)
        ]
        yes_1m = sum(float(row["contracts"]) for row in kalshi_1m if row["taker_side"] == "YES")
        no_1m = sum(float(row["contracts"]) for row in kalshi_1m if row["taker_side"] == "NO")
        kalshi_flow = normalized_flow(yes_1m, no_1m)
        current_turnover = (
            sum(float(row["contracts"]) for row in kalshi_5m) / float(open_interest)
            if open_interest and float(open_interest) > 0 else None
        )
        prior_turnover = (
            sum(float(row["contracts"]) for row in prior_5m) / float(open_interest)
            if open_interest and float(open_interest) > 0 else None
        )
        turnover_change = (
            current_turnover - prior_turnover
            if current_turnover is not None and prior_turnover is not None else None
        )
        flow_agreement = None
        if five["flow"] is not None and kalshi_flow is not None:
            flow_agreement = float(self._sign(five["flow"]) * self._sign(kalshi_flow))

        ready = bool(
            completeness >= 1.0 and rvol_1m is not None and rvol_5m is not None
            and one["flow"] is not None and five["flow"] is not None
        )
        status = "ACTIVE" if ready else "BUILDING" if btc_rows else "UNAVAILABLE"
        message = (
            "Reliable actual-trade volume features are available."
            if ready else "Building a trailing actual-trade volume baseline."
            if btc_rows else "Waiting for actual BTC trade flow."
        )
        time_fraction = clamp(seconds_remaining / 900.0, 0.0, 1.0)
        margin_direction = self._sign(threshold_margin)
        metrics = {
            "btc_rvol_1m": rvol_1m,
            "btc_rvol_5m": rvol_5m,
            "btc_flow_imbalance_1m": one["flow"],
            "btc_flow_imbalance_5m": five["flow"],
            "btc_cvd_slope_1m": one["cvd_slope"],
            "btc_cvd_slope_5m": five["cvd_slope"],
            "btc_volume_confirmation_1m": confirmation_1m,
            "btc_volume_confirmation_5m": confirmation_5m,
            "btc_vwap_distance_1m": vwap_distance_1m,
            "btc_vwap_distance_5m": vwap_distance_5m,
            "btc_vwap_z_1m": vwap_z_1m,
            "btc_vwap_z_5m": vwap_z_5m,
            "kalshi_flow_imbalance_1m": kalshi_flow,
            "kalshi_turnover_5m": current_turnover,
            "kalshi_turnover_change": turnover_change,
            "btc_kalshi_flow_agreement": flow_agreement,
        }
        features = {
            **{key: (float(value) if value is not None else 0.0) for key, value in metrics.items()},
            "volume_time_interaction": float(confirmation_5m or 0.0) * time_fraction,
            "volume_margin_interaction": float(confirmation_5m or 0.0) * margin_direction,
            "volume_volatility_interaction": float(confirmation_5m or 0.0) * float(annualized_volatility),
            "volume_settlement_interaction": float(confirmation_1m or 0.0) * float(settlement_window_fraction),
            "btc_volume_missing": float(not ready),
            "kalshi_volume_missing": float(kalshi_flow is None),
        }
        result = {
            "status": status,
            "calculation_version": CALCULATION_VERSION,
            "message": message,
            "data_completeness": completeness,
            "venue_agreement": venue_agreement,
            "sources": source_details,
            "metrics": metrics,
            "features": features,
            "feature_ready": ready,
            "availability": "shadow-only",
            "observed_at": observed_at,
        }
        bucket = int(now.timestamp()) // 5
        if persist and self._last_snapshot_bucket != (ticker, bucket):
            self._last_snapshot_bucket = (ticker, bucket)
            self.db.execute(
                """
                INSERT INTO volume_signal_snapshots(
                    observed_at,ticker,status,calculation_version,data_completeness,
                    venue_agreement,btc_rvol_1m,btc_rvol_5m,
                    btc_flow_imbalance_1m,btc_flow_imbalance_5m,
                    btc_cvd_slope_1m,btc_cvd_slope_5m,
                    btc_volume_confirmation_1m,btc_volume_confirmation_5m,
                    btc_vwap_distance_1m,btc_vwap_distance_5m,
                    btc_vwap_z_1m,btc_vwap_z_5m,kalshi_flow_imbalance_1m,
                    kalshi_turnover_5m,kalshi_turnover_change,
                    btc_kalshi_flow_agreement,values_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observed_at, ticker, status, CALCULATION_VERSION, completeness,
                    venue_agreement, rvol_1m, rvol_5m, one["flow"], five["flow"],
                    one["cvd_slope"], five["cvd_slope"], confirmation_1m,
                    confirmation_5m, vwap_distance_1m, vwap_distance_5m,
                    vwap_z_1m, vwap_z_5m, kalshi_flow, current_turnover,
                    turnover_change, flow_agreement, json.dumps(result, sort_keys=True),
                ),
            )
        self._latest = result
        return result

    def latest(self) -> dict[str, Any]:
        return self._latest

    def report(self, model: dict[str, Any] | None = None) -> dict[str, Any]:
        active = model or {}
        parameters = active.get("parameters") or {}
        names = list(parameters.get("feature_names") or [])
        coefficients = list(parameters.get("coefficients") or [])
        coefficient_map = dict(zip(names, coefficients))
        active_volume = {
            name: coefficient_map[name]
            for name in names if name.startswith(("btc_", "kalshi_", "volume_"))
            and name != "volume_acceleration"
        }
        latest_candidate = self.db.fetch_one(
            """
            SELECT version,created_at,validation_json,parameters_json
            FROM model_versions WHERE status='shadow'
            ORDER BY created_at DESC LIMIT 1
            """
        )
        candidate = None
        if latest_candidate:
            candidate = dict(latest_candidate)
            candidate["validation"] = json.loads(candidate.pop("validation_json"))
            candidate["parameters"] = json.loads(candidate.pop("parameters_json"))
        source_rows = self.db.fetch_all(
            """
            WITH ordered AS (
                SELECT
                    exchange,
                    observed_at,
                    cumulative_volume,
                    LAG(observed_at) OVER (
                        PARTITION BY exchange ORDER BY julianday(observed_at),id
                    ) AS previous_at,
                    LAG(cumulative_volume) OVER (
                        PARTITION BY exchange ORDER BY julianday(observed_at),id
                    ) AS previous_volume
                FROM btc_volume_observations
            )
            SELECT
                exchange,
                COUNT(*) AS observations,
                SUM(CASE WHEN cumulative_volume IS NULL THEN 1 ELSE 0 END) AS missing,
                SUM(CASE
                    WHEN cumulative_volume IS NOT NULL
                     AND previous_volume IS NOT NULL
                     AND cumulative_volume < previous_volume THEN 1 ELSE 0 END
                ) AS negative_changes,
                SUM(CASE
                    WHEN previous_at IS NOT NULL
                     AND (julianday(observed_at)-julianday(previous_at))*86400.0 > 30.5
                    THEN 1 ELSE 0 END
                ) AS gaps_over_30s
            FROM ordered
            GROUP BY exchange
            ORDER BY exchange
            """
        )
        source_audit = [
            {
                "exchange": str(row["exchange"]),
                "observations": int(row["observations"] or 0),
                "missing": int(row["missing"] or 0),
                "negative_changes": int(row["negative_changes"] or 0),
                "gaps_over_30s": int(row["gaps_over_30s"] or 0),
            }
            for row in source_rows
        ]
        legacy_rows = self.db.fetch_all(
            """
            SELECT s.input_json,z.result FROM signal_snapshots s
            JOIN settlements z ON z.ticker=s.ticker
            JOIN (SELECT ticker,MAX(id) id FROM signal_snapshots GROUP BY ticker) latest
              ON latest.id=s.id
            WHERE z.result IN (0,1)
            """
        )
        legacy_pairs: list[tuple[float, float]] = []
        for row in legacy_rows:
            try:
                features = json.loads(row["input_json"]).get("features", {})
                value = features.get("volume_acceleration")
                if value is not None and math.isfinite(float(value)):
                    legacy_pairs.append((float(value), float(row["result"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        correlation = None
        if len(legacy_pairs) >= 3:
            x = [pair[0] for pair in legacy_pairs]
            y = [pair[1] for pair in legacy_pairs]
            x_mean, y_mean = statistics.mean(x), statistics.mean(y)
            numerator = sum((a - x_mean) * (b - y_mean) for a, b in legacy_pairs)
            denominator = math.sqrt(
                sum((a - x_mean) ** 2 for a in x)
                * sum((b - y_mean) ** 2 for b in y)
            )
            correlation = numerator / denominator if denominator else None
        return {
            "current": self._latest,
            "active_model": active.get("version"),
            "active_volume_contributions": active_volume,
            "candidate": candidate,
            "audit": {
                "sources": source_audit,
                "source_definitions": {
                    "Coinbase": "BTC base volume over the rolling 24-hour ticker window.",
                    "Kraken": "BTC base volume over the rolling 24-hour ticker window.",
                    "Bitstamp": "BTC base volume reported by the 24-hour ticker.",
                },
                "finding": (
                    "Coinbase, Kraken, and Bitstamp ticker volume is a rolling total. "
                    "It is retained for diagnostics but is not treated as interval volume."
                ),
                "actual_trade_sources": list(BTC_TRADE_SOURCES),
                "legacy_volume_acceleration": {
                    "active_coefficient": coefficient_map.get("volume_acceleration"),
                    "settled_samples": len(legacy_pairs),
                    "outcome_correlation": correlation,
                    "interpretation": (
                        "Descriptive only; the legacy input mixes rolling-window changes "
                        "and is excluded from the new interval-volume calculations."
                    ),
                },
            },
            "generated_at": iso_now(),
        }
