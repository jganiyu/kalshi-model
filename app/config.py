from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppConfig:
    database_path: Path = Path(
        os.getenv("KALSHI_MODEL_DB", str(ROOT / "data" / "kalshi_model.db"))
    )
    kalshi_api_base: str = os.getenv(
        "KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2"
    )
    kalshi_series: str = os.getenv("KALSHI_SERIES", "KXBTC15M")
    kalshi_ws_url: str = os.getenv(
        "KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    )
    kalshi_api_key_id: str | None = os.getenv("KALSHI_API_KEY_ID") or None
    kalshi_private_key_path: Path | None = (
        Path(value).expanduser()
        if (value := os.getenv("KALSHI_PRIVATE_KEY_PATH"))
        else None
    )
    host: str = os.getenv("KALSHI_MODEL_HOST", "127.0.0.1")
    port: int = int(os.getenv("KALSHI_MODEL_PORT", "8765"))
    poll_seconds: float = max(2.0, float(os.getenv("KALSHI_MODEL_POLL_SECONDS", "5")))
    live_update_seconds: float = max(
        0.1, float(os.getenv("KALSHI_MODEL_LIVE_UPDATE_SECONDS", "0.1"))
    )
    open_browser: bool = os.getenv("KALSHI_MODEL_OPEN_BROWSER", "0") == "1"


DEFAULT_SETTINGS: dict[str, object] = {
    "starting_bankroll": 1000.0,
    "paper_trading_enabled": True,
    "risk_controls_enabled": True,
    "min_edge": 0.05,
    "fractional_kelly": 0.25,
    "max_position_pct": 0.05,
    "max_risk_per_trade_pct": 0.02,
    "max_session_drawdown_pct": 0.10,
    "slippage_cents": 0.5,
    "max_data_age_seconds": 20,
    "max_exchange_dispersion_pct": 0.40,
    "chart_window_minutes": 90,
    "kalshi_series": "KXBTC15M",
}
