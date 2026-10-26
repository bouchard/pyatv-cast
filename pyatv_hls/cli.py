"""Command line: ``pyatv-hls play <video-or-m3u8> --id <device id>``."""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import pyatv
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from pyatv_hls import hls
from pyatv_hls.session import HlsPlayer


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


async def _serve(args: argparse.Namespace, local_ip: str, workdir: Optional[Path]):
    """Return (playlist_url, server_or_None, duration_or_None) for the source."""
    source = args.source
    if source.startswith(("http://", "https://")):
        return source, None, None
    path = Path(source).resolve()
    if path.suffix == ".m3u8":
        server = hls.StaticHlsServer(path, local_ip)
        return await server.start(), server, None
    media = await hls.plan(path)
    if args.jit:
        server = hls.JitHlsServer(media, local_ip)
    else:
        print(f"Converting {path.name} ({len(media.segments)} segments) ...")
        playlist = await hls.convert_to_directory(media, workdir)
        server = hls.StaticHlsServer(playlist, local_ip)
    return await server.start(), server, media.duration


async def _progress(started: float, duration) -> None:
    while True:
        elapsed = time.monotonic() - started
        total = f" / {duration:.0f}s" if duration else ""
        print(f"\r{elapsed:8.0f}s{total}", end="", flush=True)
        await asyncio.sleep(1)


async def play(args: argparse.Namespace) -> None:
    """Connect, serve the media just in time, and play until it ends."""
    address, port, credentials = await _find_device(args)
    # Only created when just-in-time conversion is turned off
    workdir = None
    if not args.jit:
        workdir = tempfile.TemporaryDirectory(prefix="pyatv-hls-", dir=args.workdir)
    player = HlsPlayer(address, port, credentials)
    await player.connect()
    server = None
    try:
        assert player.control
        url, server, duration = await _serve(
            args, player.control.local_ip, Path(workdir.name) if workdir else None
        )
        await player.play(url, title=Path(args.source).stem, start=args.start)
        ticker = asyncio.ensure_future(_progress(time.monotonic(), duration))
        try:
            limit = (duration - args.start + 30) if duration else None
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
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="device identifier, e.g. F0:70:4F:CD:3A:6C")
    target.add_argument("--host", help="device IP address")
    p.add_argument("--credentials", help="override stored AirPlay credentials")
    p.add_argument(
        "--jit", action=argparse.BooleanOptionalAction, default=True,
        help="convert to HLS just in time, as the receiver asks (default); "
        "--no-jit converts the whole file to --workdir before playing",
    )  # fmt: skip
    p.add_argument("--workdir", help="parent directory for --no-jit output (default: temp)")
    p.add_argument("--start", type=float, default=0.0, help="start position (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(play(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
