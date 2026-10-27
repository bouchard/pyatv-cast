"""The command line: choosing how to deliver, playing, error messages and exit codes."""

import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from pyatv import exceptions as pyatv_exceptions

from pyatv_cast import cli, config, dlna, sources, stream, targets
from pyatv_cast.errors import UserError
from pyatv_cast.airplay import AirPlayError
from pyatv_cast.sources import Input, MediaSource
from pyatv_cast.targets import TV, AirPlayInfo, Target
from tests.test_hls import _make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SINK_MKV = {"video/mp4", "video/x-mkv", "video/vnd.dlna.mpeg-tts"}
SINK_MP4_ONLY = {"video/mp4"}
SINK_HLS = SINK_MKV | {"application/vnd.apple.mpegurl"}


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def summary(video="h264", pix="yuv420p", audio="aac", duration=60.0):
    return stream.Summary(0, video, pix, 0 if audio else None, 0, audio, duration)


def local(path="/m/a.mkv"):
    return MediaSource("a", [Input(path)], Path(path))


def remote(url="http://h/a.mp4", **kw):
    return MediaSource("a", [Input(url)], **kw)


# -- choosing how to deliver -------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,source,summ,sink,hls,kind,reasons",
    [
        ("auto", local("/m/a.mkv"), summary(), SINK_MKV, False, "file", []),
        ("auto", local("/m/a.mkv"), summary(audio="dts"), SINK_MKV, False, "stream", ["audio is dts"]),
        ("auto", local("/m/a.mkv"), summary(audio="dts"), SINK_HLS, True, "hls", ["audio is dts"]),
        ("auto", local("/m/a.avi"), summary(), SINK_MP4_ONLY, False, "stream", ["the TV does not list .avi files"]),
        ("direct", local("/m/a.avi"), summary(audio="dts"), SINK_MP4_ONLY, False, "file", []),
        ("convert", local("/m/a.mkv"), summary(), SINK_MKV, False, "stream", ["--mode convert"]),
        ("convert", local("/m/a.mkv"), summary(), SINK_HLS, True, "hls", ["--mode convert"]),
        ("auto", remote("http://h/a.mp4"), summary(), SINK_MKV, False, "url", []),
        ("auto", remote("https://h/a.mp4"), summary(), SINK_MKV, False, "stream", ["the TV cannot fetch https:// addresses itself"]),
        ("auto", remote("http://h/a.mp4"), summary(audio="dts"), SINK_MKV, False, "stream", ["audio is dts"]),
        ("direct", remote("https://h/a.mp4"), summary(), SINK_MKV, False, "url", []),
        ("convert", remote("http://h/a.mp4"), summary(), SINK_MKV, False, "stream", ["--mode convert"]),
        ("auto", remote("https://h/live.m3u8"), summary(), SINK_HLS, True, "url", []),
        ("auto", remote("https://h/live.m3u8"), summary(), SINK_MKV, False, "stream", ["the TV does not play HLS playlists"]),
        ("auto", local("/m/index.m3u8"), summary(), SINK_HLS, True, "static", []),
        ("auto", remote("https://youtube/watch", via_ytdlp=True), summary(), SINK_MKV, False, "stream", []),
        ("auto", MediaSource("a", [Input("v"), Input("a")]), summary(), SINK_MKV, False, "stream", []),
    ],
)  # fmt: skip
def test_choose_delivery(mode, source, summ, sink, hls, kind, reasons):
    delivery = cli.choose_delivery(mode, source, summ, sink, hls)
    assert (delivery.kind, delivery.reasons) == (kind, reasons)


def test_https_remote_file_explains_why_it_is_proxied():
    delivery = cli.choose_delivery("auto", remote("https://h/a.mp4"), summary(), SINK_MKV, False)
    assert delivery.kind == "stream" and "https://" in delivery.reasons[0]
    delivery = cli.choose_delivery("auto", remote("https://h/a"), summary(), SINK_MKV, False)
    assert "does not list" in delivery.reasons[0]  # unknown container: says so


@pytest.mark.parametrize(
    "code,words",
    [(705, "busy"), (716, "firewall"), (None, "allow"), (999, "allow")],
)
def test_dlna_errors_become_advice(code, words):
    err = cli.explain_dlna_error(dlna.DlnaError("Play failed", code), "Living Room")
    assert "Living Room refused the request: Play failed" in err.message and words in err.hint


