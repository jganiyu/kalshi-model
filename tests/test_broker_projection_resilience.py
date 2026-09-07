from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.db import Database
from app.services.broker import KalshiBroker, OrderIntent, PaperBroker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "projection.db")
    database.initialize()
    return database


def insert_fill(db: Database, fill_id: str, *, mode: str = "DEMO",
                action: str = "BUY", quantity: float = 2, price: float = .4,
                ticker: str = "FIFO", at: str = "2026-09-04T10:00:00Z") -> None:
    db.execute(
        """INSERT INTO broker_fills(
            mode,fill_id,ticker,side,action,contracts,price,fee,strategy,source,filled_at,raw_json
        ) VALUES (?,?,?,'YES',?,?,?,.01,'MANUAL','manual',?,'{}')""",
        (mode, fill_id, ticker, action, quantity, price, at),
    )


def trace_reads(db: Database, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    queries: list[str] = []
    connect = db.connect

    def traced_connect():
        connection = connect()
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(db, "connect", traced_connect)
    return queries


def fill_scans(queries: list[str]) -> int:
    return sum("SELECT * FROM broker_fills WHERE mode=" in sql for sql in queries)


def test_cached_recent_trades_keep_fifo_and_do_not_scan_history_each_summary(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy-low", price=.2)
    insert_fill(db, "buy-high", price=.8, at="2026-09-04T10:01:00Z")
    insert_fill(db, "sell", action="SELL", quantity=3, price=.7,
                at="2026-09-04T10:02:00Z")
    queries = trace_reads(db, monkeypatch)
    first = broker.recent_trades(5)
    assert first[0]["realized_pnl"] == pytest.approx(.875)
    assert first[0]["status"] == "PARTIALLY CLOSED"
    for _ in range(10):
        assert broker.recent_trades(5) == first
    assert fill_scans(queries) == 1
    first[0]["price"] = 999
    assert broker.recent_trades()[0]["price"] == pytest.approx(.5)


def test_revision_detects_direct_corrections_deletes_and_mode_changes(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo, live = KalshiBroker("DEMO", db), KalshiBroker("LIVE", db)
    insert_fill(db, "buy")
    assert demo.recent_trades()[0]["price"] == .4
    assert live.recent_trades() == []
    # A second Database instance models an independent writer/process.
    writer = Database(db.path)
    writer.execute("UPDATE broker_fills SET price=.6 WHERE fill_id='buy'")
    assert demo.recent_trades()[0]["price"] == .6
    queries = trace_reads(db, monkeypatch)
    writer.execute("UPDATE broker_fills SET raw_json='{}' WHERE fill_id='buy'")
    demo.recent_trades()
    assert fill_scans(queries) == 0
    writer.execute("UPDATE broker_fills SET mode='LIVE' WHERE fill_id='buy'")
    assert demo.recent_trades() == []
    assert live.recent_trades()[0]["price"] == .6
    writer.execute("DELETE FROM broker_fills WHERE fill_id='buy'")
    assert live.recent_trades() == []


def test_other_environment_changes_do_not_invalidate_cached_economics(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "demo")
    expected = broker.recent_trades()
    queries = trace_reads(db, monkeypatch)
    insert_fill(db, "live", mode="LIVE", price=.9)
    assert broker.recent_trades() == expected
    assert fill_scans(queries) == 0


def test_settlement_updates_invalidate_cached_fifo(db: Database) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy")
    assert broker.recent_trades()[0]["status"] == "OPEN"
    db.execute("""INSERT INTO broker_settlements(
        mode,ticker,side,settled_at,market_result,position_won,realized_pnl,fees,raw_json
    ) VALUES ('DEMO','FIFO','YES','2026-09-04T10:15:00Z','YES',1,1.19,.01,'{}')""")
    assert broker.recent_trades()[0]["realized_pnl"] == pytest.approx(1.19)
    db.execute("UPDATE broker_settlements SET market_result='NO',available_cash_after=12")
    settled = broker.recent_trades()[0]
    assert settled["realized_pnl"] == pytest.approx(-.81)
    assert settled["available_cash_after"] == 12
    db.execute("DELETE FROM broker_settlements")
    assert broker.recent_trades()[0]["status"] == "OPEN"


def test_cash_and_market_display_metadata_refresh_without_economic_scan(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy")
    assert broker.recent_trades()[0]["available_cash_after"] is None
    queries = trace_reads(db, monkeypatch)
    db.execute("""INSERT INTO broker_account_snapshots(
        mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
    ) VALUES ('DEMO','2026-09-04T10:00:01Z',99,0,0,'{}')""")
    db.execute("""INSERT INTO markets(ticker,status,strike,raw_json,first_seen_at,updated_at)
        VALUES ('FIFO','finalized',100,'{"expiration_value":102}', 'x','x')""")
    row = broker.recent_trades()[0]
    assert row["available_cash_after"] == 99
    assert row["settlement_margin"] == 2
    db.execute("UPDATE markets SET strike=101 WHERE ticker='FIFO'")
    assert broker.recent_trades()[0]["settlement_margin"] == 1
    assert fill_scans(queries) == 0


def test_projection_cache_stays_bounded_and_keeps_full_ledger_limit(db: Database) -> None:
    broker = KalshiBroker("DEMO", db)
    for index in range(110):
        insert_fill(db, str(index), ticker=f"M{index}")
    assert len(broker.recent_trades(1000)) == 8
    assert len(broker.trade_ledger()) == 100
    assert broker._ledger_cache is not None
    assert len(broker._ledger_cache[1]) == 100


def test_entry_review_and_protection_metadata_stay_fresh_on_cache_hits(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy")
    db.execute("""INSERT INTO broker_order_intents(
        mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
        status,strategy,source,created_at,updated_at,margin_volatility_index
    ) VALUES ('DEMO','entry','FIFO','YES','BUY',2,.4,'FILLED','TEST','automatic','x','x',1)""")
    db.execute("""INSERT INTO broker_positions(
        mode,ticker,side,contracts,updated_at,threshold_breach_enabled,threshold_exit_status
    ) VALUES ('DEMO','FIFO','YES',2,'x',1,'MONITORING')""")
    session_id = db.execute("""INSERT INTO trade_review_sessions(
        environment,ticker,recording_started_at,created_at
    ) VALUES ('DEMO','FIFO','x','x')""")
    db.execute("""INSERT INTO trade_review_links(
        session_id,environment,trade_ref,source_type,ticker
    ) VALUES (?,'DEMO','DEMO:FIFO:YES','broker','FIFO')""", (session_id,))
    first = broker.recent_trades()[0]
    assert first["margin_volatility_index"] == 1
    assert first["threshold_breach_exit"]["status"] == "MONITORING"
    assert first["review_status"] == "RECORDING"
    queries = trace_reads(db, monkeypatch)
    db.execute("UPDATE broker_order_intents SET margin_volatility_index=2")
    db.execute("UPDATE broker_positions SET threshold_exit_status='TRIGGERED',contracts=1")
    db.execute("UPDATE trade_review_sessions SET status='FINALIZED',coverage=.9")
    row = broker.recent_trades()[0]
    assert row["margin_volatility_index"] == 2
    assert row["threshold_breach_exit"]["status"] == "TRIGGERED"
    assert row["threshold_breach_exit"]["remaining_contracts"] == 1
    assert row["review_status"] == "FINALIZED"
    assert row["review_coverage"] == .9
    assert fill_scans(queries) == 0


def test_revision_and_economics_share_one_read_snapshot(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy")
    writer = Database(db.path)
    connect = db.connect
    corrected = False

    def interleave(sql: str) -> None:
        nonlocal corrected
        if "SELECT * FROM broker_fills" in sql and not corrected:
            corrected = True
            writer.execute("UPDATE broker_fills SET price=.6 WHERE fill_id='buy'")

    def traced_connect():
        connection = connect()
        connection.set_trace_callback(interleave)
        return connection

    monkeypatch.setattr(db, "connect", traced_connect)
    assert broker.recent_trades()[0]["price"] == .4
    assert corrected
    assert broker.recent_trades()[0]["price"] == .6


def test_rolled_back_correction_does_not_invalidate_projection(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = KalshiBroker("DEMO", db)
    insert_fill(db, "buy")
    expected = broker.recent_trades()
    queries = trace_reads(db, monkeypatch)
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute("UPDATE broker_fills SET price=.9")
            raise RuntimeError("rollback")
    assert broker.recent_trades() == expected
    assert fill_scans(queries) == 0


class ReadOnlyAccountClient:
    async def balance(self):
        return {"balance_dollars": "1000", "portfolio_value": 0}

    async def orders(self, **kwargs):
        return {"orders": []}

    async def fills(self):
        return {"fills": [{"fill_id": "new", "ticker": "FIFO", "side": "yes",
                           "action": "buy", "count": 2, "yes_price_dollars": ".4"}]}

    async def positions(self):
        return {"market_positions": []}

    async def settlements(self):
        return {"settlements": [{"ticker": "FIFO", "market_result": "yes"}]}


@pytest.mark.parametrize("method", ["_upsert_fill", "_replace_positions", "_upsert_settlement", "portfolio"])
@pytest.mark.parametrize("existing_watermark", [False, True])
def test_failed_authoritative_application_never_advances_watermark(
    db: Database, monkeypatch: pytest.MonkeyPatch, method: str, existing_watermark: bool,
) -> None:
    broker = KalshiBroker("DEMO", db, ReadOnlyAccountClient())  # type: ignore[arg-type]
    if existing_watermark:
        db.execute("""INSERT INTO broker_reconciliation_watermarks
            VALUES ('DEMO','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','old')""")
    before = db.fetch_one("SELECT * FROM broker_reconciliation_watermarks WHERE mode='DEMO'")

    def fail(*args, **kwargs):
        raise RuntimeError("application failed")

    monkeypatch.setattr(broker, method, fail)
    with pytest.raises(RuntimeError, match="application failed"):
        asyncio.run(broker.reconcile(full_audit=True))
    assert db.fetch_one("SELECT * FROM broker_reconciliation_watermarks WHERE mode='DEMO'") == before
    state = broker.readiness()
    assert state["reconciliation_required"]
    assert not state["reconciled"]
    assert broker._reconcile_generation == 0


def test_successful_authoritative_application_advances_watermark(db: Database) -> None:
    broker = KalshiBroker("DEMO", db, ReadOnlyAccountClient())  # type: ignore[arg-type]
    assert broker.recent_trades() == []
    asyncio.run(broker.reconcile(full_audit=True))
    watermark = db.fetch_one("SELECT * FROM broker_reconciliation_watermarks WHERE mode='DEMO'")
    assert watermark and watermark["last_full_at"]
    assert broker.recent_trades()[0]["status"] == "SETTLED"


def test_successful_endpoint_facts_survive_stalled_then_failed_sibling(db: Database) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        class PartialClient(ReadOnlyAccountClient):
            async def settlements(self):
                await release.wait()
                raise RuntimeError("settlements unavailable")

            async def positions(self):
                return {"market_positions": [
                    {"ticker": "FIFO", "position": 2},
                    {"ticker": "UNMENTIONED", "position": 0},
                ]}

        broker = KalshiBroker("DEMO", db, PartialClient())  # type: ignore[arg-type]
        db.execute("""INSERT INTO broker_positions(mode,ticker,side,contracts,updated_at)
            VALUES ('DEMO','UNMENTIONED','YES',3,'x')""")
        task = asyncio.create_task(broker.reconcile(full_audit=True))
        try:
            for _ in range(100):
                if db.fetch_one("SELECT 1 FROM broker_fills WHERE fill_id='new'"):
                    break
                await asyncio.sleep(.001)
            assert not task.done()
            assert db.fetch_one("SELECT 1 FROM broker_fills WHERE fill_id='new'")
            assert broker.execution_portfolio()["available_cash"] == 1000
            assert db.fetch_one("SELECT contracts FROM broker_positions WHERE ticker='FIFO'")["contracts"] == 2
            assert db.fetch_one("SELECT contracts FROM broker_positions WHERE ticker='UNMENTIONED'")["contracts"] == 3
            assert not db.fetch_one("SELECT * FROM broker_reconciliation_watermarks")
        finally:
            release.set()
            with pytest.raises(RuntimeError, match="settlements unavailable"):
                await task
        assert broker.readiness()["reconciliation_required"]
        assert db.fetch_one("SELECT 1 FROM broker_fills WHERE fill_id='new'")

    asyncio.run(scenario())


def test_old_position_snapshot_cannot_reopen_newly_closed_private_position(db: Database) -> None:
    broker = KalshiBroker("DEMO", db)
    broker._upsert_position({"ticker": "CLOSED", "position": 5})
    generations = dict(broker._position_fact_generations)
    broker.adopt_private_event({"type": "market_position", "msg": {
        "ticker": "CLOSED", "position": 0,
    }})
    broker._replace_positions(
        [{"ticker": "CLOSED", "position": 5}], expected_generations=generations,
    )
    row = db.fetch_one("SELECT * FROM broker_positions WHERE mode='DEMO' AND ticker='CLOSED'")
    assert row["contracts"] == 0
    assert row["status"] == "closed"


@pytest.mark.parametrize("method", ["_upsert_fill", "_replace_positions", "portfolio"])
@pytest.mark.parametrize("cancel", [False, True])
def test_reconciliation_db_batches_leave_loop_responsive_and_drain_on_cancel(
    db: Database, monkeypatch: pytest.MonkeyPatch, method: str, cancel: bool,
) -> None:
    async def scenario() -> None:
        broker = KalshiBroker("DEMO", db, ReadOnlyAccountClient())  # type: ignore[arg-type]
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = getattr(broker, method)
        loop_thread = threading.get_ident()

        def slow_application(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(3), "The event loop did not release the database worker"
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(broker, method, slow_application)
        task = asyncio.create_task(broker.reconcile(full_audit=True))
        try:
            for _ in range(1000):
                if started.is_set():
                    break
                await asyncio.sleep(.001)
            assert started.is_set()
            # This coroutine is progressing while the actual application path
            # is blocked in a database worker, not just a mocked async client.
            assert not finished.is_set()
            assert not task.done()
            if cancel:
                task.cancel()
                await asyncio.sleep(.01)
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
                assert broker._reconcile_lock.locked()
                assert not db.fetch_one("SELECT * FROM broker_reconciliation_watermarks")
        finally:
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        assert finished.is_set()
        assert not broker._reconcile_lock.locked()
        if cancel:
            assert not db.fetch_one("SELECT * FROM broker_reconciliation_watermarks")
            assert broker.readiness()["reconciliation_required"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["DEMO", "LIVE"])
@pytest.mark.parametrize("cash,equity", [(100, 150), (150, 100)])
def test_execution_projection_matches_full_portfolio_inputs_without_history(
    db: Database, monkeypatch: pytest.MonkeyPatch, mode: str, cash: float, equity: float,
) -> None:
    broker = KalshiBroker(mode, db, ReadOnlyAccountClient())  # type: ignore[arg-type]
    broker.session_armed = broker.automatic_armed = True
    broker._update_mode_state(connected=True, authenticated=True, reconciled=True,
                              reconciliation_required=False)
    db.execute("""INSERT INTO broker_account_snapshots(
        mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
    ) VALUES (?,'2026-09-06T00:00:00Z',?,?,0,'{}')""", (mode, cash, equity))
    for position_mode, ticker, status in [(mode, "HELD", "open"),
                                         (mode, "CLOSED", "closed"),
                                         ("LIVE" if mode == "DEMO" else "DEMO", "OTHER", "open")]:
        db.execute("""INSERT INTO broker_positions(mode,ticker,side,contracts,updated_at,status)
            VALUES (?,?,'YES',2,'x',?)""", (position_mode, ticker, status))
    full = broker.portfolio(include_ledger=False)
    queries = trace_reads(db, monkeypatch)
    compact = broker.execution_portfolio()
    assert compact == {key: full[key] for key in compact}
    assert compact["current_bankroll"] == max(cash, equity)
    assert [position["ticker"] for position in compact["positions"]] == ["HELD"]
    assert not any(table in sql for sql in queries for table in
                   ("broker_fills", "broker_orders", "broker_order_intents", "broker_settlements"))
    assert not any("SUM(" in sql.upper() for sql in queries)
    broker.disarm("test")
    assert not broker.execution_portfolio()["automatic_trade_allowed"]
    db.execute("UPDATE broker_account_snapshots SET available_balance=200 WHERE mode=?", (mode,))
    db.execute("UPDATE broker_positions SET contracts=1 WHERE mode=? AND ticker='HELD'", (mode,))
    fresh = broker.execution_portfolio()
    assert fresh["available_cash"] == fresh["current_bankroll"] == 200
    assert fresh["positions"][0]["contracts"] == 1


def test_execution_projection_retains_reconciliation_and_kill_switch_blockers(db: Database) -> None:
    broker = KalshiBroker("DEMO", db, ReadOnlyAccountClient())  # type: ignore[arg-type]
    broker.session_armed = broker.automatic_armed = True
    broker._update_mode_state(connected=True, authenticated=True, reconciled=False,
                              reconciliation_required=True)
    blocked = broker.execution_portfolio()
    assert not blocked["automatic_trade_allowed"]
    assert blocked["automatic_trade_block_reason"] == "Reconciling Kalshi account activity."
    broker._update_mode_state(reconciled=True, reconciliation_required=False, kill_switch=True)
    assert broker.execution_portfolio()["automatic_trade_block_reason"] == "The kill switch is active."


def test_paper_execution_projection_preserves_existing_risk_fields() -> None:
    class PaperAccount:
        def portfolio(self):
            return {"current_bankroll": 100, "available_cash": 80,
                    "session_drawdown_pct": .1, "automatic_trade_allowed": False,
                    "automatic_trade_block_reason": "Daily loss limit reached."}

    broker = PaperBroker(PaperAccount())  # type: ignore[arg-type]
    assert broker.execution_portfolio() == broker.portfolio()


@pytest.mark.parametrize("mode", ["DEMO", "LIVE"])
@pytest.mark.parametrize("snapshot_quantity", [None, 0, 8])
@pytest.mark.parametrize("new_fact", ["position", "fill", "order", "acknowledgement"])
def test_slow_account_snapshot_cannot_erase_newer_exposure_evidence(
    db: Database, mode: str, snapshot_quantity: int | None, new_fact: str,
) -> None:
    async def scenario() -> None:
        requested, release = asyncio.Event(), asyncio.Event()

        class SlowSnapshotClient(ReadOnlyAccountClient):
            async def positions(self):
                requested.set()
                await release.wait()
                return {"market_positions": ([] if snapshot_quantity is None else [
                    {"ticker": "RACING", "position": snapshot_quantity},
                ])}

            async def fills(self):
                return {"fills": []}

            async def settlements(self):
                return {"settlements": []}

        broker = KalshiBroker(mode, db, SlowSnapshotClient())  # type: ignore[arg-type]
        broker._upsert_position({"ticker": "RACING", "position": 2})
        # An unrelated stale local position must still be closed by the scan.
        broker._upsert_position({"ticker": "OLD", "position": 1})
        task = asyncio.create_task(broker.reconcile(full_audit=True))
        await requested.wait()
        if new_fact == "position":
            broker.adopt_private_event({"type": "market_position", "msg": {
                "ticker": "RACING", "position": 3,
            }})
            expected_quantity = 3
        elif new_fact == "fill":
            broker.adopt_private_event({"type": "fill", "msg": {
                "ticker": "RACING", "fill_id": "private", "side": "yes",
                "action": "buy", "count": 2, "yes_price_dollars": ".4",
            }})
            expected_quantity = 2
        elif new_fact == "order":
            broker.adopt_private_event({"type": "user_order", "msg": {
                "ticker": "RACING", "order_id": "private-order", "side": "yes",
                "action": "buy", "initial_count": 2, "fill_count": 2,
                "remaining_count": 0, "yes_price_dollars": ".4", "status": "executed",
            }})
            expected_quantity = 2
        else:
            broker._adopt_texas_acknowledged_fill(OrderIntent(
                mode=mode, ticker="RACING", side="YES", action="BUY",
                contracts=3, limit_price=.4, strategy="TEXAS_HOLDEM_2_0", source="automatic",
            ), 3)
            expected_quantity = 3
        release.set()
        await task
        row = db.fetch_one("SELECT * FROM broker_positions WHERE mode=? AND ticker='RACING'", (mode,))
        assert row["status"] == "open"
        assert row["contracts"] == expected_quantity
        assert db.fetch_one("SELECT status FROM broker_positions WHERE mode=? AND ticker='OLD'", (mode,))["status"] == "closed"
        if new_fact == "fill":
            assert db.fetch_one("SELECT contracts FROM broker_fills WHERE mode=? AND fill_id='private'", (mode,))["contracts"] == 2
        # The guard does not freeze the position forever: a later uncontested
        # snapshot can apply the exchange's current authoritative exposure.
        await broker.reconcile(full_audit=True)
        row = db.fetch_one("SELECT * FROM broker_positions WHERE mode=? AND ticker='RACING'", (mode,))
        assert row["contracts"] == (snapshot_quantity or 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["DEMO", "LIVE"])
@pytest.mark.parametrize("recovery", ["private", "entry", "exit"])
@pytest.mark.parametrize("new_quantity", [0, 3])
def test_targeted_recovery_preserves_position_events_received_during_lookup(
    db: Database, mode: str, recovery: str, new_quantity: int,
) -> None:
    async def scenario():
        requested, release = asyncio.Event(), asyncio.Event()
        old_quantity = 5 if new_quantity == 0 else 0

        class TargetedClient:
            async def positions(self, **kwargs):
                requested.set()
                await release.wait()
                return {"market_positions": [{"ticker": "TARGETED", "position": old_quantity}]}

            async def order(self, order_id):
                return await self.order_by_client_id("intent")

            async def order_by_client_id(self, client_id, **kwargs):
                if recovery == "exit":
                    return None
                return {"order_id": "order", "client_order_id": client_id,
                        "ticker": "TARGETED", "side": "yes", "action": "buy",
                        "count": 2, "fill_count": 2, "remaining_count": 0,
                        "yes_price_dollars": ".4", "status": "executed"}

        broker = KalshiBroker(mode, db, TargetedClient())  # type: ignore[arg-type]
        broker._upsert_position({"ticker": "TARGETED", "position": 2})
        action = "SELL" if recovery == "exit" else "BUY"
        db.execute("""INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,status,
            strategy,source,decision_snapshot_json,created_at,updated_at
        ) VALUES (?,'intent','TARGETED','YES',?,2,.4,'RECONCILIATION_REQUIRED',
            'TEXAS_HOLDEM_2_0','automatic','{"protective_exit":true}','x','x')""", (mode, action))
        if recovery == "exit":
            operation = broker.recover_ambiguous_protective_exit("intent")
        elif recovery == "entry":
            operation = broker.recover_ambiguous_entry(OrderIntent(
                mode=mode, ticker="TARGETED", side="YES", action="BUY", contracts=2,
                limit_price=.4, strategy="TEXAS_HOLDEM_2_0", source="automatic",
                client_order_id="intent",
            ))
        else:
            operation = broker.recover_private_event(order_id="order", ticker="TARGETED")
        task = asyncio.create_task(operation)
        await requested.wait()
        broker.adopt_private_event({"type": "market_position", "msg": {
            "ticker": "TARGETED", "position": new_quantity,
        }})
        release.set()
        await task
        row = db.fetch_one("SELECT * FROM broker_positions WHERE mode=? AND ticker='TARGETED'", (mode,))
        assert row["contracts"] == new_quantity
        assert row["status"] == ("closed" if new_quantity == 0 else "open")
        if recovery == "exit" and new_quantity > 0:
            intent = db.fetch_one("SELECT status FROM broker_order_intents WHERE mode=? AND client_order_id='intent'", (mode,))
            assert intent["status"] == "RECONCILIATION_REQUIRED"

    asyncio.run(scenario())
