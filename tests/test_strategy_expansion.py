from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import AppConfig
from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.engine import AnalysisEngine
from app.services.broker import KalshiBroker
from app.services.decision import Decision, make_trade_assessment
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "strategies.db")
    db.initialize()
    db.update_settings(
        {"paper_trading_enabled": True, "threshold_margin_gate_dollars": 0}
    )
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


def buy(side: str, assessment: dict, confidence: str = "Moderate") -> Decision:
    economics = assessment["buy"]
    return Decision(
        "BUY", "BUY_EDGE", confidence, "fixture",
        assessment["model_probability"], assessment["market_probability"],
        economics["net_edge"], economics["expected_value"],
        economics["executable_price"], economics["fee_per_contract"],
        0.02, 20.0, 20, side,
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
    standard_decisions: dict[str, Decision] | None = None,
    threshold_margin_dollars: float | None = None,
) -> dict:
    return service.consider_strategies(
        ticker=ticker,
        assessments=side_assessments,
        standard_decisions=standard_decisions or {
            "YES": hold("YES"), "NO": hold("NO")
        },
        seconds_remaining=seconds_remaining,
        market_status=status,
        market_open_time="2026-08-24T12:00:00+00:00",
        market_observed_at=observed_at,
        threshold_state=threshold_state,
        settlement_window={"coverage": coverage},
        z_distance=z_distance,
        threshold_margin_dollars=threshold_margin_dollars,
        model_version="test",
        now=now,
    )


def test_threshold_margin_gate_is_directional_and_resets_confirmation(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "MARGIN-GATE")
    db.update_settings(
        {
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "threshold_margin_gate_dollars": 50.0,
            "automatic_confirmation_seconds": 5,
            "automatic_min_confidence": "Moderate",
        }
    )
    service = PaperTradingService(db)
    side_assessments = assessments(0.75, yes_bid=0.38, yes_ask=0.40)
    decisions = {
        "YES": buy("YES", side_assessments["YES"]),
        "NO": hold("NO"),
    }

    blocked = strategy_call(
        service,
        ticker="MARGIN-GATE",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:00+00:00",
        standard_decisions=decisions,
        threshold_margin_dollars=49.99,
        now=0,
    )["standard_edge_readiness"]
    assert blocked["status"] == "BLOCKED"
    assert blocked["gates"]["threshold_margin"] == {
        "enabled": True,
        "passed": False,
        "current": pytest.approx(49.99),
        "required": pytest.approx(50.0),
        "detail": (
            "The BTC proxy must be at least $50.00 above the threshold "
            "for an Up entry."
        ),
    }
    assert blocked["metrics"]["confirmation"]["locked"] is True

    strategy_call(
        service,
        ticker="MARGIN-GATE",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:01+00:00",
        standard_decisions=decisions,
        threshold_margin_dollars=55.0,
        now=1,
    )
    progressing = strategy_call(
        service,
        ticker="MARGIN-GATE",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:04+00:00",
        standard_decisions=decisions,
        threshold_margin_dollars=55.0,
        now=4,
    )["standard_edge_readiness"]
    assert progressing["metrics"]["confirmation"]["progress"] > 0

    reset = strategy_call(
        service,
        ticker="MARGIN-GATE",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:05+00:00",
        standard_decisions=decisions,
        threshold_margin_dollars=-55.0,
        now=5,
    )["standard_edge_readiness"]
    assert reset["gates"]["threshold_margin"]["passed"] is False
    assert reset["metrics"]["confirmation"]["progress"] == 0
    assert db.fetch_one("SELECT id FROM paper_trades WHERE ticker='MARGIN-GATE'") is None

    down_passed = service._threshold_margin_gate(
        db.settings(), side="NO", margin_dollars=-55.0
    )
    down_blocked = service._threshold_margin_gate(
        db.settings(), side="NO", margin_dollars=55.0
    )
    assert down_passed["passed"] is True
    assert down_passed["required"] == pytest.approx(-50.0)
    assert down_blocked["passed"] is False
    assert down_blocked["detail"].endswith("below the threshold for a Down entry.")


def test_buy_and_sell_ev_are_action_specific() -> None:
    result = assessments(0.80, yes_bid=0.82, yes_ask=0.83)["YES"]

    assert result["buy"]["expected_value"] < 0
    assert result["buy"]["expected_value"] == pytest.approx(
        result["buy"]["net_edge"]
    )
    assert result["buy"]["executable_price"] > 0.83
    assert result["sell"]["executable_price"] < 0.82


