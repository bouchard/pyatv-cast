"""Command line: ``pyatv-cast SOURCE`` plays a file or URL on your TV.

With no other options it finds the TV (asking once if there are several, then
remembering), sends the file untouched if the TV can play it, converts just in time if not,
and asks yt-dlp for the video when given a web page.
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

import aiohttp
from pyatv import exceptions as pyatv_exceptions

from pyatv_cast import config, dlna, hls, sources, stream, subtitles, targets
from pyatv_cast.errors import UPNP_HINTS, UserError
from pyatv_cast.airplay import AirPlayError, AirPlayPlayer
from pyatv_cast.live import LiveHls

HLS_MIME = "application/vnd.apple.mpegurl"
# pyatv has no common base class for its errors; these are the ones a connection can raise
PYATV_ERRORS = (
    pyatv_exceptions.ConnectionFailedError,
    pyatv_exceptions.ConnectionLostError,
    pyatv_exceptions.AuthenticationError,
    pyatv_exceptions.InvalidCredentialsError,
    pyatv_exceptions.OperationTimeoutError,
    pyatv_exceptions.ProtocolError,
    pyatv_exceptions.HttpError,
    pyatv_exceptions.InvalidResponseError,
    pyatv_exceptions.BackOffError,
    pyatv_exceptions.PlaybackError,
)
START_WATCH_SECONDS = 25.0
WATCH_INTERVAL = 2.0
EXAMPLES = """\
examples:
  pyatv-cast movie.mkv                     play a file on your TV
  pyatv-cast https://youtu.be/VIDEO        any page yt-dlp understands
  pyatv-cast movie.mkv --start 600         begin 10 minutes in
  pyatv-cast --list                        show the TVs on the network
  pyatv-cast movie.mkv --choose            pick a different TV