def test_lost_contact_message():
    err = cli._lost_contact("TV", asyncio.TimeoutError())
    assert "lost contact with TV (TimeoutError)" in err.message and "still on" in err.hint


def test_check_start_validates_the_start_time():
    cli._check_start(parse("a.mkv", "--start", "30"), 60.0)
    cli._check_start(parse("a.mkv", "--start", "30"), None)
    with pytest.raises(UserError, match="cannot be negative"):
        cli._check_start(parse("a.mkv", "--start", "-1"), 60.0)
    with pytest.raises(UserError, match="past the end of the video \\(60s\\)") as info:
        cli._check_start(parse("a.mkv", "--start", "90"), 60.0)
    assert "under 60" in info.value.hint


# -- subtitles ---------------------------------------------------------------------------


async def test_find_subtitle_rules(tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"x")
    (tmp_path / "Movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n")
    src = MediaSource("Movie", [Input(str(video))], video)
    assert (await cli._find_subtitle(parse("x"), src)).sidecar.name == "Movie.srt"
    assert await cli._find_subtitle(parse("x", "--no-subtitles"), src) is None
    assert await cli._find_subtitle(parse("x"), remote()) is None  # nothing to look next to
    named = await cli._find_subtitle(parse("x", "--subtitles", str(tmp_path / "Movie.srt")), remote())
    assert named.sidecar.name == "Movie.srt"
    with pytest.raises(UserError, match="subtitle file not found") as info:
        await cli._find_subtitle(parse("x", "--subtitles", str(tmp_path / "no.srt")), src)
    assert "--subtitles" in info.value.hint


async def test_unconvertible_subtitle_is_a_warning(capsys):
    class Broken:
        sidecar = Path("bad.srt")

        async def srt(self, offset=0.0):
            raise RuntimeError("ffmpeg failed: invalid data")

        async def webvtt(self):
            raise RuntimeError("ffmpeg failed: invalid data")

    assert await cli._check_subtitle(Broken(), "srt") is None
    assert await cli._check_subtitle(Broken(), "webvtt") is None
    assert "subtitles skipped" in capsys.readouterr().out
    assert await cli._check_subtitle(None, "srt") is None


# -- start watchdog and waiting ------------------------------------------------------------


class FakeDlnaPlayer:
    """Stands in for DlnaPlayer: records calls, answers from class-level settings."""

    sink = SINK_MKV
    states = ["PLAYING"]
    connect_error = None
    play_error = None
    last = None

    def __init__(self, renderer):
        self.renderer = renderer
        self.local_ip = "127.0.0.1"
        self.mime = self.flags = None
        self.played = None
        self.calls = []
        self.ended = asyncio.Event()
        FakeDlnaPlayer.last = self

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def sink_types(self):
        return set(self.sink)

    async def play(self, url, title="", start=0.0, subtitle_url=None):
        if self.play_error:
            raise self.play_error
        self.played = {"url": url, "title": title, "start": start, "subtitle_url": subtitle_url}
        self.ended.set()

    async def state(self):
        state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        if isinstance(state, Exception):
            raise state
        return state

    async def wait_until_ended(self):
        await self.ended.wait()

    async def stop(self):
        self.calls.append("stop")

    async def close(self):
        self.calls.append("close")


@pytest.fixture
def fake_dlna(monkeypatch):
    monkeypatch.setattr(FakeDlnaPlayer, "sink", SINK_MKV)
    monkeypatch.setattr(FakeDlnaPlayer, "states", ["PLAYING"])
    monkeypatch.setattr(FakeDlnaPlayer, "connect_error", None)
    monkeypatch.setattr(FakeDlnaPlayer, "play_error", None)
    monkeypatch.setattr(cli.dlna, "DlnaPlayer", FakeDlnaPlayer)
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 0.2)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    return FakeDlnaPlayer