def test_standard_readiness_uses_automatic_side_and_effective_target(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "STANDARD-HUD")
    db.update_settings(
        {
            "selected_side": "YES",
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "minimum_buy_probability": 0.65,
            "buy_edge": 0.10,
            "hold_buffer": 0.005,
            "automatic_confirmation_seconds": 5,
            "automatic_entry_window_minutes": 15,
            "automatic_min_confidence": "Moderate",
        }
    )
    service = PaperTradingService(db)
    side_assessments = assessments(0.28, yes_bid=0.48, yes_ask=0.50)
    decisions = {
        "YES": hold("YES"),
        "NO": buy("NO", side_assessments["NO"]),
    }

    confirming = strategy_call(
        service,
        ticker="STANDARD-HUD",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:00+00:00",
        standard_decisions=decisions,
        now=0,
    )
    readiness = confirming["standard_edge_readiness"]

    assert db.settings()["selected_side"] == "YES"
    assert readiness["side"] == "NO"
    assert readiness["status"] == "CONFIRMING"
    assert readiness["metrics"]["probability"]["current"] == pytest.approx(0.72)
    assert readiness["metrics"]["probability"]["required"] == pytest.approx(0.65)
    assert readiness["metrics"]["net_ev"]["required"] == pytest.approx(0.105)
    assert readiness["metrics"]["confirmation"]["locked"] is False
    assert all(gate["passed"] for gate in readiness["gates"].values())

    entered = strategy_call(
        service,
        ticker="STANDARD-HUD",
        side_assessments=side_assessments,
        observed_at="2026-08-24T12:01:05+00:00",
        standard_decisions=decisions,
        now=5,
    )
    readiness = entered["standard_edge_readiness"]
    assert entered["entered"] is True
    assert readiness["status"] == "ENTERED"
    assert readiness["ready"] is True
    assert all(metric["passed"] for metric in readiness["metrics"].values())


def test_standard_readiness_confirmation_resets_when_side_changes(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "STANDARD-RESET")
    db.update_settings(
        {
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "buy_edge": 0.05,
            "automatic_confirmation_seconds": 5,
            "automatic_entry_window_minutes": 15,
            "automatic_min_confidence": "Moderate",
        }
    )
    service = PaperTradingService(db)
    down_assessments = assessments(0.25, yes_bid=0.48, yes_ask=0.50)
    up_assessments = assessments(0.75, yes_bid=0.48, yes_ask=0.50)
    down_decisions = {
        "YES": hold("YES"), "NO": buy("NO", down_assessments["NO"]),
    }
    up_decisions = {
        "YES": buy("YES", up_assessments["YES"]), "NO": hold("NO"),
    }

    strategy_call(
        service, ticker="STANDARD-RESET", side_assessments=down_assessments,
        observed_at="2026-08-24T12:01:00+00:00",
        standard_decisions=down_decisions, now=0,
    )
    progressed = strategy_call(
        service, ticker="STANDARD-RESET", side_assessments=down_assessments,
        observed_at="2026-08-24T12:01:02+00:00",
        standard_decisions=down_decisions, now=2,
    )
    switched = strategy_call(
        service, ticker="STANDARD-RESET", side_assessments=up_assessments,
        observed_at="2026-08-24T12:01:03+00:00",
        standard_decisions=up_decisions, now=3,
    )

    assert progressed["standard_edge_readiness"]["metrics"]["confirmation"][
        "progress"
    ] == pytest.approx(0.4)
    assert switched["standard_edge_readiness"]["side"] == "YES"
    assert switched["standard_edge_readiness"]["metrics"]["confirmation"][
        "progress"
    ] == pytest.approx(0.0)


def test_standard_readiness_locks_when_spread_gate_fails(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "STANDARD-SPREAD")
    db.update_settings(
        {
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "automatic_min_confidence": "Moderate",
        }
    )
    service = PaperTradingService(db)
    wide = assessments(0.75, yes_bid=0.40, yes_ask=0.45)

    result = strategy_call(
        service,
        ticker="STANDARD-SPREAD",
        side_assessments=wide,
        observed_at="2026-08-24T12:01:00+00:00",
        seconds_remaining=200,
        now=0,
    )
    readiness = result["standard_edge_readiness"]

    assert readiness["status"] == "BLOCKED"
    assert readiness["gates"]["spread"]["passed"] is False
    assert readiness["blocker"] == "Spread exceeds the Standard Edge limit."
    assert readiness["metrics"]["confirmation"]["locked"] is True
    assert readiness["metrics"]["confirmation"]["progress"] == 0


