from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS
from app.db import MIGRATIONS, Database
from app.domain import iso_now, kalshi_fee
from app.services.decision import Decision, make_trade_assessment
from app.services.paper import PaperTradingService


def make_db(tmp_path: Path, **settings: object) -> Database:
    db = Database(tmp_path / "swing.db")
    db.initialize()
    db.update_settings(
        {
            "paper_trading_enabled": True,
            "swing_enabled": True,
            "swing_entry_window_seconds": 300,
            "swing_max_entry_price": 0.05,
            "swing_target_exit_price": 0.10,
            "swing_bankroll_pct": 0.01,
            "swing_min_model_advantage": 0.03,
            "swing_fallback_mode": "Exit",
            "swing_fallback_seconds_remaining": 120,
            "swing_stop_loss_cents": None,
            "swing_max_spread": 0.03,
            "swing_min_liquidity_contracts": 1,
            "swing_confirmation_seconds": 0,
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "threshold_margin_gate_dollars": 0,
            **settings,
        }
    )
    return db


def add_market(db: Database, ticker: str) -> None:
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
            "2026-08-24T12:00:00+00:00", "2026-08-24T12:15:00+00:00",
            "2026-08-24T12:15:00+00:00", None, "", "", "{}",
            "2026-08-24T12:00:00+00:00", "2026-08-24T12:00:00+00:00",
        ),
    )


def hold(side: str) -> Decision:
    return Decision(
        "HOLD", "NO_EDGE", "Low", "fixture", 0.5, 0.5, 0.0, None,
        None, None, 0.0, 0.0, 0, side,
    )