async def test_watchdog_is_quiet_once_playing(fake_dlna, capsys):
    fake_dlna.states = [dlna.DlnaError("busy"), aiohttp.ClientError(), "TRANSITIONING", "PLAYING"]
    await cli._watch_start(fake_dlna(None), SimpleNamespace(requests=3), "TV")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "hits,words",
    [(0, "never asked this computer"), (4, "has not started playing"), (None, "has not started playing")],
)
async def test_watchdog_explains_a_tv_that_never_starts(fake_dlna, capsys, hits, words):
    fake_dlna.states = ["TRANSITIONING"]
    await cli._watch_start(fake_dlna(None), SimpleNamespace(requests=hits), "Living Room")
    out = capsys.readouterr().out
    assert words in out and "hint:" in out
    assert ("Firewall" in out) is (hits == 0)


async def test_wait_returns_after_the_duration_even_without_an_end_event(fake_dlna, capsys):
    player = fake_dlna(None)
    await cli._wait(player, None, "TV", MediaSource("a", [Input("x")]), summary(duration=-29.9), parse("a"))
    assert "/ -30s" in capsys.readouterr().out  # the progress line ran while waiting


# -- playing over DLNA ---------------------------------------------------------------------


def tv_target(protocol="dlna"):
    air = AirPlayInfo("AA:BB", "10.0.0.5", 7000, object())
    return Target(TV("Living Room TV", "10.0.0.5", dlna.Renderer("http://10.0.0.5/d", "Living Room TV", "u", "http://10.0.0.5/c"), air), protocol)


async def _local_source(tmp_path, name="Movie.mkv", vcodec="libx264", acodec="aac"):
    video = tmp_path / name
    _make_video(video, vcodec, acodec)
    src = MediaSource(video.stem, [Input(str(video))], video)
    info = await stream.probe_input(src.inputs[0])
    return src, stream.summarize([info])


@needs_ffmpeg
async def test_dlna_sends_a_playable_file_untouched(fake_dlna, tmp_path, capsys):
    src, summ = await _local_source(tmp_path)
    await cli._play_dlna(parse(str(src.path), "--start", "5"), src, summ, tv_target(), None)
    player = fake_dlna.last
    assert player.played["url"].endswith("/media.mkv") and player.played["start"] == 5.0
    assert player.mime == "video/x-mkv" and player.calls == ["stop", "close"]
    assert "Sending the video as is" in capsys.readouterr().out


@needs_ffmpeg
async def test_dlna_converts_just_in_time_when_the_container_is_unsupported(fake_dlna, tmp_path, capsys):
    fake_dlna.sink = SINK_MP4_ONLY | {"video/vnd.dlna.mpeg-tts"}
    src, summ = await _local_source(tmp_path, "Movie.mkv")
    await cli._play_dlna(parse(str(src.path), "--no-subtitles", "--start", "3"), src, summ, tv_target(), None)
    player = fake_dlna.last
    assert player.played["url"].endswith("/stream.ts") and player.played["start"] == 0.0
    assert player.mime == "video/vnd.dlna.mpeg-tts" and player.flags == stream.STREAM_FLAGS
    out = capsys.readouterr().out
    assert "Converting just in time (the TV does not list .mkv files): copying video, copying audio." in out


@needs_ffmpeg
@pytest.mark.parametrize("no_jit", [False, True])
async def test_dlna_uses_hls_when_the_tv_lists_it(fake_dlna, tmp_path, capsys, no_jit):
    fake_dlna.sink = SINK_HLS
    src, summ = await _local_source(tmp_path, "Movie.mkv")
    argv = [str(src.path), "--no-subtitles", "--mode", "convert"] + (["--no-jit"] if no_jit else [])
    await cli._play_dlna(parse(*argv), src, summ, tv_target(), tmp_path / "work")
    player = fake_dlna.last
    assert player.mime == cli.HLS_MIME and player.played["url"].endswith("/index.m3u8")
    out = capsys.readouterr().out
    assert "Converting to HLS as the TV asks" in out and ("segments" in out) is no_jit


async def test_dlna_hands_over_a_plain_http_url(fake_dlna, capsys):
    src, summ = remote("http://media.example/a.mp4"), summary()
    await cli._play_dlna(parse("x"), src, summ, tv_target(), None)
    assert fake_dlna.last.played["url"] == "http://media.example/a.mp4"
    assert fake_dlna.last.mime == "video/mp4"


