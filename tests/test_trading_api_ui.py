from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

import app.main as main
from app.db import Database
from app.services.broker import KalshiBroker


class ApiTrading:
    def __init__(self, broker: KalshiBroker):
        self._broker = broker
        self.selected_mode = "DEMO"

    def broker(self, mode: str | None = None):
        return self._broker

    def summary(self, _current=None):
        portfolio = self._broker.portfolio()
        return {
            "selected_mode": "DEMO",
            "selected": portfolio,
            "modes": {"PAPER": {"mode": "PAPER"}, "DEMO": portfolio, "LIVE": {"mode": "LIVE"}},
        }

    def selected_summary(self, _current=None):
        portfolio = self._broker.portfolio()
        for key in ("fills", "intents", "settlements"):
            portfolio.pop(key, None)
        return {"selected_mode": "DEMO", "selected": portfolio}

    def schedule_process(self, _current=None):
        return None


def test_trading_api_exposes_isolated_structured_account_state(
    tmp_path: Path, monkeypatch,
) -> None:
    db = Database(tmp_path / "api.db")
    db.initialize()
    broker = KalshiBroker("DEMO", db)
    fake_engine = SimpleNamespace(
        trading=ApiTrading(broker),
        dashboard={"current": None},
    )
    monkeypatch.setattr(main, "engine", fake_engine)

    async def request():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/api/trading")
            selected_response = await client.get("/api/trading/selected")
            audit = await client.get("/api/trading/DEMO/audit")
        return response, selected_response, audit

    response, selected_response, audit = asyncio.run(request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_mode"] == "DEMO"
    selected = payload["selected"]
    for field in (
        "readiness", "risk_state", "reconciliation_state", "protective_exit_state", "positions",
        "orders", "fills", "intents", "settlements", "stop_loss_state",
        "profit_take_state", "strategy_results", "allocation_cap",
        "remaining_allocation",
    ):
        assert field in selected
    assert payload["modes"]["PAPER"]["mode"] == "PAPER"
    assert payload["modes"]["LIVE"]["mode"] == "LIVE"
    assert selected_response.status_code == 200
    assert selected_response.json()["selected_mode"] == "DEMO"
    assert "modes" not in selected_response.json()
    for field in ("fills", "intents", "settlements"):
        assert field not in selected_response.json()["selected"]
    assert audit.status_code == 200
    assert audit.json() == {"mode": "DEMO", "events": []}


def test_arm_disarm_immediately_refreshes_dashboard_with_monotonic_readiness(
    tmp_path: Path, monkeypatch,
) -> None:
    """A pre-action dashboard snapshot must not re-arm a confirmed disarm."""
    db = Database(tmp_path / "arming.db")
    db.initialize()
    broker = KalshiBroker("DEMO", db)
    broker._update_mode_state(authenticated=True, reconciled=True, reconciliation_required=False)
    trading = ApiTrading(broker)
    fake_engine = SimpleNamespace(trading=trading, dashboard={"current": None})

    def refresh_trading_dashboard() -> None:
        fake_engine.dashboard["trading"] = trading.summary()

    fake_engine.refresh_trading_dashboard = refresh_trading_dashboard
    monkeypatch.setattr(main, "engine", fake_engine)

    async def request():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            armed = await client.post(
                "/api/trading/DEMO/arm",
                json={"confirmation": "ARM DEMO TRADING", "automatic": False},
            )
            # This represents a response already in flight before disarm.
            stale_dashboard = dict(fake_engine.dashboard["trading"])
            disarmed = await client.post("/api/trading/DEMO/disarm")
        return armed, disarmed, stale_dashboard

    armed, disarmed, stale_dashboard = asyncio.run(request())
    assert armed.status_code == 200
    assert disarmed.status_code == 200
    assert armed.json()["arming_generation"] < disarmed.json()["arming_generation"]
    assert stale_dashboard["selected"]["readiness"]["session_armed"] is True
    assert fake_engine.dashboard["trading"]["selected"]["readiness"]["session_armed"] is False
    assert (
        stale_dashboard["selected"]["readiness"]["arming_generation"]
        < fake_engine.dashboard["trading"]["selected"]["readiness"]["arming_generation"]
    )


def test_trading_ui_contains_mode_safety_and_confirmation_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/index.html").read_text()
    script = (root / "app/static/app.js").read_text()
    styles = (root / "app/static/styles.css").read_text()

    for mode in ("PAPER", "DEMO", "LIVE"):
        assert f'data-trading-mode="{mode}"' in template
    for control in (
        "live-armed-indicator", "automatic-trading-toggle", "arm-trading",
        "hud-arm-trading",
        "kill-trading", "reconcile-trading", "trade-confirmation",
        "exchange-order-table", "position-table",
    ):
        assert f'id="{control}"' in template
    assert "Maximum exposure" in script
    assert "Estimated fees" in script
    assert "Risk review" in script
    assert 'disabled = !preview.risk?.passed' in script
    assert "ARM LIVE TRADING" in script
    assert "Type ${phrase} to arm this session" not in script
    assert 'pending ? "Confirm" : "Arm session"' in script
    assert "Click Confirm within 6 seconds" in script
    assert "VERIFY DEMO TRADING" in script
    assert "data.ledger || []" in script
    # Dashboard recent fills use the bounded, indexed recent-trades payload;
    # the complete ledger remains on the Trading page.
    assert "paper.recent_trades || []" in script
    assert "Unsettled positions" in template
    assert "they are not resting orders" in template
    assert "trade.display_status || trade.status" in script
    # Filled order rows show the confirmed execution price; resting rows fall
    # back to their limit rather than displaying a misleading 0.01 sell cap.
    assert "order.average_fill_price ?? order.limit_price" in script
    assert 'data-window="15"' in template
    assert 'data-window="180"' not in template
    assert "Settle margin" in template
    assert "threshold_margin_gate_dollars" in script
    assert "$$('[data-arm-session]')" in script
    assert 'id="volume-signals-evidence"' in template
    assert "volume_signal_report" in script
    assert "RVOL 1m / 5m" in script
    assert 'api("/api/trading/selected")' in script
    assert 'api("/api/calibration/summary")' in script
    assert 'api("/api/calibration/evidence")' in script
    assert "Loading evidence in background" in script
    assert "if (!portfolio.readiness?.reconciled)" in script
    assert "applyConfirmedTradingReadiness(mode, readiness)" in script
    assert "refreshTradingControlData()" in script
    assert '"Disarming…"' in script
    assert 'void loadPaper().catch(() => {});' in script
    assert 'passButton.disabled = Boolean(pass.scheduled)' in script
    assert 'pass.passed ? "Pass following round" : "Pass next round"' in script
    assert 'button.textContent = "Next round passed"' in script
    assert "preserveConfirmedArmingReadiness(dashboard)" in script
    assert "arming_generation" in script
    assert "data.strategy?.texas_holdem" in script
    assert '"Kalshi market data reconnecting"' in script
    assert 'id="connection-coinbase"' in template
    assert 'id="connection-kraken"' in template
    assert 'id="connection-bitstamp"' in template
    assert 'id="connection-kalshi-market"' in template
    assert 'id="connection-kalshi-account"' in template
    assert "function renderConnectionHud(streams, btc, current)" in script
    assert 'data-state="reconnecting"' in styles


def test_dashboard_theme_switch_reuses_persisted_theme_without_header_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/index.html").read_text()
    script = (root / "app/static/app.js").read_text()
    styles = (root / "app/static/styles.css").read_text()

    assert 'id="dashboard-theme-toggle"' in template
    assert 'id="last-update"' not in template
    assert 'id="header-model-version"' not in template
    assert 'localStorage.setItem("kalshi-theme-v2", preference)' in script
    assert '$("#dashboard-theme-toggle").addEventListener("change"' in script
    assert ".theme-switch input:checked + i { background: var(--green); }" in styles
    assert "#texas-pass-next-round + .standard-edge-hud-arm { margin-top: 6px; }" in styles


def test_private_stream_accepts_current_singular_event_types() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/services/streaming.py").read_text()
    assert '{"user_order", "fill", "market_position"}' in source
    assert '"channels": ["user_orders", "fill", "market_positions"]' in source
