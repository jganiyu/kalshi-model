from __future__ import annotations

import socket
import threading
import webbrowser

import uvicorn

from app.config import AppConfig


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


def main() -> None:
    config = AppConfig()
    port = find_available_port(config.host, config.port)
    if port != config.port:
        print(
            f"Port {config.port} is already in use; starting Kalshi Model on {port}.",
            flush=True,
        )
    if config.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{config.host}:{port}")).start()
    uvicorn.run("app.main:app", host=config.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