async def test_dlna_serves_a_local_hls_playlist(fake_dlna, tmp_path):
    fake_dlna.sink = SINK_HLS
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    src = MediaSource("index", [Input(str(playlist))], playlist)
    await cli._play_dlna(parse("x"), src, summary(), tv_target(), None)
    assert fake_dlna.last.played["url"].endswith("/index.m3u8") and fake_dlna.last.mime == cli.HLS_MIME


@needs_ffmpeg
async def test_dlna_streams_a_web_video_by_ffmpeg(fake_dlna, tmp_path, capsys):
    src, summ = await _local_source(tmp_path)
    web = MediaSource("Clip", src.inputs, None, via_ytdlp=True)  # as yt-dlp would return it
    await cli._play_dlna(parse("https://x/y"), web, summ, tv_target(), None)
    assert fake_dlna.last.played["url"].endswith("/stream.ts")
    assert "Converting just in time: copying video, copying audio." in capsys.readouterr().out


@needs_ffmpeg
async def test_dlna_subtitle_url_reaches_the_tv(fake_dlna, tmp_path):
    src, summ = await _local_source(tmp_path)
    (tmp_path / "Movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n")
    await cli._play_dlna(parse(str(src.path)), src, summ, tv_target(), None)
    assert fake_dlna.last.played["subtitle_url"].endswith("/subs.srt")


async def test_dlna_unreachable_tv(fake_dlna):
    fake_dlna.connect_error = OSError(65, "No route to host")
    with pytest.raises(UserError, match="cannot reach Living Room TV at 10.0.0.5: No route to host") as info:
        await cli._play_dlna(parse("x"), remote(), summary(), tv_target(), None)
    assert "same network" in info.value.hint


async def test_dlna_refusal_is_explained_and_cleaned_up(fake_dlna):
    fake_dlna.play_error = dlna.DlnaError("SetAVTransportURI failed: HTTP 500", 716)
    with pytest.raises(UserError, match="refused the request") as info:
        await cli._play_dlna(parse("x"), remote(), summary(), tv_target(), None)
    assert "firewall" in info.value.hint and fake_dlna.last.calls == ["stop", "close"]


async def test_dlna_lost_connection(fake_dlna):
    fake_dlna.play_error = aiohttp.ClientConnectionError("gone")
    with pytest.raises(UserError, match="lost contact"):
        await cli._play_dlna(parse("x"), remote(), summary(), tv_target(), None)


@needs_ffmpeg
async def test_dlna_reports_what_ffmpeg_complained_about(fake_dlna, tmp_path, capsys, monkeypatch):
    src, summ = await _local_source(tmp_path)
    web = MediaSource("Clip", [Input(str(tmp_path / "gone.mkv"))], None, via_ytdlp=True)

    class Player(FakeDlnaPlayer):
        async def play(self, url, title="", start=0.0, subtitle_url=None):
            async with aiohttp.ClientSession() as http:
                async with http.get(url) as resp:
                    await resp.read()
            await super().play(url, title, start, subtitle_url)

    monkeypatch.setattr(cli.dlna, "DlnaPlayer", Player)
    await cli._play_dlna(parse("x"), web, summ, tv_target(), None)
    assert "ffmpeg reported:" in capsys.readouterr().out


# -- playing over AirPlay ------------------------------------------------------------------


class FakeAirPlayPlayer:
    connect_error = None
    play_error = None
    last = None

    def __init__(self, address, port, credentials):
        self.args = (address, port, credentials)
        self.local_ip = "127.0.0.1"
        self.played = None
        self.calls = []
        self.ended = asyncio.Event()
        FakeAirPlayPlayer.last = self

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def play(self, url, title="", start=0.0):
        if self.play_error:
            raise self.play_error
        self.played = (url, title, start)
        self.ended.set()

    async def wait_until_ended(self):
        await self.ended.wait()

    async def stop(self):
        self.calls.append("stop")

    async def close(self):
        self.calls.append("close")


@pytest.fixture
def fake_airplay(monkeypatch):
    monkeypatch.setattr(FakeAirPlayPlayer, "connect_error", None)
    monkeypatch.setattr(FakeAirPlayPlayer, "play_error", None)
    monkeypatch.setattr(cli, "AirPlayPlayer", FakeAirPlayPlayer)
    return FakeAirPlayPlayer


