"""Command line: ``pyatv-hls play <video-or-m3u8> --id <device id>``."""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

import pyatv
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from pyatv_hls import dlna, hls, subtitles
from pyatv_hls.session import HlsPlayer

HLS_MIME = "application/vnd.apple.mpegurl"


async def _find_device(args: argparse.Namespace):
    loop = asyncio.get_running_loop()
    storage = FileStorage.default_storage(loop)
    await storage.load()
    kwargs = {"hosts": [args.host]} if args.host else {"identifier": args.id}
    configs = await pyatv.scan(loop, timeout=10, storage=storage, **kwargs)
    if not configs:
        sys.exit("device not found")
    config = configs[0]
    service = config.get_service(Protocol.AirPlay)
    if service is None:
        sys.exit("device does not advertise AirPlay")
    settings = await storage.get_settings(config)
    creds = args.credentials or settings.protocols.airplay.credentials
    if not creds:
        sys.exit("no AirPlay credentials; pair first: atvremote --protocol airplay pair")
    return str(config.address), service.port, parse_credentials(creds)


class Source(NamedTuple):
    """What to tell the receiver to fetch, and the server providing it."""

    url: str
    server: Optional[Any]
    duration: Optional[float]
    mime: str = HLS_MIME
    subtitle_url: Optional[str] = None


async def _serve(args: argparse.Namespace, local_ip: str, workdir: Optional[Path]) -> Source:
    """Prepare ``args.source`` for the receiver and start serving it."""
    source = args.source
    if source.startswith(("http://", "https://")):
        return Source(source, None, None)
    path = Path(source).resolve()
    if path.suffix == ".m3u8":
        server = hls.StaticHlsServer(path, local_ip)
        return Source(await server.start(), server, None)
    subtitle = None
    if not args.no_subtitles:
        explicit = Path(args.subtitles) if args.subtitles else None
        try:
            subtitle = await subtitles.find(path, explicit)
        except FileNotFoundError as ex:
            sys.exit(str(ex))
    if args.direct:
        info = await hls.probe(path)
        direct = dlna.DirectFileServer(path, local_ip, subtitle=subtitle)
        duration = float(info["format"]["duration"])
        url = await direct.start()
        return Source(url, direct, duration, dlna.guess_mime(path), direct.subtitle_url)
    media = await hls.plan(path, subtitle)
    if args.jit:
        server = hls.JitHlsServer(media, local_ip)
    else:
        print(f"Converting {path.name} ({len(media.segments)} segments) ...")
        playlist = await hls.convert_to_directory(media, workdir)
        server = hls.StaticHlsServer(playlist, local_ip)
    return Source(await server.start(), server, media.duration)


async def _make_player(args: argparse.Namespace):
    """Find the receiver and return an unconnected player for the chosen protocol."""
    if args.protocol == "dlna":
        try:
            renderer = await dlna.find_renderer(args.host, args.id, args.location)
        except dlna.DlnaError as ex:
            sys.exit(str(ex))
        return dlna.DlnaPlayer(renderer)
    address, port, credentials = await _find_device(args)
    return HlsPlayer(address, port, credentials)


async def _progress(started: float, duration) -> None:
    while True:
        elapsed = time.monotonic() - started
        total = f" / {duration:.0f}s" if duration else ""
        print(f"\r{elapsed:8.0f}s{total}", end="", flush=True)
        await asyncio.sleep(1)


async def play(args: argparse.Namespace) -> None:
    """Connect, serve the media, and play until it ends."""
    player = await _make_player(args)
    # Only created when just-in-time conversion is turned off
    workdir = None
    if not args.jit:
        workdir = tempfile.TemporaryDirectory(prefix="pyatv-hls-", dir=args.workdir)
    await player.connect()
    server = None
    try:
        if isinstance(player, dlna.DlnaPlayer) and args.direct is None:
            args.direct = not await player.supports_hls()
            print("renderer plays HLS" if not args.direct else "renderer has no HLS: sending the file as is")
        source = await _serve(args, player.local_ip, Path(workdir.name) if workdir else None)
        server = source.server
        if isinstance(player, dlna.DlnaPlayer):
            player.mime = source.mime
        extra = {"subtitle_url": source.subtitle_url} if isinstance(player, dlna.DlnaPlayer) else {}
        await player.play(source.url, title=Path(args.source).stem, start=args.start, **extra)
        ticker = asyncio.ensure_future(_progress(time.monotonic(), source.duration))
        try:
            limit = (source.duration - args.start + 30) if source.duration else None
            await asyncio.wait_for(player.wait_until_ended(), limit)
        except asyncio.TimeoutError:
            pass
        finally:
            ticker.cancel()
    finally:
        await player.stop()
        if server:
            await server.stop()
        await player.close()
        if workdir:
            workdir.cleanup()
        print()


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="pyatv-hls")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("play", help="play a video file, .m3u8 or http(s) HLS URL")
    p.add_argument("source")
    p.add_argument(
        "--protocol", choices=["airplay", "dlna"], default="airplay",
        help="how to reach the receiver (default: airplay)",
    )  # fmt: skip
    target = p.add_mutually_exclusive_group()
    target.add_argument("--id", help="AirPlay device identifier, or part of a DLNA name")
    target.add_argument("--host", help="device IP address")
    target.add_argument("--location", help="DLNA device description URL (skips discovery)")
    p.add_argument(
        "--direct", action=argparse.BooleanOptionalAction, default=None,
        help="DLNA: serve the file unchanged instead of as HLS (default: ask the "
        "renderer, and send the file as is unless it lists HLS)",
    )  # fmt: skip
    p.add_argument("--credentials", help="override stored AirPlay credentials")
    p.add_argument(
        "--jit", action=argparse.BooleanOptionalAction, default=True,
        help="convert to HLS just in time, as the receiver asks (default); "
        "--no-jit converts the whole file to --workdir before playing",
    )  # fmt: skip
    p.add_argument("--workdir", help="parent directory for --no-jit output (default: temp)")
    p.add_argument(
        "--subtitles", metavar="FILE",
        help="subtitle file (.srt or .vtt) to show; by default a file next to the video, "
        "or a text track inside it, is used (English preferred)",
    )  # fmt: skip
    p.add_argument("--no-subtitles", action="store_true", help="do not send subtitles")
    p.add_argument("--start", type=float, default=0.0, help="start position (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if not (args.id or args.host or args.location):
        parser.error("one of --id, --host or --location is required")
    if args.subtitles and args.no_subtitles:
        parser.error("--subtitles and --no-subtitles cannot be used together")
    if args.protocol == "airplay" and (args.location or args.direct is not None):
        parser.error("--location and --direct only apply to --protocol dlna")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(play(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
