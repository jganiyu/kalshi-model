from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

import app.mobile as mobile
from app.config import DEFAULT_SETTINGS
from app.db import Database
from app.mobile import create_mobile_app, mobile_snapshot


class FakeDatabase:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def settings(self) -> dict[str, object]:
        return {"mobile_monitor_enabled": self.enabled}


class FakeEngine:
    def __init__(self, dashboard: dict):
        self.dashboard = dashboard
        self.queues: set[asyncio.Queue[None]] = set()

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self.queues.discard(queue)


def readiness() -> dict:
    return {
        "side": "NO",
        "status": "CONFIRMING",
        "metrics": {
            "probability": {"current": 0.72, "required": 0.65, "progress": 1, "passed": True},
            "net_ev": {"current": 0.084, "required": 0.105, "progress": 0.8, "passed": False},
            "confirmation": {
                "current_seconds": 3.2, "required_seconds": 5, "progress": 0.64,
                "passed": False, "locked": True,
            },
        },
        "gates": {
            "spread": {"current": 0.012, "required": 0.03, "passed": True},
            "liquidity": {"current": 140, "required": 42, "passed": True},
            "data": {"passed": True, "detail": "Fresh"},
            "quality": {"current": "Moderate", "required": "Moderate", "passed": True},
            "threshold_margin": {
                "enabled": True, "current": -50.25, "required": -50.0,
                "passed": True, "detail": "Outside the threshold band",
            },
            "volatility": {
                "enabled": True, "current": 6.2, "required": 7.5,
                "cushion_ratio": 1.4, "passed": True, "status": "PASS",
                "detail": "Volatility is within the configured limit.",
            },
            "risk": {"passed": True, "detail": "Clear"},
        },
        "blocker": "Waiting for 2.1¢ more EV",
    }


def dashboard(mode: str = "PAPER") -> dict:
    paper_trades = [
        {
            "opened_at": f"2026-08-28T12:{index:02d}:00+00:00",
            "side": "YES",
            "entry_price": 0.51,
            "contracts": index + 1,
            "strategy": "STANDARD_EDGE",
            "status": "settled",
            "realized_pnl": 1.25,
            "available_cash_after": 1001.25,
            "settlement_margin": 23.75,
        }
        for index in range(12)
    ]
    demo_trade = {**paper_trades[0], "side": "NO", "strategy": "DEMO_ONLY"}
    live_trade = {**paper_trades[0], "side": "YES", "strategy": "LIVE_ONLY"}
    modes = {
        "PAPER": {
            "mode": "PAPER", "available_cash": 995.0,
            "positions": [{
                "ticker": "KXBTC15M-TEST", "side": "YES", "contracts": 10,
                "entry_price": 0.51, "committed_dollars": 5.1,
                "strategy": "STANDARD_EDGE",
            }],
        },
        "DEMO": {"mode": "DEMO", "available_cash": 500.0, "positions": [], "ledger": [demo_trade]},
        "LIVE": {"mode": "LIVE", "available_cash": 300.0, "positions": [], "ledger": [live_trade]},
    }
    return {
        "system": {"status": "live", "updated_at": "2026-08-28T12:00:00+00:00"},
        "btc": {"price": 79_500.25},
        "current": {
            "strike": 79_450.0,
            "time_remaining_seconds": 182,
            "yes_ask": 0.80,
            "no_ask": 0.21,
            "standard_edge_readiness": readiness(),
        },
        "paper": {"recent_paper_trades": paper_trades},
        "trading": {"selected_mode": mode, "selected": modes[mode], "modes": modes},
        "credentials": {"api_key": "must-never-leave-the-mac"},
    }


def request(app, method: str, path: str):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mobile") as client:
            return await client.request(method, path)

    return asyncio.run(run())