@needs_ffmpeg
async def test_airplay_plays_a_local_file_as_hls(fake_airplay, tmp_path, capsys):
    src, summ = await _local_source(tmp_path)
    (tmp_path / "Movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n")
    await cli._play_airplay(parse(str(src.path)), src, summ, tv_target("airplay"), None)
    url, title, start = fake_airplay.last.played
    assert url.endswith("/master.m3u8") and title == "Movie" and fake_airplay.last.calls == ["stop", "close"]
    assert fake_airplay.last.args[:2] == ("10.0.0.5", 7000)
    assert "choose them in the TV's own subtitle menu" in capsys.readouterr().out


@needs_ffmpeg
async def test_airplay_no_jit_converts_first(fake_airplay, tmp_path, capsys):
    src, summ = await _local_source(tmp_path)
    await cli._play_airplay(parse(str(src.path), "--no-jit", "--no-subtitles"), src, summ, tv_target("airplay"), tmp_path / "w")
    assert fake_airplay.last.played[0].endswith("/index.m3u8") and "segments" in capsys.readouterr().out


async def test_airplay_passes_hls_addresses_and_serves_local_playlists(fake_airplay, tmp_path):
    await cli._play_airplay(parse("x"), remote("https://h/live.m3u8"), summary(), tv_target("airplay"), None)
    assert fake_airplay.last.played[0] == "https://h/live.m3u8"
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n")
    src = MediaSource("index", [Input(str(playlist))], playlist)
    await cli._play_airplay(parse("x"), src, summary(), tv_target("airplay"), None)
    assert fake_airplay.last.played[0].endswith("/index.m3u8")


@pytest.mark.parametrize("error", [pyatv_exceptions.ConnectionFailedError("refused"), OSError("down"), asyncio.TimeoutError()])
async def test_airplay_connection_failures_suggest_repairing(fake_airplay, error):
    fake_airplay.connect_error = error
    with pytest.raises(UserError, match="could not connect to Living Room TV over AirPlay") as info:
        await cli._play_airplay(parse("x"), remote("https://h/a.m3u8"), summary(), tv_target("airplay"), None)
    assert "atvremote --id AA:BB --protocol airplay pair" in info.value.hint


async def test_airplay_refusal_is_explained(fake_airplay):
    fake_airplay.play_error = AirPlayError("POST /command refused: 400")
    with pytest.raises(UserError, match="refused the AirPlay request: POST /command refused: 400"):
        await cli._play_airplay(parse("x"), remote("https://h/a.m3u8"), summary(), tv_target("airplay"), None)
    assert fake_airplay.last.calls == ["stop", "close"]
    fake_airplay.connect_error = AirPlayError("SETUP refused")
    with pytest.raises(UserError, match="SETUP refused"):
        await cli._play_airplay(parse("x"), remote("https://h/a.m3u8"), summary(), tv_target("airplay"), None)


async def test_airplay_lost_connection_is_explained(fake_airplay):
    fake_airplay.play_error = ConnectionResetError("reset")
    with pytest.raises(UserError, match="Living Room TV"):
        await cli._play_airplay(parse("x"), remote("https://h/a.m3u8"), summary(), tv_target("airplay"), None)


async def test_airplay_wait_gives_up_after_the_duration(fake_airplay):
    player = FakeAirPlayPlayer(None, None, None)
    await cli._wait_airplay(player, -29.9, parse("x"))  # limit is 0.1 s and nothing ends it


# -- run() and main() ----------------------------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


async def test_forget(isolated_config, capsys):
    await cli.run(parse("--forget"))
    assert "No TV was remembered" in capsys.readouterr().out
    config.save_tv({"name": "TV", "host": "h"})
    await cli.run(parse("--forget"))
    assert "Forgot your TV" in capsys.readouterr().out and config.saved_tv() is None


async def test_run_without_a_source_explains():
    with pytest.raises(UserError, match="nothing to play") as info:
        await cli.run(parse())
    assert "pyatv-cast movie.mkv" in info.value.hint