"""


@dataclass
class Delivery:
    """How the source will reach the TV."""

    kind: str  # "file", "url", "hls", "static", "stream"
    reasons: List[str]  # why conversion is needed, if it is


def choose_delivery(
    mode: str,
    source: sources.MediaSource,
    summary: stream.Summary,
    sink: set,
    supports_hls: bool,
) -> Delivery:
    """Pick the least invasive way to get ``source`` to a DLNA renderer."""
    reasons = stream.conversion_reasons(summary, source.location, sink)
    if source.via_ytdlp or len(source.inputs) > 1:
        # Resolved addresses are signed, need headers or come as separate video and audio
        return Delivery("stream", [])
    if source.is_hls_playlist:
        if supports_hls:
            return Delivery("static" if source.is_local else "url", [])
        return Delivery("stream", ["the TV does not play HLS playlists"])
    if mode == "convert":
        return Delivery("hls" if source.is_local and supports_hls else "stream", ["--mode convert"])
    if source.is_local:
        if mode == "direct" or not reasons:
            return Delivery("file", [])
        return Delivery("hls" if supports_hls else "stream", reasons)
    secure = source.location.startswith("https://")
    if mode == "direct" or (not reasons and not secure):
        return Delivery("url", [])
    return Delivery("stream", reasons or ["the TV cannot fetch https:// addresses itself"])


def explain_dlna_error(ex: dlna.DlnaError, tv_name: str) -> UserError:
    """Turn a renderer error into advice."""
    hint = UPNP_HINTS.get(ex.code or 0)
    if hint is None:
        hint = (
            f"Is {tv_name} awake? Some TVs show an 'allow' prompt the first time a new "
            "device sends to them: accept it with the remote. Run with -v for details."
        )
    return UserError(f"{tv_name} refused the request: {ex}", hint)


def _lost_contact(tv_name: str, ex: BaseException) -> UserError:
    return UserError(
        f"lost contact with {tv_name} ({type(ex).__name__}).",
        "Check that the TV is still on and on the same network, then run again.",
    )


async def _check_subtitle(sub: Optional[Any], fmt: str, offset: float = 0.0) -> Optional[Any]:
    """Convert the subtitle now, so a bad file is a warning and not a failed playback."""
    if sub is None:
        return None
    try:
        await (sub.webvtt() if fmt == "webvtt" else sub.srt(offset))
    except RuntimeError as ex:
        where = sub.sidecar or "the embedded track"
        print(f"warning: subtitles skipped, could not convert {where}: {ex}")
        return None
    return sub


async def _find_subtitle(args: argparse.Namespace, source: sources.MediaSource) -> Optional[Any]:
    if args.no_subtitles or source.is_hls_playlist:
        return None
    explicit = Path(args.subtitles).expanduser() if args.subtitles else None
    if explicit is None and not source.is_local:
        return None
    try:
        return await subtitles.find(source.path or Path("."), explicit)
    except FileNotFoundError as ex:
        raise UserError(str(ex), "Check the path given to --subtitles.") from ex


async def _ticker(started: float, duration: Optional[float]) -> None:
    while True:
        elapsed = time.monotonic() - started
        total = f" / {duration:.0f}s" if duration else ""
        print(f"\r  {elapsed:7.0f}s{total}", end="", flush=True)
        await asyncio.sleep(1)


async def _watch_start(player: dlna.DlnaPlayer, server: Any, tv_name: str) -> None:
    """If the TV has not started playing after a while, say why that usually is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_WATCH_SECONDS
    while loop.time() < deadline:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            if await player.state() in ("PLAYING", "PAUSED_PLAYBACK"):
                return
        except (dlna.DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
            continue
    hits = getattr(server, "requests", None)
    print()
    if hits is None:
        print(f"warning: {tv_name} has not started playing yet.\n  hint: if the TV shows an 'allow' prompt, accept it with the remote.")
    elif hits == 0:
        print(
            f"warning: {tv_name} accepted the command but never asked this computer for the "
            "video.\n  hint: a firewall on this Mac is the usual cause (System Settings > "
            "Network > Firewall: allow incoming connections for Python), or the TV is "
            "showing an 'allow' prompt for you to accept."
        )
    else:
        print(
            f"warning: {tv_name} fetched the video but has not started playing it.\n"
            "  hint: it may not support this format. Try `--mode convert`. If the TV shows an "
            "'allow' prompt, accept it with the remote."
        )


def _check_start(args: argparse.Namespace, duration: Optional[float]) -> None:
    if args.start < 0:
        raise UserError("--start cannot be negative.")
    if duration and args.start >= duration:
        raise UserError(
            f"--start {args.start:.0f}s is past the end of the video ({duration:.0f}s).",
            f"Choose a start under {duration:.0f} seconds.",
        )


async def _play_dlna(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
) -> None:
    tv = target.tv
    player = dlna.DlnaPlayer(tv.renderer)
    try:
        await player.connect()
    except OSError as ex:
        raise UserError(
            f"cannot reach {tv.name} at {tv.host}: {ex.strerror or ex}.",
            "Is the TV on, and on the same network as this computer?",
        ) from ex
    server: Any = None
    try:
        sink = await player.sink_types()
        supports_hls = any("mpegurl" in m for m in sink)
        delivery = choose_delivery(args.mode, source, summary, sink, supports_hls)
        sub = await _find_subtitle(args, source)
        url, server, mime, start = await _prepare_dlna(
            args, source, summary, delivery, sink, sub, player, workdir
        )
        player.mime = mime
        _announce(delivery, server)
        await player.play(
            url, title=source.title, start=start,
            subtitle_url=getattr(server, "subtitle_url", None),
        )  # fmt: skip
        print(f"Playing on {tv.name}. Press Ctrl-C to stop.")
        await _wait(player, server, tv.name, source, summary, args)
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        raise _lost_contact(tv.name, ex) from ex
    except dlna.DlnaError as ex:
        raise explain_dlna_error(ex, tv.name) from ex
    finally:
        await player.stop()
        if server:
            await server.stop()
        await player.close()
        if server is not None and getattr(server, "last_error", None):
            print(f"\nffmpeg reported: {server.last_error}")


async def _prepare_dlna(args, source, summary, delivery, sink, sub, player, workdir):
    """Start whatever serves the media, returning (url, server, mime, start for the TV)."""
    ip = player.local_ip
    kind = delivery.kind
    if kind == "url":
        return source.location, None, stream.matching_mime(source.location, sink), args.start
    if kind == "file":
        sub = await _check_subtitle(sub, "srt")
        server = dlna.DirectFileServer(source.path, ip, subtitle=sub)
        mime = stream.matching_mime(source.location, sink)
        return await server.start(), server, mime, args.start
    if kind == "static":
        server = hls.StaticHlsServer(source.path, ip)
        return await server.start(), server, HLS_MIME, args.start
    if kind == "hls":
        sub = await _check_subtitle(sub, "webvtt")
        media = await hls.plan(source.path, sub)
        url, server = await _hls_server(args, media, ip, workdir)
        return url, server, HLS_MIME, args.start
    plan = stream.make_plan(source.inputs, summary, force_convert=args.mode == "convert")
    sub = await _check_subtitle(sub, "srt", args.start)
    server = stream.StreamServer(plan, ip, start=args.start, subtitle=sub)
    server.description = plan.description  # shown by _announce
    server.mime = next((m for m in stream.TS_MIMES if m in sink), stream.TS_MIMES[0])
    player.flags = stream.STREAM_FLAGS
    return await server.start(), server, server.mime, 0.0  # ffmpeg already skipped ahead


async def _hls_server(args, media, ip, workdir):
    if args.no_jit:
        print(f"Converting {media.source.name} ({len(media.segments)} segments) ...")
        server = hls.StaticHlsServer(await hls.convert_to_directory(media, workdir), ip)
    else:
        server = hls.JitHlsServer(media, ip)
    return await server.start(), server


def _announce(delivery: Delivery, server: Any) -> None:
    why = "; ".join(delivery.reasons)
    if delivery.kind in ("file", "url"):
        print("Sending the video as is: no conversion needed.")
    elif delivery.kind == "static":
        print("Serving the HLS playlist as is.")
    elif delivery.kind == "hls":
        print(f"Converting to HLS as the TV asks ({why}).")
    else:
        what = getattr(server, "description", "")
        suffix = f" ({why})" if why else ""
        print(f"Converting just in time{suffix}: {what}.")


async def _wait(player, server, tv_name, source, summary, args) -> None:
    duration = summary.duration or source.duration
    ticker = asyncio.ensure_future(_ticker(time.monotonic(), duration))
    watcher = asyncio.ensure_future(_watch_start(player, server, tv_name))
    try:
        limit = (duration - args.start + 30) if duration else None
        await asyncio.wait_for(player.wait_until_ended(), limit)
    except asyncio.TimeoutError:
        pass
    finally:
        ticker.cancel()
        watcher.cancel()
        print()


async def _play_airplay(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
) -> None:
    tv = target.tv
    info = tv.airplay
    player = AirPlayPlayer(info.address, info.port, info.credentials)
    server: Any = None
    live: Optional[LiveHls] = None
    try:
        try:
            await player.connect()
        except AirPlayError:
            raise
        except (*PYATV_ERRORS, OSError, asyncio.TimeoutError) as ex:
            raise UserError(
                f"could not connect to {tv.name} over AirPlay: {ex}",
                "The TV's AirPlay port changes between sessions, so try again. If it keeps "
                "failing, the pairing may have been removed: pair again with "
                f"`atvremote --id {info.identifier} --protocol airplay pair`.",
            ) from ex
        start = args.start
        if source.is_hls_playlist and not source.is_local and not source.via_ytdlp:
            url = source.location
        elif source.is_hls_playlist and source.is_local:
            server = hls.StaticHlsServer(source.path, player.local_ip)
            url = await server.start()
        elif not source.is_local:
            # Remote video: ffmpeg writes HLS segments as it downloads, and playback starts
            # as soon as the first ones exist. (--download-first fetches everything first.)
            plan = stream.make_plan(source.inputs, summary, force_convert=args.mode == "convert")
            print(f"Preparing the video ({plan.description}); playback starts in a moment ...")
            live = LiveHls(plan, workdir / "live", start=args.start)
            server = hls.StaticHlsServer(await live.start(), player.local_ip)
            url = await server.start()
            start = 0.0  # ffmpeg already skipped ahead
            if args.subtitles:
                print("note: subtitles are not sent while streaming live; add --download-first.")
        else:
            sub = await _check_subtitle(await _find_subtitle(args, source), "webvtt")
            media = await hls.plan(source.path, sub)
            url, server = await _hls_server(args, media, player.local_ip, workdir)
            if sub:
                print("Subtitles are offered as a track: choose them in the TV's own subtitle menu.")
        await player.play(url, title=source.title, start=start)
        print(f"Playing on {tv.name} over AirPlay. Press Ctrl-C to stop.")
        await _wait_airplay(player, summary.duration or source.duration, args, live)
    except AirPlayError as ex:
        raise UserError(
            f"{tv.name} refused the AirPlay request: {ex}",
            "Stop whatever the TV is playing and try again. Run with -v for details.",
        ) from ex
    except (*PYATV_ERRORS, OSError, asyncio.TimeoutError) as ex:
        raise _lost_contact(tv.name, ex) from ex
    finally:
        await player.stop()
        if live:
            await live.stop()
        if server:
            await server.stop()
        await player.close()


async def _wait_airplay(
    player: AirPlayPlayer, duration: Optional[float], args, live: Optional[LiveHls] = None
) -> None:
    ticker = asyncio.ensure_future(_ticker(time.monotonic(), duration))
    waiting = [asyncio.ensure_future(player.wait_until_ended())]
    if live:
        waiting.append(asyncio.ensure_future(live.failed.wait()))
    try:
        limit = (duration - args.start + 30) if duration else None
        await asyncio.wait(waiting, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiting:
            task.cancel()
        ticker.cancel()
        print()
    if live and live.failed.is_set():
        raise UserError(
            f"the video stopped downloading part way: {live.error}",
            "The connection may have dropped, or the link expired. Try again, or use "
            "`--download-first` to fetch it completely before playing.",
        )


def _print_progress(done: int, total: Optional[int], speed: Optional[float]) -> None:
    size = sources.format_size
    percent = f"{100 * done / total:5.1f}%  " if total else ""
    of = f" / {size(total)}" if total else ""
    rate = f"  {size(speed)}/s" if speed else ""
    print(f"\r  Downloading  {percent}{size(done)}{of}{rate}   ", end="", flush=True)


async def _download_first(
    args: argparse.Namespace, source: sources.MediaSource, directory: Path
) -> tuple:
    """Fetch a remote video completely, then return it as a local source and its summary."""
    about = f" (about {sources.format_size(source.size_estimate)})" if source.size_estimate else ""
    print(f"Downloading {source.title!r}{about} before playing ...")
    local = await sources.download(
        source, directory, _print_progress, want_subtitles=not args.no_subtitles,
        verbose=args.verbose, on_note=lambda text: print(f"\nnote: {text}"),
    )  # fmt: skip
    print("\n  Done.")
    info = await stream.probe_input(local.inputs[0])
    return local, stream.summarize([info])


async def _list(args: argparse.Namespace) -> None:
    print("Looking for TVs ...")
    tvs = await targets.list_tvs(args.credentials)
    saved = config.saved_tv()
    if not tvs:
        raise targets.no_tv_error([], None)
    print(f"Found {len(tvs)} TV{'s' if len(tvs) != 1 else ''}:")
    for tv in tvs:
        mark = "  (your TV)" if saved and targets.matches(tv, saved["host"]) else ""
        print(f"  {tv.name:<28} {tv.host:<15} {', '.join(tv.protocols)}{mark}")


async def run(args: argparse.Namespace) -> None:
    """Do what the command line asked."""
    if args.forget:
        print("Forgot your TV." if config.forget_tv() else "No TV was remembered.")
        if not args.source and not args.list:
            return
    if args.list:
        await _list(args)
        return
    if not args.source:
        raise UserError("nothing to play.", "Give a video file or URL: `pyatv-cast movie.mkv`.")
    stream.ensure_ffmpeg()

    source = await sources.resolve(
        args.source, args.ytdlp_format, args.cookies_from_browser, args.verbose
    )
    if source.via_ytdlp:
        print(f"yt-dlp found: {source.title}")
    summary: Optional[stream.Summary] = None
    must_download = False
    try:
        summary = stream.summarize([await stream.probe_input(i) for i in source.inputs])
    except stream.ProbeError as ex:
        if not source.via_ytdlp:
            raise
        # Sites often refuse ffmpeg's direct requests but let yt-dlp's downloader through
        print(f"note: ffmpeg cannot read this video straight from the site ({ex.reason}).")
        must_download = True
    _check_start(args, (summary.duration if summary else None) or source.duration)

    target = await targets.resolve_target(args.tv, args.protocol, args.choose, args.credentials)
    print(f"{source.title}  ->  {target.tv.name} ({target.protocol.upper()})")

    # A temporary folder is needed to convert to disk (--no-jit), and for AirPlay to be
    # given a remote video, either as growing HLS segments or as a full download
    remote_for_airplay = (
        target.protocol == "airplay" and not source.is_local
        and not (source.is_hls_playlist and not source.via_ytdlp)
    )
    download = must_download or (remote_for_airplay and args.download_first)
    workdir = None
    if args.workdir and not Path(args.workdir).is_dir():
        raise UserError(f"--workdir {args.workdir} is not a folder.", "Create it, or leave --workdir out.")
    if args.no_jit or remote_for_airplay or download:
        workdir = tempfile.TemporaryDirectory(prefix="pyatv-cast-", dir=args.workdir)
    try:
        path = Path(workdir.name) if workdir else None
        if download:
            source, summary = await _download_first(args, source, path)
            _check_start(args, summary.duration or source.duration)
        if target.protocol == "dlna":
            await _play_dlna(args, source, summary, target, path)
        else:
            await _play_airplay(args, source, summary, target, path)
    finally:
        if workdir:
            try:
                workdir.cleanup()
            except OSError:  # an abandoned download thread may still be writing into it
                pass


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="pyatv-cast",
        description="Play a video file or URL on your TV (DLNA or AirPlay).",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", help="a video file, a media URL, or a web page URL")
    tv = parser.add_argument_group("choosing the TV")
    tv.add_argument(
        "--tv", "--host", "--id", "--location", dest="tv", metavar="TV",
        help="the TV's name (or part of it), IP address or AirPlay id. By default the TV is "
        "found automatically, asked about once if there are several, and remembered",
    )  # fmt: skip
    tv.add_argument(
        "--choose", action="store_true",
        help="ask which TV to use, even if one is remembered",
    )  # fmt: skip
    tv.add_argument("--forget", action="store_true", help="forget the remembered TV")
    tv.add_argument(
        "--list", action="store_true", help="list the TVs found on the network and exit"
    )  # fmt: skip
    tv.add_argument(
        "--protocol", choices=["auto", "dlna", "airplay"], default="auto",
        help="auto (default) uses DLNA when the TV has it, else AirPlay",
    )  # fmt: skip
    play = parser.add_argument_group("playback")
    play.add_argument("--start", type=float, default=0.0, metavar="SECONDS", help="start position")
    play.add_argument(
        "--mode", choices=["auto", "direct", "convert"], default="auto",
        help="auto (default) sends the file untouched when the TV can play it and converts "
        "just in time otherwise; direct never converts; convert always converts",
    )  # fmt: skip
    play.add_argument(
        "--subtitles", metavar="FILE",
        help="subtitle file (.srt/.vtt); by default one next to the video or inside it is used",
    )  # fmt: skip
    play.add_argument("--no-subtitles", action="store_true", help="send no subtitles")
    play.add_argument(
        "--download-first", action="store_true",
        help="AirPlay with a web or remote video: download all of it, with progress, before "
        "playing (gives a seek bar and subtitles), instead of streaming it live",
    )  # fmt: skip
    web = parser.add_argument_group("web pages (yt-dlp)")
    web.add_argument(
        "--ytdlp-format", default=sources.DEFAULT_YTDLP_FORMAT, metavar="FORMAT",
        help="yt-dlp format selector (default: up to 1080p, H.264 + AAC)",
    )  # fmt: skip
    web.add_argument(
        "--cookies-from-browser", metavar="BROWSER",
        help="use this browser's login cookies, e.g. chrome or firefox",
    )  # fmt: skip
    adv = parser.add_argument_group("advanced")
    adv.add_argument(
        "--no-jit", action="store_true", help="HLS only: convert everything to disk before playing"
    )  # fmt: skip
    adv.add_argument("--workdir", metavar="DIR", help="where --no-jit writes (default: temporary)")
    adv.add_argument("--credentials", help="AirPlay credentials, overriding the stored ones")
    adv.add_argument(
        "-v", "--verbose", action="store_true", help="show everything sent and received"
    )  # fmt: skip
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns the exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["play"]:  # the old ``pyatv-cast play FILE`` form still works
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(name)s: %(message)s"
    )
    try:
        asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except UserError as ex:
        print(ex.render(), file=sys.stderr)
        return 1
    except Exception as ex:  # pylint: disable=broad-except
        print(
            f"error: something unexpected went wrong: {type(ex).__name__}: {ex}\n"
            "  hint: run again with -v for details; if it keeps happening, please report it "
            "at https://github.com/bouchard/pyatv-cast/issues",
            file=sys.stderr,
        )
        if args.verbose:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
