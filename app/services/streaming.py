from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.asyncio.client import connect

from app.services.kalshi import orderbook_metrics
from app.services.market_data import ExchangeQuote


logger = logging.getLogger(__name__)
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())

QuoteHandler = Callable[[ExchangeQuote], Awaitable[None]]
StatusHandler = Callable[[str, bool, str | None], Awaitable[None]]
KalshiHandler = Callable[[dict[str, Any], dict[str, Any] | None], Awaitable[None]]
PrivateKalshiHandler = Callable[[dict[str, Any]], Awaitable[None]]


def kalshi_websocket_headers(
    key_id: str,
    private_key_path: Path,
    *,
    timestamp_ms: int | None = None,
    request_path: str = "/trade-api/ws/v2",
) -> dict[str, str]:
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("Kalshi private key must be an RSA key")
    message = f"{timestamp}GET{request_path.split('?', 1)[0]}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


class KalshiOrderBook:
    def __init__(self) -> None:
        self.ticker: str | None = None
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}

    def apply(
        self, message: dict[str, Any], *, calculate_metrics: bool = True
    ) -> dict[str, Any] | None:
        message_type = message.get("type")
        payload = message.get("msg") or {}
        if message_type == "orderbook_snapshot":
            self.ticker = payload.get("market_ticker")
            self.yes = self._levels(payload.get("yes_dollars_fp") or payload.get("yes") or [])
            self.no = self._levels(payload.get("no_dollars_fp") or payload.get("no") or [])
        elif message_type == "orderbook_delta":
            if self.ticker and payload.get("market_ticker") != self.ticker:
                return None
            side = payload.get("side")
            price = self._number(payload.get("price_dollars") or payload.get("price"))
            delta = self._number(payload.get("delta_fp") or payload.get("delta"))
            if side not in {"yes", "no"} or price is None or delta is None:
                return None
            levels = self.yes if side == "yes" else self.no
            quantity = round(levels.get(price, 0.0) + delta, 8)
            if quantity <= 1e-8:
                levels.pop(price, None)
            else:
                levels[price] = quantity
        else:
            return None
        return self.metrics() if calculate_metrics else {}

    def metrics(self) -> dict[str, Any]:
        return orderbook_metrics(
            {
                "orderbook_fp": {
                    "yes_dollars": [[price, quantity] for price, quantity in self.yes.items()],
                    "no_dollars": [[price, quantity] for price, quantity in self.no.items()],
                }
            }
        )

    @classmethod
    def _levels(cls, rows: list[list[Any]]) -> dict[float, float]:
        levels: dict[float, float] = {}
        for row in rows:
            if len(row) < 2:
                continue
            price = cls._number(row[0])
            quantity = cls._number(row[1])
            if price is not None and quantity is not None and quantity > 0:
                levels[price] = quantity
        return levels

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class BitcoinWebSocketFeeds:
    coinbase_url = "wss://ws-feed.exchange.coinbase.com"
    kraken_url = "wss://ws.kraken.com/v2"

    def __init__(self, on_quote: QuoteHandler, on_status: StatusHandler):
        self.on_quote = on_quote
        self.on_status = on_status

    async def run_coinbase(self) -> None:
        await self._reconnecting("Coinbase", self._coinbase_connection)

    async def run_kraken(self) -> None:
        await self._reconnecting("Kraken", self._kraken_connection)

    async def _reconnecting(
        self, source: str, connection: Callable[[], Awaitable[None]]
    ) -> None:
        delay = 1.0
        while True:
            try:
                await connection()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s stream disconnected: %s", source, exc)
                await self.on_status(source, False, str(exc))
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _coinbase_connection(self) -> None:
        async with connect(
            self.coinbase_url,
            ssl=TLS_CONTEXT,
            ping_interval=20,
            ping_timeout=20,
            max_queue=256,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker", "heartbeat"],
                    }
                )
            )
            await self.on_status("Coinbase", True, None)
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("type") != "ticker" or message.get("product_id") != "BTC-USD":
                    continue
                await self.on_quote(
                    ExchangeQuote(
                        exchange="Coinbase",
                        price=float(message["price"]),
                        bid=self._optional_float(message.get("best_bid")),
                        ask=self._optional_float(message.get("best_ask")),
                        volume=self._optional_float(message.get("volume_24h")),
                        latency_ms=0.0,
                    )
                )

    async def _kraken_connection(self) -> None:
        async with connect(
            self.kraken_url,
            ssl=TLS_CONTEXT,
            ping_interval=20,
            ping_timeout=20,
            max_queue=256,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": ["BTC/USD"],
                            "snapshot": True,
                        },
                        "req_id": 1,
                    }
                )
            )
            await self.on_status("Kraken", True, None)
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("channel") != "ticker" or not message.get("data"):
                    continue
                payload = message["data"][0]
                if payload.get("symbol") != "BTC/USD":
                    continue
                await self.on_quote(
                    ExchangeQuote(
                        exchange="Kraken",
                        price=float(payload["last"]),
                        bid=self._optional_float(payload.get("bid")),
                        ask=self._optional_float(payload.get("ask")),
                        volume=self._optional_float(payload.get("volume")),
                        latency_ms=0.0,
                    )
                )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class KalshiWebSocketFeed:
    def __init__(
        self,
        websocket_url: str,
        key_id: str,
        private_key_path: Path,
        ticker: Callable[[], str | None],
        on_message: KalshiHandler,
        on_status: StatusHandler,
    ):
        self.websocket_url = websocket_url
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.ticker = ticker
        self.on_message = on_message
        self.on_status = on_status

    async def run(self) -> None:
        delay = 1.0
        while True:
            ticker = self.ticker()
            if not ticker:
                await asyncio.sleep(0.5)
                continue
            try:
                await self._connection(ticker)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Kalshi stream disconnected: %s", exc)
                await self.on_status("Kalshi", False, str(exc))
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _connection(self, ticker: str) -> None:
        headers = kalshi_websocket_headers(self.key_id, self.private_key_path)
        async with connect(
            self.websocket_url,
            ssl=TLS_CONTEXT,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_queue=2048,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker", "orderbook_delta"],
                            "market_tickers": [ticker],
                        },
                    }
                )
            )
            await websocket.send(
                json.dumps(
                    {
                        "id": 2,
                        "cmd": "subscribe",
                        "params": {"channels": ["market_lifecycle_v2"]},
                    }
                )
            )
            await self.on_status("Kalshi", True, None)
            book = KalshiOrderBook()
            last_sequence: int | None = None
            last_book_emit = 0.0
            while self.ticker() == ticker:
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                except TimeoutError:
                    continue
                message = json.loads(raw)
                message_type = message.get("type")
                if message_type in {"orderbook_snapshot", "orderbook_delta"}:
                    sequence = message.get("seq")
                    if (
                        message_type == "orderbook_delta"
                        and last_sequence is not None
                        and sequence != last_sequence + 1
                    ):
                        raise RuntimeError("Kalshi order-book sequence gap")
                    if isinstance(sequence, int):
                        last_sequence = sequence
                    book.apply(message, calculate_metrics=False)
                    now = time.monotonic()
                    if message_type == "orderbook_snapshot" or now - last_book_emit >= 0.1:
                        await self.on_message(message, book.metrics())
                        last_book_emit = now
                elif message_type == "ticker":
                    await self.on_message(message, None)
                elif message_type in {"market_lifecycle", "market_lifecycle_v2"}:
                    await self.on_message(message, None)


class KalshiPrivateWebSocketFeed:
    """Private order/fill updates; REST reconciliation remains authoritative."""

    def __init__(
        self,
        websocket_url: str,
        key_id: str,
        private_key_path: Path,
        environment: str,
        on_message: PrivateKalshiHandler,
        on_status: StatusHandler,
    ):
        self.websocket_url = websocket_url
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.environment = str(environment).upper()
        self.on_message = on_message
        self.on_status = on_status

    @property
    def source(self) -> str:
        return f"Kalshi {self.environment.title()} Trading"

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connection()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s stream disconnected: %s", self.source, exc)
                await self.on_status(self.source, False, str(exc))
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _connection(self) -> None:
        headers = kalshi_websocket_headers(self.key_id, self.private_key_path)
        async with connect(
            self.websocket_url,
            ssl=TLS_CONTEXT,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_queue=2048,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["user_orders", "fill", "market_positions"]
                        },
                    }
                )
            )
            await self.on_status(self.source, True, None)
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("type") in {"user_order", "fill", "market_position"}:
                    await self.on_message(message)
