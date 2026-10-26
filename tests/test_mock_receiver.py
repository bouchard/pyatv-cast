"""End-to-end test against an in-process mock receiver (loopback, no encryption)."""

import asyncio
import plistlib

import pytest

from pyatv_hls import playlist, session
from pyatv_hls.session import HlsPlayer

MASTER = playlist.build_master(1_000_000, "avc1.640028,mp4a.40.2", "1280x720")
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\nseg0.ts\n#EXT-X-ENDLIST\n"


async def _read_message(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    body = await reader.readexactly(int(headers.get("Content-Length", 0)))
    return lines[0], headers, body


def _respond(writer, code=200, message="OK", body=b"", extra=""):
    writer.write(
        f"HTTP/1.1 {code} {message}\r\n{extra}Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


class MockReceiver:
    """Speaks just enough of the receiver side of the HLS flow."""

    def __init__(self):
        self.reverse = None
        self.session_id = None
        self.play_body = None
        self.fcup_responses = {}
        self.commands = []
        self.polls = 0
        self.playing = asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _fcup(self, url, request_id):
        reader, writer = self.reverse
        body = plistlib.dumps(
            {
                "sessionID": 1,
                "type": "unhandledURLRequest",
                "request": {
                    "FCUP_Response_URL": url,
                    "FCUP_Response_RequestID": request_id,
                    "FCUP_Response_ClientRef": 40030004,
                },
            },
            fmt=plistlib.FMT_XML,
        )
        writer.write(
            b"POST /event HTTP/1.1\r\nContent-Type: text/x-apple-plist+xml\r\n"
            + f"X-Apple-Session-ID: {self.session_id}\r\n".encode()
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        status, _, _ = await _read_message(reader)  # sender's 200 to the event
        assert " 200 " in status

    async def _handle(self, reader, writer):
        try:
            while True:
                line, headers, body = await _read_message(reader)
                method, path, _ = line.split(" ", 2)
                if path == "/reverse":
                    self.session_id = headers["X-Apple-Session-ID"]
                    assert headers["Upgrade"] == "PTTH/1.0"
                    _respond(writer, 101, "Switching Protocols",
                             extra="Connection: Upgrade\r\nUpgrade: PTTH/1.0\r\n")  # fmt: skip
                    self.reverse = (reader, writer)
                    # Park: _fcup() owns this stream from now on (we send requests)
                    await asyncio.Event().wait()
                elif path == "/play":
                    assert headers["X-Apple-Session-ID"] == self.session_id
                    self.play_body = plistlib.loads(body)
                    _respond(writer)
                    asyncio.ensure_future(self._fcup(playlist.MASTER_URL, 1))
                elif path == "/action":
                    params = plistlib.loads(body)["params"]
                    url = params["FCUP_Response_URL"]
                    self.fcup_responses[url] = params["FCUP_Response_Data"]
                    _respond(writer)
                    if url == playlist.MASTER_URL:
                        asyncio.ensure_future(self._fcup(playlist.MEDIA_URL, 2))
                    else:
                        self.playing.set()
                elif path == "/playback-info":
                    self.polls += 1
                    done = self.polls > 3
                    info = {"duration": -1.0 if done else 10.0,
                            "position": float(self.polls), "rate": 1.0}  # fmt: skip
                    _respond(writer, body=plistlib.dumps(info, fmt=plistlib.FMT_XML),
                             extra="Content-Type: text/x-apple-plist+xml\r\n")  # fmt: skip
                else:
                    self.commands.append(path)
                    _respond(writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass


async def _noop(connection):
    return None


@pytest.fixture
async def receiver():
    mock = MockReceiver()
    port = await mock.start()
    yield mock, port
    mock.server.close()


async def test_full_flow(receiver, monkeypatch):
    mock, port = receiver
    monkeypatch.setattr(session, "POLL_INTERVAL", 0.05)
    statuses = []

    player = HlsPlayer("127.0.0.1", port, secure=_noop, on_status=statuses.append)
    await player.connect()
    player.set_documents(MASTER, MEDIA)
    await asyncio.wait_for(player.play(), 10)
    await player.pause()
    await player.resume()
    await player.seek(42.5)
    await player.stop()
    await player.close()

    assert mock.play_body["Content-Location"] == playlist.MASTER_URL
    assert mock.fcup_responses[playlist.MASTER_URL] == MASTER.encode()
    assert mock.fcup_responses[playlist.MEDIA_URL] == MEDIA.encode()
    assert statuses and statuses[0]["duration"] == 10.0
    assert mock.commands == [
        "/rate?value=0.000000",
        "/rate?value=1.000000",
        "/scrub?position=42.500",
        "/stop",
    ]


async def test_unknown_document_fails(receiver, monkeypatch):
    mock, port = receiver
    player = HlsPlayer("127.0.0.1", port, secure=_noop)
    await player.connect()
    # Only a master is registered, so the media FCUP is answered with a 404
    player.set_documents(MASTER, MEDIA)
    del player._documents[playlist.MEDIA_URL]
    with pytest.raises(session.HlsPlaybackError, match="unknown document"):
        await asyncio.wait_for(player.play(), 10)
    await player.close()
