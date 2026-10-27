# pyatv-hls

An AirPlay 2 **video sender** that extends [pyatv](https://github.com/postlund/pyatv).
It plays a video file (or an HLS URL) on AirPlay 2 receivers that pull HLS over HTTP,
and converts the file to HLS **just in time**, so nothing is written to disk. It can
also send to **DLNA** (UPnP MediaRenderer) TVs as an alternative, see [DLNA](#dlna).

pyatv can stream audio (`stream_file`) and, on Apple TVs, play a URL (`play_url`). On
some receivers, notably Samsung TVs, `stream_file` shows an audio-only icon and
`play_url` is rejected with `404` on `POST /play`. Those receivers use a different
flow, the one Apple's own senders and apps such as Airflow use. This package
implements it.

> **Status: works on one receiver, a Samsung UTU7000_KA (AirPlay SDK 3.3.0.54).** The
> protocol is undocumented. It was reconstructed by capturing what a commercial sender
> sends to a fake receiver, so other receivers may need changes. Reports and captures
> are welcome.

## How it works

Everything runs over one HAP-encrypted RTSP connection, using pyatv's pairing and
crypto:

1. `pair-verify` with your stored credentials.
2. `SETUP` (session), connect the encrypted **event channel** it announces, `RECORD`,
   `GET /info`.
3. `SETUP` with a stream of `type` 130; the receiver returns a `streamID`.
4. `POST /command` with header `X-Apple-StreamID`. The body is
   `{"params": {"data": <binary plist>}}`, and the inner plist is an
   `insertPlayQueueItem` whose `Content-Location` is a plain `http://` HLS playlist,
   followed by `setRate`.
5. The receiver fetches the playlist and segments itself over HTTP from this machine.
   `POST /feedback` every 2 s keeps the session alive, and the event channel reports
   `playbackState`, `timeJumped`, `itemPlayedToEnd` and so on.
6. `removePlayQueueItem` and `TEARDOWN` end it.

There is no `/play`, `/reverse` or FairPlay step on these receivers.

### Just-in-time HLS (on by default)

Given a video file, the package probes it once, lists its keyframes (about a second for
a 100-minute film) and builds a complete VOD playlist. Each `.ts` segment is made by
`ffmpeg` only when the receiver requests it, so playback starts at once, seeking is
instant and there are no temporary files.

Turn it off with `--no-jit` (CLI) or by calling `convert_to_directory()` (library). The
whole file is then converted to a directory first, with exactly the same segments, and
served as static files. Use that when you want the files kept (`--workdir`) or a slow
transcode finished before playback starts.

- H.264 video and AAC audio are **copied** (a remux, no quality loss, about 0.06 s per
  segment).
- Anything else is transcoded, **only the stream that needs it**: other video codecs or
  10-bit video go to H.264, other audio (AC-3, DTS, ...) goes to stereo AAC.
- Segments are cut just before the next keyframe's decode time, and all timestamps are
  shifted together, so neighbouring segments neither overlap nor leave gaps.
- An English audio track is preferred, otherwise the first.

An `http(s)://` HLS URL is sent to the receiver as is, and a local `.m3u8` is served as
a static directory.

## Subtitles

A subtitle is picked automatically, in this order, preferring English at each step:

1. a file you name with `--subtitles FILE` (`.srt` or `.vtt`)
2. a sidecar file next to the video whose name starts with the video's name
   (`Movie.srt`, `Movie.en.srt`, ...)
3. a text track inside the video (SRT, ASS, `mov_text`, WebVTT; picture-based tracks
   such as PGS are skipped)

Use `--no-subtitles` to send none. Files in Windows-1252 are converted to UTF-8, and
everything is converted in memory by ffmpeg, so nothing is written to disk.

- **AirPlay** gets the subtitle as a WebVTT track in a master playlist. The receiver only
  downloads it once the track is **selected on the TV** (use the TV's own subtitle or
  caption menu during playback). The track is offered as the default, but the Samsung
  tested does not select it by itself.
- **DLNA** gets the subtitle as an SRT file announced the way Samsung renderers read it
  (a `CaptionInfo.sec` response header and `sec:CaptionInfoEx` metadata), plus a
  `text/srt` resource for other renderers. This works with `--direct` and the default
  direct mode for renderers without HLS.

Subtitles are not burned into the picture: that needs an ffmpeg built with libass, which
many builds (Homebrew's included) lack.

## DLNA

Many TVs that support AirPlay also run a DLNA MediaRenderer. `--protocol dlna` uses it
instead. Nothing needs to be paired; the TV is found over SSDP, controlled with SOAP
`AVTransport` calls (`SetAVTransportURI`, `Play`, `Pause`, `Seek`, `Stop`), and then
fetches the media over HTTP from this machine. If the renderer lists HLS among what it
plays, the same just-in-time HLS server is used; otherwise the file is sent unchanged.

```sh
pyatv-hls play movie.mkv --protocol dlna --host 192.168.1.50
pyatv-hls play movie.mkv --protocol dlna --id "Living Room"       # part of its name
pyatv-hls play movie.mkv --protocol dlna --location http://192.168.1.50:9197/dmr
pyatv-hls play movie.mkv --protocol dlna --host 192.168.1.50 --direct
```

The CLI asks the renderer what it plays and picks for you. `--direct` serves the file
unchanged, with HTTP Range support, instead of HLS, and `--no-direct` forces HLS. In
direct mode the file must be in a format the TV plays itself.
End of playback is detected by polling the transport state.

> Tested on the same Samsung UTU7000_KA, which lists no HLS among the media types it
> plays, so the CLI sends it the file as is. That TV shows an "allow" prompt the first
> time a new sender connects; accept it on the TV or playback stays in `TRANSITIONING`.
> Renderers differ in which media types they accept.

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on your `PATH` (not needed for HLS URLs or `.m3u8` files)
- An AirPlay 2 receiver you have already **paired** with pyatv

## Install

```sh
uv pip install .          # or: pip install .
```

Until pyatv's SRP code supports `srptools` 2.x, this package pins `srptools<2`.

## Pair first

```sh
atvremote --id <device-id> --protocol airplay pair
```

Enter the PIN shown on the TV. pyatv stores the credentials in its own storage
(`~/.pyatv.conf`); this package reads them from there and never prints them.

## Usage

```sh
pyatv-hls play movie.mkv --id AA:BB:CC:DD:EE:FF
pyatv-hls play movie.mp4 --host 192.168.1.50 --start 600
pyatv-hls play https://example.com/stream/index.m3u8 --id AA:BB:CC:DD:EE:FF -v
pyatv-hls play movie.mkv --id AA:BB:CC:DD:EE:FF --no-jit --workdir /tmp/hls
```

| Option | Meaning |
| --- | --- |
| `--protocol` | `airplay` (default) or `dlna` |
| `--id` / `--host` | Receiver identifier (AirPlay) or part of its name (DLNA), or its IP address. One of `--id`, `--host`, `--location` is required |
| `--location URL` | DLNA only: the renderer's device description URL, skipping discovery |
| `--direct` / `--no-direct` | DLNA only: send the file unchanged, or force HLS (default: ask the renderer) |
| `--start SECONDS` | Start position |
| `--subtitles FILE` / `--no-subtitles` | Use this subtitle file, or send none (default: find one) |
| `--jit` / `--no-jit` | Convert as the receiver asks (default) or convert everything first |
| `--workdir DIR` | Parent directory for `--no-jit` output (default: a temporary directory, removed afterwards) |
| `--credentials` | Override the stored AirPlay credentials |
| `-v` | Log every request, response and event |

Include `-v` output in a bug report. Ctrl-C stops playback cleanly.

The machine running this must be reachable from the TV: the TV fetches the playlist and
segments over HTTP from a port opened on this machine's LAN address for the duration of
playback.

### As a library

```python
from pathlib import Path
from pyatv_hls import HlsPlayer, JitHlsServer, plan

player = HlsPlayer(address, port, credentials)   # pyatv HapCredentials
await player.connect()

media = await plan(Path("movie.mkv"))
server = JitHlsServer(media, player.control.local_ip)
url = await server.start()

await player.play(url, title="Movie", start=0.0)
await player.wait_until_ended()   # or pause() / resume() / stop()

await player.stop()
await server.stop()
await player.close()
```

For the non-just-in-time mode, replace the server with
`StaticHlsServer(await convert_to_directory(media, Path("/tmp/hls")), player.control.local_ip)`.

Pass `on_event=callback` to `HlsPlayer` to receive the receiver's decoded events.

For DLNA, `DlnaPlayer` has the same methods (`connect`, `play`, `pause`, `resume`, `stop`,
`wait_until_ended`, `close`, `local_ip`):

```python
from pyatv_hls import DlnaPlayer, find_renderer

player = DlnaPlayer(await find_renderer(host="192.168.1.50"))
await player.connect()
# serve media with JitHlsServer(media, player.local_ip) exactly as above
await player.play(url, title="Movie")
```

## Development

```sh
uv run --group dev pytest
```

The session tests run an in-process mock receiver on loopback (no encryption, no
hardware). The HLS tests generate a tiny video with `ffmpeg` and are skipped without it.

## Limitations

- AirPlay is tested on one receiver model, a Samsung UTU7000_KA. Others may differ.
- DLNA is tested on the same Samsung model (direct file and subtitles) and otherwise
  against a mock renderer.
- No seeking while playing yet (use `--start`), choosing between several subtitle or audio
  tracks, burned-in subtitles, DRM or playlists.
- Transcoding uses software `libx264`, so a high-bitrate source that needs it may not
  convert faster than real time.

## Acknowledgements

The protocol was worked out by capturing a commercial AirPlay sender talking to a fake
receiver built on pyatv's server-side pairing code, and by reading the
[pyatv](https://github.com/postlund/pyatv) codebase.
