from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.services.kalshi_trading import (
    AmbiguousSubmissionError,
    AuthenticatedRequestController,
    KalshiTradingClient,
    KalshiTradingError,
    RequestBudget,
)


def make_client(http: httpx.AsyncClient, monkeypatch) -> KalshiTradingClient:
    monkeypatch.setattr(
        "app.services.kalshi_trading.signed_headers", lambda *_: {"test": "unsigned"}
    )
    return KalshiTradingClient(
        http, "http://example.test/trade-api/v2", "unused", Path("unused.pem"),
        environment="DEMO",
    )


@pytest.mark.parametrize("slots", [2, 3])
def test_recovery_overload_leaves_execution_capacity(slots: int) -> None:
    async def scenario() -> None:
        controller = AuthenticatedRequestController(max_in_flight=slots)
        release = asyncio.Event()
        started = [asyncio.Event() for _ in range(slots - 1)]

        async def hold(event: asyncio.Event) -> None:
            event.set()
            await release.wait()

        holders = [asyncio.create_task(controller.run(
            controller.RECOVERY, lambda event=event: hold(event)
        )) for event in started]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
        blocked = asyncio.create_task(controller.run(
            controller.RECOVERY, lambda: asyncio.sleep(0), admission_timeout=0.03
        ))
        try:
            # The reserved slot must remain available even when recovery is queued.
            await asyncio.wait_for(controller.run(
                controller.EXECUTION, lambda: asyncio.sleep(0)
            ), 0.1)
            with pytest.raises(TimeoutError):
                await blocked
            assert controller._queued == []
        finally:
            release.set()
            await asyncio.gather(*holders)
        assert controller._in_flight == controller._nonexecution_in_flight == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("budget, code", [
    (RequestBudget(admission=0.02, total=0.1), "admission_timeout"),
    (RequestBudget(admission=0.1, total=0.02), "deadline_exceeded"),
])
def test_submission_queue_expiry_is_known_unsent(monkeypatch, budget, code) -> None:
    async def scenario() -> None:
        dispatched: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: dispatched.append(request) or httpx.Response(200, json={})
        )) as http:
            client = make_client(http, monkeypatch)
            client._requests = AuthenticatedRequestController(max_in_flight=1)
            release, started = asyncio.Event(), asyncio.Event()

            async def hold() -> None:
                started.set()
                await release.wait()

            holding = asyncio.create_task(client._requests.run(0, hold))
            await started.wait()
            try:
                with pytest.raises(KalshiTradingError) as error:
                    await client._request(
                        "POST", "/portfolio/events/orders", submission=True,
                        budget=budget,
                    )
                assert not isinstance(error.value, AmbiguousSubmissionError)
                assert error.value.code == code
                assert error.value.transport is False
                assert error.value.details["wire_elapsed_ms"] is None
                assert dispatched == []
                assert client._requests._queued == []
            finally:
                release.set()
                await holding

    asyncio.run(scenario())


def test_real_stalled_submission_has_total_deadline_and_remains_ambiguous(monkeypatch) -> None:
    async def scenario() -> None:
        accepted = 0
        disconnected = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal accepted
            accepted += 1
            try:
                await reader.readuntil(b"\r\n\r\n")
                # Send no response: enforce a deadline even with httpx timeouts disabled.
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(timeout=None, trust_env=False) as http:
            client = make_client(http, monkeypatch)
            client.base_url = f"http://127.0.0.1:{port}/trade-api/v2"
            started = asyncio.get_running_loop().time()
            try:
                with pytest.raises(AmbiguousSubmissionError) as error:
                    await client._request(
                        "POST", "/portfolio/events/orders", submission=True,
                        budget=RequestBudget(admission=0.05, total=0.1),
                    )
                assert error.value.details["failure_kind"] == "deadline_exceeded"
                assert error.value.details["wire_elapsed_ms"] is not None
                assert error.value.transport is True
                assert asyncio.get_running_loop().time() - started < 0.5
                assert accepted == 1  # Never resubmit after an uncertain wire result.
                assert client._requests._in_flight == 0
                await asyncio.wait_for(disconnected.wait(), 1)
            finally:
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())


