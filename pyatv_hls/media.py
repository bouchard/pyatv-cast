"""Media preparation: remux to HLS without re-encoding, and serve the segments."""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiohttp import web

from pyatv_hls.playlist import h264_codec_string

_LOGGER = logging.getLogger(__name__)


@dataclass
class HlsMedia:
    """A ready-to-serve HLS rendition."""

    directory: Path
    playlist: Path
    bandwidth: int
    codecs: str
    resolution: Optional[str] = None
    frame_rate: Optional[float] = None


async def _run(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')[-500:]}")
    return out


async def probe(source: Path) -> dict:
    """Return codec/resolution/bitrate facts about ``source`` via ffprobe."""
    out = await _run(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(source),
    )  # fmt: skip
    return json.loads(out)


def _fps(rate: str) -> Optional[float]:
    try:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else None
    except (ValueError, AttributeError):
        return None


def describe(info: dict, directory: Path, playlist: Path) -> HlsMedia:
    """Build an :class:`HlsMedia` from ffprobe output."""
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if video["codec_name"] != "h264":
        raise ValueError(
            f"video codec is {video['codec_name']}; only H.264 can be remuxed "
            "without re-encoding"
        )
    codecs = [h264_codec_string(video.get("profile"), video.get("level"))]
    if audio and audio["codec_name"] == "aac":
        codecs.append("mp4a.40.2")
    bandwidth = int(info.get("format", {}).get("bit_rate", 0)) or 8_000_000
    return HlsMedia(
        directory=directory,
        playlist=playlist,
        bandwidth=bandwidth,
        codecs=",".join(codecs),
        resolution=f"{video['width']}x{video['height']}",
        frame_rate=_fps(video.get("avg_frame_rate", "")),
    )


async def prepare(source: Path, workdir: Path, segment_seconds: int = 10) -> HlsMedia:
    """Return HLS media for ``source``.

    An existing ``.m3u8`` is used as is. Anything else is remuxed with ``-c copy``
    (fast, lossless) into ``workdir``.
    """
    source = source.resolve()
    if source.suffix == ".m3u8":
        first_segment = next(
            line for line in source.read_text().splitlines()
            if line and not line.startswith("#")
        )  # fmt: skip
        info = await probe(source.parent / first_segment)
        return describe(info, source.parent, source)

    info = await probe(source)
    workdir.mkdir(parents=True, exist_ok=True)
    playlist = workdir / "index.m3u8"
    if not playlist.exists():
        _LOGGER.info("Remuxing %s to HLS in %s", source.name, workdir)
        await _run(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-f", "hls", "-hls_time", str(segment_seconds),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(workdir / "seg%05d.ts"),
            str(playlist),
        )  # fmt: skip
    return describe(info, workdir, playlist)


class SegmentServer:
    """Plain HTTP server for media segments (the receiver fetches these directly)."""

    def __init__(self, directory: Path, address: str, port: int = 0) -> None:
        """Initialize a new SegmentServer."""
        self._address = address
        self._port = port
        self._app = web.Application()
        # Only segments are exposed, never the playlist or other files
        self._app.router.add_static("/", directory, follow_symlinks=False)
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> str:
        """Start serving and return the base URL."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        assert site._server and site._server.sockets  # noqa: SLF001
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
