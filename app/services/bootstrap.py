from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Database
from app.domain import calibration_metrics, iso_now, settlement_probability
from app.services.kalshi import KalshiPublicClient, as_float, market_strike
from app.services.market_data import BitcoinCompositeFeed


def _candle_close(section: dict[str, Any] | None) -> float | None:
    if not section:
        return None
    for key in ("close_dollars", "close"):
        value = as_float(section.get(key))
        if value is not None:
            return value
    return None


class HistoricalBootstrapService:
    """Builds point-in-time observations using only fields known before settlement."""

    def __init__(
        self,
        db: Database,
        kalshi: KalshiPublicClient,
        bitcoin: BitcoinCompositeFeed,
    ):
        self.db = db
        self.kalshi = kalshi
        self.bitcoin = bitcoin

    async def run(self, limit: int = 96) -> dict[str, Any]:
        recent = await self.kalshi.settled_markets(limit)
        markets = sorted(recent, key=lambda row: row.get("close_time", ""))[-limit:]
        markets = [row for row in markets if row.get("result") in {"yes", "no"} and market_strike(row)]
        if not markets:
            return {"imported": 0, "skipped": 0, "reason": "No recent settled markets returned."}
        closes = [datetime.fromisoformat(row["close_time"].replace("Z", "+00:00")) for row in markets]
        start = min(closes) - timedelta(minutes=25)
        end = max(closes)

        coinbase_rows: list[list[float]] = []
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(minutes=290))
            coinbase_rows.extend(
                await self.bitcoin.coinbase_candles(cursor.isoformat(), chunk_end.isoformat(), 60)
            )
            cursor = chunk_end + timedelta(seconds=1)
        coinbase = {int(row[0]): row for row in coinbase_rows}

        candle_payloads: dict[str, list[dict[str, Any]]] = {}
        # Production currently rejects very long query strings below the documented
        # 100-ticker cap, so keep batches short enough for conservative proxy limits.
        for offset in range(0, len(markets), 10):
            batch = markets[offset : offset + 10]
            batch_closes = [
                datetime.fromisoformat(row["close_time"].replace("Z", "+00:00"))
                for row in batch
            ]
            batch_start = min(batch_closes) - timedelta(minutes=20)
            batch_end = max(batch_closes)
            results = await self.kalshi.batch_candles(
                [row["ticker"] for row in batch],
                int(batch_start.timestamp()),
                int(batch_end.timestamp()),
            )
            for item in results:
                ticker = item.get("market_ticker") or item.get("ticker") or ""
                candle_payloads[ticker] = item.get("candlesticks", [])

        imported = 0
        skipped = 0
        for market in markets:
            ticker = market["ticker"]
            exists = self.db.fetch_one(
                "SELECT id FROM signal_snapshots WHERE ticker=? AND material_reason='historical bootstrap'",
                (ticker,),
            )
            if exists:
                skipped += 1
                continue
            close_dt = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
            observation_ts = int((close_dt - timedelta(minutes=5)).timestamp())
            spot_row = min(coinbase_rows, key=lambda row: abs(int(row[0]) - observation_ts), default=None)
            market_candles = candle_payloads.get(ticker, [])
            eligible = [row for row in market_candles if int(row.get("end_period_ts", 0)) <= observation_ts]
            market_candle = max(eligible, key=lambda row: row.get("end_period_ts", 0), default=None)
            if not spot_row or not market_candle or abs(int(spot_row[0]) - observation_ts) > 120:
                skipped += 1
                continue
            yes_bid = _candle_close(market_candle.get("yes_bid"))
            yes_ask = _candle_close(market_candle.get("yes_ask"))
            if yes_bid is None or yes_ask is None:
                skipped += 1
                continue
            spot = float(spot_row[4])
            prior = [row for row in coinbase_rows if observation_ts - 900 <= int(row[0]) <= observation_ts]
            returns = [math.log(float(b[4]) / float(a[4])) for a, b in zip(prior, prior[1:]) if a[4] and b[4]]
            if len(returns) >= 2:
                mean = sum(returns) / len(returns)
                variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
                annualized_volatility = math.sqrt(variance * 525960)
            else:
                annualized_volatility = 0.55
            strike = float(market_strike(market))
            estimate = settlement_probability(
                spot, strike, 300, annualized_volatility, model_version="baseline-1.0"
            )
            market_probability = (yes_bid + yes_ask) / 2
            observed_at = datetime.fromtimestamp(observation_ts, UTC).isoformat()
            result = 1 if market["result"] == "yes" else 0
            raw = json.dumps(market)
            self.db.execute(
                """
                INSERT INTO markets(
                    ticker,event_ticker,status,title,strike,open_time,close_time,
                    expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
                    first_seen_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET status=excluded.status,result=excluded.result,
                    raw_json=excluded.raw_json,updated_at=excluded.updated_at
                """,
                (
                    ticker, market.get("event_ticker"), market.get("status", "finalized"),
                    market.get("title"), strike, market.get("open_time"), market.get("close_time"),
                    market.get("expected_expiration_time"), market.get("result"),
                    market.get("rules_primary"), market.get("rules_secondary"), raw,
                    observed_at, iso_now(),
                ),
            )
            self.db.execute(
                """
                INSERT OR IGNORE INTO settlements(
                    ticker,settled_at,result,settlement_value,raw_json,processed_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (ticker, market.get("settlement_ts") or market["close_time"], result,
                 as_float(market.get("settlement_value_dollars")), raw, iso_now()),
            )
            features = {
                "z_distance": estimate.z_distance,
                "time_fraction": 300 / 900,
                "volatility_5m": annualized_volatility,
                "volatility_15m": annualized_volatility,
                "momentum_1m": 0.0,
                "momentum_5m": math.log(spot / float(prior[0][4])) if prior else 0.0,
                "dispersion_pct": 0.0,
                "orderbook_imbalance": 0.0,
                "market_probability": market_probability,
            }
            edge = estimate.probability - market_probability
            self.db.execute(
                """
                INSERT INTO signal_snapshots(
                    observed_at,ticker,signal,reason_code,confidence,explanation,
                    model_probability,market_probability,edge,expected_value,
                    suggested_fraction,suggested_dollars,suggested_contracts,model_version,
                    input_json,btc_state_json,kalshi_state_json,material_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    observed_at, ticker, "HISTORICAL OBSERVATION", "BOOTSTRAP", "Low",
                    "Point-in-time bootstrap observation; never used as a paper trade.",
                    estimate.probability, market_probability, edge, None, 0, 0, 0,
                    "baseline-1.0", json.dumps({"features": features, "source": "coinbase+kalshi-candles"}),
                    json.dumps({"price": spot, "source": "Coinbase 1-minute candle"}),
                    json.dumps({"yes_bid": yes_bid, "yes_ask": yes_ask}), "historical bootstrap",
                ),
            )
            imported += 1
        metrics = calibration_metrics(
            (row["model_probability"], row["result"])
            for row in self.db.fetch_all(
                """
                SELECT s.model_probability,z.result FROM signal_snapshots s
                JOIN settlements z ON z.ticker=s.ticker
                WHERE s.material_reason='historical bootstrap'
                """
            )
        )
        return {
            "imported": imported,
            "skipped": skipped,
            "source_markets": len(markets),
            "calibration": metrics,
            "limitation": "Coinbase spot is a free proxy; settlement uses CF Benchmarks BRTI.",
        }
