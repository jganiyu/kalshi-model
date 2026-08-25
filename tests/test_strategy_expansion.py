from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import AppConfig
from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.engine import AnalysisEngine
from app.services.decision import Decision, make_trade_assessment
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "strategies.db")
    db.initialize()
    db.update_settings({"paper_trading_enabled": True})
    return db


def add_market(
    db: Database,
    ticker: str,
    *,
    status: str = "active",
    opened_at: str = "2026-08-24T12:00:00+00:00",
) -> None:
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, status, "test", 100.0, opened_at,
            "2026-08-24T12:15:00+00:00", "2026-08-24T12:15:00+00:00",
            None, "", "", "{}", opened_at, opened_at,
        ),
    )


def hold(side: str) -> Decision:
    return Decision(
        "HOLD", "NO_EDGE", "Low", "fixture", 0.5, 0.5, 0.0, None,
        None, None, 0.0, 0.0, 0, side,
    )


def assessments(
    probability: float = 0.75,
    *,
    yes_bid: float = 0.38,
    yes_ask: float = 0.40,
    yes_size: float = 1_000,
    settings: dict | None = None,
) -> dict[str, dict]:
    market = {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": 1 - yes_ask,
        "no_ask": 1 - yes_bid,
        "yes_ask_size": yes_size,
        "no_ask_size": yes_size,
    }
    configured = {"slippage_cents": 0.5, **(settings or {})}
    quality = {"reliable": True, "trade_allowed": True, "reason": "current"}
    return {
        side: make_trade_assessment(
            up_probability=probability,
            market=market,
            settings=configured,
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }


def strategy_call(
    service: PaperTradingService,
    *,
    ticker: str,
    side_assessments: dict[str, dict],
    observed_at: str,
    now: float,
    threshold_state: dict | None = None,
    seconds_remaining: float = 800,
    status: str = "active",
    coverage: float = 0.0,
    z_distance: float = 0.0,
) -> dict:
    return service.consider_strategies(
        ticker=ticker,
        assessments=side_assessments,
        standard_decisions={"YES": hold("YES"), "NO": hold("NO")},
        seconds_remaining=seconds_remaining,
        market_status=status,
        market_open_time="2026-08-24T12:00:00+00:00",
        market_observed_at=observed_at,
        threshold_state=threshold_state,
        settlement_window={"coverage": coverage},
        z_distance=z_distance,
        model_version="test",
        now=now,
    )


def test_buy_and_sell_ev_are_action_specific() -> None:
    result = assessments(0.80, yes_bid=0.82, yes_ask=0.83)["YES"]

    assert result["buy"]["expected_value"] < 0
    assert result["buy"]["expected_value"] == pytest.approx(
        result["buy"]["net_edge"]
    )
    assert result["buy"]["executable_price"] > 0.83
    assert result["sell"]["executable_price"] < 0.82


def test_early_threshold_waits_for_stability_and_next_quote(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "EARLY")
    service = PaperTradingService(db)
    side_assessments = assessments()
    unstable = {
        "first_observed_at": "2026-08-24T11:59:30+00:00",
        "latest_observed_at": "2026-08-24T12:00:00.200000+00:00",
    }

    blocked = strategy_call(
        service,
        ticker="EARLY",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:00:00.500000+00:00",
        threshold_state=unstable,
        now=0,
    )
    assert blocked["active_strategy"] is None

    stable = {**unstable, "latest_observed_at": "2026-08-24T11:59:58+00:00"}
    armed = strategy_call(
        service,
        ticker="EARLY",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:00:00.500000+00:00",
        threshold_state=stable,
        now=0,
    )
    same_quote = strategy_call(
        service,
        ticker="EARLY",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:00:00.500000+00:00",
        threshold_state=stable,
        now=2,
    )
    filled = strategy_call(
        service,
        ticker="EARLY",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:00:02.500000+00:00",
        threshold_state=stable,
        now=2.1,
    )

    assert armed["active_strategy"] == "EARLY_THRESHOLD"
    assert armed["effective_bankroll_allocation"] == pytest.approx(0.03)
    assert same_quote["early_threshold"]["next_quote_seen"] is False
    assert same_quote["entered"] is False
    assert filled["entered"] is True
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='EARLY'")
    assert entry and entry["strategy"] == "EARLY_THRESHOLD"
    assert entry["model_probability"] == pytest.approx(0.75)
    assert entry["expected_value"] > 0
    assert entry["entry_cost"] + entry["entry_fees"] <= 30


def test_unopened_market_cannot_enter(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "UNOPENED", status="initialized")
    service = PaperTradingService(db)
    result = strategy_call(
        service,
        ticker="UNOPENED",
        side_assessments=assessments(),
        observed_at="2026-08-24T11:59:59+00:00",
        threshold_state={
            "first_observed_at": "2026-08-24T11:59:30+00:00",
            "latest_observed_at": "2026-08-24T11:59:30+00:00",
        },
        status="initialized",
        now=0,
    )

    assert result["entered"] is False
    assert result["blocked_reason"] == "The market is not active."


def test_negative_ev_high_probability_cannot_enter_late(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "NEGATIVE")
    db.update_settings({"late_min_probability": 0.80})
    service = PaperTradingService(db)
    expensive = assessments(0.80, yes_bid=0.82, yes_ask=0.83)

    for now in (0, 4):
        result = strategy_call(
            service,
            ticker="NEGATIVE",
            side_assessments=expensive,
            observed_at=f"2026-08-24T12:14:{now:02.0f}+00:00",
            seconds_remaining=60,
            coverage=0.90,
            z_distance=3.0,
            now=now,
        )

    assert result["active_strategy"] is None
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_entries")["count"] == 0


def test_late_conviction_confirms_and_uses_three_percent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "LATE")
    service = PaperTradingService(db)
    high_probability = assessments(0.90, yes_bid=0.87, yes_ask=0.88)

    armed = strategy_call(
        service,
        ticker="LATE",
        side_assessments=high_probability,
        observed_at="2026-08-24T12:14:00+00:00",
        seconds_remaining=60,
        coverage=0.80,
        z_distance=2.5,
        now=0,
    )
    filled = strategy_call(
        service,
        ticker="LATE",
        side_assessments=high_probability,
        observed_at="2026-08-24T12:14:03+00:00",
        seconds_remaining=57,
        coverage=0.85,
        z_distance=2.5,
        now=3,
    )

    assert armed["active_strategy"] == "LATE_CONVICTION"
    assert armed["effective_bankroll_allocation"] == pytest.approx(0.03)
    assert filled["entered"] is True
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='LATE'")
    assert entry and entry["strategy"] == "LATE_CONVICTION"
    assert entry["entry_cost"] + entry["entry_fees"] <= 30


