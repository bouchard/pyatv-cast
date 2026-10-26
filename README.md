# pyatv-hls

An AirPlay 2 **HLS video sender** that extends [pyatv](https://github.com/postlund/pyatv).

pyatv can stream audio (`stream_file`) and, on Apple TVs, play a URL (`play_url`).
Some receivers, notably Samsung TVs, advertise the HLS video feature bit
(`VideoHTTPLiveStreams`) but not the plain `Video` bit. On those devices pyatv's
`stream_file` shows an audio-only icon and `play_url` is rejected with `404` on
`POST /play`. This package is an attempt to implement the AirPlay HLS flow that
receivers like [UxPlay](https://github.com/FDH2/UxPlay) implement.

> **Status: experimental, and it does not work on the one TV it has been tried on.**
> The sender logic passes tests against a mock receiver, but a real Samsung
> UTU7000_KA (AirPlay SDK 3.3.0.54) rejects it, see [Findings](#findings). The
> protocol is undocumented by Apple and was reconstructed from open-source *receiver*
> code, so the sender side is partly guesswork. Reports and captures are welcome.

## Findings

Tested against a Samsung TU690T (model `UTU7000_KA`, AirPlay SDK 3.3.0.54) that plays
video from the Airflow app:

- Pair-verify with stored HAP credentials succeeds on both connections.
- `GET /info` and `POST /feedback` work. `POST /command` returns `454 Session Not
  Found`, and `400` once an RTSP `SETUP` session exists, so session routes are served.
- `POST /reverse`, `/play`, `/action`, `/rate`, `/stop`, `/fp-setup` and
  `GET /playback-info` all return **`404 Not Found`**, both before and after `SETUP`.
  The HLS feature bit is advertised, but these routes are not served on this firmware
  in the way this package (and UxPlay) expect.
- While Airflow plays to the same TV it holds two TCP connections to it (the AirPlay
  port plus one more port, likely the event channel) and **no UDP media stream**. The
  TV in turn connects to an HTTP server run by Airflow on the sender and pulls the
  media from there. So the TV does fetch media over HTTP; how it is told where to
  fetch from is not yet known.

Open question: what request makes this receiver start pulling from the sender.

## Intended flow (as implemented, verified only against a mock)

1. **Control connection**: pair-verify with your stored HAP credentials (reusing
   pyatv's auth and encryption).
2. **Reverse connection**: a second authenticated connection upgraded with
   `POST /reverse` (`Upgrade: PTTH/1.0`). After the `101`, the receiver sends HTTP
   requests to the sender on this socket.
3. **Play**: `POST /play` with `Content-Location: mlhls://localhost/master.m3u8`.
4. **FCUP**: the receiver asks, over the reverse connection, for the master and media
   playlists. The sender acknowledges each request and returns the playlist with
   `POST /action` (`unhandledURLResponse`).
5. **Segments**: the receiver downloads segments straight from a small HTTP server run
   by this package, and plays. `GET /playback-info` reports progress; `/rate`,
   `/scrub` and `/stop` control playback.

For local files, `ffmpeg` remuxes to HLS with `-c copy` (no re-encoding), so the
source must be **H.264** video (AAC audio is passed through).

## Requirements

- Python 3.9+
- `ffmpeg` and `ffprobe` on your `PATH` (only when you pass a video file rather than
  an existing `.m3u8`)
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
pyatv-hls play movie.mp4 --id AA:BB:CC:DD:EE:FF
pyatv-hls play movie.mp4 --host 192.168.1.50 --start 600
pyatv-hls play existing/index.m3u8 --id AA:BB:CC:DD:EE:FF -v
```

`-v` logs every request and response exchanged with the receiver, which is what to
include in a bug report.

The machine running this must be reachable from the TV: the TV fetches segments over
HTTP from a port opened on this machine's LAN address for the duration of playback.

### As a library

```python
from pathlib import Path
from pyatv_hls import HlsPlayer, SegmentServer, prepare, playlist

media = await prepare(Path("movie.mp4"), Path("/tmp/hls"))
player = HlsPlayer(address, port, credentials)   # pyatv HapCredentials
await player.connect()

server = SegmentServer(media.directory, player.control.local_ip)
base_url = await server.start()
player.set_documents(
    master=playlist.build_master(media.bandwidth, media.codecs, media.resolution),
    media=playlist.rewrite_media(media.playlist.read_text(), base_url),
)
await player.play()          # blocks until the item ends
```

## Development

```sh
uv run --group dev pytest
```

The tests run an in-process mock receiver on loopback (no encryption, no hardware).

## Limitations

- H.264/AAC sources only (no transcoding).
- No FairPlay/DRM, subtitles, multiple audio tracks, or playlists.
- Receivers that require extra setup steps before `/play` (for example `fp-setup`) are
  not handled yet.
- Not working on the Samsung UTU7000_KA, see [Findings](#findings).

## Acknowledgements

Protocol details were derived from reading open-source receivers, chiefly
[UxPlay](https://github.com/FDH2/UxPlay), and from the
[pyatv](https://github.com/postlund/pyatv) codebase.
