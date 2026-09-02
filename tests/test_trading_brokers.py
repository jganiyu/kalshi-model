from __future__ import annotations

import asyncio
import json
from functools import wraps
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import AppConfig, DEFAULT_SETTINGS
from app.db import Database, MIGRATIONS
from app.domain import iso_now
from app.services.broker import KalshiBroker, OrderIntent, fill_aggregate
from app.services.credentials import CredentialStore
from app.services.kalshi_trading import (
    AmbiguousSubmissionError,
    KalshiTradingClient,
    KalshiTradingError,
    outcome_to_book,
    normalize_order_price,
    signed_headers,
)
from app.services.paper import PaperTradingService
from app.services.trading import TradingCoordinator, protective_exit_reason


def run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class FakeTradingClient:
    def __init__(self) -> None:
        self.environment = "DEMO"
        self.key_id = "fake-demo-key"
        self.created: list[dict] = []
        self.canceled: list[str] = []
        self.remote_orders: list[dict] = []
        self.remote_fills: list[dict] = []
        self.remote_positions: list[dict] = []
        self.remote_settlements: list[dict] = []
        self.remote_markets: dict[str, dict] = {}
        self.timeout = False
        self.balance_payload: dict = {
            "balance_dollars": "1000.0000",
            "portfolio_value": 0,
        }
        self.cancel_tickers: list[str | None] = []
        self.balance_calls = 0
        self.reject_sells = False
        self.reject_error: KalshiTradingError | None = None
        self.cancel_ioc = False

    async def balance(self):
        self.balance_calls += 1
        return dict(self.balance_payload)

    async def orders(self, **_):
        return {"orders": list(self.remote_orders)}

    async def fills(self, **_):
        return {"fills": list(self.remote_fills)}

    async def positions(self, **_):
        return {"market_positions": list(self.remote_positions)}

    async def settlements(self, **_):
        return {"settlements": list(self.remote_settlements)}

    async def market(self, ticker: str):
        return dict(self.remote_markets.get(ticker) or {"ticker": ticker, "status": "active"})

    async def create_order(self, **payload):
        if self.timeout:
            raise AmbiguousSubmissionError("ambiguous")
        self.created.append(payload)
        if self.reject_sells and payload["action"] == "SELL":
            raise self.reject_error or KalshiTradingError("invalid order", status_code=400)
        unfilled_ioc = bool(
            self.cancel_ioc
            and payload.get("time_in_force") == "immediate_or_cancel"
        )
        order = {
            "order_id": f"order-{len(self.created)}",
            "client_order_id": payload["client_order_id"],
            "ticker": payload["ticker"],
            "fill_count": "0.00",
            "remaining_count": "0.00" if unfilled_ioc else f'{payload["contracts"]}.00',
        }
        book_side, book_price = outcome_to_book(
            payload["side"], payload["action"], payload["limit_price"]
        )
        self.remote_orders.append({
            **order,
            "side": book_side,
            "price": str(book_price),
            "count": str(payload["contracts"]),
            "status": "canceled" if unfilled_ioc else "resting",
            "reduce_only": payload["reduce_only"],
        })
        return order

    async def cancel_order(
        self,
        exchange_order_id: str,
        *,
        market_ticker: str | None = None,
    ):
        self.canceled.append(exchange_order_id)
        self.cancel_tickers.append(market_ticker)
        for order in self.remote_orders:
            if order["order_id"] == exchange_order_id:
                order["status"] = "canceled"
        return {"order_id": exchange_order_id, "status": "canceled"}


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "trading.db")
    db.initialize()
    return db


async def ready_broker(tmp_path: Path, mode: str = "DEMO") -> tuple[Database, KalshiBroker, FakeTradingClient]:
    db = make_db(tmp_path)
    client = FakeTradingClient()
    client.environment = mode
    broker = KalshiBroker(mode, db, client)  # type: ignore[arg-type]
    await broker.reconcile()
    if mode == "LIVE":
        db.execute(
            "UPDATE broker_mode_state SET demo_verified_at=?,limits_reviewed_at=? WHERE mode='LIVE'",
            (iso_now(), iso_now()),
        )
        broker.arm(confirmation="ARM LIVE TRADING", automatic=True)
    else:
        broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    return db, broker, client


def test_v2_single_book_mapping() -> None:
    assert outcome_to_book("YES", "BUY", 0.42) == ("bid", 0.42)
    assert outcome_to_book("YES", "SELL", 0.42) == ("ask", 0.42)
    assert outcome_to_book("NO", "BUY", 0.42) == ("ask", 0.58)
    assert outcome_to_book("NO", "SELL", 0.42) == ("bid", 0.58)
    assert outcome_to_book("YES", "SELL", 0.01) == ("ask", 0.01)
    assert outcome_to_book("NO", "SELL", 0.01) == ("bid", 0.99)


def test_exchange_order_prices_use_directional_whole_cent_rounding() -> None:
    assert normalize_order_price(0.8150000000000001, "BUY") == 0.82
    assert normalize_order_price(0.795, "SELL") == 0.79
    assert OrderIntent(
        "DEMO", "T", "NO", "BUY", 1, 0.8150000000000001,
        "STANDARD_EDGE", "automatic",
    ).limit_price == 0.82


def test_exchange_order_prices_follow_tapered_market_ticks() -> None:
    ranges = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]
    assert normalize_order_price(
        0.953, "BUY", side="YES", price_ranges=ranges
    ) == 0.953
    assert normalize_order_price(
        0.815, "BUY", side="NO", price_ranges=ranges
    ) == 0.82
    assert normalize_order_price(
        0.0001, "SELL", side="YES", price_ranges=ranges
    ) == 0.001
    assert normalize_order_price(
        0.0001, "SELL", side="NO", price_ranges=ranges
    ) == 0.001


