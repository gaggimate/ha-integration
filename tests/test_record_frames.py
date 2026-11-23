"""Characterization tests for scripts/record_frames.py (scripts/ is not a package)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from .fake_server import FakeGaggiMate

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "record_frames.py"
_spec = importlib.util.spec_from_file_location("record_frames_under_test", SCRIPT)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)


@pytest.fixture
async def server(socket_enabled):
    srv = FakeGaggiMate()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


async def _record_with_pushes(srv: FakeGaggiMate, frames: list, seconds: float = 2.0):
    async def run():
        # SystemExit inside a bare task would escape into the event loop; carry it back instead.
        try:
            return await rf.record(srv.host, srv.port, seconds)
        except SystemExit as exc:
            return exc

    task = asyncio.create_task(run())
    async with asyncio.timeout(5):
        while not srv.connections:
            await asyncio.sleep(0.01)
    for f in frames:
        await srv.push(f)
    result = await task
    if isinstance(result, SystemExit):
        raise result
    return result


async def test_record_captures_state_and_telemetry(server):
    state, telemetry = await rf.record(server.host, server.port, 2.0)
    assert "sys" in state or "warn" in state
    assert "ct" in telemetry
    assert state == server.state
    assert telemetry == server.telemetry


async def test_record_ignores_non_status_frames(server):
    server.send_initial = False
    server.keepalive = False
    junk_state = {"tp": "res:other", "sys": {"x": 1}}
    junk_telemetry = {"tp": "res:other", "ct": 99.0}
    real_state = {"tp": "evt:status", "sys": {"real": 1}}
    real_tel = {"tp": "evt:status", "ct": 93.5}
    state, telemetry = await _record_with_pushes(
        server, [junk_state, junk_telemetry, real_state, real_tel, junk_state, junk_telemetry]
    )
    assert state == real_state
    assert telemetry == real_tel


async def test_record_only_non_status_frames_exits(server):
    server.send_initial = False
    server.keepalive = False
    with pytest.raises(SystemExit):
        await _record_with_pushes(
            server, [{"tp": "res:other", "sys": {}}, {"tp": "res:other", "ct": 1}], seconds=1.5
        )


async def test_record_no_frames_exact_systemexit_message(server):
    server.send_initial = False
    server.keepalive = False
    with pytest.raises(SystemExit) as exc:
        await rf.record(server.host, server.port, 0.3)
    assert str(exc.value.code) == (
        "Did not see both a state and a telemetry frame in 0.3s "
        "(state=no, telemetry=no); try --seconds 15"
    )


async def test_record_state_only_message_reports_partial(server):
    server.send_initial = False
    server.keepalive = False
    with pytest.raises(SystemExit) as exc:
        await _record_with_pushes(server, [{"tp": "evt:status", "sys": {"a": 1}}], seconds=1.5)
    assert "state=yes, telemetry=no" in str(exc.value.code)


async def test_record_timeout_is_swallowed(server):
    # The duration elapsing raises TimeoutError inside record(); it must not escape.
    # With frames present the call returns normally; without, only SystemExit (never TimeoutError).
    result = await rf.record(server.host, server.port, 1.0)
    assert len(result) == 2
    server.send_initial = False
    server.keepalive = False
    with pytest.raises(SystemExit):
        await rf.record(server.host, server.port, 0.2)


@pytest.mark.parametrize("host", ["gaggimate.local", "192.168.1.50", "localhost", "127.0.0.1", "::1"])
def test_validate_host_accepts(host):
    assert rf._validate_host(host) == host


@pytest.mark.parametrize("host", ["http://x", "x/y", "host:80", "a b", " host", "host ", "", "host\n", "-x"])
def test_validate_host_rejects(host):
    with pytest.raises(SystemExit) as exc:
        rf._validate_host(host)
    assert "Invalid host" in str(exc.value.code)


def test_validate_out_dir_resolves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rf._validate_out_dir("sub/../frames") == (tmp_path / "frames").resolve()
    assert rf._validate_out_dir(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.parametrize("value", ["", "   "])
def test_validate_out_dir_rejects_empty(value):
    with pytest.raises(SystemExit) as exc:
        rf._validate_out_dir(value)
    assert str(exc.value.code) == "--out-dir must not be empty"


def _run_main(monkeypatch, argv, called):
    async def fake_record(host, port, seconds):
        called.append((host, port, seconds))
        return {"tp": "evt:status", "sys": {}}, {"tp": "evt:status", "ct": 1}

    monkeypatch.setattr(rf, "record", fake_record)
    monkeypatch.setattr("sys.argv", ["record_frames.py", *argv])
    return rf.main()


@pytest.mark.parametrize("name", ["recorded", "my_shot-1", "v1.2", "a.b", "...", ".hidden"])
def test_name_accepted_and_written(tmp_path, monkeypatch, name):
    called: list = []
    assert _run_main(monkeypatch, ["gaggimate.local", "--name", name, "--out-dir", str(tmp_path)], called) == 0
    assert called == [("gaggimate.local", 80, 5.0)]
    data = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))
    assert set(data) == {"state", "telemetry"}


@pytest.mark.parametrize("name", [".", "..", "a/b", "a\b", "../x", "", "a b"])
def test_name_rejected_before_connecting(tmp_path, monkeypatch, name):
    called: list = []
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["gaggimate.local", "--name", name, "--out-dir", str(tmp_path)], called)
    assert "Invalid --name" in str(exc.value.code)
    assert called == []
    assert list(tmp_path.iterdir()) == []