def test_mobile_snapshot_matches_dashboard_hud_market_and_recent_trades() -> None:
    source = dashboard()
    payload = mobile_snapshot(source)
    assert payload["readiness"] == source["current"]["standard_edge_readiness"]
    assert payload["readiness"]["gates"]["volatility"]["cushion_ratio"] == 1.4
    assert payload["market"] == {
        "to_beat": 79_450.0,
        "btc_proxy": 79_500.25,
        "btc_margin": 50.25,
        "time_remaining_seconds": 182,
        "up_price": 0.80,
        "down_price": 0.21,
    }
    assert payload["recent_trades"] == source["paper"]["recent_paper_trades"][:10]
    assert payload["available_cash"] == 995.0
    assert payload["open_trades"] == [{
        "ticker": "KXBTC15M-TEST", "side": "YES", "contracts": 10,
        "entry_price": 0.51, "exposure": 5.1,
        "strategy": "STANDARD_EDGE", "status": None,
    }]


def test_mobile_histories_are_isolated_by_selected_environment() -> None:
    assert mobile_snapshot(dashboard("PAPER"))["recent_trades"][0]["strategy"] == "STANDARD_EDGE"
    assert mobile_snapshot(dashboard("DEMO"))["recent_trades"][0]["strategy"] == "DEMO_ONLY"
    assert mobile_snapshot(dashboard("LIVE"))["recent_trades"][0]["strategy"] == "LIVE_ONLY"


def test_mobile_surface_has_no_mutations_or_sensitive_data() -> None:
    source = dashboard()
    source["current"]["standard_edge_readiness"]["private_key"] = "secret"
    source["current"]["standard_edge_readiness"]["gates"]["risk"]["api_key_id"] = "secret"
    app = create_mobile_app(FakeEngine(source), FakeDatabase(), lambda: 8767)

    response = request(app, "GET", "/api/snapshot")
    assert response.status_code == 200
    serialized = response.text.lower()
    for forbidden in ("private_key", "api_key", "credential", "must-never-leave", "secret"):
        assert forbidden not in serialized
    for method, path in (
        ("POST", "/api/snapshot"),
        ("PUT", "/api/settings"),
        ("POST", "/api/trading/LIVE/orders"),
        ("DELETE", "/api/credentials"),
    ):
        assert request(app, method, path).status_code in {404, 405}


def test_mobile_monitor_is_disabled_by_default_and_gates_every_http_route() -> None:
    assert DEFAULT_SETTINGS["mobile_monitor_enabled"] is False
    app = create_mobile_app(FakeEngine(dashboard()), FakeDatabase(enabled=False), lambda: 8767)
    for path in ("/", "/api/snapshot", "/manifest.webmanifest", "/assets/mobile.js"):
        response = request(app, "GET", path)
        assert response.status_code == 503


def test_mobile_setting_migration_is_additive_and_preserves_existing_configuration_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "existing.db")
    db.initialize()
    db.update_settings({"starting_bankroll": 4321.0})
    snapshots_before = db.configuration_snapshots()
    db.execute("DELETE FROM settings WHERE key='mobile_monitor_enabled'")

    db.initialize()

    assert db.settings()["mobile_monitor_enabled"] is False
    assert db.settings()["starting_bankroll"] == 4321.0
    assert db.configuration_snapshots() == snapshots_before


