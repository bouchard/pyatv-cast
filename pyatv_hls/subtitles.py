"""Subtitle discovery and conversion.

A subtitle is taken from, in order: a file you name, a sidecar file next to the video
(``movie.srt``, ``movie.en.srt``, ...) or a text track embedded in the video. English
is preferred in each place. Nothing is written to disk: conversion happens in memory
through ffmpeg.

HLS carries subtitles as WebVTT; DLNA renderers (Samsung) fetch an SRT file.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from pyatv_hls import hls

_LOGGER = logging.getLogger(__name__)

SIDECAR_SUFFIXES = (".srt", ".vtt")
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
ENGLISH = {"en", "eng", "english"}
_LANGUAGE_NAMES = {"en": "English", "eng": "English", "english": "English"}
_FORMAT = {".srt": "srt", ".vtt": "webvtt"}


@dataclass
class Subtitle:
    """One chosen subtitle track."""

    language: str
    name: str
    sidecar: Optional[Path] = None  # a file next to (or named for) the video
    video: Optional[Path] = None  # the video holding an embedded track
    stream_index: int = 0  # index among the video's subtitle streams
    _cache: dict = field(default_factory=dict, repr=False)

    async def webvtt(self) -> bytes:
        """The subtitle as WebVTT."""
        return await self._convert("webvtt")

    async def srt(self) -> bytes:
        """The subtitle as SRT."""
        return await self._convert("srt")

    async def _convert(self, fmt: str) -> bytes:
        if fmt not in self._cache:
            if self.sidecar is not None:
                text = _decode(self.sidecar.read_bytes())
                src = _FORMAT.get(self.sidecar.suffix.lower(), "srt")
                self._cache[fmt] = await hls._run_with_input(
                    text.encode(), "ffmpeg", "-nostdin", "-v", "error",
                    "-f", src, "-i", "pipe:0", "-f", fmt, "pipe:1",
                )  # fmt: skip
            else:
                self._cache[fmt] = await hls._run(
                    "ffmpeg", "-nostdin", "-v", "error", "-i", str(self.video),
                    "-map", f"0:s:{self.stream_index}", "-f", fmt, "pipe:1",
                )  # fmt: skip
        return self._cache[fmt]


def _decode(raw: bytes) -> str:
    """Decode subtitle bytes: UTF-8 (with or without BOM), else Windows-1252."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _language_of(stem: str, video_stem: str) -> str:
    """The language token in ``movie.en.srt``, or an empty string."""
    extra = stem[len(video_stem):].lstrip(".").lower()
    return extra.split(".")[0] if extra else ""


def _display(language: str) -> str:
    return _LANGUAGE_NAMES.get(language.lower(), language.capitalize() or "Subtitles")


def sidecars(video: Path) -> List[Subtitle]:
    """Subtitle files next to ``video`` whose name starts with the video's name."""
    found = []
    for path in sorted(video.parent.iterdir()):
        if path.suffix.lower() not in SIDECAR_SUFFIXES:
            continue
        if path.stem == video.stem or path.stem.startswith(video.stem + "."):
            language = _language_of(path.stem, video.stem)
            found.append(Subtitle(language, _display(language), sidecar=path))
    return found


def embedded(video: Path, info: dict) -> List[Subtitle]:
    """Text subtitle tracks inside ``video``, from its ffprobe ``info``."""
    streams = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    found = []
    for index, stream in enumerate(streams):
        if stream.get("codec_name") not in TEXT_CODECS:
            continue  # bitmap subtitles (PGS, VobSub) cannot be converted to text
        tags = stream.get("tags", {})
        language = tags.get("language", "")
        found.append(
            Subtitle(language, tags.get("title") or _display(language), video=video, stream_index=index)
        )
    return found


def _prefer_english(candidates: List[Subtitle]) -> Optional[Subtitle]:
    for candidate in candidates:
        if candidate.language.lower() in ENGLISH:
            return candidate
    return candidates[0] if candidates else None


async def find(video: Path, explicit: Optional[Path] = None) -> Optional[Subtitle]:
    """Choose the subtitle to send with ``video``, or None if it has none."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"subtitle file not found: {explicit}")
        return Subtitle("", _display(""), sidecar=explicit)
    chosen = _prefer_english(sidecars(video))
    if chosen is None:
        chosen = _prefer_english(embedded(video, await hls.probe(video)))
    if chosen is not None:
        _LOGGER.info("subtitles: %s (%s)", chosen.name, chosen.sidecar or "embedded")
    return chosen
