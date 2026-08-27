from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


logger = logging.getLogger(__name__)


class KalshiTradingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class AmbiguousSubmissionError(KalshiTradingError):
    """The request may have reached Kalshi and must be reconciled before retrying."""


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
    if not 0.01 <= float(outcome_price) <= 0.99:
        raise ValueError("Limit price must be between 1 and 99 cents.")
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 2,
        submission: bool = False,
    ) -> dict[str, Any]:
        signing_path = f"{self.api_prefix}{path}"
        for attempt in range(retries + 1):
            headers = signed_headers(
                self.key_id, self.private_key_path, method, signing_path
            )
            try:
                response = await self.client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if submission:
                    raise AmbiguousSubmissionError(
                        "Order submission timed out; reconciliation is required before retrying."
                    ) from exc
                if attempt >= retries:
                    raise KalshiTradingError("Kalshi is unreachable.") from exc
                await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                continue
            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(10.0, max(0.25, float(retry_after or 0)))
                except ValueError:
                    delay = min(4.0, 0.5 * (2**attempt))
                await asyncio.sleep(delay)
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
            raise KalshiTradingError(
                message, status_code=response.status_code, code=code
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
    ) -> dict[str, Any]:
        query = {"limit": 1000, **(params or {})}
        items: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            payload = await self._request("GET", path, params=query)
            items.extend(payload.get(item_key) or [])
            cursor = str(payload.get("cursor") or "")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            query["cursor"] = cursor
        return {item_key: items, "cursor": ""}

    async def positions(self, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._all_pages(
            "/portfolio/positions", "market_positions", params=params
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
        return await self._all_pages("/portfolio/orders", "orders", params=params)

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

    async def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        payload = await self.orders()
        for order in payload.get("orders", []):
            if str(order.get("client_order_id") or "") == client_order_id:
                return order
        return None

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
    ) -> dict[str, Any]:
        if self.environment == "LIVE" and not live_authorized:
            raise KalshiTradingError("Live order submission is not armed.")
        if self.environment == "LIVE" and os.getenv("PYTEST_CURRENT_TEST"):
            raise KalshiTradingError("Production orders are disabled during tests.")
        if int(contracts) < 1:
            raise ValueError("Contracts must be a positive whole number.")
        book_side, book_price = outcome_to_book(side, action, limit_price)
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{int(contracts)}.00",
            "price": f"{book_price:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
            "reduce_only": bool(reduce_only),
            "subaccount": 0,
        }
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
        )
