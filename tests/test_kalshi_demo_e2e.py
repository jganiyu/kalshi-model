from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx
import pytest

from app.services.kalshi_trading import KalshiTradingClient


pytestmark = pytest.mark.skipif(
    os.getenv("KALSHI_DEMO_E2E") != "1",
    reason="Opt in with KALSHI_DEMO_E2E=1 and an explicit Demo test ticker.",
)


def test_opt_in_demo_create_ack_cancel_and_reconcile() -> None:
    key_id = os.getenv("KALSHI_DEMO_API_KEY_ID")
    key_path = os.getenv("KALSHI_DEMO_PRIVATE_KEY_PATH")
    ticker = os.getenv("KALSHI_DEMO_TEST_TICKER")
    assert key_id and key_path and ticker

    async def scenario() -> None:
        async with httpx.AsyncClient(timeout=15) as http:
            client = KalshiTradingClient(
                http,
                "https://external-api.demo.kalshi.co/trade-api/v2",
                key_id,
                Path(key_path),
                environment="DEMO",
            )
            await asyncio.gather(
                client.balance(), client.positions(), client.orders(), client.fills()
            )
            client_order_id = f"kalshi-model-e2e-{uuid.uuid4()}"
            created = await client.create_order(
                ticker=ticker,
                client_order_id=client_order_id,
                side="YES",
                action="BUY",
                contracts=1,
                limit_price=.01,
                post_only=True,
            )
            assert created["order_id"]
            await client.cancel_order(str(created["order_id"]))
            reconciled = await client.order_by_client_id(client_order_id)
            assert reconciled and reconciled["status"] == "canceled"

    asyncio.run(scenario())