async def test_list_shows_tvs_and_marks_yours(isolated_config, monkeypatch, capsys):
    one = TV("Living Room TV", "10.0.0.5", renderer=None)
    two = TV("Bedroom", "10.0.0.6")
    one.airplay = AirPlayInfo("id", "10.0.0.5", 7000, None)

    async def found(credentials=None):
        return [one, two]

    monkeypatch.setattr(targets, "list_tvs", found)
    config.save_tv({"name": "Bedroom", "host": "10.0.0.6"})
    await cli.run(parse("--list"))
    out = capsys.readouterr().out
    assert "Found 2 TVs:" in out and "AirPlay (not paired)" in out
    assert [line for line in out.splitlines() if "Bedroom" in line][0].endswith("(your TV)")

    async def nothing(credentials=None):
        return []

    monkeypatch.setattr(targets, "list_tvs", nothing)
    with pytest.raises(UserError, match="no TV was found"):
        await cli.run(parse("--list"))
    monkeypatch.setattr(targets, "list_tvs", lambda credentials=None: found())
    await cli.run(parse("--forget", "--list"))  # forgets, then still lists
    assert config.saved_tv() is None


@pytest.fixture
def run_env(monkeypatch):
    """Everything outside cli.run() replaced, recording what it was asked to do."""
    env = SimpleNamespace(
        played=[], target=tv_target(), source=remote("http://h/a.mp4", duration=100.0),
        probe_error=None, downloads=[],
    )
    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", lambda: None)

    async def resolve(text, fmt, cookies, verbose):
        env.resolved = (text, fmt, cookies, verbose)
        return env.source

    async def probe(source):
        if env.probe_error:
            raise env.probe_error
        return {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}], "format": {"duration": "100"}}

    async def find_target(spec, protocol, choose, credentials):
        env.asked = (spec, protocol, choose, credentials)
        return env.target

    async def play_dlna(args, source, summ, target, workdir):
        env.played.append(("dlna", workdir))

    async def play_airplay(args, source, summ, target, workdir):
        env.played.append(("airplay", workdir))

    async def download_first(args, source, directory):
        env.downloads.append((source, directory))
        local = MediaSource(source.title, [Input("/tmp/x.mkv")], Path("/tmp/x.mkv"))
        return local, summary(duration=100.0)

    monkeypatch.setattr(cli, "_download_first", download_first)
    monkeypatch.setattr(cli.sources, "resolve", resolve)
    monkeypatch.setattr(cli.stream, "probe_input", probe)
    monkeypatch.setattr(cli.targets, "resolve_target", find_target)
    monkeypatch.setattr(cli, "_play_dlna", play_dlna)
    monkeypatch.setattr(cli, "_play_airplay", play_airplay)
    return env


async def test_run_plays_over_the_chosen_protocol(run_env, capsys):
    await cli.run(parse("a.mp4", "--tv", "Den", "--protocol", "dlna", "--choose", "--cookies-from-browser", "chrome", "-v"))
    assert run_env.resolved == ("a.mp4", sources.DEFAULT_YTDLP_FORMAT, "chrome", True)
    assert run_env.asked == ("Den", "dlna", True, None)
    assert run_env.played == [("dlna", None)]
    assert "a  ->  Living Room TV (DLNA)" in capsys.readouterr().out
    run_env.target = tv_target("airplay")
    await cli.run(parse("a.mp4"))
    assert run_env.played[-1][0] == "airplay"


async def test_run_announces_yt_dlp_and_checks_the_start_time(run_env, capsys):
    run_env.source = MediaSource("A Web Video", [Input("https://cdn/v")], via_ytdlp=True, duration=100.0)
    await cli.run(parse("https://youtu.be/x"))
    assert "yt-dlp found: A Web Video" in capsys.readouterr().out
    with pytest.raises(UserError, match="past the end"):
        await cli.run(parse("https://youtu.be/x", "--start", "500"))


async def test_run_no_jit_uses_and_removes_a_work_directory(run_env, tmp_path):
    await cli.run(parse("a.mp4", "--no-jit", "--workdir", str(tmp_path)))
    (_, workdir), = run_env.played
    assert workdir.parent == tmp_path and not workdir.exists()


def test_main_returns_zero_on_success(monkeypatch):
    async def fine(args):
        return None

    monkeypatch.setattr(cli, "run", fine)
    assert cli.main(["a.mp4"]) == 0
    assert cli.main(["play", "a.mp4"]) == 0  # the older `play` form still works