def test_custom_risk_limit_caps_fixed_strategy(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "CAPPED")
    db.update_settings({"max_risk_per_trade_pct": 0.02})
    service = PaperTradingService(db)
    stable = {
        "first_observed_at": "2026-08-24T11:59:30+00:00",
        "latest_observed_at": "2026-08-24T11:59:58+00:00",
    }

    first = strategy_call(
        service,
        ticker="CAPPED",
        side_assessments=assessments(),
        observed_at="2026-08-24T12:00:00+00:00",
        threshold_state=stable,
        now=0,
    )
    result = strategy_call(
        service,
        ticker="CAPPED",
        side_assessments=assessments(),
        observed_at="2026-08-24T12:00:03+00:00",
        threshold_state=stable,
        now=3,
    )

    assert first["effective_bankroll_allocation"] == pytest.approx(0.02)
    assert result["entered"] is True
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='CAPPED'")
    assert entry and entry["entry_cost"] + entry["entry_fees"] <= 20


def test_one_automatic_entry_across_all_strategies(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "ONCE")
    service = PaperTradingService(db)
    stable = {
        "first_observed_at": "2026-08-24T11:59:30+00:00",
        "latest_observed_at": "2026-08-24T11:59:58+00:00",
    }
    strategy_call(
        service, ticker="ONCE", side_assessments=assessments(),
        observed_at="2026-08-24T12:00:00+00:00", threshold_state=stable, now=0,
    )
    entered = strategy_call(
        service, ticker="ONCE", side_assessments=assessments(),
        observed_at="2026-08-24T12:00:03+00:00", threshold_state=stable, now=3,
    )
    blocked = strategy_call(
        service, ticker="ONCE", side_assessments=assessments(0.90),
        observed_at="2026-08-24T12:14:00+00:00", seconds_remaining=60,
        coverage=0.90, z_distance=3.0, now=20,
    )

    assert entered["entered"] is True
    assert blocked["entered"] is False
    assert "already exists" in blocked["blocked_reason"]
    assert db.fetch_one("SELECT COUNT(*) count FROM paper_entries")["count"] == 1


