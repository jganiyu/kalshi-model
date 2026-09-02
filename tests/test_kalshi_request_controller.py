from __future__ import annotations

import asyncio

from app.services.kalshi_trading import AuthenticatedRequestController


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
