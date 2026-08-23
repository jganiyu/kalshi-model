from __future__ import annotations

import socket

import pytest

from app import __main__ as launcher


def test_find_available_port_skips_ports_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[int] = []

    def available(_host: str, port: int) -> bool:
        checked.append(port)
        return port == 8767

    monkeypatch.setattr(launcher, "_port_is_available", available)

    assert launcher.find_available_port("127.0.0.1", 8765) == 8767
    assert checked == [8765, 8766, 8767]


def test_port_probe_detects_active_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        assert launcher._port_is_available("127.0.0.1", port) is False


@pytest.mark.parametrize("port", [0, 65536])
def test_find_available_port_rejects_invalid_preference(port: int) -> None:
    with pytest.raises(ValueError):
        launcher.find_available_port("127.0.0.1", port)
