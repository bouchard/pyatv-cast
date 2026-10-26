"""Command line: device lookup, source handling and the play loop (all faked)."""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyatv.auth.server_auth import CLIENT_CREDENTIALS
from pyatv.const import Protocol

from pyatv_hls import cli, hls

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _args(**kwargs):
    defaults = dict(
        source="movie.mp4", id="AA:BB", host=None, credentials=None, jit=True,
        workdir=None, start=0.0, verbose=False,
    )  # fmt: skip
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class FakeStorage:
    def __init__(self, credentials=CLIENT_CREDENTIALS):
        self.credentials = credentials

    async def load(self):
        pass

    async def get_settings(self, config):
        airplay = SimpleNamespace(credentials=self.credentials)
        return SimpleNamespace(protocols=SimpleNamespace(airplay=airplay))


class FakeConfig:
    address = "10.0.0.9"

    def __init__(self, service):
        self._service = service

    def get_service(self, protocol):
        assert protocol == Protocol.AirPlay
        return self._service


def _patch_scan(monkeypatch, configs, storage=None, seen=None):
    async def scan(loop, **kwargs):
        if seen is not None:
            seen.update(kwargs)
        return configs

    monkeypatch.setattr(cli.pyatv, "scan", scan)
    monkeypatch.setattr(
        cli.FileStorage, "default_storage", staticmethod(lambda loop: storage or FakeStorage())
    )


async def test_find_device_by_id(monkeypatch):
    seen = {}
    _patch_scan(monkeypatch, [FakeConfig(SimpleNamespace(port=7000))], seen=seen)
    address, port, credentials = await cli._find_device(_args())
    assert (address, port) == ("10.0.0.9", 7000)
    assert credentials.client_id and seen["identifier"] == "AA:BB"


async def test_find_device_by_host_with_credential_override(monkeypatch):
    seen = {}
    storage = FakeStorage(credentials=None)
    _patch_scan(monkeypatch, [FakeConfig(SimpleNamespace(port=1))], storage, seen)
    await cli._find_device(_args(id=None, host="10.0.0.9", credentials=CLIENT_CREDENTIALS))
    assert seen["hosts"] == ["10.0.0.9"]


@pytest.mark.parametrize(
    "configs,storage,message",
    [
        ([], None, "device not found"),
        ([FakeConfig(None)], None, "does not advertise AirPlay"),
        ([FakeConfig(SimpleNamespace(port=1))], FakeStorage(None), "pair first"),
    ],
)
async def test_find_device_errors(monkeypatch, configs, storage, message):
    _patch_scan(monkeypatch, configs, storage)
    with pytest.raises(SystemExit, match=message):
        await cli._find_device(_args())


async def test_serve_http_url_is_passed_through():
    url = "https://example.com/a/index.m3u8"
    assert await cli._serve(_args(source=url), "127.0.0.1", None) == (url, None, None)


async def test_serve_local_m3u8(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    url, server, duration = await cli._serve(_args(source=str(playlist)), "127.0.0.1", None)
    try:
        assert url.endswith("/index.m3u8") and duration is None
    finally:
        await server.stop()


@needs_ffmpeg
@pytest.mark.parametrize("jit", [True, False])
async def test_serve_video(tmp_path, capsys, jit):
    from tests.test_hls import _make_video

    source = tmp_path / "in.mkv"
    _make_video(source, "libx264", "aac")
    workdir = tmp_path / "work"
    url, server, duration = await cli._serve(
        _args(source=str(source), jit=jit), "127.0.0.1", workdir
    )
    try:
        assert url.endswith("/index.m3u8") and duration == pytest.approx(14, abs=0.5)
        assert isinstance(server, hls.JitHlsServer if jit else hls.StaticHlsServer)
        assert (workdir / "index.m3u8").exists() is (not jit)
    finally:
        await server.stop()
    assert ("Converting" in capsys.readouterr().out) is (not jit)


async def test_progress_prints_elapsed(capsys):
    task = asyncio.ensure_future(cli._progress(asyncio.get_running_loop().time(), 120))
    await asyncio.sleep(0.05)
    task.cancel()
    assert "/ 120s" in capsys.readouterr().out


class FakePlayer:
    instances = []

    def __init__(self, address, port, credentials):
        self.control = SimpleNamespace(local_ip="127.0.0.1")
        self.calls = []
        self.finish = True
        FakePlayer.instances.append(self)

    async def connect(self):
        self.calls.append("connect")

    async def play(self, url, title="", start=0.0):
        self.calls.append(("play", url, title, start))

    async def wait_until_ended(self):
        if not self.finish:
            await asyncio.Event().wait()

    async def stop(self):
        self.calls.append("stop")

    async def close(self):
        self.calls.append("close")


class FakeServer:
    stopped = False

    async def stop(self):
        FakeServer.stopped = True


@pytest.fixture
def fake_play(monkeypatch):
    FakePlayer.instances.clear()
    FakeServer.stopped = False
    served = {}

    async def find(args):
        return "10.0.0.9", 7000, object()

    async def serve(args, local_ip, workdir):
        served.update(local_ip=local_ip, workdir=workdir)
        return "http://127.0.0.1:1/index.m3u8", FakeServer(), served.get("duration")

    monkeypatch.setattr(cli, "_find_device", find)
    monkeypatch.setattr(cli, "_serve", serve)
    monkeypatch.setattr(cli, "HlsPlayer", FakePlayer)
    return served


async def test_play_runs_to_the_end(fake_play):
    await cli.play(_args(source="/films/My Movie.mp4", start=12.0))
    (player,) = FakePlayer.instances
    assert player.calls == [
        "connect",
        ("play", "http://127.0.0.1:1/index.m3u8", "My Movie", 12.0),
        "stop",
        "close",
    ]
    assert fake_play["workdir"] is None and FakeServer.stopped


async def test_play_gives_up_after_the_duration(fake_play, monkeypatch):
    async def serve(args, local_ip, workdir):
        return "http://x/index.m3u8", FakeServer(), -29.9  # limit = duration + 30 s

    monkeypatch.setattr(cli, "_serve", serve)
    FakePlayer.finish = False
    try:
        await asyncio.wait_for(cli.play(_args()), 5)
    finally:
        FakePlayer.finish = True
    assert FakePlayer.instances[0].calls[-2:] == ["stop", "close"]


async def test_play_without_jit_cleans_up_its_workdir(fake_play, tmp_path):
    await cli.play(_args(jit=False, workdir=str(tmp_path)))
    workdir = fake_play["workdir"]
    assert workdir.parent == tmp_path and not workdir.exists()


def test_main_parses_arguments(monkeypatch):
    ran = []
    monkeypatch.setattr(sys, "argv", ["pyatv-hls", "play", "m.mkv", "--id", "AA", "--no-jit", "-v"])
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: (ran.append(coro.cr_frame.f_locals["args"]), coro.close()))
    cli.main()
    (args,) = ran
    assert (args.source, args.id, args.jit, args.verbose) == ("m.mkv", "AA", False, True)


def test_main_ignores_ctrl_c(monkeypatch):
    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["pyatv-hls", "play", "m.mkv", "--host", "1.2.3.4"])
    monkeypatch.setattr(cli.asyncio, "run", interrupted)
    cli.main()  # must not raise
