from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.domain import texas_holdem_exit_reason, texas_holdem_phase
from app.mobile import mobile_snapshot
from app.main import clean_settings_payload
from app.services.broker import KalshiBroker
from app.services.decision import make_trade_assessment
from app.services.paper import PaperTradingService
from app.services.trading import protective_exit_reason


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "texas.db")
    db.initialize()
    db.update_settings(
        {
            "paper_trading_enabled": True,
            "texas_holdem_enabled": True,
            "texas_holdem_max_entry_price": 0.50,
            "texas_holdem_flop_target": 0.60,
            "texas_holdem_turn_target": 0.50,
            "texas_holdem_river_target": 0.95,
            "texas_holdem_river_stop": 0.60,
            "texas_holdem_entry_window_seconds": 20,
            "texas_holdem_additional_retries": 2,
            "risk_controls_enabled": False,
            "slippage_cents": 0.5,
        }
    )
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "TEXAS", "TEXAS", "active", "test", 100.0,
            "2026-09-01T12:00:00+00:00", "2026-09-01T12:15:00+00:00",
            "2026-09-01T12:15:00+00:00", None, "", "", "{}",
            "2026-09-01T12:00:00+00:00", "2026-09-01T12:00:00+00:00",
        ),
    )
    return db


def assessments(*, yes_bid: float, yes_ask: float) -> dict[str, dict]:
    market = {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": 1 - yes_ask,
        "no_ask": 1 - yes_bid,
        "yes_bid_size": 100,
        "yes_ask_size": 100,
        "no_bid_size": 100,
        "no_ask_size": 100,
    }
    quality = {"reliable": True, "trade_allowed": True, "reason": "fresh"}
    return {
        side: make_trade_assessment(
            up_probability=0.72,
            market=market,
            settings={"slippage_cents": 0.5},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }


def run_strategy(
    service: PaperTradingService,
    side_assessments: dict[str, dict],
    *,
    margin: float,
    observed_at: str = "2026-09-01T12:00:01+00:00",
    seconds_remaining: float = 899,
) -> dict:
    return service.consider_strategies(
        ticker="TEXAS",
        assessments=side_assessments,
        standard_decisions={},
        seconds_remaining=seconds_remaining,
        market_status="active",
        market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at=observed_at,
        threshold_state={},
        settlement_window={},
        z_distance=0,
        threshold_margin_dollars=margin,
        model_version="test",
    )


def test_phase_boundaries_and_exit_targets() -> None:
    assert texas_holdem_phase(899)["key"] == "FLOP"
    assert texas_holdem_phase(601)["key"] == "FLOP"
    assert texas_holdem_phase(600)["key"] == "TURN"
    assert texas_holdem_phase(301)["key"] == "TURN"
    assert texas_holdem_phase(300)["key"] == "RIVER"
    settings = {
        "texas_holdem_flop_target": 0.60,
        "texas_holdem_flop_stop": 0.60,
        "texas_holdem_turn_target": 0.50,
        "texas_holdem_turn_stop": 0.60,
        "texas_holdem_river_target": 0.95,
        "texas_holdem_river_stop": 0.60,
    }
    assert texas_holdem_exit_reason(0.60, 800, settings)[0] == "TEXAS_FLOP_TARGET"
    assert texas_holdem_exit_reason(0.59, 800, settings)[0] == "TEXAS_FLOP_STOP"
    assert texas_holdem_exit_reason(0.50, 500, settings)[0] == "TEXAS_TURN_TARGET"
    assert texas_holdem_exit_reason(
        0.59, 500, {**settings, "texas_holdem_turn_target": .95}
    )[0] == "TEXAS_TURN_STOP"
    assert texas_holdem_exit_reason(0.95, 200, settings)[0] == "TEXAS_RIVER_TARGET"
    assert texas_holdem_exit_reason(0.60, 200, settings)[0] == "TEXAS_RIVER_STOP"
    assert texas_holdem_exit_reason(0.75, 200, settings)[0] is None


def test_opening_play_is_contrarian_and_threshold_exit_exempt(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    result = run_strategy(
        service,
        assessments(yes_bid=0.52, yes_ask=0.54),
        margin=25.0,
    )
    texas = result["texas_holdem"]
    assert result["active_strategy"] == "TEXAS_HOLDEM"
    assert texas["side"] == "NO"
    assert texas["status"] in {"ATTEMPTING", "ENTERED"}
    entry = db.fetch_one(
        "SELECT * FROM paper_entries WHERE ticker='TEXAS' AND strategy='TEXAS_HOLDEM'"
    )
    assert entry is not None
    assert entry["side"] == "NO"
    assert entry["entry_price"] <= 0.50 + 1e-12
    assert entry["threshold_breach_enabled"] == 0
    assert entry["stop_loss_price"] is None


def test_price_cap_blocks_opening_attempt_without_consuming_retry(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    result = run_strategy(
        service,
        assessments(yes_bid=0.30, yes_ask=0.32),
        margin=25.0,
    )["texas_holdem"]
    assert result["status"] == "WAITING"
    assert result["attempt_count"] == 0
    assert "50¢" in result["blocker"]
    assert db.fetch_one("SELECT id FROM paper_entries WHERE ticker='TEXAS'") is None


def test_opening_expiry_uses_the_saved_window_in_status_text(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({"texas_holdem_entry_window_seconds": 7})
    service = PaperTradingService(db)
    result = run_strategy(
        service, assessments(yes_bid=0.52, yes_ask=0.54), margin=25.0,
        observed_at="2026-09-01T12:00:08+00:00", seconds_remaining=892,
    )["texas_holdem"]
    assert result["status"] == "FOLDED"
    assert result["blocker"] == "The 7-second opening play expired."


def test_retries_require_new_market_state_and_fold_after_three_attempts(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    attempts_seen: list[dict] = []

    def submit(**kwargs):
        attempts_seen.append(kwargs)
        return True, 0.05

    def call(size: float, second: int) -> dict:
        side_assessments = assessments(yes_bid=0.52, yes_ask=0.54)
        side_assessments["NO"]["ask_size"] = size
        return service.consider_strategies(
            ticker="TEXAS",
            assessments=side_assessments,
            standard_decisions={},
            seconds_remaining=900 - second,
            market_status="active",
            market_open_time="2026-09-01T12:00:00+00:00",
            market_observed_at=f"2026-09-01T12:00:{second:02d}+00:00",
            threshold_state={},
            settlement_window={},
            z_distance=0,
            threshold_margin_dollars=25.0,
            model_version="test",
            entry_exists_override=False,
            fixed_entry_handler=submit,
        )["texas_holdem"]

    assert call(100, 1)["attempt_count"] == 1
    assert call(100, 2)["attempt_count"] == 1
    assert "fresh executable quote" in call(100, 3)["blocker"]
    assert call(90, 4)["attempt_count"] == 2
    assert call(80, 5)["attempt_count"] == 3
    folded = call(70, 6)
    assert folded["status"] == "FOLDED"
    assert folded["blocker"] == "All opening-play attempts were used."
    assert len(attempts_seen) == 3
    assert all(item["time_in_force"] == "immediate_or_cancel" for item in attempts_seen)
    assert all(item["maximum_entry_price"] == pytest.approx(0.50) for item in attempts_seen)


def test_flop_target_closes_paper_position_and_records_reason(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    run_strategy(service, assessments(yes_bid=0.52, yes_ask=0.54), margin=25.0)
    closed = service.process_texas_holdem_exits(
        "TEXAS",
        {
            "time_remaining_seconds": 800,
            "no_bid": 0.65,
            "no_bid_size": 100,
            "yes_ask": 0.35,
        },
    )
    assert closed == 1
    entry = db.fetch_one("SELECT status FROM paper_entries WHERE ticker='TEXAS'")
    round_row = db.fetch_one(
        "SELECT status,exit_reason FROM texas_holdem_rounds WHERE ticker='TEXAS'"
    )
    assert entry["status"] == "closed"
    assert round_row == {"status": "EXITED", "exit_reason": "TEXAS_FLOP_TARGET"}


def test_mobile_snapshot_exposes_read_only_texas_state() -> None:
    texas = {
        "enabled": True,
        "phase": {"key": "TURN", "progress": 0.25},
        "targets": {"flop": 0.60, "turn": 0.50, "river": 0.95, "river_stop": 0.60},
        "private_key": "never",
    }
    payload = mobile_snapshot(
        {
            "system": {},
            "btc": {},
            "current": {"texas_holdem": texas},
            "paper": {},
            "trading": {"selected_mode": "PAPER", "selected": {}},
        }
    )
    assert payload["texas_holdem"]["phase"]["key"] == "TURN"
    assert "private_key" not in payload["texas_holdem"]


def test_texas_position_uses_phase_exit_instead_of_generic_protection() -> None:
    position = {
        "strategy": "TEXAS_HOLDEM",
        "side": "YES",
        "stop_loss_price": 0.70,
    }
    settings = {
        "global_profit_take_enabled": True,
        "global_profit_take_price": 0.99,
        "threshold_breach_exit_enabled": True,
        "threshold_breach_exit_buffer_dollars": 0,
        "texas_holdem_flop_target": 0.60,
        "texas_holdem_turn_target": 0.50,
        "texas_holdem_river_target": 0.95,
        "texas_holdem_river_stop": 0.60,
    }
    assert protective_exit_reason(
        position, 0.55, 800, settings,
        btc_proxy=90, threshold=100, data_reliable=True,
    )[0] == "TEXAS_FLOP_STOP"
    assert protective_exit_reason(
        position, 0.60, 800, settings,
        btc_proxy=90, threshold=100, data_reliable=True,
    )[0] == "TEXAS_FLOP_TARGET"
    assert protective_exit_reason(
        position, 0.60, 200, settings,
        btc_proxy=110, threshold=100, data_reliable=True,
    )[0] == "TEXAS_RIVER_STOP"


def test_texas_metadata_never_enters_threshold_breach_path() -> None:
    settings = {
        "global_profit_take_enabled": False,
        "threshold_breach_exit_enabled": True,
        "threshold_breach_exit_buffer_dollars": 0,
        "texas_holdem_flop_target": .95, "texas_holdem_flop_stop": .01,
    }
    reason, _ = protective_exit_reason(
        {"side": "YES", "strategy_metadata_json": '{"strategy":"TEXAS_HOLDEM"}'},
        .50, 800, settings, btc_proxy=90, threshold=100, data_reliable=True,
    )
    assert reason is None


def test_texas_retry_guard_ignores_prior_texas_ioc_but_not_other_strategy(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    broker = KalshiBroker("DEMO", db)
    values = (
        "DEMO", "client-1", "TEXAS", "NO", "BUY", 5, 0.48,
        "PARTIALLY_FILLED", "TEXAS_HOLDEM", "automatic",
        "2026-09-01T12:00:01+00:00", "2026-09-01T12:00:01+00:00",
    )
    db.execute(
        """
        INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )
    assert broker.has_automatic_entry("TEXAS") is True
    assert broker.has_automatic_entry(
        "TEXAS", exclude_strategy="TEXAS_HOLDEM"
    ) is False
    db.execute(
        """
        INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "DEMO", "client-2", "TEXAS", "YES", "BUY", 1, 0.40,
            "FILLED", "STANDARD_EDGE", "automatic",
            "2026-09-01T12:00:02+00:00", "2026-09-01T12:00:02+00:00",
        ),
    )
    assert broker.has_automatic_entry(
        "TEXAS", exclude_strategy="TEXAS_HOLDEM"
    ) is True


def test_texas_settings_are_validated_and_defaults_are_safe() -> None:
    cleaned = clean_settings_payload(
        {
            "texas_holdem_enabled": True,
            "texas_holdem_max_entry_price": 0.50,
            "texas_holdem_entry_window_seconds": 20,
            "texas_holdem_additional_retries": 2,
        }
    )
    assert cleaned == {
        "texas_holdem_enabled": True,
        "texas_holdem_max_entry_price": 0.50,
        "texas_holdem_entry_window_seconds": 20,
        "texas_holdem_additional_retries": 2,
    }
    with pytest.raises(Exception):
        clean_settings_payload({"texas_holdem_max_entry_price": 1.0})
