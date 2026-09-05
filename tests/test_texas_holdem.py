from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.domain import TEXAS_HOLDEM_V2, texas_holdem_exit_reason, texas_holdem_phase
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
    result = {
        side: make_trade_assessment(
            up_probability=0.72,
            market=market,
            settings={"slippage_cents": 0.5},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }
    for value in result.values():
        value["margin_volatility"] = {
            "mvi": 8.0, "reliable": True, "reliability_state": "RELIABLE",
            "observed_at": "2026-09-01T12:00:00+00:00",
        }
    return result


def run_strategy(
    service: PaperTradingService,
    side_assessments: dict[str, dict],
    *,
    margin: float,
    observed_at: str = "2026-09-01T12:00:01+00:00",
    seconds_remaining: float = 899,
    ticker: str = "TEXAS",
    market_open_time: str = "2026-09-01T12:00:00+00:00",
) -> dict:
    return service.consider_strategies(
        ticker=ticker,
        assessments=side_assessments,
        standard_decisions={},
        seconds_remaining=seconds_remaining,
        market_status="active",
        market_open_time=market_open_time,
        market_observed_at=observed_at,
        threshold_state={},
        settlement_window={},
        z_distance=0,
        threshold_margin_dollars=margin,
        model_version="test",
    )


def test_texas_pass_skips_only_next_round_persists_and_keeps_environment_isolated(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    quotes = assessments(yes_bid=.45, yes_ask=.46)

    scheduled = service.texas_holdem_pass_next_round(
        environment="DEMO", source_ticker="PREVIOUS",
        market_open_time="2026-09-01T11:45:00+00:00",
    )
    # Repeated clicks schedule the same round, rather than a second pass.
    assert service.texas_holdem_pass_next_round(
        environment="DEMO", source_ticker="PREVIOUS",
        market_open_time="2026-09-01T11:45:00+00:00",
    ) == scheduled

    # A DEMO pass does not alter Paper, and does not alter the user's settings.
    paper = run_strategy(service, quotes, margin=10, observed_at="2026-09-01T12:00:01+00:00")
    assert paper["texas_holdem"]["status"] != "PASSED"
    assert db.settings()["paper_trading_enabled"] is True

    # Recreating the service proves the pending pass is durable across restart.
    restored = PaperTradingService(db)
    passed = run_strategy(
        restored, quotes, margin=10, ticker="DEMO-TEXAS",
        observed_at="2026-09-01T12:00:01+00:00",
    )
    # That was PAPER again; its entry remains eligible.
    assert passed["texas_holdem"]["status"] != "PASSED"
    demo_passed = restored._texas_holdem_state(
        ticker="DEMO-TEXAS", assessments=quotes, opening_elapsed=1,
        seconds_remaining=899, threshold_margin_dollars=10,
        market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:01+00:00", status_open=True,
        execution_mode="DEMO", automatic_enabled=True,
        execution_block_reason=None, entry_exists=False, model_version="test",
        fixed_entry_handler=lambda **_: (True, .45), execution_risk_by_side={},
    )
    assert demo_passed["status"] == "PASSED"
    assert "next round remains eligible" in demo_passed["blocker"]

    # A consumed pass does not lock the control: while the passed market is
    # current, schedule exactly its following market for a separate pass.
    following = restored.texas_holdem_pass_next_round(
        environment="DEMO", source_ticker="DEMO-TEXAS",
        market_open_time="2026-09-01T12:00:00+00:00",
    )
    assert following["target_open_time"].startswith("2026-09-01T12:15:00")

    next_round = restored._texas_holdem_state(
        ticker="DEMO-NEXT", assessments=quotes, opening_elapsed=1,
        seconds_remaining=899, threshold_margin_dollars=10,
        market_open_time="2026-09-01T12:15:00+00:00",
        market_observed_at="2026-09-01T12:15:01+00:00", status_open=True,
        execution_mode="DEMO", automatic_enabled=True,
        execution_block_reason=None, entry_exists=False, model_version="test",
        fixed_entry_handler=lambda **_: (True, .45), execution_risk_by_side={},
    )
    assert next_round["status"] == "PASSED"


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

    # Zero is an explicit phase-stop opt-out, not a 0¢ trigger. Targets still
    # work independently in every phase.
    disabled = {
        **settings,
        "texas_holdem_flop_stop": 0,
        "texas_holdem_turn_stop": 0,
        "texas_holdem_river_stop": 0,
        "texas_holdem_flop_target": .95,
        "texas_holdem_turn_target": .95,
        "texas_holdem_river_target": .99,
    }
    assert texas_holdem_exit_reason(.01, 800, disabled)[0] is None
    assert texas_holdem_exit_reason(.01, 500, disabled)[0] is None
    assert texas_holdem_exit_reason(.01, 200, disabled)[0] is None


def test_opening_play_is_contrarian_and_threshold_exit_exempt(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    result = run_strategy(
        service,
        assessments(yes_bid=0.52, yes_ask=0.54),
        margin=25.0,
    )
    texas = result["texas_holdem"]
    assert result["active_strategy"] == TEXAS_HOLDEM_V2
    assert texas["side"] == "NO"
    assert texas["status"] in {"ATTEMPTING", "ENTERED"}
    entry = db.fetch_one(
        "SELECT * FROM paper_entries WHERE ticker='TEXAS' AND strategy='TEXAS_HOLDEM_2_0'"
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


def test_texas_v2_requires_reliable_mvi_on_each_entry_attempt(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    quotes = assessments(yes_bid=.52, yes_ask=.54)
    quotes["NO"]["margin_volatility"] = {
            "mvi": 3.99, "reliable": True, "observed_at": "2026-09-01T12:00:00+00:00"
    }
    blocked = run_strategy(service, quotes, margin=25.0)["texas_holdem"]
    assert blocked["attempt_count"] == 0
    assert "MVI ≥ 4.0" in blocked["blocker"]
    quotes["NO"]["margin_volatility"] = {
        "mvi": 8.0, "reliable": False, "observed_at": "2026-09-01T12:00:00+00:00"
    }
    unavailable = run_strategy(
        service, quotes, margin=25.0, observed_at="2026-09-01T12:00:02+00:00"
    )["texas_holdem"]
    assert unavailable["attempt_count"] == 0
    assert "fresh reliable MVI" in unavailable["blocker"]
    quotes["NO"]["margin_volatility"] = {
        "mvi": 8.0, "reliable": True, "observed_at": "2026-09-01T12:00:00+00:00"
    }
    admitted = run_strategy(
        service, quotes, margin=25.0, observed_at="2026-09-01T12:00:03+00:00"
    )["texas_holdem"]
    assert admitted["attempt_count"] == 1
    evidence = db.fetch_one("SELECT evidence_json FROM texas_holdem_attempts WHERE attempt_number=1")
    assert '"mvi_minimum": 4.0' in str(evidence["evidence_json"])


def test_texas_v2_mvi_gate_boost_and_mode_isolation(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({
        "paper_texas_holdem_v2_mvi_minimum": 4.0,
        "demo_texas_holdem_v2_mvi_minimum": 6.0,
    })
    service = PaperTradingService(db)
    quotes = assessments(yes_bid=.52, yes_ask=.54)
    seen: list[dict] = []

    def submit(**kwargs):
        seen.append(kwargs)
        return True, kwargs["bankroll_fraction"]

    quotes["NO"]["margin_volatility"] = {
        "mvi": 3.99, "reliable": True, "observed_at": "2026-09-01T12:00:00+00:00",
    }
    blocked = service._texas_holdem_state(
        ticker="GATE", assessments=quotes, opening_elapsed=1, seconds_remaining=899,
        threshold_margin_dollars=25, market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:01+00:00", status_open=True,
        execution_mode="PAPER", automatic_enabled=True, execution_block_reason=None,
        entry_exists=False, model_version="test", fixed_entry_handler=submit,
        execution_risk_by_side={},
    )
    assert blocked["attempt_count"] == 0 and "MVI ≥ 4.0" in blocked["blocker"]
    quotes["NO"]["margin_volatility"]["mvi"] = 4.0
    admitted = service._texas_holdem_state(
        ticker="GATE", assessments=quotes, opening_elapsed=1, seconds_remaining=899,
        threshold_margin_dollars=25, market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:02+00:00", status_open=True,
        execution_mode="PAPER", automatic_enabled=True, execution_block_reason=None,
        entry_exists=False, model_version="test", fixed_entry_handler=submit,
        execution_risk_by_side={},
    )
    assert admitted["attempt_count"] == 1
    assert seen[-1]["bankroll_fraction"] == pytest.approx(.01)
    quotes["NO"]["margin_volatility"]["mvi"] = 8.0
    quotes["NO"]["ask_size"] = 99  # a new executable quote permits retry
    boosted = service._texas_holdem_state(
        ticker="GATE", assessments=quotes, opening_elapsed=3, seconds_remaining=897,
        threshold_margin_dollars=25, market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:03+00:00", status_open=True,
        execution_mode="PAPER", automatic_enabled=True, execution_block_reason=None,
        entry_exists=False, model_version="test", fixed_entry_handler=submit,
        execution_risk_by_side={},
    )
    assert boosted["attempt_count"] == 2
    assert seen[-1]["bankroll_fraction"] == pytest.approx(.015)
    evidence = db.fetch_one(
        "SELECT evidence_json FROM texas_holdem_attempts WHERE attempt_number=2"
    ) or {}
    assert '"mvi_boost_multiplier": 1.5' in evidence["evidence_json"]
    # The Demo gate remains distinct from Paper's saved value.
    quotes["NO"]["margin_volatility"]["mvi"] = 5.0
    demo = service._texas_holdem_state(
        ticker="DEMO-GATE", assessments=quotes, opening_elapsed=1, seconds_remaining=899,
        threshold_margin_dollars=25, market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:01+00:00", status_open=True,
        execution_mode="DEMO", automatic_enabled=True, execution_block_reason=None,
        entry_exists=False, model_version="test", fixed_entry_handler=submit,
        execution_risk_by_side={},
    )
    assert demo["attempt_count"] == 0 and "MVI ≥ 6.0" in demo["blocker"]


def test_legacy_texas_does_not_gain_v2_gate_or_rules(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,status,entry_price_cap,
             flop_target,turn_target,river_target,river_stop,created_at,updated_at)
           VALUES ('PAPER','LEGACY','WAITING',.5,.6,.5,.95,.6,?,?)""",
        ("2026-09-01T12:00:00+00:00", "2026-09-01T12:00:00+00:00"),
    )
    quotes = assessments(yes_bid=.52, yes_ask=.54)
    quotes["NO"]["margin_volatility"] = {}
    entered = PaperTradingService(db)._texas_holdem_state(
        ticker="LEGACY", assessments=quotes, opening_elapsed=1, seconds_remaining=899,
        threshold_margin_dollars=25, market_open_time="2026-09-01T12:00:00+00:00",
        market_observed_at="2026-09-01T12:00:01+00:00", status_open=True,
        execution_mode="PAPER", automatic_enabled=True, execution_block_reason=None,
        entry_exists=False, model_version="test", fixed_entry_handler=lambda **_: (True, .05),
        execution_risk_by_side={},
    )
    assert entered["strategy"] == "TEXAS_HOLDEM"
    assert entered["display_name"] == "Texas Hold’em"
    assert entered["rules"] == {}
    assert entered["attempt_count"] == 1


def test_texas_v2_clock_moves_to_earlier_authoritative_fill_and_stale_btc_does_not_latch(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,strategy,side,threshold,status,
             entry_price_cap,flop_target,turn_target,river_target,river_stop,created_at,updated_at)
           VALUES ('PAPER','CLOCK','TEXAS_HOLDEM_2_0','NO',100,'ENTERED',.5,.6,.5,.95,.6,?,?)""",
        ("2026-09-01T12:00:00+00:00", "2026-09-01T12:00:00+00:00"),
    )
    row = db.fetch_one("SELECT * FROM texas_holdem_rounds WHERE ticker='CLOCK'") or {}
    row = service._ensure_texas_v2_fill_clock(row, "2026-09-01T12:02:00+00:00")
    row = service._ensure_texas_v2_fill_clock(row, "2026-09-01T12:01:00+00:00")
    assert row["first_filled_at"].startswith("2026-09-01T12:01:00")
    assert row["thesis_checkpoint_at"].startswith("2026-09-01T12:06:00")
    stale = service.texas_v2_thesis_state(
        row, btc_proxy=100, observed_at="2026-09-01T12:06:00+00:00",
        btc_observed_at="2026-09-01T12:05:00+00:00", data_reliable=True,
    )
    assert stale["btc_fresh"] is False and stale["status"] == "WAITING"
    missing = service.texas_v2_thesis_state(
        row, btc_proxy=float("nan"), observed_at="2026-09-01T12:06:00+00:00",
        btc_observed_at=None, data_reliable=True,
    )
    assert missing["btc_fresh"] is False and missing["status"] == "WAITING"
    assert db.fetch_one("SELECT post_fill_breached_at FROM texas_holdem_rounds WHERE ticker='CLOCK'")["post_fill_breached_at"] is None


def test_v2_breach_latches_when_global_exit_has_priority(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    run_strategy(service, assessments(yes_bid=.52, yes_ask=.54), margin=25.0)
    assert service.process_texas_holdem_exits("TEXAS", {
        "observed_at": "2026-09-01T12:02:00+00:00",
        "btc_observed_at": "2026-09-01T12:02:00+00:00",
        "time_remaining_seconds": 780, "btc_proxy": 100.0,
        "data_quality": {"reliable": True}, "no_bid": .99, "no_bid_size": 100,
    }) == 1
    row = db.fetch_one(
        "SELECT exit_reason,post_fill_breached_at FROM texas_holdem_rounds WHERE ticker='TEXAS'"
    ) or {}
    assert row["exit_reason"] == "GLOBAL_PROFIT_TAKE"
    assert row["post_fill_breached_at"].startswith("2026-09-01T12:02:00")


def test_texas_v2_thesis_checkpoint_is_one_shot_and_strictly_over_fifty(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({
        "texas_holdem_flop_target": .95, "texas_holdem_turn_target": .95,
        "texas_holdem_river_target": .99, "texas_holdem_flop_stop": 0,
        "texas_holdem_turn_stop": 0, "texas_holdem_river_stop": 0,
    })
    service = PaperTradingService(db)
    run_strategy(service, assessments(yes_bid=.52, yes_ask=.54), margin=25.0)
    # NO was entered at 12:00:01, so 12:05:01 is the immutable checkpoint.
    closed = service.process_texas_holdem_exits("TEXAS", {
        "observed_at": "2026-09-01T12:05:01+00:00",
        "btc_observed_at": "2026-09-01T12:05:01+00:00",
        "time_remaining_seconds": 599, "btc_proxy": 150.0,
        "data_quality": {"reliable": True}, "no_bid": .40, "no_bid_size": 100,
    })
    assert closed == 0  # $50 exactly is not strictly greater than $50.
    row = db.fetch_one(
        "SELECT strategy,first_filled_at,thesis_checkpoint_at,thesis_status FROM texas_holdem_rounds WHERE ticker='TEXAS'"
    )
    assert row["strategy"] == TEXAS_HOLDEM_V2
    assert row["first_filled_at"].startswith("2026-09-01T12:00:01")
    assert row["thesis_checkpoint_at"].startswith("2026-09-01T12:05:01")
    assert row["thesis_status"] == "NO_EXIT"
    # The terminal no-exit result never trails a later, worse proxy.
    assert service.process_texas_holdem_exits("TEXAS", {
        "observed_at": "2026-09-01T12:06:00+00:00",
        "btc_observed_at": "2026-09-01T12:06:00+00:00",
        "time_remaining_seconds": 540, "btc_proxy": 180.0,
        "data_quality": {"reliable": True}, "no_bid": .40, "no_bid_size": 100,
    }) == 0
    assert db.fetch_one("SELECT thesis_status FROM texas_holdem_rounds WHERE ticker='TEXAS'")["thesis_status"] == "NO_EXIT"


def test_texas_v2_thesis_exit_and_post_fill_breach_latch(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.update_settings({
        "texas_holdem_flop_target": .95, "texas_holdem_turn_target": .95,
        "texas_holdem_river_target": .99, "texas_holdem_flop_stop": 0,
        "texas_holdem_turn_stop": 0, "texas_holdem_river_stop": 0,
    })
    service = PaperTradingService(db)
    run_strategy(service, assessments(yes_bid=.52, yes_ask=.54), margin=25.0)
    # A post-fill touch is a breach and prevents the thesis-failure exit.
    service.process_texas_holdem_exits("TEXAS", {
        "observed_at": "2026-09-01T12:02:00+00:00",
        "btc_observed_at": "2026-09-01T12:02:00+00:00",
        "time_remaining_seconds": 780, "btc_proxy": 100.0,
        "data_quality": {"reliable": True}, "no_bid": .40, "no_bid_size": 100,
    })
    assert service.process_texas_holdem_exits("TEXAS", {
        "observed_at": "2026-09-01T12:05:01+00:00",
        "btc_observed_at": "2026-09-01T12:05:01+00:00",
        "time_remaining_seconds": 599, "btc_proxy": 180.0,
        "data_quality": {"reliable": True}, "no_bid": .40, "no_bid_size": 100,
    }) == 0
    assert db.fetch_one("SELECT thesis_status,post_fill_breached_at FROM texas_holdem_rounds WHERE ticker='TEXAS'")["thesis_status"] == "BREACHED"

    # An independent new V2 round exits at the same exact checkpoint when the
    # thesis has not breached and BTC is strictly more than $50 unfavorable.
    db.execute(
        """INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) SELECT 'TEXAS-2',event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at FROM markets WHERE ticker='TEXAS'"""
    )
    run_strategy(service, assessments(yes_bid=.52, yes_ask=.54), margin=25.0,
                 ticker="TEXAS-2")
    assert service.process_texas_holdem_exits("TEXAS-2", {
        "observed_at": "2026-09-01T12:05:01+00:00",
        "btc_observed_at": "2026-09-01T12:05:01+00:00",
        "time_remaining_seconds": 599, "btc_proxy": 151.0,
        "data_quality": {"reliable": True}, "no_bid": .40, "no_bid_size": 100,
    }) == 1
    assert db.fetch_one("SELECT exit_reason FROM texas_holdem_rounds WHERE ticker='TEXAS-2'")["exit_reason"] == "TEXAS_THESIS_FAILURE"


def test_texas_v2_new_rounds_are_labeled_without_relabeling_legacy_data(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,status,entry_price_cap,
             flop_target,turn_target,river_target,river_stop,created_at,updated_at)
           VALUES ('PAPER','LEGACY','WAITING',.5,.6,.5,.95,.6,?,?)""",
        ("2026-09-01T11:00:00+00:00", "2026-09-01T11:00:00+00:00"),
    )
    assert db.fetch_one("SELECT strategy FROM texas_holdem_rounds WHERE ticker='LEGACY'")["strategy"] == "TEXAS_HOLDEM"
    run_strategy(PaperTradingService(db), assessments(yes_bid=.52, yes_ask=.54), margin=25.0)
    assert db.fetch_one("SELECT strategy FROM texas_holdem_rounds WHERE ticker='TEXAS'")["strategy"] == TEXAS_HOLDEM_V2


def test_texas_v2_rehydrates_post_fill_breach_from_durable_review_history(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    db.execute(
        """INSERT INTO texas_holdem_rounds(
            environment,ticker,strategy,threshold,side,status,entry_price_cap,
            flop_target,turn_target,river_target,river_stop,first_filled_at,
            thesis_checkpoint_at,created_at,updated_at
        ) VALUES ('PAPER','REHYDRATE','TEXAS_HOLDEM_2_0',100,'NO','ENTERED',.5,
            .6,.5,.95,.6,?,?,?,?)""",
        (
            "2026-09-01T12:00:00+00:00", "2026-09-01T12:05:00+00:00",
            "2026-09-01T12:00:00+00:00", "2026-09-01T12:00:00+00:00",
        ),
    )
    session_id = db.execute(
        """INSERT INTO trade_review_sessions(
            environment,ticker,recording_started_at,created_at,status,calculation_version
        ) VALUES ('PAPER','REHYDRATE',? ,?,'RECORDING','test')""",
        ("2026-09-01T12:00:00+00:00", "2026-09-01T12:00:00+00:00"),
    )
    db.execute(
        """INSERT INTO trade_review_points(
            session_id,observed_at,btc_proxy,threshold,data_reliable,state_json
        ) VALUES (?,?,?,?,1,'{}')""",
        (session_id, "2026-09-01T12:02:00+00:00", 100.0, 100.0),
    )
    row = db.fetch_one("SELECT * FROM texas_holdem_rounds WHERE ticker='REHYDRATE'") or {}
    restored = service.texas_v2_thesis_state(
        row, btc_proxy=180.0, observed_at="2026-09-01T12:05:00+00:00",
        btc_observed_at="2026-09-01T12:05:00+00:00", data_reliable=True
    )
    assert restored["status"] == "BREACHED"
    assert db.fetch_one("SELECT post_fill_breached_at FROM texas_holdem_rounds WHERE ticker='REHYDRATE'")["post_fill_breached_at"].startswith("2026-09-01T12:02:00")


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


def test_v2_reconciliation_needs_entry_evidence_and_partial_entry_blocks_retry(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    broker = KalshiBroker("DEMO", db)
    now = "2026-09-01T12:00:00+00:00"
    # A waiting/passed V2 round by itself is not enough to relabel unrelated
    # exchange activity as an automatic Texas position or fill.
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,strategy,status,entry_price_cap,
             flop_target,turn_target,river_target,river_stop,created_at,updated_at)
           VALUES ('DEMO','BARE','TEXAS_HOLDEM_2_0','WAITING',.5,.6,.5,.95,.6,?,?)""",
        (now, now),
    )
    broker._upsert_position({"ticker": "BARE", "position_fp": "2.00"})
    broker._upsert_fill({
        "fill_id": "bare-fill", "ticker": "BARE", "side": "YES", "action": "BUY",
        "count": "1.00", "yes_price_dollars": ".40", "created_time": now,
    })
    bare = db.fetch_one(
        "SELECT strategy,source FROM broker_positions WHERE mode='DEMO' AND ticker='BARE'"
    ) or {}
    fill = db.fetch_one(
        "SELECT strategy FROM broker_fills WHERE mode='DEMO' AND fill_id='bare-fill'"
    ) or {}
    assert bare == {"strategy": None, "source": None}
    assert fill["strategy"] is None

    db.execute(
        """INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at
        ) VALUES ('DEMO','v2-partial','LINKED','YES','BUY',5,.4,'PARTIALLY_FILLED',
                  'TEXAS_HOLDEM_2_0','automatic',?,?)""",
        (now, now),
    )
    broker._upsert_position({"ticker": "LINKED", "position_fp": "2.00"})
    linked = db.fetch_one(
        "SELECT strategy,source FROM broker_positions WHERE mode='DEMO' AND ticker='LINKED'"
    ) or {}
    assert linked == {"strategy": TEXAS_HOLDEM_V2, "source": "automatic"}
    assert broker.has_automatic_entry("LINKED") is True
    db.execute("UPDATE broker_order_intents SET status='CANCELED' WHERE client_order_id='v2-partial'")
    assert broker.has_automatic_entry("LINKED") is False
    db.execute("UPDATE broker_order_intents SET status='REJECTED' WHERE client_order_id='v2-partial'")
    assert broker.has_automatic_entry("LINKED") is False


def test_v2_fractional_confirmed_remainder_never_rounds_up_to_buy_more(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    service = PaperTradingService(db)
    now = "2026-09-01T12:00:00+00:00"
    db.execute(
        """INSERT INTO texas_holdem_rounds(
            environment,ticker,strategy,side,status,entry_price_cap,flop_target,turn_target,
            river_target,river_stop,target_contracts,created_at,updated_at
        ) VALUES ('DEMO','FRACTION','TEXAS_HOLDEM_2_0','NO','PARTIALLY_FILLED',.5,.6,.5,
                  .95,.6,5,?,?)""",
        (now, now),
    )
    db.execute(
        """INSERT INTO broker_positions(
            mode,ticker,side,contracts,average_price,market_exposure,realized_pnl,fees,
            strategy,source,opened_at,updated_at,status
        ) VALUES ('DEMO','FRACTION','NO',4.5,.4,1.8,0,0,'TEXAS_HOLDEM_2_0',
                  'automatic',?,?,'open')""",
        (now, now),
    )
    called = False

    def submit(**_):
        nonlocal called
        called = True
        return True, .05

    state = service._texas_holdem_state(
        ticker="FRACTION", assessments=assessments(yes_bid=.52, yes_ask=.54),
        opening_elapsed=1, seconds_remaining=899, threshold_margin_dollars=25,
        market_open_time=now, market_observed_at="2026-09-01T12:00:01+00:00",
        status_open=True, execution_mode="DEMO", automatic_enabled=True,
        execution_block_reason=None, entry_exists=False, model_version="test",
        fixed_entry_handler=submit, execution_risk_by_side={},
    )
    assert called is False
    assert state["attempt_count"] == 0
    assert "confirmed exposure" in state["blocker"]


def test_texas_settings_are_validated_and_defaults_are_safe() -> None:
    cleaned = clean_settings_payload(
        {
            "texas_holdem_enabled": True,
            "texas_holdem_max_entry_price": 0.50,
            "texas_holdem_entry_window_seconds": 20,
            "texas_holdem_additional_retries": 2,
            "live_texas_holdem_v2_mvi_minimum": 4.0,
        }
    )
    assert cleaned == {
        "texas_holdem_enabled": True,
        "texas_holdem_max_entry_price": 0.50,
        "texas_holdem_entry_window_seconds": 20,
        "texas_holdem_additional_retries": 2,
        "live_texas_holdem_v2_mvi_minimum": 4.0,
    }
    with pytest.raises(Exception):
        clean_settings_payload({"texas_holdem_max_entry_price": 1.0})
    with pytest.raises(Exception):
        clean_settings_payload({"live_texas_holdem_v2_mvi_minimum": 10.1})
