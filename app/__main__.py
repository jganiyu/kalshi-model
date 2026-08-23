from __future__ import annotations

import socket
import threading
import time
import webbrowser
from typing import Any

import uvicorn

from app.config import FROZEN, AppConfig
from app.services.credentials import credential_directory


NATIVE_WINDOW_WIDTH = 1440
NATIVE_WINDOW_HEIGHT = 900
NATIVE_WINDOW_MIN_SIZE = (1000, 680)
SERVER_START_TIMEOUT_SECONDS = 30.0


def _port_is_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        return False
    return True


def find_available_port(host: str, preferred_port: int, search_limit: int = 100) -> int:
    if not 1 <= preferred_port <= 65535:
        raise ValueError("preferred_port must be between 1 and 65535")
    if search_limit < 1:
        raise ValueError("search_limit must be positive")
    final_port = min(65535, preferred_port + search_limit - 1)
    for port in range(preferred_port, final_port + 1):
        if _port_is_available(host, port):
            return port
    raise RuntimeError(f"No available local port found from {preferred_port} to {final_port}")


class LocalAppServer:
    def __init__(self, host: str, port: int):
        uvicorn_config = uvicorn.Config(
            "app.main:app",
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.thread = threading.Thread(
            target=self.server.run,
            name="kalshi-model-server",
            daemon=True,
        )

    def start(self, timeout: float = SERVER_START_TIMEOUT_SECONDS) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started and time.monotonic() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError("Kalshi Model's local service stopped during startup.")
            time.sleep(0.05)
        if not self.server.started:
            self.stop()
            raise TimeoutError("Kalshi Model's local service did not start in time.")

    def stop(self, timeout: float = 10.0) -> None:
        if not self.thread.is_alive():
            return
        self.server.should_exit = True
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=2.0)


def _load_webview() -> Any:
    import webview

    return webview


def run_native_app(config: AppConfig, port: int) -> None:
    webview = _load_webview()
    server = LocalAppServer(config.host, port)
    storage_path = credential_directory() / "webview"
    storage_path.mkdir(parents=True, exist_ok=True)
    url = f"http://{config.host}:{port}"

    server.start()
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.create_window(
            "Kalshi Model",
            url,
            width=NATIVE_WINDOW_WIDTH,
            height=NATIVE_WINDOW_HEIGHT,
            min_size=NATIVE_WINDOW_MIN_SIZE,
            background_color="#f7f7f5",
            zoomable=True,
        )
        webview.start(
            gui="cocoa",
            private_mode=False,
            storage_path=str(storage_path),
        )
    finally:
        server.stop()


def run_browser_app(config: AppConfig, port: int) -> None:
    if config.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{config.host}:{port}")).start()
    uvicorn.run("app.main:app", host=config.host, port=port, log_level="info")


def main() -> None:
    config = AppConfig()
    port = find_available_port(config.host, config.port)
    if port != config.port:
        print(
            f"Port {config.port} is already in use; starting Kalshi Model on {port}.",
            flush=True,
        )
    if FROZEN:
        run_native_app(config, port)
    else:
        run_browser_app(config, port)


if __name__ == "__main__":
    main()
