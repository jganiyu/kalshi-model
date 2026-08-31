from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Response

from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.services.trade_review import (
    TradeReviewService,
    broker_trade_ref,
    paper_trade_ref,
    review_metadata,
)


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "trade-review.db")
    db.initialize()
    return db


def add_market(db: Database, ticker: str = "TEST") -> None:
    now = iso_now()
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, "active", "test", 100.0,
            "2026-08-29T12:00:00Z", "2026-08-29T12:15:00Z",
            "2026-08-29T12:15:00Z", None, "", "", "{}", now, now,
        ),
    )


def current(ticker: str = "TEST", **updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ticker": ticker,
        "status": "active",
        "strike": 100.0,
        "open_time": "2026-08-29T12:00:00Z",
        "close_time": "2026-08-29T12:15:00Z",
        "time_remaining_seconds": 800,
        "btc_proxy": 104.0,
        "yes_bid": 0.61,
        "yes_ask": 0.62,
        "no_bid": 0.37,
        "no_ask": 0.38,
        "spread": 0.01,
        "liquidity": 200,
        "open_interest": 500,
        "volume": 900,
        "up_probability": 0.70,
        "model_version": "review-model-1",
        "forecast": {
            "signal": "LIKELY_UP", "up_probability": 0.70,
            "down_probability": 0.30, "explanation": "test",
        },
        "trade_assessments": {
            "YES": {"model_probability": 0.70, "buy": {"expected_value": 0.07}},
            "NO": {"model_probability": 0.30, "buy": {"expected_value": -0.08}},
        },
        "trade_decisions": {"YES": {"signal": "BUY"}, "NO": {"signal": "HOLD"}},
        "data_quality": {"reliable": True, "reason": "fresh"},
        "margin_volatility": {
            "mvi": 3.2, "expected_remaining_move": 8.0,
            "cushion_ratio": 0.5, "calculation_version": "mvi-1",
        },
        "standard_edge_readiness": {
            "status": "CONFIRMING", "side": "YES", "blocker": "Confirming",
            "gates": {
                "spread": {"passed": True}, "data": {"passed": True},
                "risk": {"passed": True},
            },
        },
        "settlement_window": {"coverage": 1.0},
        "trading_mode": "PAPER",
    }
    values.update(updates)
    return values


def add_paper_trade(db: Database, ticker: str = "TEST") -> int:
    trade_id = db.execute(
        """
        INSERT INTO paper_trades(
            ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
            model_probability,market_probability,edge,expected_value,confidence,
            model_version,status,source,strategy
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, "YES", "2026-08-29T12:00:12Z", 0.62, 10, 6.2, 0.1,
            0.70, 0.62, 0.08, 0.07, "High", "review-model-1", "open",
            "automatic", "STANDARD_EDGE",
        ),
    )
    db.execute(
        """
        INSERT INTO paper_entries(
            trade_id,ticker,side,opened_at,entry_price,initial_contracts,
            remaining_contracts,entry_cost,entry_fees,source,status,strategy
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id, ticker, "YES", "2026-08-29T12:00:12Z", 0.62,
            10, 10, 6.2, 0.1, "automatic", "open", "STANDARD_EDGE",
        ),
    )
    return trade_id