def test_exchange_hud_ready_state_uses_the_same_execution_risk_gate(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "EXCHANGE-HUD")
    db.update_settings(
        {
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "automatic_min_confidence": "Moderate",
            "automatic_confirmation_seconds": 1,
        }
    )
    service = PaperTradingService(db)
    side_assessments = assessments(0.75, yes_bid=0.38, yes_ask=0.40)
    decisions = {
        "YES": buy("YES", side_assessments["YES"]),
        "NO": hold("NO"),
    }
    result = service.consider_strategies(
        ticker="EXCHANGE-HUD",
        assessments=side_assessments,
        standard_decisions=decisions,
        seconds_remaining=300,
        market_status="active",
        market_open_time="2026-08-24T12:00:00+00:00",
        market_observed_at="2026-08-24T12:01:00+00:00",
        threshold_state=None,
        settlement_window={"coverage": 1.0},
        z_distance=2.0,
        model_version="test",
        portfolio={
            "automatic_trade_allowed": True,
            "automatic_trade_block_reason": None,
        },
        now=0,
        execution_mode="DEMO",
        automatic_enabled=True,
        execution_risk_by_side={
            "YES": {
                "passed": False,
                "primary_blocker": "The order exceeds the remaining mode allocation.",
            }
        },
    )
    readiness = result["standard_edge_readiness"]
    assert result["entered"] is False
    assert readiness["ready"] is False
    assert readiness["status"] == "BLOCKED"
    assert readiness["gates"]["risk"]["passed"] is False
    assert readiness["blocker"] == "The order exceeds the remaining mode allocation."


def test_forecast_or_hold_direction_alone_cannot_call_exchange_entry(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "FORECAST-ONLY")
    db.update_settings(
        {"early_threshold_enabled": False, "late_conviction_enabled": False}
    )
    service = PaperTradingService(db)
    calls: list[dict] = []

    def fixed_entry(**kwargs):
        calls.append(kwargs)
        return True, 0.01

    result = service.consider_strategies(
        ticker="FORECAST-ONLY",
        assessments=assessments(0.90),
        standard_decisions={"YES": hold("YES"), "NO": hold("NO")},
        seconds_remaining=300,
        market_status="active",
        market_open_time="2026-08-24T12:00:00+00:00",
        market_observed_at="2026-08-24T12:01:00+00:00",
        threshold_state=None,
        settlement_window={"coverage": 1.0},
        z_distance=3.0,
        model_version="test",
        execution_mode="DEMO",
        automatic_enabled=True,
        execution_risk_by_side={"YES": {"passed": True}},
        fixed_entry_handler=fixed_entry,
    )
    assert result["entered"] is False
    assert calls == []


def test_standard_readiness_separates_data_from_entry_quality(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "STANDARD-QUALITY")
    db.update_settings(
        {
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "automatic_min_confidence": "Moderate",
        }
    )
    service = PaperTradingService(db)
    side_assessments = assessments(0.75)
    decisions = {
        "YES": buy("YES", side_assessments["YES"], confidence="Low"),
        "NO": hold("NO"),
    }

    result = strategy_call(
        service,
        ticker="STANDARD-QUALITY",
        side_assessments=side_assessments,
        standard_decisions=decisions,
        observed_at="2026-08-24T12:01:00+00:00",
        seconds_remaining=200,
        now=0,
    )
    readiness = result["standard_edge_readiness"]

    assert readiness["gates"]["data"]["passed"] is True
    assert readiness["gates"]["quality"]["passed"] is False
    assert readiness["gates"]["quality"]["current"] == "Low"
    assert readiness["gates"]["quality"]["required"] == "Moderate"
    assert readiness["blocker"] == "Entry quality is Low; Moderate is required."
    assert readiness["metrics"]["confirmation"]["locked"] is True


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
    assert armed["standard_edge_readiness"]["status"] == "BLOCKED"
    assert armed["standard_edge_readiness"]["priority_strategy"] == "EARLY_THRESHOLD"
    assert "priority" in armed["standard_edge_readiness"]["blocker"]
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


