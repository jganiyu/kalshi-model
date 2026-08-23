from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.domain import robust_composite


@dataclass(frozen=True)
class ExchangeQuote:
    exchange: str
    price: float
    bid: float | None
    ask: float | None
    volume: float | None
    latency_ms: float


@dataclass(frozen=True)
class CompositeQuote:
    price: float | None
    dispersion_pct: float | None
    quotes: list[ExchangeQuote]
    errors: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "dispersion_pct": self.dispersion_pct,
            "exchange_count": len(self.quotes),
            "quotes": [asdict(quote) for quote in self.quotes],
            "errors": self.errors,
        }


class BitcoinCompositeFeed:
    endpoints = {
        "Coinbase": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        "Kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
        "Bitstamp": "https://www.bitstamp.net/api/v2/ticker/btcusd/",
    }

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self) -> CompositeQuote:
        responses = await asyncio.gather(
            *(self._fetch_one(name, url) for name, url in self.endpoints.items()),
            return_exceptions=True,
        )
        quotes: list[ExchangeQuote] = []
        errors: dict[str, str] = {}
        for name, response in zip(self.endpoints, responses):
            if isinstance(response, BaseException):
                errors[name] = str(response)
            else:
                quotes.append(response)
        price, dispersion = robust_composite([quote.price for quote in quotes])
        return CompositeQuote(price=price, dispersion_pct=dispersion, quotes=quotes, errors=errors)

    async def _fetch_one(self, name: str, url: str) -> ExchangeQuote:
        started = time.perf_counter()
        response = await self.client.get(url, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        latency = (time.perf_counter() - started) * 1000
        payload = response.json()
        if name == "Coinbase":
            price = float(payload["price"])
            bid = float(payload["bid"])
            ask = float(payload["ask"])
            volume = float(payload["volume"])
        elif name == "Kraken":
            result = next(iter(payload["result"].values()))
            price = float(result["c"][0])
            bid = float(result["b"][0])
            ask = float(result["a"][0])
            volume = float(result["v"][1])
        else:
            price = float(payload["last"])
            bid = float(payload["bid"])
            ask = float(payload["ask"])
            volume = float(payload["volume"])
        return ExchangeQuote(name, price, bid, ask, volume, latency)

    async def coinbase_candles(
        self, start_iso: str, end_iso: str, granularity: int = 60
    ) -> list[list[float]]:
        response = await self.client.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles",
            params={"start": start_iso, "end": end_iso, "granularity": granularity},
        )
        response.raise_for_status()
        candles = response.json()
        return sorted(candles, key=lambda row: row[0])
