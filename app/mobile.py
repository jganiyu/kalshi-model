from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import ROOT


MOBILE_ROOT = ROOT / "app" / "mobile_static"
MOBILE_TEMPLATE = ROOT / "app" / "templates" / "mobile.html"
SENSITIVE_KEY_PARTS = ("credential", "private_key", "api_key", "secret", "token")


def _safe_copy(value: Any) -> Any:
    """Copy dashboard data while refusing credential-shaped keys at any depth."""
    if isinstance(value, dict):
        return {
            str(key): _safe_copy(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_safe_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_copy(item) for item in value]
    return copy.deepcopy(value)


def mobile_snapshot(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Return only the Dashboard data needed by the read-only phone surface."""
    trading = dashboard.get("trading") or {}
    mode = str(trading.get("selected_mode") or "PAPER").upper()
    modes = trading.get("modes") or {}
    selected = trading.get("selected") or modes.get(mode) or {}
    current = dashboard.get("current") or {}
    readiness = current.get("standard_edge_readiness") or (
        current.get("automatic_entry") or {}
    ).get("standard_edge_readiness")
    if mode == "PAPER":
        trades = (dashboard.get("paper") or {}).get("recent_paper_trades") or []
    else:
        trades = selected.get("ledger") or []
    allowed_trade_fields = {
        "activity_at",
        "opened_at",
        "filled_at",
        "side",
        "entry_price",
        "price",
        "contracts",
        "strategy",
        "source",
        "status",
        "display_status",
        "action",
        "realized_pnl",
        "available_cash_after",
    }
    recent_trades = [
        {
            key: _safe_copy(value)
            for key, value in trade.items()
            if key in allowed_trade_fields
        }
        for trade in trades[:10]
    ]
    system = dashboard.get("system") or {}
    btc = dashboard.get("btc") or {}
    return {
        "mode": mode,
        "updated_at": system.get("updated_at"),
        "source_status": system.get("status"),
        "market": {
            "to_beat": current.get("strike"),
            "btc_proxy": btc.get("price"),
            "time_remaining_seconds": current.get("time_remaining_seconds"),
        },
        "readiness": _safe_copy(readiness),
        "recent_trades": recent_trades,
    }


def _tailscale_binary() -> str | None:
    discovered = shutil.which("tailscale")
    if discovered:
        return discovered
    bundled = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return str(bundled) if bundled.exists() else None


def tailscale_private_url() -> str | None:
    binary = _tailscale_binary()
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            check=False,
            env={**os.environ, "TAILSCALE_BE_CLI": "1"},
            text=True,
            timeout=1.5,
        )
        if completed.returncode:
            return None
        dns_name = str((json.loads(completed.stdout).get("Self") or {}).get("DNSName") or "")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return f"https://{dns_name.rstrip('.')}" if dns_name else None


def tailscale_serve_target() -> str | None:
    binary = _tailscale_binary()
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "serve", "status", "--json"],
            capture_output=True,
            check=False,
            env={**os.environ, "TAILSCALE_BE_CLI": "1"},
            text=True,
            timeout=1.5,
        )
        if completed.returncode:
            return None
        web = json.loads(completed.stdout).get("Web") or {}
        for host in web.values():
            for handler in (host.get("Handlers") or {}).values():
                proxy = str(handler.get("Proxy") or "")
                if proxy:
                    return proxy.rstrip("/")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError):
        return None
    return None


def mobile_status(db: Any, port: int) -> dict[str, Any]:
    enabled = bool(db.settings().get("mobile_monitor_enabled", False))
    binary = _tailscale_binary()
    cli = (
        f"TAILSCALE_BE_CLI=1 {binary}"
        if binary and binary.startswith("/Applications/")
        else binary or "tailscale"
    )
    expected_target = f"http://127.0.0.1:{port}"
    detected_url = tailscale_private_url() if enabled else None
    serve_target = tailscale_serve_target() if enabled else None
    tailscale_ready = bool(detected_url and serve_target == expected_target)
    tailscale_issue = None
    if enabled and not binary:
        tailscale_issue = "Install and sign in to Tailscale on this Mac."
    elif enabled and not detected_url:
        tailscale_issue = "Sign in to Tailscale on this Mac, then refresh."
    elif enabled and serve_target != expected_target:
        tailscale_issue = (
            f"Tailscale Serve points to {serve_target}. Copy and run the updated command."
            if serve_target else "Run the copied Tailscale Serve command, then refresh."
        )
    return {
        "enabled": enabled,
        "status": "running" if enabled else "disabled",
        "port": port,
        "local_url": expected_target if enabled else None,
        "private_url": detected_url if tailscale_ready else None,
        "detected_private_url": detected_url,
        "tailscale_ready": tailscale_ready,
        "tailscale_target": serve_target,
        "tailscale_issue": tailscale_issue,
        "tailscale_command": f"{cli} serve --bg {expected_target}",
    }


def create_mobile_app(
    engine: Any,
    db: Any,
    port_provider: Callable[[], int],
) -> FastAPI:
    app = FastAPI(
        title="Kalshi Model Mobile Monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/assets", StaticFiles(directory=MOBILE_ROOT), name="mobile-assets")

    @app.middleware("http")
    async def secure_read_only_surface(request: Request, call_next: Any) -> Response:
        enabled = bool(db.settings().get("mobile_monitor_enabled", False))
        if not enabled:
            response: Response = JSONResponse(
                {"detail": "Mobile Monitor is disabled on the Mac."}, status_code=503
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def mobile_index() -> HTMLResponse:
        return HTMLResponse(MOBILE_TEMPLATE.read_text(encoding="utf-8"))

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(
            MOBILE_ROOT / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/service-worker.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            MOBILE_ROOT / "service-worker.js",
            media_type="application/javascript",
        )

    @app.get("/icon-180.png")
    async def icon_180() -> FileResponse:
        return FileResponse(ROOT / "app" / "static" / "icon-180.png", media_type="image/png")

    @app.get("/icon-1024.png")
    async def icon_1024() -> FileResponse:
        return FileResponse(ROOT / "app" / "static" / "icon-1024.png", media_type="image/png")

    @app.get("/api/snapshot")
    async def snapshot() -> dict[str, Any]:
        return mobile_snapshot(engine.dashboard)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, **mobile_status(db, port_provider())}

    @app.websocket("/ws/live")
    async def live(websocket: WebSocket) -> None:
        if not bool(db.settings().get("mobile_monitor_enabled", False)):
            await websocket.close(code=1008, reason="Mobile Monitor is disabled.")
            return
        await websocket.accept()
        try:
            payload = mobile_snapshot(engine.dashboard)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            await websocket.send_json({"type": "snapshot", "data": payload})
            while True:
                await asyncio.sleep(0.25)
                if not bool(db.settings().get("mobile_monitor_enabled", False)):
                    await websocket.close(code=1008, reason="Mobile Monitor was disabled.")
                    return
                payload = mobile_snapshot(engine.dashboard)
                next_encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if next_encoded != encoded:
                    encoded = next_encoded
                    await websocket.send_json({"type": "snapshot", "data": payload})
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise

    return app