def test_market_summary_preserves_exchange_routing_index(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    summary = engine._market_summary(
        {"ticker": "KXBTC15M-TEST", "status": "active", "exchange_index": 2}
    )
    assert summary and summary["exchange_index"] == 2


def test_live_limit_review_persists_across_calibration_saves_and_restart(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    broker = engine.trading.broker("LIVE")
    assert isinstance(broker, KalshiBroker)
    reviewed_at = "2026-08-28T12:00:00+00:00"
    db.execute(
        """
        UPDATE broker_mode_state SET authenticated=1,reconciled=1,
            reconciliation_required=0,demo_verified_at=?,limits_reviewed_at=?
        WHERE mode='LIVE'
        """,
        (reviewed_at, reviewed_at),
    )
    broker.session_armed = True
    broker.automatic_armed = True

    current = db.settings()
    unchanged_live_limits = {
        key: value for key, value in current.items()
        if key.startswith("live_")
        and key != "live_automatic_trading_enabled"
    }
    asyncio.run(engine.apply_settings({
        **unchanged_live_limits,
        "minimum_buy_probability": 0.70,
    }))

    assert broker.session_armed is True
    assert broker.automatic_armed is True
    assert broker.mode_state()["limits_reviewed_at"] == reviewed_at

    asyncio.run(engine.apply_settings({"live_max_amount_per_order": 25.0}))

    assert broker.session_armed is False
    assert broker.automatic_armed is False
    assert broker.mode_state()["limits_reviewed_at"] == reviewed_at

    restarted = AnalysisEngine(AppConfig(database_path=db.path), db)
    restarted_broker = restarted.trading.broker("LIVE")
    assert isinstance(restarted_broker, KalshiBroker)
    restarted_broker.arm(confirmation="ARM LIVE TRADING")
    assert restarted_broker.readiness()["limits_reviewed"] is True
    assert restarted_broker.session_armed is True


def test_demo_execution_state_uses_demo_book_without_live_fallback(
    tmp_path: Path,
) -> None:
    class DemoPublicClient:
        async def market(self, ticker: str) -> dict:
            return {
                "ticker": ticker,
                "liquidity_dollars": "2.00",
                "yes_bid_dollars": "0.0000",
                "yes_ask_dollars": "1.0000",
                "no_bid_dollars": "0.0000",
                "no_ask_dollars": "1.0000",
            }

        async def orderbook(self, ticker: str) -> dict:
            return {
                "orderbook_fp": {
                    "no_dollars": [["0.5000", "2"], ["0.7600", "6"]],
                    "yes_dollars": [],
                }
            }

    db = make_db(tmp_path)
    db.update_settings({"trading_mode": "DEMO"})
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    engine.kalshi_demo = DemoPublicClient()  # type: ignore[assignment]
    market = {
        "ticker": "KXBTC15M-DEMO",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }
    live_state = {
        "ticker": market["ticker"],
        "yes_bid": 0.32,
        "yes_ask": 0.34,
        "no_bid": 0.66,
        "no_ask": 0.68,
    }

    state = asyncio.run(
        engine._execution_state_for(
            market, live_state, "2026-08-24T12:00:00+00:00"
        )
    )

    assert state["execution_market_mode"] == "DEMO"
    assert state["yes_bid"] is None
    assert state["no_ask"] is None
    assert state["no_bid"] == pytest.approx(0.76)
    assert state["yes_ask"] == pytest.approx(0.24)


def test_paper_execution_state_keeps_live_book(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    live_state = {"ticker": "KXBTC15M-LIVE", "yes_bid": 0.48, "yes_ask": 0.49}

    state = asyncio.run(
        engine._execution_state_for(
            {"ticker": "KXBTC15M-LIVE"},
            live_state,
            "2026-08-24T12:00:00+00:00",
        )
    )

    assert state["yes_ask"] == pytest.approx(0.49)
    assert state["execution_market_mode"] == "LIVE"


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
    assert 'id="orderbook-environment-label"' in markup
    assert 'id="standard-edge-hud"' in markup
    assert 'id="standard-edge-confirmation-track"' in markup
    assert 'id="standard-edge-quality-gate"' in markup
    assert markup.count('class="hud-help"') == 13
    assert 'id="standard-edge-volatility-gate"' in markup
    assert 'id="standard-edge-cushion"' in markup
    assert 'data-chart-mode="volatility"' in markup
    assert 'id: "maximum_margin_volatility"' in script
    assert 'data-tooltip="The model probability' in markup
    assert 'aria-label="About risk controls"' in markup
    assert markup.count("v={{ asset_version }}") == 2
    assert "v=0.4.1" not in markup
    assert "recent_paper_trades" in script
    assert "standard_edge_readiness" in script
    assert 'current?.execution_market_mode || "LIVE"' in script
    assert 'api("/api/model-side"' not in script
