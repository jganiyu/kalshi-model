from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

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


def test_port_probe_requests_immediate_restart_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def setsockopt(self, *args: object) -> None:
            calls.append(args)

        def bind(self, address: tuple[str, int]) -> None:
            calls.append(("bind", address))

    monkeypatch.setattr(launcher.socket, "socket", lambda *_args: Probe())

    assert launcher._port_is_available("127.0.0.1", 8767) is True
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls
    assert ("bind", ("127.0.0.1", 8767)) in calls


@pytest.mark.parametrize("port", [0, 65536])
def test_find_available_port_rejects_invalid_preference(port: int) -> None:
    with pytest.raises(ValueError):
        launcher.find_available_port("127.0.0.1", port)


def test_native_trackpad_zoom_enables_wkwebview_magnification() -> None:
    calls: list[bool] = []
    native_webview = SimpleNamespace(
        setAllowsMagnification_=lambda enabled: calls.append(enabled)
    )
    window = SimpleNamespace(
        uid="master",
        gui=SimpleNamespace(
            BrowserView=SimpleNamespace(
                instances={"master": SimpleNamespace(webview=native_webview)}
            )
        ),
    )

    launcher._enable_native_trackpad_zoom(window)

    assert calls == [True]


def test_native_app_opens_window_and_stops_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeServer:
        def __init__(self, host: str, port: int, *args: object):
            calls.append(("server", (host, port, *args)))

        def start(self) -> None:
            calls.append(("start", None))

        def stop(self) -> None:
            calls.append(("stop", None))

    class FakeWebview:
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(title: str, url: str, **kwargs: object) -> None:
            calls.append(("window", (title, url, kwargs)))

        @staticmethod
        def start(**kwargs: object) -> None:
            calls.append(("webview", kwargs))

    monkeypatch.setattr(launcher, "LocalAppServer", FakeServer)
    monkeypatch.setattr(launcher, "find_available_port", lambda *_args: 8767)
    monkeypatch.setattr(launcher, "_load_webview", lambda: FakeWebview)
    monkeypatch.setattr(launcher, "credential_directory", lambda: tmp_path)

    launcher.run_native_app(SimpleNamespace(host="127.0.0.1"), 8765)

    assert calls[0] == ("server", ("127.0.0.1", 8765))
    assert calls[1] == ("start", None)
    assert calls[-1] == ("stop", None)
    assert calls[2] == (
        "server",
        ("127.0.0.1", 8767, "app.main:mobile_app", "kalshi-model-mobile-monitor"),
    )
    assert calls[3] == ("start", None)
    assert calls[4][0] == "window"
    assert calls[4][1][0:2] == ("Kalshi Model", "http://127.0.0.1:8765")
    assert calls[5] == (
        "webview",
        {
            "gui": "cocoa",
            "private_mode": False,
            "storage_path": str(tmp_path / "webview"),
        },
    )


def test_native_app_stops_server_when_window_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stopped = False

    class FakeServer:
        def __init__(self, _host: str, _port: int, *_args: object):
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            nonlocal stopped
            stopped = True

    class FailingWebview:
        settings: dict[str, object] = {}

        @staticmethod
        def create_window(_title: str, _url: str, **_kwargs: object) -> None:
            raise RuntimeError("window failed")

    monkeypatch.setattr(launcher, "LocalAppServer", FakeServer)
    monkeypatch.setattr(launcher, "find_available_port", lambda *_args: 8767)
    monkeypatch.setattr(launcher, "_load_webview", lambda: FailingWebview)
    monkeypatch.setattr(launcher, "credential_directory", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="window failed"):
        launcher.run_native_app(SimpleNamespace(host="127.0.0.1"), 8765)

    assert stopped is True
