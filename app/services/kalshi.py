from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.domain import parse_time


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_strike(market: dict[str, Any]) -> float | None:
    strike = as_float(market.get("floor_strike"))
    if strike is not None:
        return strike
    custom = market.get("custom_strike") or {}
    for key in ("value", "strike", "target"):
        value = as_float(custom.get(key))
        if value is not None:
            return value
    return None


def orderbook_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    yes_levels = book.get("yes_dollars") or book.get("yes") or []
    no_levels = book.get("no_dollars") or book.get("no") or []

    def levels(rows: list[list[Any]]) -> list[tuple[float, float]]:
        parsed = []
        for row in rows:
            if len(row) >= 2 and as_float(row[0]) is not None and as_float(row[1]) is not None:
                parsed.append((float(row[0]), float(row[1])))
        return parsed

    yes = levels(yes_levels)
    no = levels(no_levels)
    yes_depth = sum(quantity for _, quantity in sorted(yes, reverse=True)[:5])
    no_depth = sum(quantity for _, quantity in sorted(no, reverse=True)[:5])
    total = yes_depth + no_depth
    imbalance = (yes_depth - no_depth) / total if total else 0.0
    yes_asks = sorted([(1.0 - price, quantity) for price, quantity in no])
    no_asks = sorted([(1.0 - price, quantity) for price, quantity in yes])
    return {
        "yes_bids": sorted(yes, reverse=True),
        "no_bids": sorted(no, reverse=True),
        "yes_asks": yes_asks,
        "no_asks": no_asks,
        "yes_depth_5": yes_depth,
        "no_depth_5": no_depth,
        "imbalance": imbalance,
    }


class KalshiPublicClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str, series: str):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.series = series

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # The engine gives this client a small, dedicated public-data pool.
        # Repeating the timeout here also keeps direct callers bounded: stale
        # market data is unsafe, but it must not stall the next refresh.
        response = await self.client.get(
            f"{self.base_url}{path}", params=params,
            timeout=httpx.Timeout(connect=2.5, read=3.5, write=3.5, pool=1.0),
        )
        response.raise_for_status()
        return response.json()

    async def active_markets(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        open_result, unopened_result = await asyncio.gather(
            self._get(
                "/markets",
                {"series_ticker": self.series, "status": "open", "limit": 20},
            ),
            self._get(
                "/markets",
                {"series_ticker": self.series, "status": "unopened", "limit": 20},
            ),
        )
        now = datetime.now(UTC)

        def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            valid = []
            for market in items:
                close = parse_time(market.get("close_time"))
                if close and close >= now:
                    valid.append(market)
            return sorted(valid, key=lambda item: item.get("close_time", ""))

        open_markets = ordered(open_result.get("markets", []))
        future_markets = ordered(unopened_result.get("markets", []))
        current = open_markets[0] if open_markets else None
        candidates = open_markets[1:] + future_markets
        next_market = sorted(candidates, key=lambda item: item.get("close_time", ""))[0] if candidates else None
        return current, next_market

    async def orderbook(self, ticker: str) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}/orderbook", {"depth": 20})

    async def market(self, ticker: str) -> dict[str, Any]:
        payload = await self._get(f"/markets/{ticker}")
        return payload.get("market", payload)

    async def trades(
        self, ticker: str, *, min_ts: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"ticker": ticker, "limit": min(limit, 1000)}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        payload = await self._get("/markets/trades", params)
        return payload.get("trades", [])

    async def settled_markets(self, limit: int = 250) -> list[dict[str, Any]]:
        payload = await self._get(
            "/markets",
            {"series_ticker": self.series, "status": "settled", "limit": min(limit, 1000)},
        )
        return payload.get("markets", [])
    async def historical_markets(self, limit: int = 250) -> list[dict[str, Any]]:
        payload = await self._get(
            "/historical/markets",
            {"series_ticker": self.series, "limit": min(limit, 1000)},
        )
        return payload.get("markets", [])

    async def batch_candles(
        self, tickers: list[str], start_ts: int, end_ts: int
    ) -> list[dict[str, Any]]:
        if not tickers:
            return []
        payload = await self._get(
            "/markets/candlesticks",
            {
                "market_tickers": ",".join(tickers[:100]),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": 1,
            },
        )
        return payload.get("markets", [])
