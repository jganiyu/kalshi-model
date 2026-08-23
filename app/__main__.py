from __future__ import annotations

import threading
import webbrowser

import uvicorn

from app.config import AppConfig


def main() -> None:
    config = AppConfig()
    if config.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{config.host}:{config.port}")).start()
    uvicorn.run("app.main:app", host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
