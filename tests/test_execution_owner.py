from __future__ import annotations

import asyncio
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import __main__ as launcher
from app.services.execution_owner import ExecutionOwnerError, ExecutionOwnerLock


PROBE = """
import sys
from pathlib import Path
from app.services.execution_owner import ExecutionOwnerError, ExecutionOwnerLock
try:
    with ExecutionOwnerLock(Path(sys.argv[1])):
        print('owned', flush=True)
        if len(sys.argv) > 2:
            sys.stdin.readline()
except ExecutionOwnerError as exc:
    print(str(exc), file=sys.stderr, flush=True)
    sys.exit(23)
"""


def probe(database: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(database)],
        capture_output=True, text=True, timeout=5,
    )


def test_another_process_is_blocked_until_release_without_touching_db(tmp_path: Path) -> None:
    database = tmp_path / "existing.db"
    database.write_bytes(b"existing database contents")
    with ExecutionOwnerLock(database) as owner:
        inode = owner.path.stat().st_ino
        denied = probe(database)
        assert denied.returncode == 23
        assert "already running" in denied.stderr
        assert denied.stdout == ""
        # Independent databases have independent execution ownership.
        assert probe(tmp_path / "other.db").returncode == 0
    assert probe(database).returncode == 0
    assert owner.path.stat().st_ino == inode
    assert database.read_bytes() == b"existing database contents"


def test_os_releases_ownership_when_process_is_killed(tmp_path: Path) -> None:
    database = tmp_path / "crash.db"
    process = subprocess.Popen(
        [sys.executable, "-c", PROBE, str(database), "hold"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 5)[0], "Owner did not start"
        assert process.stdout.readline().strip() == "owned"
        with pytest.raises(ExecutionOwnerError, match="already running"):
            with ExecutionOwnerLock(database):
                pytest.fail("A second process acquired execution ownership")
        process.kill()
        process.communicate(timeout=5)
        with ExecutionOwnerLock(database):
            pass
        assert not database.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_database_symlink_uses_same_owner(tmp_path: Path) -> None:
    database = tmp_path / "data.db"
    database.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(database)
    with ExecutionOwnerLock(database):
        assert probe(alias).returncode == 23


def test_duplicate_launcher_fails_before_port_search_or_server_start(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "data.db"
    monkeypatch.setattr(launcher, "AppConfig", lambda: SimpleNamespace(database_path=database))
    monkeypatch.setattr(
        launcher, "find_available_port", lambda *_: pytest.fail("Searched alternate ports")
    )
    with ExecutionOwnerLock(database):
        with pytest.raises(SystemExit, match="already running"):
            launcher.main()


@pytest.mark.parametrize("native", [True, False])
def test_launcher_preflight_releases_for_definitive_lifespan_lock(
    tmp_path: Path, monkeypatch, native: bool,
) -> None:
    database = tmp_path / "data.db"
    config = SimpleNamespace(database_path=database, host="127.0.0.1", port=8765)
    started: list[str] = []
    monkeypatch.setattr(launcher, "AppConfig", lambda: config)
    monkeypatch.setattr(launcher, "FROZEN", native)
    monkeypatch.setattr(launcher, "find_available_port", lambda *_: 8766)

    def start(config, port):
        with ExecutionOwnerLock(config.database_path):
            started.append("engine")
            assert port == 8766

    monkeypatch.setattr(launcher, "run_native_app", start)
    monkeypatch.setattr(launcher, "run_browser_app", start)
    launcher.main()
    assert started == ["engine"]
    assert probe(database).returncode == 0


@pytest.fixture
def lifespan_app(tmp_path: Path, monkeypatch):
    # Import the real lifespan without resolving any configured API credentials.
    monkeypatch.setattr("app.config.resolve_credentials", lambda: (None, None, "none"))
    from app import main

    monkeypatch.setattr(main, "config", SimpleNamespace(database_path=tmp_path / "data.db"))
    return main


def test_lifespan_denies_second_engine_before_database_initialization(lifespan_app, monkeypatch) -> None:
    async def scenario() -> None:
        def forbidden():
            pytest.fail("Duplicate service initialized its database")

        monkeypatch.setattr(lifespan_app, "db", SimpleNamespace(initialize=forbidden))
        with ExecutionOwnerLock(lifespan_app.config.database_path):
            with pytest.raises(ExecutionOwnerError, match="already running"):
                async with lifespan_app.lifespan(lifespan_app.app):
                    pytest.fail("Duplicate execution service started")

    asyncio.run(scenario())


def test_lifespan_holds_owner_through_engine_shutdown_and_allows_restart(lifespan_app, monkeypatch) -> None:
    async def scenario() -> None:
        events: list[str] = []
        stop_started, finish_stop = asyncio.Event(), asyncio.Event()

        async def start():
            events.append("start")

        async def stop():
            stop_started.set()
            await finish_stop.wait()
            events.append("stop")

        monkeypatch.setattr(lifespan_app, "db", SimpleNamespace(
            initialize=lambda: events.append("initialize")
        ))
        monkeypatch.setattr(lifespan_app, "engine", SimpleNamespace(start=start, stop=stop))
        context = lifespan_app.lifespan(lifespan_app.app)
        await context.__aenter__()
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(stop_started.wait(), 1)
        try:
            with pytest.raises(ExecutionOwnerError):
                with ExecutionOwnerLock(lifespan_app.config.database_path):
                    pytest.fail("Owner released before engine stopped")
        finally:
            finish_stop.set()
            await closing
        # A later TestClient/app lifespan can acquire normally after shutdown.
        async with lifespan_app.lifespan(lifespan_app.app):
            pass
        assert events == ["initialize", "start", "stop"] * 2

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_start", [True, False])
def test_lifespan_cleans_up_and_releases_on_failure(lifespan_app, monkeypatch, fail_start) -> None:
    async def scenario() -> None:
        stopped: list[bool] = []

        async def start():
            if fail_start:
                raise RuntimeError("startup failed")

        async def stop():
            stopped.append(True)

        monkeypatch.setattr(lifespan_app, "db", SimpleNamespace(initialize=lambda: None))
        monkeypatch.setattr(lifespan_app, "engine", SimpleNamespace(start=start, stop=stop))
        with pytest.raises(RuntimeError, match="failed"):
            async with lifespan_app.lifespan(lifespan_app.app):
                raise RuntimeError("runtime failed")
        assert stopped == [True]
        with ExecutionOwnerLock(lifespan_app.config.database_path):
            pass

    asyncio.run(scenario())
