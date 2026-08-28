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

    def summary(self):
        portfolio = self._broker.portfolio()
        return {
            "selected_mode": "DEMO",
            "selected": portfolio,
            "modes": {"PAPER": {"mode": "PAPER"}, "DEMO": portfolio, "LIVE": {"mode": "LIVE"}},
        }


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
            audit = await client.get("/api/trading/DEMO/audit")
        return response, audit

    response, audit = asyncio.run(request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_mode"] == "DEMO"
    selected = payload["selected"]
    for field in (
        "readiness", "risk_state", "reconciliation_state", "positions",
        "orders", "fills", "intents", "settlements", "stop_loss_state",
        "profit_take_state", "strategy_results", "allocation_cap",
        "remaining_allocation",
    ):
        assert field in selected
    assert payload["modes"]["PAPER"]["mode"] == "PAPER"
    assert payload["modes"]["LIVE"]["mode"] == "LIVE"
    assert audit.status_code == 200
    assert audit.json() == {"mode": "DEMO", "events": []}


def test_trading_ui_contains_mode_safety_and_confirmation_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "app/templates/index.html").read_text()
    script = (root / "app/static/app.js").read_text()

    for mode in ("PAPER", "DEMO", "LIVE"):
        assert f'data-trading-mode="{mode}"' in template
    for control in (
        "live-armed-indicator", "automatic-trading-toggle", "arm-trading",
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
    assert "paper.ledger || []" in script
    assert "Unsettled positions" in template
    assert "they are not resting orders" in template
    assert "trade.display_status || trade.status" in script


def test_private_stream_accepts_current_singular_event_types() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/services/streaming.py").read_text()
    assert '{"user_order", "fill", "market_position"}' in source
    assert '"channels": ["user_orders", "fill", "market_positions"]' in source