def test_main_reports_user_errors_without_a_traceback(monkeypatch, capsys):
    async def fails(args):
        raise UserError("the TV is on fire", "put it out")

    monkeypatch.setattr(cli, "run", fails)
    assert cli.main(["a.mp4"]) == 1
    err = capsys.readouterr().err
    assert err == "error: the TV is on fire\n  hint: put it out\n"


def test_main_handles_ctrl_c(monkeypatch, capsys):
    async def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupted)
    assert cli.main(["a.mp4"]) == 130
    assert "Stopped." in capsys.readouterr().out


@pytest.mark.parametrize("verbose", [False, True])
def test_main_wraps_surprises_with_a_next_step(monkeypatch, capsys, verbose):
    async def crash(args):
        raise ZeroDivisionError("oops")

    monkeypatch.setattr(cli, "run", crash)
    assert cli.main(["a.mp4"] + (["-v"] if verbose else [])) == 1
    captured = capsys.readouterr()
    assert "something unexpected went wrong: ZeroDivisionError: oops" in captured.err
    assert "run again with -v" in captured.err and "issues" in captured.err
    assert ("Traceback" in captured.err) is verbose


def test_main_defaults_to_sys_argv(monkeypatch):
    async def fine(args):
        assert args.source == "from_argv.mp4"

    monkeypatch.setattr(cli, "run", fine)
    monkeypatch.setattr(sys, "argv", ["pyatv-cast", "from_argv.mp4"])
    assert cli.main() == 0


def test_help_lists_examples(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "pyatv-cast movie.mkv" in out and "--cookies-from-browser" in out and "--choose" in out


# -- live streaming and downloading for AirPlay ----------------------------------------------


class FakeLive:
    """Stands in for LiveHls: no ffmpeg, a playlist that already exists."""

    instances = []
    fail_after = None

    def __init__(self, plan, directory, start=0.0):
        self.plan, self.directory, self.start_at = plan, directory, start
        self.failed = asyncio.Event()
        self.error = None
        self.stopped = False
        FakeLive.instances.append(self)

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "index.m3u8").write_text("#EXTM3U\n")
        if FakeLive.fail_after is not None:
            asyncio.get_running_loop().call_later(FakeLive.fail_after, self._fail)
        return self.directory / "index.m3u8"

    def _fail(self):
        self.error = "Connection reset by peer"
        self.failed.set()

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_live(monkeypatch):
    FakeLive.instances = []
    FakeLive.fail_after = None
    monkeypatch.setattr(cli, "LiveHls", FakeLive)
    return FakeLive


async def test_airplay_streams_a_remote_video_live(fake_airplay, fake_live, tmp_path, capsys):
    await cli._play_airplay(parse("x", "--start", "30"), remote("https://h/v.mp4"), summary(), tv_target("airplay"), tmp_path)
    live = fake_live.instances[0]
    url, title, start = fake_airplay.last.played
    assert url.endswith("/index.m3u8") and start == 0.0  # ffmpeg already skipped ahead
    assert live.start_at == 30.0 and live.directory == tmp_path / "live" and live.stopped
    out = capsys.readouterr().out
    assert "Preparing the video (copying video, copying audio)" in out and "note:" not in out


async def test_live_mode_says_when_explicit_subtitles_cannot_be_used(fake_airplay, fake_live, tmp_path, capsys):
    await cli._play_airplay(parse("x", "--subtitles", "a.srt"), remote("https://h/v.mp4"), summary(), tv_target("airplay"), tmp_path)
    assert "subtitles are not sent while streaming live; add --download-first" in capsys.readouterr().out


async def test_a_live_stream_that_breaks_part_way_is_reported(fake_airplay, fake_live, tmp_path, monkeypatch):
    fake_live.fail_after = 0.05

    async def never_ends(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_airplay, "wait_until_ended", never_ends)
    with pytest.raises(UserError, match="stopped downloading part way: Connection reset by peer") as info:
        await cli._play_airplay(parse("x"), remote("https://h/v.mp4"), summary(duration=60.0), tv_target("airplay"), tmp_path)
    assert "--download-first" in info.value.hint and fake_live.instances[0].stopped


