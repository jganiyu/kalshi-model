from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.domain import iso_now, threshold_breach_exit_state
from app.main import clean_settings_payload
from app.mobile import mobile_snapshot
from app.services.paper import PaperTradingService


def make_service(tmp_path: Path, **settings: object) -> tuple[Database, PaperTradingService]:
    db = Database(tmp_path / "threshold-exit.db")
    db.initialize()
    db.update_settings(
        {
            "risk_controls_enabled": False,
            "global_profit_take_enabled": False,
            "threshold_breach_exit_enabled": True,
            "threshold_breach_exit_buffer_dollars": 0.0,
            **settings,
        }
    )
    now = iso_now()
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,raw_json,first_seen_at,updated_at
        ) VALUES ('BREACH','BREACH','active','test',100,?,?,?,?,?,?)
        """,
        (now, now, now, "{}", now, now),
    )
    return db, PaperTradingService(db)


def market(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ticker": "BREACH",
        "strike": 100.0,
        "btc_proxy": 101.0,
        "data_quality": {"reliable": True},
        "yes_bid": 0.38,
        "yes_ask": 0.40,
        "no_bid": 0.59,
        "no_ask": 0.61,
        "yes_bid_size": 1_000,
        "no_bid_size": 1_000,
    }
    values.update(updates)
    return values


def open_position(service: PaperTradingService, side: str, contracts: int = 10) -> None:
    order = service.place_order(
        ticker="BREACH",
        side=side,
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=contracts,
        limit_price=0.40 if side == "YES" else 0.61,
    )
    assert order["status"] == "filled"


def test_side_aware_threshold_math_and_exact_crossing() -> None:
    up = threshold_breach_exit_state("YES", 100, 100, buffer_dollars=0)
    assert up["exit_level"] == 100
    assert up["breached"] is True
    assert up["distance_to_exit"] == 0

    buffered_up = threshold_breach_exit_state("YES", 98, 100, buffer_dollars=-2)
    buffered_down = threshold_breach_exit_state("NO", 102, 100, buffer_dollars=-2)
    assert buffered_up["exit_level"] == 98
    assert buffered_up["breached"] is True
    assert buffered_down["exit_level"] == 102
    assert buffered_down["breached"] is True
    assert threshold_breach_exit_state(
        "YES", 98.01, 100, buffer_dollars=-2
    )["breached"] is False
    assert threshold_breach_exit_state(
        "NO", 101.99, 100, buffer_dollars=-2
    )["breached"] is False
    assert threshold_breach_exit_state(
        "YES", 102, 100, buffer_dollars=2
    )["breached"] is True
    assert threshold_breach_exit_state(
        "NO", 98, 100, buffer_dollars=2
    )["breached"] is True


@pytest.mark.parametrize(
    ("side", "safe_proxy", "breach_proxy", "exit_level"),
    [("YES", 98.01, 98.0, 98.0), ("NO", 101.99, 102.0, 102.0)],
)
def test_paper_exits_up_and_down_at_buffered_boundary(
    tmp_path: Path,
    side: str,
    safe_proxy: float,
    breach_proxy: float,
    exit_level: float,
) -> None:
    db, service = make_service(
        tmp_path, threshold_breach_exit_buffer_dollars=-2.0
    )
    open_position(service, side)

    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=safe_proxy)
    ) == 0
    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=breach_proxy)
    ) == 1

    entry = db.fetch_one("SELECT * FROM paper_entries")
    order = db.fetch_one(
        "SELECT * FROM paper_orders WHERE source='threshold_breach_exit'"
    )
    assert entry and entry["status"] == "closed"
    assert entry["exit_reason"] == "THRESHOLD_BREACH_EXIT"
    assert entry["threshold_exit_level"] == pytest.approx(exit_level)
    assert entry["threshold_trigger_btc_proxy"] == pytest.approx(breach_proxy)
    assert entry["threshold_trigger_threshold"] == pytest.approx(100.0)
    assert entry["threshold_triggered_at"]
    assert entry["threshold_exit_status"] == "Exited"
    assert order and order["filled_contracts"] == 10
    assert order["filled_price"] == pytest.approx(
        (0.38 if side == "YES" else 0.59) - 0.005
    )
    assert db.fetch_one(
        "SELECT COUNT(*) count FROM paper_trades WHERE side<>?",
        (side,),
    )["count"] == 0


def test_zero_buffer_manual_and_automatic_entries_exit_and_ignore_entry_limits(
    tmp_path: Path,
) -> None:
    db, service = make_service(tmp_path)
    open_position(service, "YES")
    db.execute(
        "UPDATE paper_entries SET source='automatic',strategy='STANDARD_EDGE'"
    )
    db.update_settings(
        {"max_risk_per_trade_pct": 0.0, "max_position_pct": 0.0}
    )

    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=100.0)
    ) == 1
    assert db.fetch_one("SELECT exit_reason FROM paper_entries")["exit_reason"] == (
        "THRESHOLD_BREACH_EXIT"
    )


def test_partial_liquidity_exits_only_filled_quantity_without_duplicates(
    tmp_path: Path,
) -> None:
    db, service = make_service(tmp_path)
    open_position(service, "YES", contracts=10)

    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=99.0, yes_bid_size=4)
    ) == 1
    entry = db.fetch_one("SELECT * FROM paper_entries")
    assert entry and entry["remaining_contracts"] == 6
    assert db.fetch_one(
        "SELECT SUM(filled_contracts) contracts FROM paper_orders "
        "WHERE source='threshold_breach_exit'"
    )["contracts"] == 4

    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=99.0, yes_bid_size=6)
    ) == 1
    assert db.fetch_one("SELECT status FROM paper_entries")["status"] == "closed"
    assert db.fetch_one(
        "SELECT SUM(filled_contracts) contracts FROM paper_orders "
        "WHERE source='threshold_breach_exit'"
    )["contracts"] == 10
    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=99.0, yes_bid_size=6)
    ) == 0


def test_unfilled_canceled_closed_disabled_and_unreliable_positions_do_not_exit(
    tmp_path: Path,
) -> None:
    db, service = make_service(tmp_path)
    unfilled = service.place_order(
        ticker="BREACH",
        side="YES",
        action="BUY",
        order_type="LIMIT",
        market=market(),
        contracts=2,
        limit_price=0.20,
    )
    assert unfilled["status"] == "open"
    assert service.cancel_order(int(unfilled["id"])) is True
    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=99.0)
    ) == 0

    open_position(service, "YES")
    assert service.process_threshold_breach_exits(
        "BREACH",
        market(btc_proxy=99.0, data_quality={"reliable": False}),
    ) == 0
    assert db.fetch_one("SELECT threshold_exit_status FROM paper_entries")[
        "threshold_exit_status"
    ] == "Blocked"
    db.update_settings({"threshold_breach_exit_enabled": False})
    assert service.process_threshold_breach_exits(
        "BREACH", market(btc_proxy=99.0)
    ) == 0
    assert db.fetch_one("SELECT status FROM paper_entries")["status"] == "open"


def test_settings_defaults_validation_and_additive_columns(tmp_path: Path) -> None:
    db, _service = make_service(tmp_path)
    settings = db.settings()
    assert settings["threshold_breach_exit_enabled"] is True
    assert settings["threshold_breach_exit_buffer_dollars"] == 0.0
    assert clean_settings_payload(
        {
            "threshold_breach_exit_enabled": False,
            "threshold_breach_exit_buffer_dollars": 2,
        }
    ) == {
        "threshold_breach_exit_enabled": False,
        "threshold_breach_exit_buffer_dollars": 2.0,
    }
    assert clean_settings_payload(
        {"threshold_breach_exit_buffer_dollars": -2}
    )["threshold_breach_exit_buffer_dollars"] == -2.0
    paper_columns = {
        row["name"] for row in db.fetch_all("PRAGMA table_info(paper_entries)")
    }
    broker_columns = {
        row["name"] for row in db.fetch_all("PRAGMA table_info(broker_positions)")
    }
    assert "threshold_trigger_btc_proxy" in paper_columns
    assert "threshold_trigger_btc_proxy" in broker_columns


def test_mobile_api_and_ui_expose_protection_without_credentials() -> None:
    protection = threshold_breach_exit_state(
        "YES", 101.0, 100.0, enabled=True, buffer_dollars=2.0
    )
    dashboard = {
        "system": {"updated_at": iso_now(), "status": "live"},
        "btc": {"price": 101.0},
        "current": {"ticker": "BREACH", "strike": 100.0},
        "paper": {"recent_paper_trades": []},
        "trading": {
            "selected_mode": "PAPER",
            "selected": {
                "available_cash": 100.0,
                "positions": [{
                    "ticker": "BREACH", "side": "YES", "contracts": 2,
                    "entry_price": .4, "committed_dollars": .8,
                    "threshold_breach_exit": protection,
                }],
            },
        },
        "credentials": {"private_key": "never-return-this"},
    }
    payload = mobile_snapshot(dashboard)
    assert payload["open_trades"][0]["threshold_breach_exit"] == protection
    assert "never-return-this" not in str(payload)

    root = Path(__file__).resolve().parents[1]
    desktop = (root / "app" / "static" / "app.js").read_text()
    mobile = (root / "app" / "mobile_static" / "mobile.js").read_text()
    template = (root / "app" / "templates" / "index.html").read_text()
    for source in (desktop, mobile):
        assert "Threshold breach exit:" in source
        assert "Distance to exit" in source
        assert "Threshold breach exit" in source
    assert "Enable Threshold Breach Exit" in desktop
    assert "Threshold exit buffer" in desktop
    assert "<th>Threshold breach exit</th>" in template
