from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import AppConfig
from app.db import Database
from app.domain import iso_now, texas_holdem_exit_reason, texas_holdem_phase
from app.engine import AnalysisEngine
from app.services.paper import PaperTradingService
from app.services.trading import TradingCoordinator, protective_exit_reason


def coordinator(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.update_settings({"texas_holdem_flop_stop": 0, "texas_holdem_turn_stop": 0,
                        "texas_holdem_river_stop": 0, "texas_holdem_river_target": .50})
    trading = TradingCoordinator(AppConfig(database_path=db.path), db, PaperTradingService(db))
    for mode in ("LIVE", "DEMO"):
        trading.brokers[mode].session_armed = True
    return trading


def position(trading, mode="LIVE", ticker="HELD"):
    trading.db.execute(
        "INSERT INTO broker_positions(mode,ticker,side,contracts,strategy,updated_at,status) "
        "VALUES (?,?, 'YES',8,'TEXAS_HOLDEM',?,'open')", (mode, ticker, iso_now()),
    )


def frame(ticker="HELD", mode="LIVE", bid=.52):
    return {"ticker": ticker, "execution_market_mode": mode, "status": "active",
            "close_time": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            "observed_at": iso_now(), "executable_quote_at": iso_now(),
            "yes_bid": bid, "no_bid": .47, "data_quality": {"reliable": False}}


def engine_for(trading):
    engine = AnalysisEngine.__new__(AnalysisEngine)
    engine.trading = trading
    engine.dashboard = {"current": None}
    engine._latest_btc = None
    engine._current_market = frame()
    engine._market_state = None
    engine._update_lock = asyncio.Lock()
    engine._last_kalshi_persist = time.monotonic()
    engine._last_kalshi_ws_book = 0.0
    engine._book_persist_task = None
    engine._schedule_live_refresh = lambda: None
    engine._schedule_publish = lambda: None
    return engine


async def drain(trading):
    await asyncio.gather(*list(trading._protective_exit_tasks.values()))
    await asyncio.gather(*list(trading._submission_tasks))


def test_private_event_database_wait_does_not_block_public_protection_loop(tmp_path, monkeypatch):
    async def scenario():
        trading = coordinator(tmp_path)
        broker = trading.brokers["LIVE"]
        broker.client = SimpleNamespace(key_id="test")
        entered, release = threading.Event(), threading.Event()
        callbacks = []
        monkeypatch.setattr("app.services.trading.resolve_trading_credentials",
                            lambda mode: ("test", tmp_path / "unused.pem", "test"))

        class Feed:
            def __init__(self, url, key, path, mode, on_message, on_status):
                callbacks.append(on_message)

            async def run(self):
                await asyncio.Event().wait()

        monkeypatch.setattr("app.services.trading.KalshiPrivateWebSocketFeed", Feed)

        def adopt(message):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(3), "Private persistence blocked the event loop"
            return {"order_id": None, "ticker": None}

        monkeypatch.setattr(broker, "adopt_private_event", adopt)
        monkeypatch.setattr(trading, "_schedule_private_event_recovery", lambda *a, **kw: None)
        loop_thread = threading.get_ident()
        trading._start_private_stream("LIVE")
        adoption = asyncio.create_task(callbacks[0]({"type": "fill"}))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            assert not adoption.done()
        finally:
            release.set()
            await adoption
            feed = trading._private_streams["LIVE"]
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    asyncio.run(scenario())


def test_websocket_exit_bypasses_ui_lock_btc_and_stalled_account(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        engine = engine_for(trading)
        await engine._update_lock.acquire()
        submitted = []

        async def submit(intent, key):
            submitted.append(intent)
            trading._pending_exit_keys.discard(key)

        trading._submit_exit = submit
        account_wait = asyncio.create_task(asyncio.Event().wait())
        try:
            await asyncio.wait_for(engine._handle_kalshi_message(
                {"type": "orderbook_delta", "msg": {"market_ticker": "HELD"}},
                {"yes_bids": [[.52, 8]], "no_bids": [[.47, 8]]},
            ), timeout=.2)
            await asyncio.wait_for(drain(trading), timeout=2)
            assert len(submitted) == 1
            assert submitted[0].source == "texas_river_target"
            assert submitted[0].contracts == 8
            assert submitted[0].time_in_force == "immediate_or_cancel"
            assert submitted[0].decision_snapshot["protective_exit"] is True
            assert not account_wait.done()
        finally:
            account_wait.cancel()
            await asyncio.gather(account_wait, return_exceptions=True)
            engine._update_lock.release()
    asyncio.run(run())


def test_empty_book_does_not_retimestamp_summary_as_executable(tmp_path):
    trading = coordinator(tmp_path)
    engine = engine_for(trading)
    state = engine._save_kalshi_snapshot(
        {"ticker": "HELD", "yes_bid_dollars": ".99", "yes_ask_dollars": "1.00"},
        {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, iso_now(),
        persist=False, allow_summary_fallback=False,
    )
    assert state["yes_bid"] is None
    assert state["yes_ask"] is None


def test_each_held_ticker_is_evaluated_and_cross_mode_frames_rejected(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        seen = []

        async def evaluate(broker, current):
            seen.append((broker.mode, current["ticker"]))

        trading._process_exits = evaluate
        trading.schedule_protective_exits("LIVE", frame("A"))
        trading.schedule_protective_exits("LIVE", frame("B"))
        trading.schedule_protective_exits("DEMO", frame("WRONG"))
        trading.schedule_protective_exits("DEMO", frame("C", "DEMO"))
        await drain(trading)
        assert set(seen) == {("LIVE", "A"), ("LIVE", "B"), ("DEMO", "C")}
    asyncio.run(run())


def test_brief_target_crossing_survives_quote_burst(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        submitted = []

        async def submit(intent, key):
            submitted.append(intent)
            trading._pending_exit_keys.discard(key)

        trading._submit_exit = submit
        # Queue a complete burst before the evaluator gets an event-loop turn.
        for bid in [.45, .52, *([.46] * 1000)]:
            trading.schedule_protective_exits("LIVE", frame(bid=bid))
        assert len(trading._latest_protective_current["LIVE"]["HELD"]) <= 7
        await drain(trading)
        assert len(submitted) == 1
        assert submitted[0].decision_snapshot["executable_bid"] == .52
        assert submitted[0].source == "texas_river_target"
    asyncio.run(run())


def test_queued_quote_does_not_borrow_later_phase_target(tmp_path, monkeypatch):
    trading = coordinator(tmp_path)
    position(trading)
    trading.db.update_settings({"texas_holdem_flop_target": .60, "texas_holdem_turn_target": .50})
    now = time.time()
    current = frame(bid=.52)
    current["close_time"] = datetime.fromtimestamp(now + 601, UTC).isoformat()
    current["protection_evaluation_at"] = now
    monkeypatch.setattr("app.services.trading.time.time", lambda: now + 2)
    assert trading._plan_protective_exits(trading.brokers["LIVE"], current) == []


def test_cross_phase_burst_preserves_earlier_valid_target(tmp_path, monkeypatch):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        trading.db.update_settings({"texas_holdem_flop_target": .50, "texas_holdem_turn_target": .60})
        now = time.time()
        current = frame(bid=.52)
        current["close_time"] = datetime.fromtimestamp(now + 601, UTC).isoformat()
        submitted = []

        async def submit(intent, key):
            submitted.append(intent)
            trading._pending_exit_keys.discard(key)

        trading._submit_exit = submit
        monkeypatch.setattr("app.services.trading.time.time", lambda: now)
        trading.schedule_protective_exits("LIVE", current)
        monkeypatch.setattr("app.services.trading.time.time", lambda: now + 2)
        trading.schedule_protective_exits("LIVE", {**current, "yes_bid": .58})
        trading.schedule_protective_exits("LIVE", {**current, "yes_bid": .40})
        await drain(trading)
        assert len(submitted) == 1
        assert submitted[0].source == "texas_flop_target"
    asyncio.run(run())


def test_slow_durable_exit_planning_does_not_block_event_loop(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        entered, release = threading.Event(), threading.Event()

        def plan(*args):
            entered.set()
            assert release.wait(3)
            return []

        trading._plan_protective_exits = plan
        task = asyncio.create_task(trading._process_exits(trading.brokers["LIVE"], frame()))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
            marker = asyncio.Event()
            asyncio.get_running_loop().call_soon(marker.set)
            await asyncio.wait_for(marker.wait(), timeout=.1)
            assert not task.done()
        finally:
            release.set()
            await task
    asyncio.run(run())


def test_protection_does_not_wait_for_background_thread_capacity(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        blocked, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))

        def history():
            blocked.set()
            release.wait(3)

        background = asyncio.create_task(asyncio.to_thread(history))
        trading._plan_protective_exits = lambda *_: []
        try:
            while not blocked.is_set():
                await asyncio.sleep(.001)
            await asyncio.wait_for(trading._process_exits(trading.brokers["LIVE"], frame()), .2)
            assert not background.done()
        finally:
            release.set()
            await background
            await trading.stop()
    asyncio.run(run())


def test_watchdog_recovers_failed_evaluation_without_another_quote(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        original = trading._plan_protective_exits
        calls = 0

        def plan(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated evaluator failure")
            return []

        trading._plan_protective_exits = plan
        trading.schedule_protective_exits("LIVE", frame())
        await drain(trading)
        assert trading.db.fetch_one("SELECT COUNT(*) AS n FROM broker_audit_events "
                                    "WHERE event_type='PROTECTIVE_EVALUATION_ERROR'")["n"] == 1
        await trading._protection_watchdog_once()
        await drain(trading)
        assert calls == 2
        await trading._protection_watchdog_once()
        assert trading.protection_health()["modes"]["LIVE"]["status"] == "Protective exits active"
        await drain(trading)
        trading._plan_protective_exits = original
    asyncio.run(run())


@pytest.mark.parametrize("blocker", ["disarm", "kill", "stale", "uncovered"])
def test_watchdog_reports_actual_protection_blocker(tmp_path, blocker):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        current = frame()
        if blocker == "stale":
            current["executable_quote_at"] = "2000-01-01T00:00:00Z"
        trading._protective_snapshots["LIVE"] = {"HELD": current} if blocker != "uncovered" else {}
        trading._protection_evaluated[("LIVE", "HELD")] = time.monotonic()
        if blocker == "disarm":
            trading.brokers["LIVE"].session_armed = False
        elif blocker == "kill":
            trading.brokers["LIVE"]._update_mode_state(kill_switch=True)
        await trading._protection_watchdog_once()
        health = trading.protection_health()["modes"]["LIVE"]
        assert health["healthy"] is False
        assert health["status"] != "Protective exits active"
        assert trading.brokers["LIVE"].session_armed is (blocker != "disarm")
        await drain(trading)
    asyncio.run(run())


def test_unknown_clock_never_means_river_but_global_take_still_works():
    assert texas_holdem_phase(None)["key"] == "UNKNOWN"
    assert texas_holdem_exit_reason(.80, None, {})[0] is None
    texas = {"strategy": "TEXAS_HOLDEM_2_0", "side": "YES"}
    assert protective_exit_reason(texas, .80, None, {}) == (None, None)
    assert protective_exit_reason(texas, .99, None, {})[0] == "GLOBAL_PROFIT_TAKE"


def test_watchdog_names_stalled_evaluator_instead_of_claiming_protection(tmp_path):
    async def run():
        trading = coordinator(tmp_path)
        position(trading)
        trading._protective_snapshots["LIVE"] = {"HELD": frame()}
        trading._protection_health["LIVE"] = {"coverage_started_monotonic": time.monotonic() - 3}
        await trading._protection_watchdog_once()
        health = trading.protection_health()["modes"]["LIVE"]
        assert health["status"] == "Protection stalled: exit evaluator not responding"
        assert not health["healthy"]
        await drain(trading)
    asyncio.run(run())
