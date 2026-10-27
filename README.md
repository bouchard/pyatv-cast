# pyatv-cast

Play a video file or a web video on your TV, with one command:

```sh
pyatv-cast movie.mkv
pyatv-cast https://www.youtube.com/watch?v=VIDEO
pyatv-cast movie.mkv --start 600          # begin ten minutes in
```

It finds your TV, sends the video the easiest way the TV allows, and tells you what to do
when something goes wrong. It speaks **DLNA** (preferred, no setup) and **AirPlay 2**
(for TVs without DLNA). The AirPlay side is built on
[pyatv](https://github.com/postlund/pyatv) by Pierre Ståhl and its contributors; see
[Credits](#credits-and-licenses).

## What it does by default

| You give it | What happens |
| --- | --- |
| A file the TV can play | Sent **untouched**: the TV downloads the file itself. |
| A file the TV cannot play | Converted **just in time** while the TV downloads it. Video and audio the TV already understands are copied, so usually only the audio (or nothing but the container) is re-encoded. No temporary files. |
| A direct media URL (`.mp4`, `.m3u8`, ...) | Handed to the TV as is when it can fetch it (plain `http://`), otherwise relayed through this computer without re-encoding. |
| Any other URL (a YouTube page, ...) | Asked of [yt-dlp](https://github.com/yt-dlp/yt-dlp), then relayed and converted only if needed. If a site will not let ffmpeg read the address directly (HTTP 403), yt-dlp downloads the video first, with a progress display. |
| A web or remote video, over AirPlay | Streamed **live**: ffmpeg writes HLS segments to a temporary folder as it downloads, and playback starts after a few seconds. `--download-first` fetches everything before playing instead. |

It asks the TV what it can play, so "can the TV play this?" is decided from the TV's own
list of media types plus the video and audio codecs, not guessed from the file name.

## Install

```sh
uv tool install .        # or: pip install .
```

You also need **ffmpeg** (with `ffprobe`) on your `PATH`: `brew install ffmpeg`. yt-dlp is
installed with the package and used as a library, so nothing else is needed.

## Choosing the TV

By default the TV is found automatically.

- With **one** TV on the network it is used and remembered.
- With **several**, you are asked once, and the answer is remembered in
  `~/.config/pyatv-cast/config.json` (it holds only the TV's name and address, never
  credentials).
- From then on the remembered TV is used without searching the network.

```sh
pyatv-cast --list                      # show the TVs found
pyatv-cast movie.mkv --choose          # pick a different TV (and remember it)
pyatv-cast movie.mkv --tv Bedroom      # this TV, just this once (name, IP or id)
pyatv-cast --forget                    # forget the remembered TV
```

`--protocol dlna` or `--protocol airplay` forces one; the default `auto` uses DLNA when the
TV has it, because it needs no pairing.

## Options

| Option | Meaning |
| --- | --- |
| `--tv TV` | A TV's name (or part of it), IP address or AirPlay id. Also `--host`, `--id`, and a DLNA description URL |
| `--choose`, `--forget`, `--list` | Pick a TV again, forget the remembered one, or list what is on the network |
| `--protocol {auto,dlna,airplay}` | How to reach the TV (default `auto`) |
| `--start SECONDS` | Start position |
| `--mode {auto,direct,convert}` | `auto` sends untouched when possible, `direct` never converts, `convert` always converts |
| `--subtitles FILE` / `--no-subtitles` | Use this subtitle file, or send none |
| `--download-first` | AirPlay with a web or remote video: download all of it (with progress) before playing, for a seek bar and subtitles, instead of streaming it live |
| `--ytdlp-format FORMAT` | yt-dlp format selector (default: up to 1080p, H.264 + AAC, so it can be copied) |
| `--cookies-from-browser BROWSER` | Use a browser's login for sites that need it, e.g. `chrome` |
| `--no-jit`, `--workdir DIR` | AirPlay/HLS only: convert everything to disk first |
| `-v` | Show everything sent and received |

## Subtitles

A subtitle is chosen automatically, preferring English: a file you name with
`--subtitles`, then a file next to the video (`Movie.srt`, `Movie.en.srt`, ...), then a
text track inside it. Conversion happens in memory.

- **DLNA**: sent as an SRT file the TV picks up (Samsung's `CaptionInfo.sec`).
- **AirPlay**: offered as a WebVTT track. Most TVs only load it once **you select it in
  the TV's own subtitle menu**.

Videos downloaded by yt-dlp are kept in `pyatv-cast-cache` inside the system temporary folder
(which the OS clears on its own), so playing the same address again, in the same format, reuses
the copy instead of downloading it again.

Subtitles for web videos are fetched by yt-dlp when the video is downloaded first
(`--download-first`, or automatically when ffmpeg cannot read the address). They are
fetched **separately** from the video and retried with a pause between tries, because sites
such as YouTube often answer subtitle requests with "429 Too Many Requests". If they never
come through, the video still plays, with a note saying so. Streaming live does not include
subtitles; use `--subtitles FILE` or `--download-first`.

## When something goes wrong

Every failure says what happened and what to try. For example:

```
error: no TV was found on the network.
  hint: Is the TV on and awake? Many TVs stop answering discovery in standby.
        Is this computer on the same Wi-Fi or network as the TV?
        If you know the TV's address, give it: `--tv 192.168.1.50`.
```

The common ones:

- **The TV never starts playing.** The CLI watches for this and says whether the TV never
  asked this computer for the video (usually a firewall: allow incoming connections for
  Python in System Settings > Network > Firewall) or fetched it but could not play it
  (try `--mode convert`).
- **An "allow" prompt on the TV.** Some TVs ask the first time a new device sends to them.
  Accept it with the remote.
- **No TV found while a VPN is on.** A VPN or tunnel can hide the TV; the message lists
  your network interfaces when there is more than one.
- **A web page will not play.** Sites change often: update yt-dlp, or add
  `--cookies-from-browser chrome` for sites that need a login.
- **"ffmpeg cannot read this video straight from the site (403)".** Normal for YouTube and
  similar sites. The video is downloaded with yt-dlp instead, which needs free disk space
  (shown before it starts); use `--workdir` to put the temporary files on a bigger disk.
- **AirPlay says "not paired".** Pair once with
  `atvremote --id <id> --protocol airplay pair` and enter the PIN shown on the TV.

## How it works

### DLNA

The TV is found over SSDP and controlled with SOAP `AVTransport` calls
(`SetAVTransportURI`, `Play`, `Pause`, `Seek`, `Stop`). It then downloads the media over HTTP
from a small server this program runs for the length of playback. The renderer's
`ConnectionManager` says which media types it plays, and that decides how the video is sent:

1. **As is**: the file with HTTP Range support.
2. **HLS**: a playlist made from the file's keyframes, with each segment produced by
   ffmpeg only when requested (when the TV lists HLS).
3. **Live MPEG-TS**: one ffmpeg process converting while the TV downloads (for TVs that
   play files but not HLS), copying whichever streams are already supported.

Subtitles go out as an SRT file announced in the DIDL metadata and in the `CaptionInfo.sec`
response header. Samsung TVs also need the `contentFeatures.dlna.org` response header and a
`Stop` before a new item; both are handled.

### AirPlay 2

Everything runs over one HAP-encrypted RTSP connection, using pyatv's pairing and crypto.
The flow was captured from a commercial sender talking to a fake receiver:

1. `pair-verify` with your stored credentials.
2. `SETUP` (session), connect the encrypted **event channel**, `RECORD`, `GET /info`.
3. `SETUP` with a stream of `type` 130; the receiver returns a `streamID`.
4. `POST /command` (header `X-Apple-StreamID`) with `{"params": {"data": <binary plist>}}`,
   whose inner plist is `insertPlayQueueItem` carrying an `http://` HLS playlist URL, then
   `setRate`. `POST /feedback` every 2 s keeps the session alive.
5. `removePlayQueueItem` and `TEARDOWN` end it.

There is no `/play`, `/reverse` or FairPlay step on these receivers. HLS comes from the
same just-in-time server as above: H.264 and AAC are copied, anything else is converted,
and segments are cut just before the next keyframe's decode time so neighbours neither
overlap nor leave gaps.

That server needs the whole file's keyframe list up front, which for a remote video would
mean downloading it all first. So a **remote** video is instead handled by ffmpeg writing
HLS segments into a temporary folder as it fetches them, with a playlist that grows until
the video is complete (an HLS `EVENT` playlist). Playback starts once the first two
segments exist, and ffmpeg stays well ahead of playback for a copied video. The temporary
folder is removed when playback ends or you press Ctrl-C.

## Library use

```python
from pyatv_cast import DlnaPlayer, JitHlsServer, find_renderer, plan
from pathlib import Path

player = DlnaPlayer(await find_renderer(host="192.168.1.50"))
await player.connect()
server = JitHlsServer(await plan(Path("movie.mkv")), player.local_ip)   # HLS
player.mime = "application/vnd.apple.mpegurl"
await player.play(await server.start(), title="Movie")
await player.wait_until_ended()
await player.stop(); await server.stop(); await player.close()
```

`AirPlayPlayer` (AirPlay) has the same methods; see `pyatv_cast/cli.py` for a complete example
of choosing a delivery method.

## Development

```sh
uv run --group dev pytest        # fails below 95% coverage
```

Network pieces are tested against in-process mock renderers and receivers. The HLS and
streaming tests generate a tiny video with ffmpeg and are skipped without it.

## Limitations

- Tested on one TV, a Samsung UTU7000_KA, over both DLNA and AirPlay. Others may differ.
- While a web video streams live over AirPlay, the TV's seek bar covers only what has been
  fetched so far, and subtitles are not included (`--download-first` fixes both).
- No seeking while playing yet (use `--start`), choice between several subtitle or audio
  tracks, burned-in subtitles (they need an ffmpeg built with libass), or DRM.
- A converted video is streamed live, so the TV's own seek bar does not work on it.
- Video that needs re-encoding uses VideoToolbox on a Mac, else software `libx264`, which
  may not keep up in real time for high-bitrate sources.

## Credits and licenses

### pyatv

This project would not exist without **[pyatv](https://github.com/postlund/pyatv)**
([pyatv.dev](https://pyatv.dev)), created by **Pierre Ståhl** and maintained by its
contributors, and released under the MIT License (Copyright (c) 2020 Pierre Ståhl). It is
the reason AirPlay works here at all. Thank you.

pyatv is used as a normal dependency: it is installed from PyPI, imported as is, and none of
its source files are copied into this repository or modified. Specifically, the AirPlay
sender relies on pyatv for:

- **Device discovery and pairing.** Scanning for AirPlay receivers, reading the credentials
  stored by pyatv (`atvremote ... pair`), and parsing them (`pyatv.scan`,
  `FileStorage`, `HapCredentials`).
- **HomeKit pairing and encryption.** The `pair-verify` handshake and the HAP-encrypted
  channel set-up, including the key derivation for the event channel
  (`pyatv.protocols.airplay.auth`, `pyatv.auth.hap_channel`, and the event-channel salts in
  `pyatv.protocols.airplay.ap2_session`).
- **RTSP/HTTP transport.** The connection and request/response handling used to talk to the
  receiver (`pyatv.support.http`, `pyatv.support.rtsp`).
- **The event channel.** `pyatv_cast.airplay` subclasses pyatv's `EventChannel` to read the
  receiver's playback events, and its `AirPlayError` extends pyatv's `PlaybackError`.

pyatv's server-side pairing code was also used to build a throw-away fake AirPlay receiver
for capturing how a commercial sender behaves. That is how the AirPlay video flow documented
above was worked out; the capture tool is not part of this repository. pyatv's open
documentation and source were the reference for the pairing and transport details.

If any pyatv source is ever copied into this project, it will keep its original copyright
and license notice and be marked as such. This project is independent: it is not affiliated
with, or endorsed by, the pyatv project.

### Other software

| Project | Used for | License |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Finding the real video address behind web pages (used as a library) | Unlicense |
| [FFmpeg](https://ffmpeg.org) | Inspecting and converting video (run as a separate program) | LGPL/GPL |
| [aiohttp](https://github.com/aio-libs/aiohttp) | The small HTTP servers the TV downloads from | Apache-2.0 |
| [ifaddr](https://github.com/ifaddr/ifaddr) | Listing network interfaces so discovery works with VPNs | MIT |
| [srptools](https://github.com/idlesign/srptools) | SRP, required by pyatv's pairing | BSD |

The DLNA/UPnP support follows the public UPnP AV and DLNA specifications; Samsung-specific
behaviour (`CaptionInfo.sec`) was learned from the TV's own requests.

This project is licensed under the MIT License; see [LICENSE](LICENSE).