def assessment(
    probability: float = 0.10,
    *,
    ask: float = 0.05,
    bid: float = 0.04,
    ask_size: float = 1_000,
    reliable: bool = True,
    slippage_cents: float = 0.5,
) -> dict[str, dict]:
    market = {
        "yes_bid": bid,
        "yes_ask": ask,
        "no_bid": 1 - ask,
        "no_ask": 1 - bid,
        "yes_ask_size": ask_size,
        "no_ask_size": ask_size,
    }
    quality = {
        "reliable": reliable,
        "trade_allowed": reliable,
        "reason": "current" if reliable else "stale",
    }
    return {
        side: make_trade_assessment(
            up_probability=probability,
            market=market,
            settings={"slippage_cents": slippage_cents},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }


def consider(
    service: PaperTradingService,
    ticker: str,
    assessments: dict[str, dict],
    observed_at: str = "2026-08-24T12:01:00+00:00",
    standard_decisions: dict[str, Decision] | None = None,
    portfolio: dict | None = None,
    now: float = 60,
    threshold_margin_dollars: float | None = None,
) -> dict:
    return service.consider_strategies(
        ticker=ticker,
        assessments=assessments,
        standard_decisions=standard_decisions or {
            "YES": hold("YES"), "NO": hold("NO")
        },
        seconds_remaining=840,
        market_status="active",
        market_open_time="2026-08-24T12:00:00+00:00",
        market_observed_at=observed_at,
        threshold_state=None,
        settlement_window={"coverage": 0.0},
        z_distance=0.0,
        threshold_margin_dollars=threshold_margin_dollars,
        model_version="test",
        portfolio=portfolio,
        now=now,
    )


def test_threshold_margin_gate_applies_to_swing_entries(tmp_path: Path) -> None:
    db = make_db(tmp_path, threshold_margin_gate_dollars=50.0)
    add_market(db, "SWING-MARGIN")
    service = PaperTradingService(db)

    blocked = consider(
        service, "SWING-MARGIN", assessment(), threshold_margin_dollars=49.0
    )
    assert blocked["entered"] is False
    assert "at least $50.00 above" in blocked["blocked_reason"]

    entered = consider(
        service, "SWING-MARGIN", assessment(), threshold_margin_dollars=55.0
    )
    assert entered["entered"] is True


def test_swing_defaults_are_conservative() -> None:
    assert DEFAULT_SETTINGS["swing_enabled"] is False
    assert DEFAULT_SETTINGS["swing_entry_window_seconds"] == 300
    assert DEFAULT_SETTINGS["swing_max_entry_price"] == pytest.approx(0.05)
    assert DEFAULT_SETTINGS["swing_target_exit_price"] == pytest.approx(0.10)
    assert DEFAULT_SETTINGS["swing_bankroll_pct"] == pytest.approx(0.01)
    assert DEFAULT_SETTINGS["swing_stop_loss_cents"] is None
    assert DEFAULT_SETTINGS["threshold_margin_gate_dollars"] == pytest.approx(50.0)


def test_exact_entry_price_qualifies_and_manual_side_is_independent(tmp_path: Path) -> None:
    db = make_db(tmp_path, selected_side="NO")
    add_market(db, "SWING-ENTRY")
    service = PaperTradingService(db)

    result = consider(service, "SWING-ENTRY", assessment())
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SWING-ENTRY'")

    assert result["entered"] is True
    assert result["active_strategy"] == "SWING"
    assert result["swing_readiness"]["side"] == "YES"
    assert result["swing_readiness"]["status"] == "ENTERED"
    assert result["swing_readiness"]["executable_ask"] == pytest.approx(0.05)
    assert result["swing_readiness"]["all_in_break_even_probability"] > 0.055
    assert entry and entry["strategy"] == "SWING"
    assert entry["entry_price"] == pytest.approx(0.055)
    assert entry["target_exit_price"] == pytest.approx(0.10)
    assert entry["fallback_exit_seconds"] == pytest.approx(120)
    assert '"maximum_entry_ask": 0.05' in entry["strategy_metadata_json"]
    api_entry = service.portfolio()["trades"][0]["entries"][0]
    assert api_entry["strategy_metadata"]["maximum_entry_ask"] == pytest.approx(0.05)
    assert set(result["swing_readiness"]["gates"]) == {
        "entry_window", "price", "model_advantage", "spread", "liquidity",
        "data", "risk",
    }


@pytest.mark.parametrize(
    ("ask", "probability", "observed_at", "expected_blocker"),
    [
        (0.051, 0.12, "2026-08-24T12:01:00+00:00", "Watching for an ask"),
        (0.05, 0.06, "2026-08-24T12:01:00+00:00", "Model advantage"),
        (0.05, 0.10, "2026-08-24T12:05:00+00:00", "window closed"),
    ],
)
def test_swing_rejects_price_probability_and_window_failures(
    tmp_path: Path,
    ask: float,
    probability: float,
    observed_at: str,
    expected_blocker: str,
) -> None:
    db = make_db(tmp_path)
    add_market(db, "SWING-BLOCK")
    service = PaperTradingService(db)

    result = consider(
        service, "SWING-BLOCK",
        assessment(probability, ask=ask, bid=max(0.01, ask - 0.01)),
        observed_at=observed_at,
    )

    assert result["entered"] is False
    assert expected_blocker.lower() in result["swing_readiness"]["blocker"].lower()
    assert db.fetch_one("SELECT id FROM paper_entries") is None


def test_swing_advantage_includes_fees_and_slippage(tmp_path: Path) -> None:
    db = make_db(tmp_path, swing_min_model_advantage=0.03)
    add_market(db, "SWING-COSTS")
    service = PaperTradingService(db)
    values = assessment(0.08, ask=0.05, bid=0.04)
    buy = values["YES"]["buy"]

    assert 0.08 - 0.05 >= 0.03
    assert buy["expected_value"] < 0.03
    result = consider(service, "SWING-COSTS", values)
    assert result["entered"] is False
    assert result["swing_readiness"]["gates"]["model_advantage"]["passed"] is False


@pytest.mark.parametrize(
    ("values", "portfolio", "gate"),
    [
        (assessment(0.12, ask=0.05, bid=0.01), None, "spread"),
        (assessment(0.12, ask_size=0), None, "liquidity"),
        (assessment(0.12, reliable=False), None, "data"),
        (
            assessment(0.12),
            {
                "automatic_trade_allowed": False,
                "automatic_trade_block_reason": "Session drawdown limit reached.",
            },
            "risk",
        ),
    ],
)
def test_swing_existing_gates_block_entry(
    tmp_path: Path, values: dict[str, dict], portfolio: dict | None, gate: str
) -> None:
    db = make_db(tmp_path)
    add_market(db, f"SWING-{gate}")
    service = PaperTradingService(db)
    result = consider(service, f"SWING-{gate}", values, portfolio=portfolio)
    assert result["entered"] is False
    assert result["swing_readiness"]["gates"][gate]["passed"] is False


def test_swing_confirmation_resets_when_a_requirement_fails(tmp_path: Path) -> None:
    db = make_db(tmp_path, swing_confirmation_seconds=2)
    add_market(db, "SWING-CONFIRM")
    service = PaperTradingService(db)
    valid = assessment(0.12)

    first = consider(service, "SWING-CONFIRM", valid, now=0)
    progressed = consider(service, "SWING-CONFIRM", valid, now=1)
    broken = consider(
        service, "SWING-CONFIRM", assessment(0.12, ask=0.06, bid=0.05), now=1.5
    )
    restarted = consider(service, "SWING-CONFIRM", valid, now=2)

    assert first["swing"]["progress"] == pytest.approx(0.0)
    assert progressed["swing"]["progress"] == pytest.approx(0.5)
    assert broken["entered"] is False
    assert restarted["swing"]["progress"] == pytest.approx(0.0)


def test_swing_uses_stronger_side_and_existing_strategy_keeps_priority(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    low = assessment(0.10)["YES"]
    stronger = {
        **low,
        "side": "NO",
        "model_probability": 0.14,
        "buy": {**low["buy"], "expected_value": 0.08, "net_edge": 0.08},
    }
    _, candidate = service._swing_candidates(
        {"YES": low, "NO": stronger}, db.settings()
    )
    assert candidate and candidate["side"] == "NO"

    add_market(db, "SWING-PRIORITY")
    values = assessment(0.80, ask=0.05, bid=0.04)
    standard = Decision(
        "BUY", "BUY_EDGE", "Moderate", "test", 0.80, 0.045, 0.70,
        0.70, 0.055, kalshi_fee(0.055), 0.01, 10.0, 10, "YES",
    )
    result = consider(
        service, "SWING-PRIORITY", values,
        standard_decisions={"YES": standard, "NO": hold("NO")},
    )
    assert result["active_strategy"] == "STANDARD_EDGE"
    assert result["swing_readiness"]["status"] == "BLOCKED"
    assert "priority" in result["swing_readiness"]["blocker"]


def open_swing(tmp_path: Path, ticker: str, **settings: object) -> tuple[Database, PaperTradingService]:
    db = make_db(tmp_path, **settings)
    add_market(db, ticker)
    service = PaperTradingService(db)
    assert consider(service, ticker, assessment())["entered"] is True
    return db, service


def exit_market(bid: float, observed_at: str, bid_size: int = 1_000) -> dict:
    return {
        "observed_at": observed_at,
        "yes_bid": bid,
        "yes_ask": min(0.99, bid + 0.01),
        "yes_bid_size": bid_size,
        "no_bid": max(0.01, 0.99 - bid),
        "no_ask": max(0.01, 1.0 - bid),
        "no_bid_size": bid_size,
    }


def test_target_uses_bid_and_realized_result_includes_costs(tmp_path: Path) -> None:
    db, service = open_swing(tmp_path, "SWING-TARGET")
    assert service.process_swing_exits(
        "SWING-TARGET", exit_market(0.099, "2026-08-24T12:03:00+00:00")
    ) == 0
    assert service.process_swing_exits(
        "SWING-TARGET", exit_market(0.10, "2026-08-24T12:03:01+00:00")
    ) == 1

    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SWING-TARGET'")
    order = db.fetch_one(
        "SELECT * FROM paper_orders WHERE ticker='SWING-TARGET' AND action='SELL'"
    )
    assert entry and entry["exit_reason"] == "TARGET"
    assert entry["exit_price"] == pytest.approx(0.095)
    assert order and order["source"] == "swing_target"
    gross_move = (0.095 - float(entry["entry_price"])) * int(entry["initial_contracts"])
    assert float(order["realized_pnl"]) < gross_move


def test_fallback_hold_settlement_and_stop_exits(tmp_path: Path) -> None:
    fallback_db, fallback = open_swing(tmp_path / "fallback", "SWING-FALLBACK")
    assert fallback.process_swing_exits(
        "SWING-FALLBACK", exit_market(0.04, "2026-08-24T12:13:00+00:00")
    ) == 1
    assert fallback_db.fetch_one(
        "SELECT exit_reason FROM paper_entries WHERE ticker='SWING-FALLBACK'"
    )["exit_reason"] == "FALLBACK"

    hold_db, hold_service = open_swing(
        tmp_path / "hold", "SWING-HOLD", swing_fallback_mode="Hold to settlement"
    )
    assert hold_service.process_swing_exits(
        "SWING-HOLD", exit_market(0.04, "2026-08-24T12:14:30+00:00")
    ) == 0
    assert hold_service.settle("SWING-HOLD", 0, "2026-08-24T12:15:00+00:00") == 1
    assert hold_db.fetch_one(
        "SELECT exit_reason FROM paper_entries WHERE ticker='SWING-HOLD'"
    )["exit_reason"] == "SETTLEMENT"

    stop_db, stop_service = open_swing(
        tmp_path / "stop", "SWING-STOP", swing_stop_loss_cents=3
    )
    assert stop_service.process_open_orders(
        "SWING-STOP", exit_market(0.03, "2026-08-24T12:02:00+00:00")
    ) == 1
    assert stop_db.fetch_one(
        "SELECT exit_reason FROM paper_entries WHERE ticker='SWING-STOP'"
    )["exit_reason"] == "STOP_LOSS"


def test_swing_partial_exit_liquidity_and_no_reentry(tmp_path: Path) -> None:
    db, service = open_swing(tmp_path, "SWING-PARTIAL")
    entry = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SWING-PARTIAL'")
    assert entry and int(entry["remaining_contracts"]) > 1
    assert service.process_swing_exits(
        "SWING-PARTIAL",
        exit_market(0.10, "2026-08-24T12:03:00+00:00", bid_size=1),
    ) == 1
    partial = db.fetch_one("SELECT * FROM paper_entries WHERE ticker='SWING-PARTIAL'")
    assert partial and partial["status"] == "open"
    assert int(partial["remaining_contracts"]) == int(entry["remaining_contracts"]) - 1

    service.process_swing_exits(
        "SWING-PARTIAL",
        exit_market(0.10, "2026-08-24T12:03:01+00:00"),
    )
    result = consider(service, "SWING-PARTIAL", assessment())
    assert result["entered"] is False
    assert "already exists" in result["blocked_reason"]
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM paper_entries WHERE ticker='SWING-PARTIAL'"
    )["count"] == 1


def test_swing_results_and_additive_migration(tmp_path: Path) -> None:
    db, service = open_swing(tmp_path / "results", "SWING-RESULT")
    service.process_swing_exits(
        "SWING-RESULT", exit_market(0.10, "2026-08-24T12:03:00+00:00")
    )
    result = service.strategy_results()["SWING"]
    assert result["entries"] == 1
    assert result["completed_trades"] == 1
    assert result["target_hit_rate"] == pytest.approx(1.0)
    assert result["average_exit_price"] == pytest.approx(0.095)
    assert result["average_holding_seconds"] is not None
    assert result["return_on_deployed_capital"] > 0

    legacy = Database(tmp_path / "legacy.db")
    with legacy.transaction() as connection:
        for version, sql in MIGRATIONS[:8]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
    legacy.initialize()
    columns = {
        row["name"] for row in legacy.fetch_all("PRAGMA table_info(paper_entries)")
    }
    assert "target_exit_price" in columns
    assert "exit_reason" in columns
    assert legacy.fetch_one(
        "SELECT MAX(version) version FROM schema_migrations"
        )["version"] == 14


def test_swing_controls_and_results_are_rendered() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "app/static/app.js").read_text()
    assert '["Swing Trade", [' in script
    assert 'id: "swing_max_entry_price"' in script
    assert 'id: "swing_target_exit_price"' in script
    assert 'SWING: "Swing trade"' in script
    assert "swing_readiness" in script