def test_mobile_responses_are_never_cached() -> None:
    app = create_mobile_app(FakeEngine(dashboard()), FakeDatabase(), lambda: 8767)
    for path in ("/", "/api/snapshot", "/manifest.webmanifest", "/service-worker.js"):
        response = request(app, "GET", path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert response.headers["pragma"] == "no-cache"
    service_worker = request(app, "GET", "/service-worker.js").text
    assert 'cache: "no-store"' in service_worker
    assert "caches.delete" in service_worker
    assert "caches.open" not in service_worker


def test_mobile_frontend_supports_live_reconnect_theme_persistence_and_compact_layout() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "app/mobile_static/mobile.js").read_text()
    styles = (root / "app/mobile_static/mobile.css").read_text()
    template = (root / "app/templates/mobile.html").read_text()
    for state in ("Connecting", "Live", "Reconnecting", "Offline"):
        assert state in script or state in template
    assert 'localStorage.getItem("kalshi-mobile-theme-v1")' in script
    assert 'localStorage.setItem("kalshi-mobile-theme-v1", theme)' in script
    assert "new WebSocket" in script
    assert "reconnectDelay" in script
    assert "overflow-x: hidden" in styles
    assert "env(safe-area-inset-top)" in styles
    assert 'id="theme-toggle"' in template
    assert 'id="market-to-beat"' in template
    assert 'id="market-btc-proxy"' in template
    assert 'id="market-btc-margin"' in template
    assert 'id="market-timer"' in template
    assert 'id="open-trade-list"' in template
    assert 'id="current-up-price"' in template
    assert 'id="current-down-price"' in template
    assert 'id="available-funds"' in template
    assert "renderOpenTrades" in script
    assert "settlement_margin" in script
    assert 'id="copy-mobile-monitor-command"' in (
        root / "app/templates/index.html"
    ).read_text()
    assert 'id="refresh-mobile-monitor-url"' in (
        root / "app/templates/index.html"
    ).read_text()


def test_app_store_tailscale_command_forces_cli_and_discovers_private_url(monkeypatch) -> None:
    binary = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    observed: dict[str, object] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout='{"Self":{"DNSName":"jonathans-mac.example.ts.net."}}',
        )

    monkeypatch.setattr(mobile, "_tailscale_binary", lambda: binary)
    monkeypatch.setattr(mobile.subprocess, "run", run)

    assert mobile.tailscale_private_url() == "https://jonathans-mac.example.ts.net"
    assert observed["command"] == [binary, "status", "--json"]
    assert observed["env"]["TAILSCALE_BE_CLI"] == "1"
    status = mobile.mobile_status(FakeDatabase(), 8767)
    assert status["tailscale_command"] == (
        "TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale "
        "serve --bg http://127.0.0.1:8767"
    )


def test_mobile_status_detects_stale_tailscale_forwarding(monkeypatch) -> None:
    binary = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

    def run(command, **_kwargs):
        if command[1:] == ["status", "--json"]:
            payload = {"Self": {"DNSName": "monitor.example.ts.net."}}
        else:
            payload = {
                "Web": {
                    "monitor.example.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}
                    }
                }
            }
        import json
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(mobile, "_tailscale_binary", lambda: binary)
    monkeypatch.setattr(mobile.subprocess, "run", run)

    stale = mobile.mobile_status(FakeDatabase(), 8768)
    assert stale["tailscale_ready"] is False
    assert stale["private_url"] is None
    assert stale["detected_private_url"] == "https://monitor.example.ts.net"
    assert "points to http://127.0.0.1:8767" in stale["tailscale_issue"]
    assert stale["tailscale_command"].endswith("http://127.0.0.1:8768")

    ready = mobile.mobile_status(FakeDatabase(), 8767)
    assert ready["tailscale_ready"] is True
    assert ready["private_url"] == "https://monitor.example.ts.net"
    assert ready["tailscale_issue"] is None


def test_mobile_websocket_sends_current_snapshot_and_live_environment_updates() -> None:
    from fastapi.testclient import TestClient

    engine = FakeEngine(dashboard("DEMO"))
    app = create_mobile_app(engine, FakeDatabase(), lambda: 8767)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as websocket:
            message = websocket.receive_json()
            assert message["type"] == "snapshot"
            assert message["data"]["mode"] == "DEMO"
            assert message["data"]["recent_trades"][0]["strategy"] == "DEMO_ONLY"
            engine.dashboard = dashboard("LIVE")
            updated = websocket.receive_json()
            assert updated["data"]["mode"] == "LIVE"
            assert updated["data"]["recent_trades"][0]["strategy"] == "LIVE_ONLY"