async def test_wait_airplay_without_live_failure_just_waits_for_the_end(fake_airplay, fake_live):
    live = fake_live(None, None)
    player = fake_airplay(None, None, None)
    player.ended.set()
    await cli._wait_airplay(player, 60.0, parse("x"), live)


def test_progress_line_formats(capsys):
    cli._print_progress(5 * 1024**2, 10 * 1024**2, 1024**2)
    cli._print_progress(2048, None, None)
    out = capsys.readouterr().out
    assert " 50.0%  5.0 MB / 10.0 MB  1.0 MB/s" in out and "Downloading  2.0 KB" in out and "%" not in out.split("\r")[-1]


async def test_download_first_fetches_then_reads_the_local_file(monkeypatch, tmp_path, capsys):
    seen = {}

    async def download(source, directory, on_progress, want_subtitles, verbose, on_note):
        seen.update(directory=directory, subs=want_subtitles)
        on_progress(1, 2, None)
        on_note("subtitles unavailable")
        return MediaSource("T", [Input(str(tmp_path / "video.mkv"))], tmp_path / "video.mkv")

    async def probe(source):
        return {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}], "format": {"duration": "8"}}

    monkeypatch.setattr(cli.sources, "download", download)
    monkeypatch.setattr(cli.stream, "probe_input", probe)
    src = MediaSource("T", [Input("https://cdn/x")], via_ytdlp=True, size_estimate=5 * 1024**2)
    local, summ = await cli._download_first(parse("x", "--no-subtitles"), src, tmp_path)
    assert local.is_local and summ.duration == 8.0 and seen == {"directory": tmp_path, "subs": False}
    out = capsys.readouterr().out
    assert "Downloading 'T' (about 5.0 MB) before playing" in out and "note: subtitles unavailable" in out and "Done." in out


async def test_run_falls_back_to_a_yt_dlp_download_when_ffmpeg_is_refused(run_env, capsys):
    run_env.source = MediaSource("Web", [Input("https://cdn/v")], via_ytdlp=True, duration=100.0)
    run_env.probe_error = stream.ProbeError("ffprobe could not read x", "hint", "Server returned 403 Forbidden")
    await cli.run(parse("https://youtu.be/x"))
    assert "note: ffmpeg cannot read this video straight from the site (Server returned 403 Forbidden)." in capsys.readouterr().out
    (source, directory), = run_env.downloads
    assert source.via_ytdlp and directory is not None and run_env.played == [("dlna", directory)]


async def test_run_does_not_hide_other_probe_failures(run_env):
    run_env.probe_error = stream.ProbeError("ffprobe could not read a.mp4", "hint", "Invalid data")
    with pytest.raises(UserError, match="could not read a.mp4"):
        await cli.run(parse("a.mp4"))
    assert run_env.downloads == []


async def test_run_checks_the_start_time_against_the_download_too(run_env):
    run_env.source = MediaSource("Web", [Input("https://cdn/v")], via_ytdlp=True, duration=500.0)
    run_env.probe_error = stream.ProbeError("x", "h", "Server returned 403 Forbidden")
    with pytest.raises(UserError, match="past the end of the video \\(100s\\)"):
        await cli.run(parse("https://youtu.be/x", "--start", "200"))  # fine for yt-dlp's 500 s, not for the file


@pytest.mark.parametrize(
    "protocol,flag,expected",
    [("airplay", True, 1), ("airplay", False, 0), ("dlna", True, 0)],
)
async def test_download_first_only_applies_to_remote_videos_on_airplay(run_env, protocol, flag, expected):
    run_env.target = tv_target(protocol)
    await cli.run(parse("https://example.com/v.mp4", *(["--download-first"] if flag else [])))
    assert len(run_env.downloads) == expected
    workdir = run_env.played[0][1]
    assert (workdir is not None) is (protocol == "airplay")  # AirPlay needs scratch space for live segments


async def test_a_remote_hls_address_needs_no_scratch_space_on_airplay(run_env):
    run_env.target = tv_target("airplay")
    run_env.source = remote("https://h/live.m3u8")
    await cli.run(parse("https://h/live.m3u8", "--download-first"))
    assert run_env.downloads == [] and run_env.played == [("airplay", None)]


def test_download_first_flag_is_off_by_default():
    assert parse("x").download_first is False and parse("x", "--download-first").download_first is True
