"""Playlist helpers for the AirPlay HLS ("mlhls") flow.

The receiver never fetches playlists itself. It asks the sender for them over the
reverse channel (FCUP), addressing them with an ``mlhls://localhost/...`` URL. Media
segments inside those playlists are ordinary URLs the receiver fetches directly.
"""

import re
from typing import Optional

LOCAL_PREFIX = "mlhls://localhost"
MASTER_PATH = "/master.m3u8"
MEDIA_PATH = "/media.m3u8"

MASTER_URL = LOCAL_PREFIX + MASTER_PATH
MEDIA_URL = LOCAL_PREFIX + MEDIA_PATH

_H264_PROFILES = {"baseline": 0x42, "main": 0x4D, "high": 0x64}


def h264_codec_string(profile: Optional[str], level: Optional[int]) -> str:
    """Return an RFC 6381 ``avc1`` string, e.g. ``avc1.640028``."""
    profile_idc = _H264_PROFILES.get((profile or "high").lower().split()[0], 0x64)
    return f"avc1.{profile_idc:02X}00{(level or 40):02X}"


def build_master(
    bandwidth: int,
    codecs: str,
    resolution: Optional[str] = None,
    frame_rate: Optional[float] = None,
) -> str:
    """Build a single-variant master playlist pointing at the media playlist."""
    attrs = [f"BANDWIDTH={bandwidth}", f'CODECS="{codecs}"']
    if resolution:
        attrs.append(f"RESOLUTION={resolution}")
    if frame_rate:
        attrs.append(f"FRAME-RATE={frame_rate:.3f}")
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-STREAM-INF:{','.join(attrs)}\n"
        f"{MEDIA_URL}\n"
    )


def rewrite_media(playlist: str, segment_base_url: str) -> str:
    """Make every segment URI in a media playlist absolute.

    ``segment_base_url`` is where the receiver can fetch segments over plain HTTP,
    e.g. ``http://10.0.0.245:8000/``.
    """
    base = segment_base_url.rstrip("/") + "/"
    out = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "://" not in stripped:
            line = base + stripped
        elif stripped.startswith("#EXT-X-MAP:") or stripped.startswith("#EXT-X-KEY:"):
            line = re.sub(
                r'URI="(?![a-z]+://)([^"]+)"',
                lambda m: f'URI="{base}{m.group(1)}"',
                line,
            )
        out.append(line)
    return "\n".join(out) + "\n"


def total_duration(playlist: str) -> float:
    """Sum the ``#EXTINF`` durations of a media playlist."""
    return sum(
        float(m.group(1)) for m in re.finditer(r"#EXTINF:([0-9.]+)", playlist)
    )