def test_signed_headers_sign_method_and_path(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    headers = signed_headers(
        "test-key-id", path, "POST", "/trade-api/v2/portfolio/events/orders?x=1",
        timestamp_ms=123,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "123"
    assert headers["KALSHI-ACCESS-SIGNATURE"]


def test_demo_live_credentials_are_physically_isolated(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    demo = CredentialStore(tmp_path, "demo").save("demo-key-id", pem)
    live = CredentialStore(tmp_path, "live").save("live-key-id", pem)
    assert demo.private_key_path != live.private_key_path
    assert CredentialStore(tmp_path, "demo").load().key_id == "demo-key-id"  # type: ignore[union-attr]
    assert CredentialStore(tmp_path, "live").load().key_id == "live-key-id"  # type: ignore[union-attr]
    assert CredentialStore(tmp_path).load() is None


@run_async
async def test_submit_persists_intent_before_network_and_is_idempotent(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    intent = OrderIntent("DEMO", "TICKER", "YES", "BUY", 2, 0.40, "STANDARD_EDGE", "automatic")
    first = await broker.submit(intent)
    second = await broker.submit(intent)
    assert first["client_order_id"] == intent.client_order_id
    assert second["client_order_id"] == intent.client_order_id
    assert len(client.created) == 1
    row = db.fetch_one("SELECT * FROM broker_order_intents")
    assert row and row["status"] == "ACKNOWLEDGED"
    assert row["exchange_order_id"] == "order-1"


@run_async
async def test_filled_exchange_position_is_unsettled_not_an_open_order(
    tmp_path: Path,
) -> None:
    _db, broker, client = await ready_broker(tmp_path)
    client.remote_positions = [{
        "ticker": "WAITING-SETTLEMENT",
        "position_fp": "16.00",
        "market_exposure_dollars": "14.4000",
        "last_updated_ts": "2026-08-28T20:37:17Z",
    }]
    client.remote_fills = [{
        "fill_id": "filled-entry",
        "ticker": "WAITING-SETTLEMENT",
        "side": "yes",
        "action": "buy",
        "count_fp": "16.00",
        "yes_price_dollars": "0.9000",
        "fee_cost_dollars": "0.1008",
        "created_time": "2026-08-28T20:37:17Z",
    }]

    await broker.reconcile()
    portfolio = broker.portfolio()

    assert portfolio["open_order_count"] == 0
    assert portfolio["positions"][0]["status"] == "open"
    assert portfolio["positions"][0]["display_status"] == "UNSETTLED"
    assert portfolio["ledger"][0]["status"] == "OPEN"
    assert portfolio["ledger"][0]["display_status"] == "UNSETTLED"


@run_async
async def test_ambiguous_timeout_requires_reconciliation_without_retry(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    client.timeout = True
    intent = OrderIntent("DEMO", "TICKER", "YES", "BUY", 1, 0.40, "MANUAL", "manual")
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)
    row = db.fetch_one("SELECT * FROM broker_order_intents")
    assert row and row["status"] == "RECONCILIATION_REQUIRED"
    assert broker.readiness()["reconciliation_required"] is True
    assert len(client.created) == 0


@run_async
async def test_reconcile_clears_unseen_ambiguous_order_after_market_settlement(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.execute(
        """
        INSERT INTO broker_positions(
            mode,ticker,side,contracts,market_exposure,updated_at,status
        ) VALUES ('DEMO','SETTLED','NO',1,.90,?,'open')
        """,
        (iso_now(),),
    )
    client.timeout = True
    intent = OrderIntent("DEMO", "SETTLED", "NO", "SELL", 1, 0.90, "MANUAL", "manual")
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)

    # This is the common restart path: the app already recorded Kalshi's
    # settlement, but the next reconciliation response no longer lists the
    # old market.
    db.execute(
        """
        INSERT INTO broker_settlements(mode,ticker,settled_at,raw_json)
        VALUES ('DEMO','SETTLED',?,'{}')
        """,
        (iso_now(),),
    )
    await broker.reconcile()

    row = db.fetch_one(
        "SELECT status,error FROM broker_order_intents WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row == {
        "status": "REJECTED",
        "error": "Kalshi did not report this timed-out order before the market settled.",
    }
    assert broker.readiness()["reconciliation_required"] is False


@run_async
async def test_reconcile_keeps_active_ambiguous_order_blocked(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    client.timeout = True
    intent = OrderIntent("DEMO", "ACTIVE", "YES", "BUY", 1, 0.40, "MANUAL", "manual")
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)

    await broker.reconcile()

    row = db.fetch_one(
        "SELECT status FROM broker_order_intents WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row == {"status": "RECONCILIATION_REQUIRED"}
    assert broker.readiness()["reconciliation_required"] is True


@run_async
async def test_reconcile_clears_unseen_ambiguous_order_when_market_closes(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    client.timeout = True
    intent = OrderIntent(
        "DEMO", "JUST-CLOSED", "YES", "BUY", 1, 0.40, "MANUAL", "manual"
    )
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)

    client.remote_markets["JUST-CLOSED"] = {
        "ticker": "JUST-CLOSED",
        "status": "closed",
    }
    await broker.reconcile()

    row = db.fetch_one(
        "SELECT status,error FROM broker_order_intents WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row == {
        "status": "REJECTED",
        "error": "Kalshi did not report this timed-out order before the market closed.",
    }
    assert broker.readiness()["reconciled"] is True


@run_async
async def test_reconcile_does_not_clear_closed_ambiguous_order_with_matching_fill(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    client.timeout = True
    intent = OrderIntent(
        "DEMO", "CLOSED-FILLED", "YES", "BUY", 1, 0.40, "MANUAL", "manual"
    )
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)
    client.remote_fills = [{
        "fill_id": "ambiguous-fill",
        "ticker": intent.ticker,
        "side": "bid",
        "count_fp": "1.00",
        "yes_price_dollars": "0.4000",
        "created_time": iso_now(),
    }]
    client.remote_markets[intent.ticker] = {
        "ticker": intent.ticker,
        "status": "closed",
    }

    await broker.reconcile()

    row = db.fetch_one(
        "SELECT status FROM broker_order_intents WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row == {"status": "RECONCILIATION_REQUIRED"}
    assert broker.readiness()["reconciled"] is False


@run_async
async def test_user_reconcile_reports_incomplete_instead_of_success(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    client.timeout = True
    intent = OrderIntent(
        "DEMO", "STILL-ACTIVE", "YES", "BUY", 1, 0.40, "MANUAL", "manual"
    )
    with pytest.raises(AmbiguousSubmissionError):
        await broker.submit(intent)

    with pytest.raises(ValueError, match="still requires reconciliation"):
        await coordinator.reconcile("DEMO")


@run_async
async def test_partial_fills_have_weighted_average_and_no_duplicates(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    intent = OrderIntent("DEMO", "TICKER", "YES", "BUY", 4, 0.50, "SWING", "automatic")
    await broker.submit(intent)
    client.remote_fills = [
        {"fill_id": "f1", "order_id": "order-1", "client_order_id": intent.client_order_id, "ticker": "TICKER", "side": "bid", "count": "1", "price": "0.40", "fee_cost_dollars": "0.01"},
        {"fill_id": "f2", "order_id": "order-1", "client_order_id": intent.client_order_id, "ticker": "TICKER", "side": "bid", "count": "2", "price": "0.50", "fee_cost_dollars": "0.02"},
    ]
    await broker.reconcile()
    await broker.reconcile()
    order = db.fetch_one("SELECT * FROM broker_orders WHERE exchange_order_id='order-1'")
    assert order and order["filled_contracts"] == pytest.approx(3)
    assert order["remaining_contracts"] == pytest.approx(1)
    assert order["average_fill_price"] == pytest.approx((0.4 + 1.0) / 3)
    assert db.fetch_one("SELECT COUNT(*) count FROM broker_fills")["count"] == 2


@run_async
async def test_allocation_caps_include_positions_resting_and_pending(tmp_path: Path) -> None:
    db, broker, _ = await ready_broker(tmp_path)
    db.update_settings({"demo_bankroll_cap_pct": 0.10})
    db.execute(
        "INSERT INTO broker_positions(mode,ticker,side,contracts,market_exposure,updated_at,status) VALUES ('DEMO','A','YES',10,40,?,'open')",
        (iso_now(),),
    )
    db.execute(
        "INSERT INTO broker_orders(mode,exchange_order_id,ticker,side,action,status,requested_contracts,remaining_contracts,limit_price,updated_at) VALUES ('DEMO','rest','B','YES','BUY','RESTING',10,10,.5,?)",
        (iso_now(),),
    )
    portfolio = broker.portfolio()
    assert portfolio["allocation_cap"] == pytest.approx(100)
    assert portfolio["allocated_capital"] == pytest.approx(45.175)
    assert portfolio["remaining_allocation"] == pytest.approx(54.825)
    db.update_settings({"demo_bankroll_cap_pct": 0.0})
    assert broker.portfolio()["remaining_allocation"] == 0


@run_async
async def test_every_new_exposure_limit_blocks_but_sell_exit_remains_possible(tmp_path: Path) -> None:
    db, broker, _ = await ready_broker(tmp_path)
    db.update_settings({"demo_max_amount_per_order": 0.10})
    buy = OrderIntent("DEMO", "T", "YES", "BUY", 1, 0.50, "MANUAL", "manual")
    assert broker.risk_check(buy)["passed"] is False
    db.execute(
        "INSERT INTO broker_positions(mode,ticker,side,contracts,market_exposure,updated_at,status) VALUES ('DEMO','T','YES',1,.5,?,'open')",
        (iso_now(),),
    )
    sell = OrderIntent("DEMO", "T", "YES", "SELL", 1, 0.40, "MANUAL", "stop_loss")
    assert broker.risk_check(sell)["passed"] is True


@run_async
async def test_kill_switch_disarms_and_attempts_resting_cancels(tmp_path: Path) -> None:
    _, broker, client = await ready_broker(tmp_path)
    await broker.submit(OrderIntent("DEMO", "T", "YES", "BUY", 1, 0.40, "MANUAL", "manual"))
    result = await broker.kill()
    assert result["active"] is True
    assert client.canceled == ["order-1"]
    assert client.cancel_tickers == ["T"]
    assert broker.readiness()["session_armed"] is False
    assert broker.readiness()["kill_switch"] is True


@run_async
async def test_demo_live_and_paper_history_are_isolated(tmp_path: Path) -> None:
    db, demo, _ = await ready_broker(tmp_path / "shared")
    live_client = FakeTradingClient()
    live = KalshiBroker("LIVE", db, live_client)  # type: ignore[arg-type]
    await live.reconcile()
    await demo.submit(OrderIntent("DEMO", "T", "YES", "BUY", 1, 0.40, "MANUAL", "manual"))
    assert len(demo.portfolio()["intents"]) == 1
    assert live.portfolio()["intents"] == []
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_trades")["count"] == 0


def test_additive_migration_preserves_existing_paper_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:10]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            "INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at) VALUES ('OLD','open','{}',?,?)",
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO paper_trades(
                ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                model_probability,market_probability,edge,expected_value,
                confidence,model_version,status,source
            ) VALUES ('OLD','NO',?,.4,2,.8,.01,.7,.4,.3,.29,'High','test','open','automatic')
            """,
            (now,),
        )
    db.initialize()
    assert db.fetch_one("SELECT side FROM paper_trades WHERE ticker='OLD'")["side"] == "NO"
    assert db.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 20
    assert db.fetch_one("SELECT COUNT(*) count FROM broker_order_intents")["count"] == 0


def test_storage_cleanup_removes_only_redundant_reconciliation_data(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "cleanup.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:18]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            "INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at) "
            "VALUES ('CLEAN','settled','{}',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO kalshi_snapshots(observed_at,ticker,orderbook_json) "
            "VALUES (?, 'CLEAN', '{\"yes\":[[40,2]]}')",
            (now,),
        )
        connection.execute(
            "INSERT INTO kalshi_trade_ticks("
            "observed_at,ticker,trade_id,contracts,is_block_trade,raw_json"
            ") VALUES (?, 'CLEAN', 'trade-1', 2, 0, '{\"trade_id\":\"trade-1\"}')",
            (now,),
        )
        connection.executemany(
            "INSERT INTO broker_audit_events("
            "mode,created_at,event_type,detail_json"
            ") VALUES ('LIVE',?,?,?)",
            [
                (
                    now,
                    "ORDER_STATE_CHANGED",
                    '{"from":"SETTLED","to":"FILLED"}',
                ),
                (now, "ARMED", '{"automatic":true}'),
            ],
        )

    db.initialize()

    assert db.fetch_one(
        "SELECT COUNT(*) count FROM broker_audit_events"
    )["count"] == 1
    assert db.fetch_one(
        "SELECT event_type FROM broker_audit_events"
    )["event_type"] == "ARMED"
    assert db.fetch_one(
        "SELECT orderbook_json FROM kalshi_snapshots"
    )["orderbook_json"] == "{}"
    assert db.fetch_one(
        "SELECT raw_json FROM kalshi_trade_ticks"
    )["raw_json"] == "{}"


@run_async
async def test_live_client_requires_explicit_runtime_authorization(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    client = KalshiTradingClient(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(201, json={}))),
        "https://external-api.kalshi.com/trade-api/v2",
        "live-key-id", key_path, environment="LIVE",
    )
    with pytest.raises(KalshiTradingError, match="not armed"):
        await client.create_order(
            ticker="T", client_order_id="c", side="YES", action="BUY",
            contracts=1, limit_price=.5,
        )
    await client.client.aclose()


def test_new_defaults_keep_exchange_automation_off_and_caps_at_100_percent() -> None:
    assert DEFAULT_SETTINGS["demo_bankroll_cap_pct"] == 1.0
    assert DEFAULT_SETTINGS["live_bankroll_cap_pct"] == 1.0
    assert DEFAULT_SETTINGS["demo_automatic_trading_enabled"] is False
    assert DEFAULT_SETTINGS["live_automatic_trading_enabled"] is False


def private_key(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "client-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@run_async
async def test_v2_order_payload_is_fixed_point_limit_and_post_only(tmp_path: Path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "order_id": "v2-order",
                "client_order_id": captured["client_order_id"],
                "fill_count": "0.00",
                "remaining_count": "2.00",
                "ts_ms": 1,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    await client.create_order(
        ticker="T", client_order_id="client-1", side="NO", action="BUY",
        contracts=2, limit_price=0.40, post_only=True,
    )
    assert captured["side"] == "ask"
    assert captured["price"] == "0.6000"
    assert captured["count"] == "2.00"
    assert captured["time_in_force"] == "good_till_canceled"
    assert captured["post_only"] is True
    assert captured["cancel_order_on_pause"] is True
    assert captured["exchange_index"] == -1
    await http.aclose()


@run_async
async def test_v2_protective_exit_payloads_use_the_yes_book_and_auto_route(
    tmp_path: Path,
) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(201, json={
            "order_id": f"exit-{len(captured)}",
            "client_order_id": payload["client_order_id"],
            "fill_count": "0.00",
            "remaining_count": "0.00",
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    for client_order_id, side, price in (
        ("close-yes", "YES", .46),
        ("close-no", "NO", .46),
    ):
        await client.create_order(
            ticker="T", client_order_id=client_order_id, side=side,
            action="SELL", contracts=2, limit_price=price, reduce_only=True,
            time_in_force="immediate_or_cancel", cancel_order_on_pause=False,
        )
    assert captured[0]["side"] == "ask"
    assert captured[0]["price"] == "0.4600"
    assert captured[1]["side"] == "bid"
    assert captured[1]["price"] == "0.5400"
    for payload in captured:
        assert payload["reduce_only"] is True
        assert payload["time_in_force"] == "immediate_or_cancel"
        assert payload["cancel_order_on_pause"] is False
        assert payload["exchange_index"] == -1
    await http.aclose()


@run_async
async def test_v2_order_payload_rounds_subcent_price_before_exchange(
    tmp_path: Path,
) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"order_id": "rounded"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    await client.create_order(
        ticker="T", client_order_id="rounded-client", side="NO", action="BUY",
        contracts=1, limit_price=0.8150000000000001,
    )
    assert captured["side"] == "ask"
    assert captured["price"] == "0.1800"
    await http.aclose()


@run_async
async def test_v2_cancel_auto_routes_by_market_ticker(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["market_ticker"] = request.url.params.get("market_ticker", "")
        return httpx.Response(200, json={"order_id": "order-1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http,
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id",
        private_key(tmp_path),
        environment="DEMO",
    )
    await client.cancel_order("order-1", market_ticker="KXBTC15M-TEST")
    assert captured["market_ticker"] == "KXBTC15M-TEST"
    await http.aclose()


@run_async
async def test_nested_exchange_error_is_actionable(tmp_path: Path) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                404,
                json={
                    "error": {
                        "code": "user_not_found",
                        "message": "user not found",
                    }
                },
            )
        )
    )
    client = KalshiTradingClient(
        http,
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id",
        private_key(tmp_path),
        environment="DEMO",
    )
    with pytest.raises(KalshiTradingError, match="no funds allocated") as error:
        await client.create_order(
            ticker="KXBTC15M-TEST",
            client_order_id="client-1",
            side="YES",
            action="BUY",
            contracts=1,
            limit_price=0.01,
        )
    assert error.value.code == "user_not_found"
    await http.aclose()


@run_async
async def test_paginated_reconciliation_reads_every_page(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor", "")
        calls.append(cursor)
        if not cursor:
            return httpx.Response(200, json={"orders": [{"order_id": "one"}], "cursor": "next"})
        return httpx.Response(200, json={"orders": [{"order_id": "two"}], "cursor": ""})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    payload = await client.orders()
    assert [row["order_id"] for row in payload["orders"]] == ["one", "two"]
    assert calls == ["", "next"]
    await http.aclose()


@run_async
async def test_rate_limit_backs_off_without_changing_client_order_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.kalshi_trading.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append(body["client_order_id"])
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(
            201,
            json={"order_id": "ok", "client_order_id": body["client_order_id"],
                  "fill_count": "0.00", "remaining_count": "1.00"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    await client.create_order(
        ticker="T", client_order_id="stable-id", side="YES", action="BUY",
        contracts=1, limit_price=0.40,
    )
    assert attempts == ["stable-id", "stable-id"]
    await http.aclose()


@run_async
async def test_server_error_on_submission_is_ambiguous(tmp_path: Path) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503, json={"message": "maintenance"})
        )
    )
    client = KalshiTradingClient(
        http, "https://external-api.demo.kalshi.co/trade-api/v2",
        "demo-key-id", private_key(tmp_path), environment="DEMO",
    )
    with pytest.raises(AmbiguousSubmissionError, match="reconciliation"):
        await client.create_order(
            ticker="T", client_order_id="ambiguous", side="YES", action="BUY",
            contracts=1, limit_price=0.40,
        )
    await http.aclose()


@run_async
async def test_automatic_order_rechecks_runtime_arming_at_submission(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    broker.set_automatic_armed(False)
    intent = OrderIntent(
        "DEMO", "T", "YES", "BUY", 1, 0.40,
        "STANDARD_EDGE", "automatic",
    )
    with pytest.raises(ValueError, match="disarmed"):
        await broker.submit(intent)
    assert client.created == []


@pytest.mark.parametrize(
    ("setting", "value", "risk_snapshot", "message"),
    [
        ("demo_bankroll_cap_pct", 0.0, {}, "remaining mode allocation"),
        ("demo_max_total_allocated_capital", 0.0, {}, "remaining mode allocation"),
        ("demo_max_amount_per_order", 0.10, {}, "maximum amount per order"),
        ("demo_max_exposure_per_market", 0.10, {}, "per-market exposure"),
        ("demo_max_total_open_exposure", 0.10, {}, "open-exposure"),
        ("demo_max_open_orders", 0, {}, "open-order count"),
        ("demo_max_daily_order_count", 0, {}, "daily order-count"),
        ("demo_max_entry_price", 0.30, {}, "entry price"),
        ("demo_max_spread", 0.01, {"spread": 0.02}, "spread"),
        ("demo_min_liquidity", 10, {"liquidity": 5}, "Liquidity"),
        ("demo_min_data_quality", "High", {"data_quality": "Moderate"}, "Data quality"),
    ],
)
@run_async
async def test_each_entry_hard_limit_blocks_submission(
    tmp_path: Path,
    setting: str,
    value: object,
    risk_snapshot: dict,
    message: str,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.update_settings({setting: value})
    intent = OrderIntent(
        "DEMO", "LIMIT", "YES", "BUY", 1, 0.40,
        "MANUAL", "manual", risk_snapshot={
            "data_reliable": True, "market_open": True,
            **risk_snapshot,
        },
    )
    with pytest.raises(ValueError, match=message):
        await broker.submit(intent)
    assert client.created == []


@run_async
async def test_rejected_intents_do_not_consume_daily_order_limit(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.update_settings({"demo_max_daily_order_count": 1})
    rejected = OrderIntent(
        "DEMO", "REJECTED", "YES", "BUY", 1, 0.40,
        "MANUAL", "manual", risk_snapshot={
            "data_reliable": False, "market_open": True,
        },
    )
    with pytest.raises(ValueError, match="stale or unreliable"):
        await broker.submit(rejected)
    assert broker.daily_order_count() == 0
    assert broker.risk_state()["daily_order_count"] == 0

    accepted = OrderIntent(
        "DEMO", "ACCEPTED", "YES", "BUY", 1, 0.40,
        "MANUAL", "manual", risk_snapshot={
            "data_reliable": True, "market_open": True,
        },
    )
    await broker.submit(accepted)
    assert broker.daily_order_count() == 1
    assert broker.risk_state()["primary_blocker"] == (
        "The daily order-count limit is active."
    )

    blocked = OrderIntent(
        "DEMO", "BLOCKED", "YES", "BUY", 1, 0.40,
        "MANUAL", "manual", risk_snapshot={
            "data_reliable": True, "market_open": True,
        },
    )
    with pytest.raises(ValueError, match="daily order-count"):
        await broker.submit(blocked)
    assert len(client.created) == 1
    assert broker.daily_order_count() == 1


@run_async
async def test_unfunded_exchange_shard_blocks_before_submission(tmp_path: Path) -> None:
    _, broker, client = await ready_broker(tmp_path)
    client.balance_payload = {
        "balance": 10000,
        "portfolio_value": 0,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": "100.0000"},
            {"exchange_index": 2, "balance": "0.0000"},
        ],
    }
    await broker.reconcile()
    intent = OrderIntent(
        "DEMO",
        "KXBTC15M-TEST",
        "YES",
        "BUY",
        1,
        0.40,
        "MANUAL",
        "manual",
        risk_snapshot={
            "data_reliable": True,
            "market_open": True,
            "exchange_index": 2,
        },
    )
    with pytest.raises(ValueError, match="exchange shard 2"):
        await broker.submit(intent)
    assert client.created == []
    assert broker.portfolio()["balance_breakdown"] == [
        {"exchange_index": 0, "balance": 100.0},
        {"exchange_index": 2, "balance": 0.0},
    ]


@run_async
async def test_demo_verification_explains_unfunded_market_shard(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.balance_payload = {
        "balance": 10000,
        "portfolio_value": 0,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": "100.0000"},
            {"exchange_index": 2, "balance": "0.0000"},
        ],
    }
    broker.set_client(client)  # type: ignore[arg-type]
    current = {
        "ticker": "KXBTC15M-TEST",
        "yes_ask": 0.50,
        "exchange_index": 2,
    }
    with pytest.raises(ValueError, match="Move mock funds to shard 2"):
        await coordinator.verify_demo(current, "VERIFY DEMO TRADING")
    assert client.created == []


@run_async
async def test_daily_realized_and_unrealized_loss_limit_blocks(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.update_settings({"demo_max_daily_loss": 50.0})
    db.execute(
        """
        INSERT INTO broker_account_snapshots(
            mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
        ) VALUES ('DEMO',?,900,0,0,'{}')
        """,
        (iso_now(),),
    )
    intent = OrderIntent(
        "DEMO", "LOSS", "YES", "BUY", 1, 0.40,
        "MANUAL", "manual", risk_snapshot={
            "data_reliable": True, "market_open": True,
        },
    )
    with pytest.raises(ValueError, match="realized and unrealized"):
        await broker.submit(intent)
    assert client.created == []


@run_async
async def test_manual_preview_rechecks_latest_market_before_confirm(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    paper = PaperTradingService(db)
    coordinator = TradingCoordinator(AppConfig(database_path=db.path), db, paper)
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    db.update_settings({"trading_mode": "DEMO", "demo_min_data_quality": "Low"})
    current = {
        "ticker": "CURRENT",
        "status": "active",
        "data_quality": {"reliable": True},
        "trade_assessments": {
            "YES": {
                "buy": {"executable_price": 0.40}, "sell": {"executable_price": 0.38},
                "spread": 0.02, "ask_size": 100,
                "data_reliable": True, "trade_allowed": True,
            }
        },
        "trade_decisions": {"YES": {"confidence": "Moderate"}},
    }
    preview = coordinator.preview_manual(
        "DEMO", {"side": "YES", "action": "BUY", "dollars": 10}, current
    )
    stale = {
        **current,
        "data_quality": {"reliable": False},
        "trade_assessments": {
            "YES": {**current["trade_assessments"]["YES"], "data_reliable": False}
        },
    }
    with pytest.raises(ValueError, match="stale or unreliable"):
        await coordinator.confirm_manual(
            preview["confirmation_token"], "DEMO", stale
        )
    assert client.created == []


@run_async
async def test_manual_custom_limit_can_rest_when_demo_ask_is_missing(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    db.update_settings({"trading_mode": "DEMO"})
    current = {
        "ticker": "SPARSE-DEMO",
        "status": "active",
        "exchange_index": None,
        "data_quality": {
            "reliable": True,
            "trade_allowed": True,
            "reason": "critical feeds are current and mutually consistent",
        },
        "trade_assessments": {
            "YES": {
                "buy": {"executable_price": None},
                "sell": {"executable_price": 0.47},
                "spread": 0.20,
                "ask_size": 1,
                "data_reliable": False,
                "trade_allowed": False,
                "quality_reason": "The Demo order book has no executable Up ask.",
            }
        },
        "trade_decisions": {"YES": {"confidence": "Low"}},
    }

    preview = coordinator.preview_manual(
        "DEMO",
        {
            "side": "YES",
            "action": "BUY",
            "contracts": 1,
            "limit_price_cents": 50,
        },
        current,
    )

    assert preview["risk"]["passed"] is True
    submitted = await coordinator.confirm_manual(
        preview["confirmation_token"], "DEMO", current
    )
    assert submitted["status"] == "ACKNOWLEDGED"
    assert client.created[0]["limit_price"] == pytest.approx(0.50)


@run_async
async def test_demo_automation_resumes_after_private_stream_reconnect(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    broker.set_client(FakeTradingClient())  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)

    await coordinator._handle_private_stream_status(
        "DEMO", False, "temporary disconnect"
    )
    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert broker.readiness()["ready_for_automatic"] is False

    await coordinator._handle_private_stream_status("DEMO", True, None)
    assert broker.readiness()["ready_for_automatic"] is True
    events = broker.audit_history(10)
    assert any(
        event["event_type"] == "PRIVATE_STREAM_RESUMED"
        and event["detail"]["automatic_resumed"] is True
        for event in events
    )


@run_async
async def test_live_automation_resumes_after_private_stream_reconnect(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("LIVE")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.environment = "LIVE"
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    db.execute(
        "UPDATE broker_mode_state SET demo_verified_at=?,limits_reviewed_at=? WHERE mode='LIVE'",
        (iso_now(), iso_now()),
    )
    broker.arm(confirmation="ARM LIVE TRADING", automatic=True)

    await coordinator._handle_private_stream_status(
        "LIVE", False, "temporary disconnect"
    )
    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert broker.readiness()["ready_for_automatic"] is False

    await coordinator._handle_private_stream_status("LIVE", True, None)
    assert broker.readiness()["ready_for_automatic"] is True
    events = broker.audit_history(10)
    assert any(
        event["event_type"] == "PRIVATE_STREAM_RESUMED"
        and event["detail"]["automatic_resumed"] is True
        for event in events
    )


@run_async
async def test_live_reconciliation_failure_pauses_then_resumes_trading(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("LIVE")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.environment = "LIVE"
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    db.execute(
        "UPDATE broker_mode_state SET demo_verified_at=?,limits_reviewed_at=? WHERE mode='LIVE'",
        (iso_now(), iso_now()),
    )
    broker.arm(confirmation="ARM LIVE TRADING", automatic=True)
    await coordinator._handle_private_stream_status(
        "LIVE", False, "temporary disconnect"
    )

    healthy_balance = client.balance

    async def failed_balance():
        raise KalshiTradingError("reconciliation unavailable")

    client.balance = failed_balance  # type: ignore[method-assign]
    await coordinator._handle_private_stream_status("LIVE", True, None)
    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert broker.readiness()["ready_for_automatic"] is False
    assert broker.mode_state()["last_error"] == "reconciliation unavailable"

    client.balance = healthy_balance  # type: ignore[method-assign]
    await coordinator._handle_private_stream_status("LIVE", True, None)
    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert broker.readiness()["ready_for_automatic"] is True
    events = broker.audit_history(20)
    assert any(event["event_type"] == "RECONCILIATION_PAUSED" for event in events)
    assert any(
        event["event_type"] == "RECONCILIATION_RESUMED"
        and event["detail"]["automatic_resumed"] is True
        for event in events
    )


@run_async
async def test_degraded_reconciliation_blocks_entries_but_uses_targeted_protective_exit(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.remote_positions = [{
        "ticker": "DEGRADED-EXIT", "position_fp": "3.00",
        "market_exposure_dollars": "1.50", "last_updated_ts": iso_now(),
    }]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    db.update_settings({"global_profit_take_enabled": True, "global_profit_take_price": .99})

    async def failed_balance():
        raise KalshiTradingError("account reconciliation unavailable")

    client.balance = failed_balance  # type: ignore[method-assign]
    with pytest.raises(KalshiTradingError):
        await broker.reconcile()
    readiness = broker.readiness()
    assert readiness["ready_for_automatic"] is False
    assert readiness["ready_for_protective_exit"] is True
    assert readiness["protective_exit_degraded"] is True

    await coordinator._process_exits(
        broker,
        {
            "ticker": "DEGRADED-EXIT", "status": "active", "observed_at": iso_now(),
            "time_remaining_seconds": 100, "yes_bid": .99, "no_bid": .01,
            "data_quality": {"reliable": True},
        },
    )
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 1
    assert client.created[0]["reduce_only"] is True
    assert client.created[0]["contracts"] == 3
    intent = db.fetch_one(
        "SELECT decision_snapshot_json FROM broker_order_intents WHERE ticker='DEGRADED-EXIT'"
    ) or {}
    evidence = json.loads(intent["decision_snapshot_json"])
    assert evidence["protective_exit_quantity_source"] == "targeted_exchange_position"
    assert evidence["protective_exit_degraded"] is True
    entry = OrderIntent(
        "DEMO", "NEW-ENTRY", "YES", "BUY", 1, .40, "STANDARD_EDGE", "automatic",
        risk_snapshot={"data_reliable": True, "market_open": True},
    )
    assert broker.risk_check(entry)["passed"] is False
    assert broker.portfolio()["protective_exit_state"]["degraded"] is True


@run_async
async def test_protective_exit_never_retries_while_prior_client_id_is_uncertain(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    client.remote_positions = [{
        "ticker": "UNCERTAIN-EXIT", "position_fp": "4.00",
        "market_exposure_dollars": "2.00",
    }]
    db.execute(
        """
        INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at
        ) VALUES ('DEMO','prior-exit','UNCERTAIN-EXIT','YES','SELL',4,.01,
                  'RECONCILIATION_REQUIRED','MANUAL','stop_loss',?,?)
        """,
        (iso_now(), iso_now()),
    )
    protected = OrderIntent(
        "DEMO", "UNCERTAIN-EXIT", "YES", "SELL", 4, .01, "MANUAL", "stop_loss",
        decision_snapshot={"protective_exit": True},
    )
    with pytest.raises(ValueError, match="unresolved client ID"):
        await broker.submit(protected)
    assert client.created == []


@run_async
async def test_protective_exit_uses_confirmed_fill_evidence_when_targeted_lookup_fails(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.execute(
        """
        INSERT INTO broker_fills(
            mode,fill_id,ticker,side,action,contracts,price,fee,filled_at
        ) VALUES ('DEMO','durable-buy','DURABLE-EXIT','NO','BUY',2,.40,0,?)
        """,
        (iso_now(),),
    )

    async def failed_positions(**_: object):
        raise KalshiTradingError("targeted position lookup unavailable")

    client.positions = failed_positions  # type: ignore[method-assign]
    protected = OrderIntent(
        "DEMO", "DURABLE-EXIT", "NO", "SELL", 9, .01, "MANUAL", "stop_loss",
        decision_snapshot={"protective_exit": True},
    )
    result = await broker.submit(protected)
    assert result["requested_contracts"] == 2
    assert client.created[0]["contracts"] == 2
    evidence = json.loads(result["decision_snapshot_json"])
    assert evidence["protective_exit_quantity_source"] == "durable_confirmed_fills"


@run_async
async def test_overlapping_reconciliations_are_coalesced(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    client = FakeTradingClient()
    broker = KalshiBroker("DEMO", db, client)  # type: ignore[arg-type]
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_balance():
        client.balance_calls += 1
        started.set()
        await release.wait()
        return dict(client.balance_payload)

    client.balance = slow_balance  # type: ignore[method-assign]
    tasks = [asyncio.create_task(broker.reconcile()) for _ in range(6)]
    await started.wait()
    release.set()
    await asyncio.gather(*tasks)

    assert client.balance_calls == 1
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM broker_audit_events "
        "WHERE mode='DEMO' AND event_type='RECONCILED'"
    )["count"] == 1


@run_async
async def test_settled_order_does_not_regress_or_generate_reconcile_churn(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    intent = OrderIntent(
        "DEMO", "SETTLED-STABLE", "YES", "BUY", 1, .40,
        "STANDARD_EDGE", "automatic",
    )
    await broker.submit(intent)
    client.remote_orders[0].update(
        {"status": "executed", "fill_count": "1.00", "remaining_count": "0.00"}
    )
    client.remote_settlements = [{
        "ticker": "SETTLED-STABLE",
        "market_result": "YES",
        "settled_time": "2026-08-30T12:15:00+00:00",
        "revenue_dollars": "1.00",
    }]

    await broker.reconcile()
    assert db.fetch_one(
        "SELECT status FROM broker_orders WHERE mode='DEMO' AND ticker='SETTLED-STABLE'"
    )["status"] == "SETTLED"
    assert db.fetch_one(
        "SELECT status FROM broker_order_intents "
        "WHERE mode='DEMO' AND ticker='SETTLED-STABLE'"
    )["status"] == "SETTLED"

    await broker.reconcile()
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM broker_audit_events "
        "WHERE mode='DEMO' AND event_type IN "
        "('ORDER_STATE_CHANGED','INTENT_STATE_CHANGED') "
        "AND json_extract(detail_json,'$.from')='SETTLED' "
        "AND json_extract(detail_json,'$.to')='FILLED'"
    )["count"] == 0


@run_async
async def test_all_four_automatic_strategies_preserve_strategy_on_intents(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    paper = PaperTradingService(db)
    coordinator = TradingCoordinator(AppConfig(database_path=db.path), db, paper)
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    db.update_settings({
        "trading_mode": "DEMO", "demo_automatic_trading_enabled": True,
        "demo_min_data_quality": "Low",
    })
    assessment = {
        "side": "YES", "buy": {"executable_price": 0.40},
        "spread": 0.01, "ask_size": 100,
        "data_reliable": True, "trade_allowed": True,
        "decision_confidence": "Moderate",
    }
    for index, strategy in enumerate(
        ("STANDARD_EDGE", "EARLY_THRESHOLD", "LATE_CONVICTION", "SWING")
    ):
        entered, _ = coordinator.submit_automatic(
            strategy=strategy, ticker=f"T-{index}", assessment=assessment,
            bankroll_fraction=0.01, model_version="test", reason="fixture",
        )
        assert entered is True
        await asyncio.gather(*list(coordinator._submission_tasks))
    rows = db.fetch_all(
        "SELECT strategy FROM broker_order_intents WHERE mode='DEMO' ORDER BY ticker"
    )
    assert {row["strategy"] for row in rows} == {
        "STANDARD_EDGE", "EARLY_THRESHOLD", "LATE_CONVICTION", "SWING",
    }


def test_profit_take_has_priority_and_disabled_profit_take_yields_swing() -> None:
    position = {
        "strategy": "SWING", "stop_loss_price": 0.99,
        "target_exit_price": 0.10, "fallback_exit_mode": "Exit",
        "fallback_exit_seconds": 120,
    }
    reason, priority = protective_exit_reason(
        position, 0.99, 60,
        {"global_profit_take_enabled": True, "global_profit_take_price": 0.99},
    )
    assert (reason, priority) == ("GLOBAL_PROFIT_TAKE", 0)
    reason, priority = protective_exit_reason(
        {**position, "stop_loss_price": None}, 0.99, 60,
        {"global_profit_take_enabled": False, "global_profit_take_price": 0.99},
    )
    assert (reason, priority) == ("SWING_TARGET", 2)


def test_fill_aggregate_supports_partial_exit_without_double_counting() -> None:
    first = fill_aggregate(
        [{"contracts": 2, "price": 0.40, "fee": 0.01}], 5
    )
    repeated = fill_aggregate(
        [{"contracts": 2, "price": 0.40, "fee": 0.01}], 5
    )
    assert first == repeated
    assert first["status"] == "PARTIALLY_FILLED"
    assert first["remaining_contracts"] == pytest.approx(3)


@run_async
async def test_reconcile_preserves_authoritative_canceled_partial_order(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    client = FakeTradingClient()
    client.remote_orders = [{
        "order_id": "canceled-partial", "ticker": "OLD", "action": "buy",
        "side": "yes", "outcome_side": "yes", "book_side": "bid",
        "status": "canceled", "initial_count_fp": "10.00",
        "fill_count_fp": "4.00", "remaining_count_fp": "0.00",
        "yes_price_dollars": "0.4000", "no_price_dollars": "0.6000",
        "last_update_time": "2026-08-01T00:00:00Z",
    }]
    client.remote_fills = [{
        "fill_id": "partial-fill", "order_id": "canceled-partial",
        "ticker": "OLD", "action": "buy", "side": "yes",
        "outcome_side": "yes", "book_side": "bid", "count_fp": "4.00",
        "yes_price_dollars": "0.4000", "no_price_dollars": "0.6000",
    }]
    broker = KalshiBroker("LIVE", db, client)  # type: ignore[arg-type]

    portfolio = await broker.reconcile()

    order = db.fetch_one(
        "SELECT * FROM broker_orders WHERE exchange_order_id='canceled-partial'"
    )
    assert order and order["status"] == "CANCELED"
    assert order["remaining_contracts"] == pytest.approx(0)
    assert order["updated_at"] == "2026-08-01T00:00:00Z"
    assert portfolio["open_order_count"] == 0
    assert portfolio["allocated_capital"] == pytest.approx(0)


@run_async
async def test_reconcile_pairs_external_closing_fill_with_original_contract_side(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    client = FakeTradingClient()
    client.remote_orders = [
        {
            "order_id": "entry", "ticker": "ROUND-TRIP", "action": "buy",
            "side": "yes", "outcome_side": "yes", "book_side": "bid",
            "status": "executed", "initial_count_fp": "10.00",
            "fill_count_fp": "10.00", "remaining_count_fp": "0.00",
            "yes_price_dollars": "0.1500", "no_price_dollars": "0.8500",
            "created_time": "2026-08-01T00:00:00Z",
            "last_update_time": "2026-08-01T00:01:00Z",
        },
        {
            "order_id": "exit", "ticker": "ROUND-TRIP", "action": "sell",
            "side": "yes", "outcome_side": "no", "book_side": "ask",
            "status": "executed", "initial_count_fp": "10.00",
            "fill_count_fp": "10.00", "remaining_count_fp": "0.00",
            "yes_price_dollars": "0.1100", "no_price_dollars": "0.8900",
            "created_time": "2026-08-02T00:00:00Z",
            "last_update_time": "2026-08-02T00:01:00Z",
        },
    ]
    client.remote_fills = [
        {
            "fill_id": "entry-fill", "order_id": "entry", "ticker": "ROUND-TRIP",
            "action": "buy", "side": "yes", "outcome_side": "yes",
            "book_side": "bid", "count_fp": "10.00",
            "yes_price_dollars": "0.1500", "no_price_dollars": "0.8500",
            "fee_cost_dollars": "0.0100", "created_time": "2026-08-01T00:00:00Z",
        },
        {
            "fill_id": "exit-fill", "order_id": "exit", "ticker": "ROUND-TRIP",
            "action": "sell", "side": "yes", "outcome_side": "no",
            "book_side": "ask", "count_fp": "10.00",
            "yes_price_dollars": "0.1100", "no_price_dollars": "0.8900",
            "fee_cost_dollars": "0.0200", "created_time": "2026-08-02T00:00:00Z",
        },
    ]
    client.remote_settlements = [{
        "ticker": "ROUND-TRIP", "market_result": "no",
        "yes_count_fp": "10.00", "no_count_fp": "10.00",
        "settled_time": "2026-08-03T00:00:00Z",
    }]
    broker = KalshiBroker("LIVE", db, client)  # type: ignore[arg-type]

    await broker.reconcile()
    ledger = broker.trade_ledger()

    assert len(ledger) == 1
    assert ledger[0]["side"] == "YES"
    assert ledger[0]["status"] == "CLOSED"
    assert ledger[0]["strategy"] == "EXTERNAL"
    assert ledger[0]["source"] == "external"
    assert ledger[0]["realized_pnl"] == pytest.approx(-0.43)
    assert ledger[0]["position_won"] is None
    entry = db.fetch_one(
        "SELECT updated_at FROM broker_orders WHERE exchange_order_id='entry'"
    )
    exit_order = db.fetch_one(
        "SELECT updated_at FROM broker_orders WHERE exchange_order_id='exit'"
    )
    assert entry and entry["updated_at"] == "2026-08-03T00:00:00Z"
    assert exit_order and exit_order["updated_at"] == "2026-08-02T00:01:00Z"


def test_exchange_strategy_ledger_counts_winning_down_settlement(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    broker = KalshiBroker("DEMO", db, FakeTradingClient())  # type: ignore[arg-type]
    now = iso_now()
    db.execute(
        """
        INSERT INTO broker_fills(
            mode,fill_id,client_order_id,ticker,side,action,contracts,price,fee,
            strategy,source,filled_at,raw_json
        ) VALUES ('DEMO','buy-down','entry','DOWN-WIN','NO','BUY',10,.2,.02,
                  'STANDARD_EDGE','automatic',?,'{}')
        """,
        (now,),
    )
    db.execute(
        """
        INSERT INTO broker_settlements(
            mode,ticker,side,settled_at,market_result,position_won,realized_pnl,fees,raw_json
        ) VALUES ('DEMO','DOWN-WIN','NO',?,'NO',1,7.98,.02,'{}')
        """,
        (now,),
    )
    result = broker.strategy_results()["STANDARD_EDGE"]
    assert result["completed_trades"] == 1
    assert result["wins"] == 1
    assert result["win_rate"] == pytest.approx(1.0)
    assert result["realized_pnl"] == pytest.approx(7.98)
    assert result["actual_fees"] == pytest.approx(.02)


def test_exchange_trade_ledger_aggregates_settlement_and_available_cash(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    broker = KalshiBroker("DEMO", db, FakeTradingClient())  # type: ignore[arg-type]
    now = iso_now()
    db.execute(
        """
        INSERT INTO markets(ticker,status,strike,raw_json,first_seen_at,updated_at)
        VALUES (?,?,?,?,?,?)
        """,
        ("SETTLED", "finalized", 100.0, "{}", now, now),
    )
    db.execute(
        """
        INSERT INTO settlements(
            ticker,settled_at,result,settlement_value,raw_json,processed_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "SETTLED",
            now,
            1,
            0.0,
            '{"expiration_value":"91.25"}',
            now,
        ),
    )
    db.execute(
        """
        INSERT INTO broker_fills(
            mode,fill_id,ticker,side,action,contracts,price,fee,strategy,source,
            filled_at,raw_json,available_cash_after
        ) VALUES
            ('DEMO','fill-1','SETTLED','NO','BUY',1,.53,.0175,'MANUAL','manual',
             '2026-08-28T03:51:49Z','{}',99.4525),
            ('DEMO','fill-2','SETTLED','NO','BUY',1,.53,.0175,'MANUAL','manual',
             '2026-08-28T03:54:15Z','{}',98.905)
        """
    )
    db.execute(
        """
        INSERT INTO broker_settlements(
            mode,ticker,side,settled_at,market_result,position_won,realized_pnl,
            fees,raw_json,available_cash_after
        ) VALUES ('DEMO','SETTLED','YES','2026-08-28T04:00:18Z','YES',0,-1.095,
                  .035,'{}',98.905)
        """
    )

    ledger = broker.trade_ledger()

    assert len(ledger) == 1
    assert ledger[0]["status"] == "SETTLED"
    assert ledger[0]["contracts"] == pytest.approx(2)
    assert ledger[0]["price"] == pytest.approx(.53)
    assert ledger[0]["realized_pnl"] == pytest.approx(-1.095)
    assert ledger[0]["available_cash_after"] == pytest.approx(98.905)
    assert ledger[0]["settlement_margin"] == pytest.approx(-8.75)
    assert broker.portfolio()["ledger"] == ledger


def test_broker_cash_migration_preserves_history_and_backfills_nearby_snapshot(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "broker-legacy.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:11]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        connection.execute(
            """
            INSERT INTO broker_fills(
                mode,fill_id,ticker,side,action,contracts,price,fee,filled_at,raw_json
            ) VALUES ('DEMO','legacy-fill','LEGACY','YES','BUY',1,.4,.01,
                      '2026-08-28T03:00:00Z','{}')
            """
        )
        connection.execute(
            """
            INSERT INTO broker_settlements(
                mode,ticker,side,settled_at,market_result,position_won,realized_pnl,
                fees,raw_json
            ) VALUES ('DEMO','LEGACY','YES','2026-08-28T03:15:00Z','YES',1,.59,
                      .01,'{}')
            """
        )
        connection.executemany(
            """
            INSERT INTO broker_account_snapshots(
                mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
            ) VALUES ('DEMO',?,?,0,0,'{}')
            """,
            [
                ("2026-08-28T03:00:10Z", 99.59),
                ("2026-08-28T03:15:12Z", 100.59),
            ],
        )

    db.initialize()

    fill = db.fetch_one("SELECT * FROM broker_fills WHERE fill_id='legacy-fill'")
    settlement = db.fetch_one(
        "SELECT * FROM broker_settlements WHERE ticker='LEGACY'"
    )
    assert fill and fill["available_cash_after"] == pytest.approx(99.59)
    assert settlement and settlement["available_cash_after"] == pytest.approx(100.59)
    assert db.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 20


@run_async
async def test_profit_take_is_managed_when_another_mode_is_selected(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.remote_positions = [
        {
            "ticker": "PROFIT", "position_fp": "10.00",
            "market_exposure_dollars": "2.00", "realized_pnl_dollars": "0.00",
            "fees_paid_dollars": "0.02",
        }
    ]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    db.execute(
        """
        UPDATE broker_positions SET strategy='SWING',target_exit_price=.10,
            fallback_exit_mode='Exit',fallback_exit_seconds=120
        WHERE mode='DEMO' AND ticker='PROFIT' AND side='YES'
        """
    )
    db.update_settings({"trading_mode": "PAPER", "global_profit_take_enabled": True})
    current = {
        "ticker": "PROFIT", "status": "active", "observed_at": iso_now(),
        "time_remaining_seconds": 300, "yes_bid": 0.99, "no_bid": 0.01,
        "trade_assessments": {}, "trade_decisions": {},
    }
    await coordinator.process(current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    intents = db.fetch_all(
        "SELECT * FROM broker_order_intents WHERE mode='DEMO' AND action='SELL'"
    )
    assert len(intents) == 1
    assert intents[0]["source"] == "global_profit_take"
    assert intents[0]["requested_contracts"] == 10
    assert intents[0]["limit_price"] == pytest.approx(0.98)
    await coordinator.process(current)
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM broker_order_intents WHERE mode='DEMO' AND action='SELL'"
    )["count"] == 1


@run_async
async def test_rejected_protective_exit_reconciles_and_backs_off(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.remote_positions = [{
        "ticker": "EXIT-RETRY", "position_fp": "10.00",
        "market_exposure_dollars": "2.00", "realized_pnl_dollars": "0.00",
        "fees_paid_dollars": "0.02",
    }]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    client.reject_sells = True
    current = {
        "ticker": "EXIT-RETRY", "status": "active", "observed_at": iso_now(),
        "time_remaining_seconds": 300, "yes_bid": 0.99, "no_bid": 0.01,
        "exchange_index": 2,
        "price_ranges": [
            {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
            {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
            {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
        ],
        "trade_assessments": {}, "trade_decisions": {},
    }

    await coordinator.process(current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert client.balance_calls == 2
    assert len(client.created) == 1
    assert client.created[0]["exchange_index"] == 2

    for _ in range(3):
        await coordinator.process(current)
        await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 1
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM broker_order_intents "
        "WHERE mode='DEMO' AND ticker='EXIT-RETRY' AND action='SELL'"
    )["count"] == 1


@run_async
async def test_timed_out_automatic_remainder_is_canceled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    db.update_settings({"trading_mode": "DEMO", "demo_automatic_trading_enabled": True})
    intent = OrderIntent(
        "DEMO", "TIMEOUT", "YES", "BUY", 2, 0.40,
        "STANDARD_EDGE", "automatic", cancel_after_seconds=-1,
        risk_snapshot={
            "data_reliable": True, "market_open": True,
            "data_quality": "Moderate", "liquidity": 100, "spread": 0.01,
        },
    )
    await broker.submit(intent)
    current = {
        "ticker": "TIMEOUT", "status": "active", "observed_at": iso_now(),
        "time_remaining_seconds": 300, "yes_bid": 0.38, "no_bid": 0.60,
        "trade_assessments": {
            "YES": {"data_reliable": True, "trade_allowed": True}
        },
        "trade_decisions": {"YES": {"signal": "BUY"}},
    }
    await coordinator.process(current)
    assert client.canceled == ["order-1"]


@run_async
async def test_fill_wins_a_cancel_race_and_reconciliation_is_authoritative(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    intent = OrderIntent("DEMO", "RACE", "YES", "BUY", 1, 0.40, "MANUAL", "manual")
    result = await broker.submit(intent)

    async def filled_during_cancel(
        _: str,
        *,
        market_ticker: str | None = None,
    ):
        assert market_ticker == "RACE"
        client.remote_orders[0].update(
            {"status": "executed", "fill_count": "1.00", "remaining_count": "0.00"}
        )
        client.remote_fills = [
            {
                "fill_id": "race-fill", "order_id": "order-1",
                "client_order_id": intent.client_order_id, "ticker": "RACE",
                "outcome_side": "yes", "book_side": "bid", "action": "buy",
                "count_fp": "1.00", "yes_price_dollars": "0.4000",
                "fee_cost": "0.0100",
            }
        ]
        raise KalshiTradingError("cancel lost the race")

    client.cancel_order = filled_during_cancel  # type: ignore[method-assign]
    with pytest.raises(KalshiTradingError):
        await broker.cancel(str(result["exchange_order_id"]))
    order = db.fetch_one("SELECT * FROM broker_orders WHERE exchange_order_id='order-1'")
    assert order and order["status"] == "FILLED"
    assert order["filled_contracts"] == pytest.approx(1)
    fill = db.fetch_one("SELECT * FROM broker_fills WHERE fill_id='race-fill'")
    assert fill and fill["fee"] == pytest.approx(0.01)


@run_async
async def test_restart_reconciliation_resolves_persisted_submitting_intent(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    now = iso_now()
    db.execute(
        """
        INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at
        ) VALUES ('DEMO','restart-id','RESTART','YES','BUY',1,.4,
                  'SUBMITTING','STANDARD_EDGE','automatic',?,?)
        """,
        (now, now),
    )
    client = FakeTradingClient()
    client.remote_orders = [
        {
            "order_id": "remote-restart", "client_order_id": "restart-id",
            "ticker": "RESTART", "outcome_side": "yes", "action": "buy",
            "yes_price_dollars": "0.4000", "initial_count_fp": "1.00",
            "fill_count_fp": "0.00", "remaining_count_fp": "1.00",
            "status": "resting",
        }
    ]
    broker = KalshiBroker("DEMO", db, client)  # type: ignore[arg-type]
    await broker.reconcile()
    row = db.fetch_one(
        "SELECT * FROM broker_order_intents WHERE client_order_id='restart-id'"
    )
    assert row and row["status"] == "RESTING"
    assert row["exchange_order_id"] == "remote-restart"
    assert broker.readiness()["reconciliation_required"] is False


@run_async
async def test_account_equity_and_cap_do_not_double_count_available_cash(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)

    async def balance():
        return {"balance": 60_000, "portfolio_value": 100_000}

    client.balance = balance  # type: ignore[method-assign]
    await broker.reconcile()
    portfolio = broker.portfolio()
    assert portfolio["available_cash"] == pytest.approx(600)
    assert portfolio["current_bankroll"] == pytest.approx(1000)
    assert portfolio["allocation_cap"] == pytest.approx(1000)


@run_async
async def test_available_balance_and_existing_market_reservations_are_hard_limits(
    tmp_path: Path,
) -> None:
    db, broker, _ = await ready_broker(tmp_path)
    db.execute(
        """
        INSERT INTO broker_account_snapshots(
            mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
        ) VALUES ('DEMO',?,.25,1000,0,'{}')
        """,
        (iso_now(),),
    )
    too_much_cash = OrderIntent(
        "DEMO", "CASH", "YES", "BUY", 1, .40, "MANUAL", "manual"
    )
    assert "available account balance" in broker.risk_check(
        too_much_cash
    )["failures"][-1]

    db.execute(
        """
        INSERT INTO broker_account_snapshots(
            mode,observed_at,available_balance,portfolio_value,allocated_capital,raw_json
        ) VALUES ('DEMO',?,1000,1000,0,'{}')
        """,
        (iso_now(),),
    )
    db.update_settings({"demo_max_exposure_per_market": 1.0})
    db.execute(
        """
        INSERT INTO broker_orders(
            mode,exchange_order_id,ticker,side,action,status,requested_contracts,
            remaining_contracts,limit_price,updated_at
        ) VALUES ('DEMO','reserved','MARKET','YES','BUY','RESTING',2,2,.4,?)
        """,
        (iso_now(),),
    )
    next_order = OrderIntent(
        "DEMO", "MARKET", "YES", "BUY", 1, .40, "MANUAL", "manual"
    )
    assert any(
        "per-market exposure" in reason
        for reason in broker.risk_check(next_order)["failures"]
    )


@run_async
async def test_no_fill_uses_no_price_and_all_fees_are_totaled(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    intent = OrderIntent("DEMO", "NO-FILL", "NO", "BUY", 1, .40, "MANUAL", "manual")
    await broker.submit(intent)
    client.remote_fills = [
        {
            "fill_id": f"fill-{index}",
            "order_id": "order-1",
            "ticker": "NO-FILL",
            "outcome_side": "no",
            "book_side": "ask",
            "action": "buy",
            "count_fp": "1.00",
            "yes_price_dollars": "0.6000",
            "no_price_dollars": "0.4000",
            "fee_cost": "0.0100",
        }
        for index in range(101)
    ]
    await broker.reconcile()
    first = db.fetch_one("SELECT price FROM broker_fills WHERE fill_id='fill-0'")
    assert first and first["price"] == pytest.approx(.4)
    assert len(broker.portfolio()["fills"]) == 100
    assert broker.portfolio()["actual_fees"] == pytest.approx(1.01)


def test_profit_take_thresholds_and_disabled_state() -> None:
    position = {"strategy": "STANDARD_EDGE", "stop_loss_price": None}
    settings = {
        "global_profit_take_enabled": True,
        "global_profit_take_price": .99,
    }
    assert protective_exit_reason(position, .98, 30, settings)[0] is None
    assert protective_exit_reason(position, .99, 30, settings)[0] == "GLOBAL_PROFIT_TAKE"
    assert protective_exit_reason(position, 1.0, 30, settings)[0] == "GLOBAL_PROFIT_TAKE"
    assert protective_exit_reason(
        position, 1.0, 30, {**settings, "global_profit_take_enabled": False}
    )[0] is None


def test_threshold_breach_reason_is_side_aware_and_requires_reliable_data() -> None:
    settings = {
        "global_profit_take_enabled": False,
        "threshold_breach_exit_enabled": True,
        "threshold_breach_exit_buffer_dollars": 2.0,
    }
    assert protective_exit_reason(
        {"side": "YES"}, .50, 30, settings,
        btc_proxy=98.0, threshold=100.0, data_reliable=True,
    )[0] == "THRESHOLD_BREACH_EXIT"
    assert protective_exit_reason(
        {"side": "NO"}, .50, 30, settings,
        btc_proxy=102.0, threshold=100.0, data_reliable=True,
    )[0] == "THRESHOLD_BREACH_EXIT"
    assert protective_exit_reason(
        {"side": "YES"}, .50, 30, settings,
        btc_proxy=97.0, threshold=100.0, data_reliable=False,
    )[0] is None


@pytest.mark.parametrize(
    ("mode", "side", "signed_position", "btc_proxy"),
    [("DEMO", "YES", "4.00", 99.0), ("LIVE", "NO", "-4.00", 101.0)],
)
@run_async
async def test_breached_exchange_position_uses_reduce_only_exit_once_after_arming(
    tmp_path: Path,
    mode: str,
    side: str,
    signed_position: str,
    btc_proxy: float,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker(mode)
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.environment = mode
    client.remote_positions = [{
        "ticker": "BREACHED", "position_fp": signed_position,
        "market_exposure_dollars": "2.00", "last_updated_ts": iso_now(),
    }]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    db.update_settings({
        "global_profit_take_enabled": False,
        "threshold_breach_exit_enabled": True,
        "threshold_breach_exit_buffer_dollars": 0.0,
        f"{mode.lower()}_max_amount_per_order": 0.0,
        f"{mode.lower()}_max_daily_order_count": 0,
    })
    current = {
        "ticker": "BREACHED", "status": "active", "observed_at": iso_now(),
        "time_remaining_seconds": 300, "yes_bid": .45, "no_bid": .55,
        "btc_proxy": btc_proxy, "strike": 100.0,
        "data_quality": {"reliable": True}, "exchange_index": 2,
    }

    await coordinator._process_exits(broker, current)
    assert client.created == []
    if mode == "LIVE":
        db.execute(
            "UPDATE broker_mode_state SET demo_verified_at=?,limits_reviewed_at=? "
            "WHERE mode='LIVE'",
            (iso_now(), iso_now()),
        )
        broker.arm(confirmation="ARM LIVE TRADING")
    else:
        broker.arm(confirmation="ARM DEMO TRADING")

    await coordinator._process_exits(broker, current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 1
    assert client.created[0]["action"] == "SELL"
    assert client.created[0]["side"] == side
    assert client.created[0]["contracts"] == 4
    assert client.created[0]["limit_price"] == pytest.approx(0.01)
    assert client.created[0]["reduce_only"] is True
    assert client.created[0]["time_in_force"] == "immediate_or_cancel"
    intent = db.fetch_one(
        "SELECT * FROM broker_order_intents WHERE mode=? AND ticker='BREACHED'",
        (mode,),
    )
    assert intent and intent["source"] == "threshold_breach_exit"
    evidence = json.loads(intent["decision_snapshot_json"])
    assert evidence["trigger"] == "THRESHOLD_BREACH_EXIT"
    assert evidence["market_style_ioc"] is True
    assert evidence["submitted_limit_floor"] == pytest.approx(0.01)
    assert evidence["threshold_trigger_btc_proxy"] == btc_proxy
    assert evidence["threshold_trigger_threshold"] == 100.0
    position = db.fetch_one(
        "SELECT * FROM broker_positions WHERE mode=? AND ticker='BREACHED'",
        (mode,),
    )
    assert position and position["threshold_exit_status"] == "Blocked"
    assert "did not fill" in position["threshold_exit_block_reason"]
    displayed = coordinator.summary(current)["modes"][mode]["positions"][0][
        "threshold_breach_exit"
    ]
    assert displayed["status"] == "Blocked"
    assert displayed["exit_level"] == 100.0
    assert displayed["btc_proxy"] == btc_proxy

    await coordinator._process_exits(broker, current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 1


@run_async
async def test_unfilled_ioc_threshold_exit_is_blocked_and_retries_only_on_new_quote(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.cancel_ioc = True
    client.remote_positions = [{
        "ticker": "UNFILLED-EXIT", "position_fp": "1.00",
        "market_exposure_dollars": "0.45", "last_updated_ts": iso_now(),
    }]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    db.update_settings({"global_profit_take_enabled": False})
    current = {
        "ticker": "UNFILLED-EXIT", "status": "active",
        "observed_at": iso_now(), "time_remaining_seconds": 300,
        "yes_bid": .45, "no_bid": .55, "btc_proxy": 99.0,
        "strike": 100.0, "data_quality": {"reliable": True},
    }

    await coordinator._process_exits(broker, current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    position = db.fetch_one(
        "SELECT threshold_exit_status,threshold_exit_block_reason "
        "FROM broker_positions WHERE mode='DEMO' AND ticker='UNFILLED-EXIT'"
    )
    assert position == {
        "threshold_exit_status": "Blocked",
        "threshold_exit_block_reason": (
            "Kalshi accepted the protective exit but it did not fill; "
            "waiting for a new executable quote."
        ),
    }
    intent = db.fetch_one(
        "SELECT status FROM broker_order_intents "
        "WHERE mode='DEMO' AND ticker='UNFILLED-EXIT' AND action='SELL'"
    )
    assert intent == {"status": "CANCELED"}

    await coordinator._process_exits(broker, current)
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 1

    await coordinator._process_exits(broker, {**current, "yes_bid": .46})
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert len(client.created) == 2


@run_async
async def test_rejected_threshold_exit_records_blocked_reason(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    client.remote_positions = [{
        "ticker": "BLOCKED-EXIT", "position_fp": "2.00",
        "market_exposure_dollars": "1.00", "last_updated_ts": iso_now(),
    }]
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING")
    client.reject_sells = True
    client.reject_error = KalshiTradingError(
        "Kalshi rejected the exit price",
        status_code=400,
        code="invalid_order",
        details={"field": "price", "message": "outside tick schedule", "api_key": "never-store"},
    )
    db.update_settings({"global_profit_take_enabled": False})
    await coordinator._process_exits(
        broker,
        {
            "ticker": "BLOCKED-EXIT", "status": "active",
            "observed_at": iso_now(), "time_remaining_seconds": 300,
            "yes_bid": .45, "no_bid": .55, "btc_proxy": 99.0,
            "strike": 100.0, "data_quality": {"reliable": True},
        },
    )
    await asyncio.gather(*list(coordinator._submission_tasks))
    position = db.fetch_one(
        "SELECT threshold_exit_status,threshold_exit_block_reason,threshold_exit_error_code,"
        "threshold_exit_error_details_json "
        "FROM broker_positions WHERE ticker='BLOCKED-EXIT'"
    )
    assert position == {
        "threshold_exit_status": "Blocked",
        "threshold_exit_block_reason": "Kalshi rejected the exit price",
        "threshold_exit_error_code": "invalid_order",
        "threshold_exit_error_details_json": json.dumps(
            {
                "code": "invalid_order",
                "details": {"field": "price", "message": "outside tick schedule", "api_key": "[redacted]"},
                "message": "Kalshi rejected the exit price",
                "status_code": 400,
            },
            sort_keys=True,
        ),
    }
    intent = db.fetch_one(
        "SELECT error,error_code,error_details_json FROM broker_order_intents "
        "WHERE ticker='BLOCKED-EXIT' AND action='SELL'"
    )
    assert intent and intent["error"] == "Kalshi rejected the exit price"
    assert intent["error_code"] == "invalid_order"
    assert "never-store" not in str(intent["error_details_json"])


@run_async
async def test_settlement_does_not_relabel_a_failed_threshold_exit_as_exited(
    tmp_path: Path,
) -> None:
    db, broker, client = await ready_broker(tmp_path)
    db.execute(
        """
        INSERT INTO broker_positions(
            mode,ticker,side,contracts,market_exposure,updated_at,status,
            threshold_breach_enabled,threshold_exit_status,threshold_exit_block_reason,
            threshold_triggered_at
        ) VALUES ('DEMO','FAILED-EXIT','NO',2,1,?,'open',1,'Blocked',
                  'Kalshi rejected the exit price',?)
        """,
        (iso_now(), iso_now()),
    )
    client.remote_positions = []
    client.remote_settlements = [{
        "ticker": "FAILED-EXIT", "market_result": "YES",
        "settled_at": iso_now(), "realized_pnl_dollars": "-1.00",
    }]
    await broker.reconcile()
    position = db.fetch_one(
        "SELECT status,threshold_exit_status,threshold_exit_block_reason "
        "FROM broker_positions WHERE mode='DEMO' AND ticker='FAILED-EXIT'"
    )
    assert position == {
        "status": "settled",
        "threshold_exit_status": "Blocked",
        "threshold_exit_block_reason": "Kalshi rejected the exit price",
    }


@run_async
async def test_confirmed_threshold_exit_fill_marks_position_exited(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path)
    now = iso_now()
    db.execute(
        """
        INSERT INTO broker_positions(
            mode,ticker,side,contracts,market_exposure,updated_at,status,
            threshold_breach_enabled,threshold_exit_status,threshold_triggered_at
        ) VALUES ('DEMO','EXITED','YES',2,1,?,'open',1,'Exit pending',?)
        """,
        (now, now),
    )
    db.execute(
        """
        INSERT INTO broker_fills(
            mode,fill_id,ticker,side,action,contracts,price,fee,source,filled_at,raw_json
        ) VALUES ('DEMO','confirmed-exit','EXITED','YES','SELL',2,.5,0,
                  'threshold_breach_exit',?,'{}')
        """,
        (now,),
    )
    client.remote_positions = []
    await broker.reconcile()
    position = db.fetch_one(
        "SELECT status,threshold_exit_status FROM broker_positions "
        "WHERE mode='DEMO' AND ticker='EXITED'"
    )
    assert position == {"status": "closed", "threshold_exit_status": "Exited"}


@run_async
async def test_sell_respects_contracts_reserved_by_another_exit(tmp_path: Path) -> None:
    db, broker, _ = await ready_broker(tmp_path)
    db.execute(
        """
        INSERT INTO broker_positions(
            mode,ticker,side,contracts,market_exposure,updated_at,status
        ) VALUES ('DEMO','EXIT','YES',5,2,?,'open')
        """,
        (iso_now(),),
    )
    db.execute(
        """
        INSERT INTO broker_orders(
            mode,exchange_order_id,ticker,side,action,status,requested_contracts,
            remaining_contracts,limit_price,updated_at
        ) VALUES ('DEMO','exit-resting','EXIT','YES','SELL','RESTING',4,4,.8,?)
        """,
        (iso_now(),),
    )
    risk = broker.risk_check(
        OrderIntent("DEMO", "EXIT", "YES", "SELL", 2, .8, "SWING", "swing_target")
    )
    assert risk["passed"] is False
    assert "Only 1 contracts" in str(risk["primary_blocker"])


@run_async
async def test_live_risk_failure_blocks_entry_without_disarming(tmp_path: Path) -> None:
    db, broker, client = await ready_broker(tmp_path, "LIVE")
    db.update_settings({"live_max_amount_per_order": .01})
    with pytest.raises(ValueError, match="maximum amount"):
        await broker.submit(
            OrderIntent("LIVE", "RISK", "YES", "BUY", 1, .4, "STANDARD_EDGE", "automatic")
        )
    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert client.created == []
    assert any(
        event["event_type"] == "AUTOMATIC_ENTRY_BLOCKED_BY_RISK"
        for event in broker.audit_history(10)
    )


@run_async
async def test_authentication_and_permission_errors_are_clear(tmp_path: Path) -> None:
    key_path = private_key(tmp_path)

    async def request(status: int, payload: dict) -> str:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
        )
        client = KalshiTradingClient(
            http,
            "https://external-api.demo.kalshi.co/trade-api/v2",
            "demo-key-id",
            key_path,
            environment="DEMO",
        )
        try:
            await client.balance()
        except KalshiTradingError as exc:
            return str(exc)
        finally:
            await http.aclose()
        return ""

    assert "clock is out of sync" in await request(
        401, {"code": "invalid_timestamp", "message": "expired timestamp"}
    )
    assert "rejected these API credentials" in await request(
        401, {"code": "unauthorized"}
    )
    assert "does not have permission" in await request(
        403, {"code": "permission_denied"}
    )


@run_async
async def test_swing_metadata_is_preserved_in_exchange_intent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    broker.set_client(FakeTradingClient())  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    db.update_settings({
        "trading_mode": "DEMO",
        "demo_automatic_trading_enabled": True,
        "demo_min_data_quality": "Low",
    })
    entered, _ = coordinator.submit_automatic(
        strategy="SWING",
        ticker="SWING-META",
        assessment={
            "side": "YES",
            "buy": {"executable_price": .04},
            "spread": .01,
            "ask_size": 10,
            "data_reliable": True,
            "trade_allowed": True,
            "decision_confidence": "Moderate",
        },
        bankroll_fraction=.01,
        model_version="test",
        reason="fixture",
        strategy_metadata={"maximum_entry_ask": .05},
    )
    assert entered is True
    await asyncio.gather(*list(coordinator._submission_tasks))
    row = db.fetch_one(
        "SELECT decision_snapshot_json FROM broker_order_intents WHERE ticker='SWING-META'"
    )
    assert row
    assert json.loads(row["decision_snapshot_json"])["strategy_metadata"] == {
        "maximum_entry_ask": .05
    }


@run_async
async def test_texas_entry_is_marketable_ioc_but_never_exceeds_cap(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    coordinator = TradingCoordinator(
        AppConfig(database_path=db.path), db, PaperTradingService(db)
    )
    broker = coordinator.broker("DEMO")
    assert isinstance(broker, KalshiBroker)
    client = FakeTradingClient()
    broker.set_client(client)  # type: ignore[arg-type]
    await broker.reconcile()
    broker.arm(confirmation="ARM DEMO TRADING", automatic=True)
    db.update_settings({
        "trading_mode": "DEMO", "demo_automatic_trading_enabled": True,
        "demo_min_data_quality": "Low", "texas_holdem_max_entry_price": .50,
    })
    entered, _ = coordinator.submit_automatic(
        strategy="TEXAS_HOLDEM", ticker="TEXAS-CAP",
        assessment={
            "side": "NO", "buy": {"executable_price": .48},
            "spread": .01, "ask_size": 100, "data_reliable": True,
            "trade_allowed": True, "decision_confidence": "High",
            "exchange_index": 7,
        },
        bankroll_fraction=.01, model_version="test", reason="fixture",
        time_in_force="immediate_or_cancel", maximum_entry_price=.50,
    )
    assert entered is True
    await asyncio.gather(*list(coordinator._submission_tasks))
    assert client.created[0]["limit_price"] == pytest.approx(.50)
    assert client.created[0]["time_in_force"] == "immediate_or_cancel"
    assert client.created[0]["exchange_index"] == 7