def test_real_disconnect_after_submission_remains_ambiguous(monkeypatch) -> None:
    async def scenario() -> None:
        accepted = 0
        disconnected = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal accepted
            try:
                await reader.readuntil(b"\r\n\r\n")
                accepted += 1
                # The exchange may have accepted the order before losing its response.
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                client = make_client(http, monkeypatch)
                client.base_url = f"http://127.0.0.1:{port}/trade-api/v2"
                with pytest.raises(AmbiguousSubmissionError) as error:
                    await client._request(
                        "POST", "/portfolio/events/orders", submission=True,
                    )
                assert error.value.details["transport_error_type"] == "RemoteProtocolError"
                assert error.value.transport is True
                assert accepted == 1
                assert client._requests._in_flight == 0
                await asyncio.wait_for(disconnected.wait(), 1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_malformed_successful_submission_is_ambiguous_without_retry(monkeypatch) -> None:
    async def scenario() -> None:
        calls = 0

        def malformed(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(201, content=b'{"order":')

        async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as http:
            client = make_client(http, monkeypatch)
            with pytest.raises(AmbiguousSubmissionError, match="unreadable"):
                await client._request(
                    "POST", "/portfolio/events/orders", submission=True,
                )
            assert calls == 1
            assert client._requests._in_flight == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [201, 503])
def test_list_submission_response_is_ambiguous_without_retry(monkeypatch, status) -> None:
    async def scenario() -> None:
        calls = 0

        def unexpected(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
            client = make_client(http, monkeypatch)
            with pytest.raises(AmbiguousSubmissionError):
                await client._request(
                    "POST", "/portfolio/events/orders", submission=True,
                )
            assert calls == 1
            assert client._requests._in_flight == 0

    asyncio.run(scenario())


def test_retry_after_consumes_total_budget_without_resubmission(monkeypatch) -> None:
    async def scenario() -> None:
        calls = 0

        def limited(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "10"}, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as http:
            client = make_client(http, monkeypatch)
            started = asyncio.get_running_loop().time()
            with pytest.raises(KalshiTradingError) as error:
                await client._request(
                    "POST", "/portfolio/events/orders", submission=True,
                    budget=RequestBudget(admission=0.02, total=0.04),
                )
            assert not isinstance(error.value, AmbiguousSubmissionError)
            assert error.value.code == "deadline_exceeded"
            assert calls == 1
            assert asyncio.get_running_loop().time() - started < 0.3

    asyncio.run(scenario())


def test_retry_diagnostics_measure_only_current_attempt_queue(monkeypatch) -> None:
    async def scenario() -> None:
        calls = 0

        async def timeout(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            raise httpx.ReadTimeout("stalled response")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            client = make_client(http, monkeypatch)
            with pytest.raises(KalshiTradingError) as error:
                await client._request("GET", "/portfolio/balance", retries=1)
            detail = error.value.details
            assert calls == 2
            assert detail["elapsed_ms"] >= 500
            assert detail["queue_wait_ms"] < 100
            assert 10 <= detail["wire_elapsed_ms"] < 200

    asyncio.run(scenario())


def test_cancellation_releases_running_and_queued_capacity() -> None:
    async def scenario() -> None:
        controller = AuthenticatedRequestController()
        started = asyncio.Event()

        async def hold() -> None:
            started.set()
            await asyncio.Event().wait()

        running = asyncio.create_task(controller.run(controller.BACKGROUND, hold))
        await started.wait()
        queued = asyncio.create_task(controller.run(controller.BACKGROUND, hold))
        await asyncio.sleep(0)
        running.cancel()
        queued.cancel()
        await asyncio.gather(running, queued, return_exceptions=True)
        assert controller._queued == []
        assert controller._in_flight == controller._background_in_flight == 0
        assert controller._nonexecution_in_flight == 0
        await asyncio.wait_for(controller.run(
            controller.BACKGROUND, lambda: asyncio.sleep(0)
        ), 0.1)

    asyncio.run(scenario())


def test_resting_account_scan_uses_background_lane(monkeypatch) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"orders": []})
        )) as http:
            client = make_client(http, monkeypatch)
            priorities: list[int] = []
            original = client._requests.run

            async def capture(priority, operation, **kwargs):
                priorities.append(priority)
                return await original(priority, operation, **kwargs)

            monkeypatch.setattr(client._requests, "run", capture)
            await client.orders(status="resting")
            await client.orders(status="resting", ticker="TEST")
            await client.order_by_client_id("missing")
            assert priorities == [
                client._requests.BACKGROUND,
                client._requests.RECOVERY,
                client._requests.BACKGROUND,
            ]

    asyncio.run(scenario())