def test_records_only_traded_markets_and_flushes_pretrade_buffer(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = TradeReviewService(db)
    start = datetime(2026, 8, 29, 12, tzinfo=UTC)
    for seconds in (0, 5, 10):
        service.observe(current(), (start + timedelta(seconds=seconds)).isoformat())
    service.observe(current(btc_proxy=999.0), "2026-08-29T12:00:11Z")
    assert db.fetch_one("SELECT COUNT(*) count FROM trade_review_sessions")["count"] == 0
    assert db.fetch_one("SELECT COUNT(*) count FROM trade_review_points")["count"] == 0

    trade_id = add_paper_trade(db)
    service.observe(current(), (start + timedelta(seconds=15)).isoformat())
    session = db.fetch_one("SELECT * FROM trade_review_sessions")
    assert session and session["environment"] == "PAPER"
    points = db.fetch_all(
        "SELECT * FROM trade_review_points WHERE sample_kind='REGULAR' ORDER BY observed_at"
    )
    assert len(points) == 4
    assert points[0]["observed_at"].endswith("12:00:00Z")
    assert points[-1]["margin"] == pytest.approx(4.0)
    assert db.fetch_one("SELECT COUNT(*) count FROM trade_review_events")["count"] >= 1
    assert review_metadata(db, "PAPER", paper_trade_ref(trade_id), "open")[
        "review_available"
    ] is False
    with pytest.raises(ValueError, match="after market settlement"):
        service.review("PAPER", paper_trade_ref(trade_id))


def test_complete_market_has_180_aligned_deduplicated_points(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    service = TradeReviewService(db)
    start = datetime(2026, 8, 29, 12, tzinfo=UTC)
    for index in range(180):
        if index == 40:
            add_paper_trade(db)
        observed = start + timedelta(seconds=index * 5 + 1)
        service.observe(
            current(btc_proxy=100 + index / 100), observed.isoformat()
        )
        service.observe(
            current(btc_proxy=100 + index / 100), observed.isoformat()
        )
    service.finalize(
        "TEST", result=1, settled_at="2026-08-29T12:15:00Z",
        settlement_value=103.0,
    )
    session = db.fetch_one("SELECT * FROM trade_review_sessions")
    assert session and session["regular_point_count"] == 180
    assert session["expected_regular_points"] == 180
    assert session["coverage"] == pytest.approx(1.0)
    assert session["status"] == "FINALIZED"
    times = db.fetch_all(
        "SELECT observed_at FROM trade_review_points WHERE sample_kind='REGULAR'"
    )
    assert all(int(row["observed_at"][17:19]) % 5 == 0 for row in times)


def test_finalized_review_has_points_events_gaps_and_summary(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    trade_id = add_paper_trade(db)
    first = TradeReviewService(db)
    first.observe(current(), "2026-08-29T12:00:15Z")

    # A restarted recorder resumes the same session and preserves an explicit gap.
    resumed = TradeReviewService(db)
    resumed.observe(
        current(
            forecast={
                "signal": "UNCERTAIN", "up_probability": 0.55,
                "down_probability": 0.45, "explanation": "changed",
            },
            up_probability=0.55,
            standard_edge_readiness={
                "status": "BLOCKED", "side": "NO", "blocker": "Risk blocked",
                "gates": {
                    "spread": {"passed": True}, "data": {"passed": True},
                    "risk": {"passed": False},
                },
            },
        ),
        "2026-08-29T12:00:30Z",
    )
    db.execute(
        """
        UPDATE paper_trades SET status='settled',settled_at=?,outcome=1,
            payout=10,realized_pnl=3.7 WHERE id=?
        """,
        ("2026-08-29T12:15:02Z", trade_id),
    )
    resumed.finalize(
        "TEST", result=1, settled_at="2026-08-29T12:15:02Z",
        settlement_value=108.0,
    )

    metadata = review_metadata(db, "PAPER", paper_trade_ref(trade_id), "settled")
    assert metadata["review_available"] is True
    assert metadata["review_status"] == "PARTIAL"
    review = resumed.review("PAPER", paper_trade_ref(trade_id))
    assert review["session"]["settlement_margin"] == pytest.approx(8.0)
    assert review["session"]["gap_count"] == 1
    assert review["gaps"][0]["seconds"] == 15
    assert review["trade"]["realized_pnl"] == pytest.approx(3.7)
    event_types = {event["event_type"] for event in review["events"]}
    assert {
        "ENTRY", "SETTLEMENT", "FORECAST_CHANGE", "CANDIDATE_SIDE_CHANGE",
        "GATE_CHANGE",
    } <= event_types
    assert review["points"][0]["state"]["trade_assessments"]["YES"]["buy"][
        "expected_value"
    ] == pytest.approx(0.07)
    assert "orderbook" not in review["points"][0]["state"]["market"]


def test_recording_continues_after_early_exit_until_market_close(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db)
    trade_id = add_paper_trade(db)
    entry = db.fetch_one("SELECT id FROM paper_entries WHERE trade_id=?", (trade_id,))
    assert entry
    db.execute(
        """
        INSERT INTO paper_orders(
            ticker,side,action,order_type,status,created_at,requested_contracts,
            filled_price,filled_contracts,fees,realized_pnl,filled_at,source,
            entry_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "TEST", "YES", "SELL", "MARKET", "filled",
            "2026-08-29T12:02:00Z", 10, .72, 10, .05, .90,
            "2026-08-29T12:02:00Z", "profit_take", entry["id"],
        ),
    )
    db.execute(
        "UPDATE paper_trades SET status='closed' WHERE id=?", (trade_id,)
    )
    service = TradeReviewService(db)
    service.observe(current(), "2026-08-29T12:02:00Z")
    service.observe(current(btc_proxy=107), "2026-08-29T12:14:55Z")
    service.finalize(
        "TEST", result=1, settled_at="2026-08-29T12:15:00Z",
        settlement_value=107,
    )
    latest = db.fetch_one(
        """
        SELECT observed_at FROM trade_review_points
        WHERE sample_kind='REGULAR' ORDER BY observed_at DESC LIMIT 1
        """
    )
    assert latest and latest["observed_at"].endswith("12:14:55Z")
    events = db.fetch_all(
        "SELECT event_type,observed_at FROM trade_review_events ORDER BY observed_at"
    )
    assert {row["event_type"] for row in events} >= {
        "ENTRY", "EXIT", "SETTLEMENT",
    }
    assert next(
        row for row in events if row["event_type"] == "EXIT"
    )["observed_at"] == "2026-08-29T12:02:00Z"


def test_paper_demo_live_are_isolated(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db)
    add_paper_trade(db)
    for mode, fill_id in (("DEMO", "demo-fill"), ("LIVE", "live-fill")):
        db.execute(
            """
            INSERT INTO broker_fills(
                mode,fill_id,ticker,side,action,contracts,price,fee,strategy,
                source,filled_at,raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                mode, fill_id, "TEST", "NO", "BUY", 2, 0.30, 0.01,
                "STANDARD_EDGE", "automatic", "2026-08-29T12:00:12Z", "{}",
            ),
        )
    service = TradeReviewService(db)
    service.observe(current(), "2026-08-29T12:00:15Z")
    sessions = db.fetch_all(
        "SELECT environment FROM trade_review_sessions ORDER BY environment"
    )
    assert [row["environment"] for row in sessions] == ["DEMO", "LIVE", "PAPER"]
    demo_ref = broker_trade_ref("DEMO", "TEST", "NO")
    live_ref = broker_trade_ref("LIVE", "TEST", "NO")
    assert db.fetch_one(
        "SELECT id FROM trade_review_links WHERE environment='DEMO' AND trade_ref=?",
        (demo_ref,),
    )
    assert not db.fetch_one(
        "SELECT id FROM trade_review_links WHERE environment='DEMO' AND trade_ref=?",
        (live_ref,),
    )


def test_multiple_trades_share_market_history_but_keep_distinct_links(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db)
    first_id = add_paper_trade(db)
    second_id = db.execute(
        """
        INSERT INTO paper_trades(
            ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
            model_probability,market_probability,edge,expected_value,confidence,
            model_version,status,source,strategy
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "TEST", "NO", "2026-08-29T12:01:00Z", .35, 3, 1.05, .01,
            .30, .35, -.05, -.06, "Low", "review-model-1", "open",
            "manual", "MANUAL",
        ),
    )
    service = TradeReviewService(db)
    service.observe(current(), "2026-08-29T12:01:05Z")
    sessions = db.fetch_all("SELECT * FROM trade_review_sessions")
    links = db.fetch_all("SELECT * FROM trade_review_links ORDER BY trade_ref")
    assert len(sessions) == 1
    assert {link["trade_ref"] for link in links} == {
        paper_trade_ref(first_id), paper_trade_ref(second_id)
    }
    assert len({link["session_id"] for link in links}) == 1


def test_legacy_history_survives_additive_migration_without_fake_review(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "legacy-review.db")
    with db.transaction() as connection:
        for version, sql in MIGRATIONS[:13]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        now = iso_now()
        connection.execute(
            "INSERT INTO markets(ticker,status,raw_json,first_seen_at,updated_at) VALUES (?,?,?,?,?)",
            ("LEGACY", "finalized", "{}", now, now),
        )
        trade_id = connection.execute(
            """
            INSERT INTO paper_trades(
                ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
                model_probability,market_probability,edge,expected_value,
                confidence,model_version,status,source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "LEGACY", "YES", now, .5, 1, .5, 0, .5, .5, 0, 0,
                "Low", "legacy", "settled", "manual",
            ),
        ).lastrowid
    db.initialize()
    assert db.fetch_one("SELECT id FROM paper_trades WHERE id=?", (trade_id,))
    assert db.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == 19
    metadata = review_metadata(db, "PAPER", paper_trade_ref(trade_id), "settled")
    assert metadata["review_available"] is False
    assert metadata["review_status"] == "UNAVAILABLE"


def test_review_api_is_read_only_and_disables_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main

    db = make_db(tmp_path)
    add_market(db)
    trade_id = add_paper_trade(db)
    service = TradeReviewService(db)
    service.observe(current(), "2026-08-29T12:00:15Z")
    db.execute(
        "UPDATE paper_trades SET status='settled',settled_at=?,outcome=1,realized_pnl=1 WHERE id=?",
        ("2026-08-29T12:15:00Z", trade_id),
    )
    service.finalize(
        "TEST", result=1, settled_at="2026-08-29T12:15:00Z",
        settlement_value=103.0,
    )
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main.engine, "trade_reviews", service)
    response = Response()
    payload = asyncio.run(
        main.historical_trade_review(
            "PAPER", paper_trade_ref(trade_id), response
        )
    )
    assert payload["environment"] == "PAPER"
    assert response.headers["cache-control"].startswith("no-store")
    serialized = json.dumps(payload).lower()
    assert "credential" not in serialized
    assert "private_key" not in serialized
    assert "api_key" not in serialized
    for endpoint, key in (
        (main.historical_trade_review_metadata, "session"),
        (main.historical_trade_review_points, "points"),
        (main.historical_trade_review_events, "events"),
        (main.historical_trade_review_trade, "trade"),
    ):
        section_response = Response()
        section = asyncio.run(
            endpoint("PAPER", paper_trade_ref(trade_id), section_response)
        )
        assert key in section
        assert section_response.headers["cache-control"].startswith("no-store")
    review_routes = [
        route for route in main.app.routes
        if getattr(route, "path", "").endswith("/reviews/{trade_ref}")
    ]
    assert review_routes and all(route.methods == {"GET"} for route in review_routes)


def test_trading_page_contains_inline_chart_and_keyboard_controls() -> None:
    javascript = Path("app/static/app.js").read_text()
    stylesheet = Path("app/static/styles.css").read_text()
    assert "toggleHistoricalTradeReview" in javascript
    assert "ArrowLeft" in javascript and "ArrowRight" in javascript
    assert 'data-review-chart-mode="volatility"' in javascript
    assert ".trade-review-row > td" in stylesheet
    assert ".trade-review-canvas" in stylesheet
