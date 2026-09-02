from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


logger = logging.getLogger(__name__)

# Account reconciliation is background recovery work. It should tolerate a
# temporarily slow Kalshi response without changing the shorter execution
# deadline used for an order submission (where an unknown result must be
# handled as ambiguous promptly).
ACCOUNT_READ_TIMEOUT = httpx.Timeout(15.0, connect=5.0, write=8.0, pool=5.0)


@dataclass(frozen=True)
class _QueuedRequest:
    priority: int
    sequence: int


class AuthenticatedRequestController:
    """Small per-account traffic controller for authenticated Kalshi REST.

    It deliberately limits background account scans to one in flight while
    reserving a second connection for an order or targeted recovery.  This
    prevents a reconciliation burst from consuming every fresh HTTPS
    connection when the network is degraded.
    """

    EXECUTION = 0
    RECOVERY = 1
    BACKGROUND = 10

    def __init__(self, *, max_in_flight: int = 2) -> None:
        self._max_in_flight = max(1, max_in_flight)
        self._condition = asyncio.Condition()
        self._queued: list[_QueuedRequest] = []
        self._sequence = 0
        self._in_flight = 0
        self._background_in_flight = 0

    async def run(
        self,
        priority: int,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        async with self._condition:
            ticket = _QueuedRequest(priority, self._sequence)
            self._sequence += 1
            self._queued.append(ticket)
            try:
                while not self._may_start(ticket):
                    await self._condition.wait()
            except asyncio.CancelledError:
                # A canceled reconciliation page must not remain at the head
                # of the queue and block the next protective request.
                self._queued.remove(ticket)
                self._condition.notify_all()
                raise
            self._queued.remove(ticket)
            self._in_flight += 1
            if priority >= self.BACKGROUND:
                self._background_in_flight += 1
        try:
            return await operation()
        finally:
            async with self._condition:
                self._in_flight -= 1
                if priority >= self.BACKGROUND:
                    self._background_in_flight -= 1
                self._condition.notify_all()

    def _may_start(self, ticket: _QueuedRequest) -> bool:
        # Strict priority among waiting requests. A low-priority reconciliation
        # page cannot begin while an exit/order recovery is waiting.
        if ticket != min(self._queued, key=lambda item: (item.priority, item.sequence)):
            return False
        if self._in_flight >= self._max_in_flight:
            return False
        # Keep one connection available for an execution or targeted recovery.
        return ticket.priority < self.BACKGROUND or self._background_in_flight == 0


class KalshiTradingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: object | None = None,
        transport: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details
        # A response from Kalshi (including a 4xx/5xx response) proves the
        # transport is up.  Keep this separate from an actual DNS/TLS/connect/
        # read failure so reconciliation does not falsely show "Reconnecting".
        self.transport = transport


class AmbiguousSubmissionError(KalshiTradingError):
    """The request may have reached Kalshi and must be reconciled before retrying."""


def normalize_order_price(
    outcome_price: float,
    action: str,
    *,
    side: str = "YES",
    price_ranges: list[dict[str, Any]] | None = None,
) -> float:
    """Return a Kalshi-valid limit using the market's variable tick schedule."""
    verb = str(action).upper()
    outcome = str(side).upper()
    if verb not in {"BUY", "SELL"} or outcome not in {"YES", "NO"}:
        raise ValueError("Order side or action is invalid.")
    price = Decimal(str(outcome_price))
    if not price.is_finite() or not Decimal("0") < price < Decimal("1"):
        raise ValueError("Limit price must be between 0 and 1 dollar.")
    book_side = "bid" if (outcome == "YES") == (verb == "BUY") else "ask"
    book_price = price if outcome == "YES" else Decimal("1") - price
    ranges: list[tuple[Decimal, Decimal, Decimal]] = []
    for raw in price_ranges or []:
        try:
            start = Decimal(str(raw.get("start")))
            end = Decimal(str(raw.get("end")))
            step = Decimal(str(raw.get("step")))
        except Exception:
            continue
        if start.is_finite() and end.is_finite() and step > 0 and start < end:
            ranges.append((start, end, step))
    ranges.sort(key=lambda item: item[0])
    if not ranges:
        ranges = [(Decimal("0"), Decimal("1"), Decimal("0.01"))]
    selected = ranges[-1]
    for index, candidate in enumerate(ranges):
        start, end, _ = candidate
        if start <= book_price < end or (
            index == len(ranges) - 1 and book_price == end
        ):
            selected = candidate
            break
    start, _, step = selected
    steps = ((book_price - start) / step).to_integral_value(
        rounding=ROUND_CEILING if book_side == "bid" else ROUND_FLOOR
    )
    rounded_book = start + steps * step
    minimum = ranges[0][0] + ranges[0][2]
    maximum = ranges[-1][1] - ranges[-1][2]
    rounded_book = min(maximum, max(minimum, rounded_book))
    rounded_outcome = (
        rounded_book if outcome == "YES" else Decimal("1") - rounded_book
    )
    return float(rounded_outcome)


def signed_headers(
    key_id: str,
    private_key_path: Path,
    method: str,
    path: str,
    *,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Kalshi private key must be an RSA key.")
    clean_path = path.split("?", 1)[0]
    signature = key.sign(
        f"{timestamp}{method.upper()}{clean_path}".encode("utf-8"),
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


def outcome_to_book(side: str, action: str, outcome_price: float) -> tuple[str, float]:
    """Map Up/Down semantics onto the V2 single YES book."""
    outcome = str(side).upper()
    verb = str(action).upper()
    if outcome not in {"YES", "NO"} or verb not in {"BUY", "SELL"}:
        raise ValueError("Order side and action are invalid.")
    if not 0 < float(outcome_price) < 1:
        raise ValueError("Limit price must be between 0 and 1 dollar.")
    book_side = "bid" if (outcome == "YES") == (verb == "BUY") else "ask"
    book_price = float(outcome_price) if outcome == "YES" else 1.0 - float(outcome_price)
    return book_side, round(book_price, 4)


def book_to_outcome(book_side: str, book_price: float, *, reduce_only: bool = False) -> tuple[str, str, float]:
    side = str(book_side).lower()
    if side not in {"bid", "ask"}:
        raise ValueError("Unknown Kalshi book side.")
    if side == "bid":
        outcome, action = ("NO", "SELL") if reduce_only else ("YES", "BUY")
    else:
        outcome, action = ("YES", "SELL") if reduce_only else ("NO", "BUY")
    price = float(book_price) if outcome == "YES" else 1.0 - float(book_price)
    return outcome, action, round(price, 4)


class KalshiTradingClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        key_id: str,
        private_key_path: Path,
        *,
        environment: str,
    ):
        normalized = str(environment).upper()
        if normalized not in {"DEMO", "LIVE"}:
            raise ValueError("Trading environment must be Demo or Live.")
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.environment = normalized
        parsed = urlparse(self.base_url)
        self.api_prefix = parsed.path.rstrip("/")
        # Each client is scoped to exactly one Demo or Live account.  Do not
        # share its queue across environments.
        self._requests = AuthenticatedRequestController()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 2,
        submission: bool = False,
        priority: int | None = None,
    ) -> dict[str, Any]:
        signing_path = f"{self.api_prefix}{path}"
        started_at = time.monotonic()
        request_priority = (
            self._requests.EXECUTION
            if submission
            else self._requests.BACKGROUND if priority is None else priority
        )
        for attempt in range(retries + 1):
            try:
                request_args: dict[str, Any] = {
                    "params": params,
                    "json": json,
                }
                if not submission:
                    request_args["timeout"] = ACCOUNT_READ_TIMEOUT

                async def send() -> httpx.Response:
                    # Kalshi rejects stale signatures. Sign only after this
                    # request has won a controller slot, never while it is
                    # still waiting behind recovery/background work.
                    request_args["headers"] = signed_headers(
                        self.key_id, self.private_key_path, method, signing_path
                    )
                    return await self.client.request(
                        method,
                        f"{self.base_url}{path}",
                        **request_args,
                    )

                response = await self._requests.run(request_priority, send)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                failure_kind = (
                    "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                )
                request_detail = {
                    "failure_kind": failure_kind,
                    "transport_error_type": type(exc).__name__,
                    "method": method,
                    "path": path,
                    "attempts": attempt + 1,
                    "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                }
                if submission:
                    raise AmbiguousSubmissionError(
                        "Order submission timed out; checking its exchange state before retrying.",
                        details=request_detail,
                        transport=True,
                    ) from exc
                if attempt >= retries:
                    raise KalshiTradingError(
                        f"Kalshi {failure_kind} while reading {path}.",
                        details=request_detail,
                        transport=True,
                    ) from exc
                base_delay = min(4.0, 0.5 * (2**attempt))
                await asyncio.sleep(base_delay + random.uniform(0, base_delay * 0.2))
                continue
            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(10.0, max(0.25, float(retry_after or 0)))
                except ValueError:
                    delay = min(4.0, 0.5 * (2**attempt))
                await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                continue
            if response.is_success:
                return response.json() if response.content else {}
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error_payload = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error_payload, dict):
                error_payload = payload
            code = str(error_payload.get("code") or "") or None
            remote_message = str(error_payload.get("message") or "")
            details = error_payload.get("details")
            if submission and response.status_code >= 500:
                raise AmbiguousSubmissionError(
                    "Kalshi returned an uncertain submission result; reconciliation is required."
                )
            if response.status_code == 401:
                auth_detail = f"{code or ''} {remote_message}".lower()
                message = (
                    "The request clock is out of sync with Kalshi."
                    if any(word in auth_detail for word in ("timestamp", "clock", "expired"))
                    else "Kalshi rejected these API credentials."
                )
            elif response.status_code == 403:
                message = "This Kalshi API key does not have permission for that action."
            elif response.status_code == 429:
                message = "Kalshi rate-limited the request."
            elif response.status_code >= 500:
                message = "Kalshi is temporarily unavailable."
            elif code == "user_not_found":
                message = (
                    "This account has no funds allocated to the market's Kalshi "
                    "exchange shard."
                )
            elif code == "market_not_found":
                message = "Kalshi could not route this order to the requested market."
            else:
                message = remote_message or "Kalshi rejected the request."
            request_detail = {
                "method": method,
                "path": path,
                "attempts": attempt + 1,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                "response": details,
            }
            raise KalshiTradingError(
                message,
                status_code=response.status_code,
                code=code,
                details=request_detail,
            )
        raise KalshiTradingError("Kalshi request failed.")

    async def balance(self) -> dict[str, Any]:
        return await self._request("GET", "/portfolio/balance")

    async def _all_pages(
        self,
        path: str,
        item_key: str,
        *,
        params: dict[str, Any] | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        query = {"limit": 1000, **(params or {})}
        items: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            payload = await self._request("GET", path, params=query, priority=priority)
            items.extend(payload.get(item_key) or [])
            cursor = str(payload.get("cursor") or "")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            query["cursor"] = cursor
        return {item_key: items, "cursor": ""}

    async def positions(
        self, *, ticker: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return await self._all_pages(
            "/portfolio/positions", "market_positions", params=params,
            priority=self._requests.RECOVERY if ticker else None,
        )

    async def orders(
        self,
        *,
        status: str | None = None,
        ticker: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return await self._all_pages(
            "/portfolio/orders", "orders", params=params,
            priority=self._requests.RECOVERY if (ticker or status) else None,
        )

    async def fills(self, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._all_pages("/portfolio/fills", "fills", params=params)

    async def settlements(self, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._all_pages(
            "/portfolio/settlements", "settlements", params=params
        )

    async def market(self, ticker: str) -> dict[str, Any]:
        """Return the authoritative market state used to resolve timed-out orders."""
        payload = await self._request("GET", f"/markets/{quote(ticker, safe='')}")
        market = payload.get("market")
        return market if isinstance(market, dict) else payload

    async def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        payload = await self._all_pages(
            "/portfolio/orders", "orders", priority=self._requests.RECOVERY
        )
        for order in payload.get("orders", []):
            if str(order.get("client_order_id") or "") == client_order_id:
                return order
        return None

    async def order(self, order_id: str) -> dict[str, Any] | None:
        """Read one order without paginating the account order history."""
        payload = await self._request(
            "GET", f"/portfolio/orders/{quote(order_id, safe='')}",
            priority=self._requests.RECOVERY,
        )
        order = payload.get("order")
        return order if isinstance(order, dict) else payload if isinstance(payload, dict) else None

    async def create_order(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,
        action: str,
        contracts: int,
        limit_price: float,
        reduce_only: bool = False,
        post_only: bool = False,
        live_authorized: bool = False,
        price_ranges: list[dict[str, Any]] | None = None,
        exchange_index: int | None = None,
        time_in_force: str = "good_till_canceled",
        cancel_order_on_pause: bool = True,
    ) -> dict[str, Any]:
        if self.environment == "LIVE" and not live_authorized:
            raise KalshiTradingError("Live order submission is not armed.")
        if self.environment == "LIVE" and os.getenv("PYTEST_CURRENT_TEST"):
            raise KalshiTradingError("Production orders are disabled during tests.")
        if int(contracts) < 1:
            raise ValueError("Contracts must be a positive whole number.")
        normalized_price = normalize_order_price(
            limit_price, action, side=side, price_ranges=price_ranges
        )
        book_side, book_price = outcome_to_book(side, action, normalized_price)
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{int(contracts)}.00",
            "price": f"{book_price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "cancel_order_on_pause": bool(cancel_order_on_pause),
            "reduce_only": bool(reduce_only),
            "subaccount": 0,
        }
        # -1 tells the V2 API to route from the ticker. This is safer than
        # silently defaulting to shard 0 when an active market lives elsewhere.
        payload["exchange_index"] = (
            int(exchange_index) if exchange_index is not None else -1
        )
        return await self._request(
            "POST",
            "/portfolio/events/orders",
            json=payload,
            retries=2,
            submission=True,
        )

    async def cancel_order(
        self,
        exchange_order_id: str,
        *,
        market_ticker: str | None = None,
    ) -> dict[str, Any]:
        params = {"market_ticker": market_ticker} if market_ticker else None
        return await self._request(
            "DELETE",
            f"/portfolio/events/orders/{exchange_order_id}",
            params=params,
            priority=self._requests.EXECUTION,
        )
