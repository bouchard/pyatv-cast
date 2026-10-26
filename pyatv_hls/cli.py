"""Command line: ``pyatv-hls play <video-or-m3u8> --id <device id>``."""

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

import pyatv
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from pyatv_hls import playlist
from pyatv_hls.media import SegmentServer, prepare
from pyatv_hls.session import HlsPlayer


def _status(info: dict) -> None:
    pos, dur = info.get("position", 0), info.get("duration", 0)
    print(f"\r{pos:8.1f} / {dur:.1f}s  rate={info.get('rate', 0)}", end="", flush=True)


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


async def play(args: argparse.Namespace) -> None:
    """Prepare media, connect, and play until the item ends."""
    address, port, credentials = await _find_device(args)

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="pyatv-hls-"))
    media = await prepare(Path(args.source), workdir)

    player = HlsPlayer(address, port, credentials, on_status=_status)
    await player.connect()
    server = SegmentServer(media.directory, player.control.local_ip)
    try:
        base_url = await server.start()
        player.set_documents(
            master=playlist.build_master(
                media.bandwidth, media.codecs, media.resolution, media.frame_rate
            ),
            media=playlist.rewrite_media(media.playlist.read_text(), base_url),
        )
        await player.play(start=args.start)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await player.stop()
    finally:
        await server.stop()
        await player.close()
        print()


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="pyatv-hls")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("play", help="play a video (or existing .m3u8) via AirPlay HLS")
    p.add_argument("source")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="device identifier, e.g. F0:70:4F:CD:3A:6C")
    target.add_argument("--host", help="device IP address")
    p.add_argument("--credentials", help="override stored AirPlay credentials")
    p.add_argument("--start", type=float, default=0.0, help="start position (s)")
    p.add_argument("--workdir", help="where to put the remuxed HLS (default: temp)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(play(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
