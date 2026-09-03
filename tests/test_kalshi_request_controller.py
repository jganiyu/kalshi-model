from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.engine import KALSHI_HTTP_LIMITS
from app.services.kalshi_trading import (
    AuthenticatedRequestController,
    KalshiTradingClient,
)


def test_priority_recovery_uses_reserved_lane_while_reconciliation_is_slow() -> None:
    async def scenario() -> None:
        controller = AuthenticatedRequestController()
        background_started = asyncio.Event()
        allow_background = asyncio.Event()
        recovery_done = asyncio.Event()

        async def background() -> dict:
            background_started.set()
            await allow_background.wait()
            return {}

        async def recovery() -> dict:
            recovery_done.set()
            return {}

        slow_scan = asyncio.create_task(
            controller.run(controller.BACKGROUND, background)
        )
        await background_started.wait()
        await controller.run(controller.RECOVERY, recovery)
        assert recovery_done.is_set()
        assert not slow_scan.done()
        allow_background.set()
        await slow_scan

    asyncio.run(scenario())


def test_queued_recovery_runs_before_an_older_background_request() -> None:
    async def scenario() -> None:
        controller = AuthenticatedRequestController(max_in_flight=1)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first() -> dict:
            first_started.set()
            await release_first.wait()
            order.append("first")
            return {}

        async def background() -> dict:
            order.append("background")
            return {}

        async def recovery() -> dict:
            order.append("recovery")
            return {}

        running = asyncio.create_task(controller.run(controller.BACKGROUND, first))
        await first_started.wait()
        queued_background = asyncio.create_task(
            controller.run(controller.BACKGROUND, background)
        )
        queued_recovery = asyncio.create_task(
            controller.run(controller.RECOVERY, recovery)
        )
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(running, queued_background, queued_recovery)
        assert order == ["first", "recovery", "background"]

    asyncio.run(scenario())


def test_cancelled_background_waiter_cannot_block_later_exit_work() -> None:
    async def scenario() -> None:
        controller = AuthenticatedRequestController(max_in_flight=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow() -> dict:
            started.set()
            await release.wait()
            return {}

        async def quick() -> dict:
            return {}

        running = asyncio.create_task(controller.run(controller.BACKGROUND, slow))
        await started.wait()
        canceled = asyncio.create_task(controller.run(controller.BACKGROUND, quick))
        await asyncio.sleep(0)
        canceled.cancel()
        try:
            await canceled
        except asyncio.CancelledError:
            pass
        exit_work = asyncio.create_task(controller.run(controller.EXECUTION, quick))
        release.set()
        await asyncio.gather(running, exit_work)

    asyncio.run(scenario())


def test_request_is_signed_only_after_it_leaves_the_queue(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        signed: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        def sign(*_: object) -> dict[str, str]:
            signed.append("signed")
            return {"KALSHI-ACCESS-TIMESTAMP": "fresh"}

        async def slow_background() -> None:
            started.set()
            await release.wait()

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        )
        client = KalshiTradingClient(
            http, "https://example.test/trade-api/v2", "key", tmp_path / "key.pem",
            environment="DEMO",
        )
        monkeypatch.setattr("app.services.kalshi_trading.signed_headers", sign)
        holding = asyncio.create_task(
            client._requests.run(client._requests.BACKGROUND, slow_background)
        )
        await started.wait()
        queued = asyncio.create_task(client._request("GET", "/portfolio/balance", retries=0))
        await asyncio.sleep(0)
        assert signed == []
        release.set()
        await asyncio.gather(holding, queued)
        await http.aclose()
        assert signed == ["signed"]

    asyncio.run(scenario())


def test_kalshi_rest_reuses_and_closes_its_warm_connection(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        accepted = 0
        connection_closed = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal accepted
            accepted += 1
            try:
                while True:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                        b"Content-Type: application/json\r\nConnection: keep-alive\r\n\r\n{}"
                    )
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                connection_closed.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            "app.services.kalshi_trading.signed_headers",
            lambda *_: {"KALSHI-ACCESS-TIMESTAMP": "fresh"},
        )
        http = httpx.AsyncClient(limits=KALSHI_HTTP_LIMITS)
        client = KalshiTradingClient(
            http, f"http://127.0.0.1:{port}/trade-api/v2", "key", tmp_path / "key.pem",
            environment="DEMO",
        )
        try:
            await client.balance()
            await client.balance()
            assert accepted == 1
        finally:
            await http.aclose()
            await asyncio.wait_for(connection_closed.wait(), timeout=1)
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