def test_threshold_first_seen_and_revisions_are_persisted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    market = {
        "ticker": "KXBTC15M-TEST",
        "status": "initialized",
        "open_time": "2026-08-24T12:00:00+00:00",
        "floor_strike": 77_000,
    }
    engine._record_threshold_observation(
        market, "2026-08-24T11:59:00+00:00", source="REST", event_type="poll"
    )
    engine._record_threshold_observation(
        market, "2026-08-24T11:59:05+00:00", source="REST", event_type="poll"
    )
    market["floor_strike"] = 77_025
    engine._record_threshold_observation(
        market,
        "2026-08-24T11:59:10+00:00",
        source="WEBSOCKET",
        event_type="metadata_updated",
    )

    rows = db.fetch_all(
        "SELECT * FROM threshold_observations WHERE ticker=? ORDER BY revision",
        (market["ticker"],),
    )
    assert len(rows) == 2
    assert rows[0]["observed_at"] == "2026-08-24T11:59:00+00:00"
    assert rows[0]["revision"] == 1 and rows[0]["changed"] == 0
    assert rows[1]["revision"] == 2 and rows[1]["changed"] == 1
    assert rows[1]["source"] == "WEBSOCKET"


def test_lifecycle_created_event_records_early_threshold(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)

    asyncio.run(
        engine._handle_market_lifecycle(
            {
                "type": "market_lifecycle_v2",
                "msg": {
                    "market_ticker": "KXBTC15M-26AUG241200-00",
                    "event_type": "created",
                    "open_ts": 1787572800,
                    "close_ts": 1787573700,
                    "additional_metadata": {
                        "event_ticker": "KXBTC15M-26AUG241200",
                        "title": "Bitcoin price in fifteen minutes",
                        "floor_strike": 77_100,
                    },
                },
            }
        )
    )

    row = db.fetch_one(
        "SELECT * FROM threshold_observations WHERE ticker=?",
        ("KXBTC15M-26AUG241200-00",),
    )
    assert row and row["threshold"] == pytest.approx(77_100)
    assert row["source"] == "WEBSOCKET"
    assert row["event_type"] == "created"


def test_old_default_risk_migrates_but_custom_value_survives(tmp_path: Path) -> None:
    def legacy(path: Path, customized: bool) -> Database:
        db = Database(path)
        with db.transaction() as connection:
            for version, sql in MIGRATIONS[:6]:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                    (version, iso_now()),
                )
            now = iso_now()
            connection.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
                ("max_risk_per_trade_pct", "0.02", now),
            )
            if customized:
                connection.execute(
                    """
                    INSERT INTO configuration_snapshots(
                        created_at,settings_json,changed_json,restored_from_id
                    ) VALUES (?,?,?,NULL)
                    """,
                    (
                        now,
                        '{"max_risk_per_trade_pct":0.02}',
                        '{"max_risk_per_trade_pct":{"before":0.03,"after":0.02}}',
                    ),
                )
        db.initialize()
        return db

    untouched = legacy(tmp_path / "untouched.db", False)
    customized = legacy(tmp_path / "customized.db", True)

    assert untouched.settings()["max_risk_per_trade_pct"] == pytest.approx(0.03)
    assert customized.settings()["max_risk_per_trade_pct"] == pytest.approx(0.02)


def test_dashboard_markup_has_one_book_and_paper_trade_history() -> None:
    root = Path(__file__).resolve().parents[1]
    markup = (root / "app/templates/index.html").read_text()
    script = (root / "app/static/app.js").read_text()

    assert markup.count('id="orderbook-rows"') == 1
    assert "up-orderbook-rows" not in markup
    assert "down-orderbook-rows" not in markup
    assert 'id="recent-paper-trades"' in markup
    assert "recent_paper_trades" in script
    assert 'api("/api/model-side"' not in script
