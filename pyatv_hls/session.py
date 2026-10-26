"""AirPlay 2 HLS playback session.

Flow (sender side), reconstructed from receiver implementations since Apple does not
document it:

1. Control connection: pair-verify (HAP, stored credentials) -> encrypted channel.
2. Reverse connection: pair-verify, then ``POST /reverse`` with ``Upgrade: PTTH/1.0``
   and the shared ``X-Apple-Session-ID``. Receiver answers ``101``.
3. Control connection: ``POST /play`` with ``Content-Location: mlhls://localhost/...``.
4. Receiver sends ``POST /event`` (type ``unhandledURLRequest``) on the reverse
   connection asking for the master and media playlists (FCUP). We acknowledge each
   and deliver the playlist with ``POST /action`` (type ``unhandledURLResponse``).
5. Receiver fetches segments straight from our HTTP server and starts playing.
   ``GET /playback-info`` reports progress; ``/rate``, ``/scrub`` and ``/stop``
   control it.
"""

import asyncio
import logging
import plistlib
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth.hap_pairing import HapCredentials
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.support.http import HttpConnection, HttpRequest, http_connect

from pyatv_hls import playlist
from pyatv_hls.reverse import ReverseConnection, connect_reverse

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "AirPlay/550.10"
POLL_INTERVAL = 1.0
START_TIMEOUT = 30.0

# Hook so tests can skip encryption against an in-process mock receiver
SecureFunc = Callable[[HttpConnection], Awaitable[None]]
StatusFunc = Callable[[Dict[str, Any]], None]


class HlsPlaybackError(exceptions.PlaybackError):
    """Raised when the receiver rejects or aborts HLS playback."""


class HlsPlayer:
    """Plays an HLS rendition on an AirPlay 2 receiver."""

    def __init__(
        self,
        address: str,
        port: int,
        credentials: Optional[HapCredentials] = None,
        secure: Optional[SecureFunc] = None,
        on_status: Optional[StatusFunc] = None,
    ) -> None:
        """Initialize a new HlsPlayer."""
        self._address = address
        self._port = port
        self._credentials = credentials
        self._secure = secure or self._verify
        self._on_status = on_status
        self.session_id = str(uuid4()).upper()
        self.playback_uuid = str(uuid4()).upper()
        self.control: Optional[HttpConnection] = None
        self.reverse: Optional[ReverseConnection] = None
        self._documents: Dict[str, bytes] = {}
        self._requested: Dict[str, int] = {}
        self._started = asyncio.Event()
        self._failed: Optional[str] = None

    async def _verify(self, connection: HttpConnection) -> None:
        if self._credentials is None:
            raise exceptions.InvalidCredentialsError("no AirPlay credentials")
        await verify_connection(self._credentials, connection)

    # -- connection setup -------------------------------------------------

    async def connect(self) -> None:
        """Open and authenticate the control and reverse connections."""
        self.control = await http_connect(self._address, self._port)
        await self._secure(self.control)

        self.reverse = await connect_reverse(self._address, self._port)
        await self._secure(self.reverse)
        self.reverse.request_handler = self._on_reverse_request

        resp = await self.reverse.post(
            "/reverse",
            headers={
                "Upgrade": "PTTH/1.0",
                "Connection": "Upgrade",
                "X-Apple-Purpose": "event",
                "X-Apple-Session-ID": self.session_id,
                "Content-Length": "0",
            },
            allow_error=True,
        )
        if resp.code != 101:
            raise HlsPlaybackError(f"/reverse refused: {resp.code} {resp.message}")
        self.reverse.mark_upgraded()

    async def close(self) -> None:
        """Close both connections."""
        for connection in (self.reverse, self.control):
            if connection:
                connection.close()
        self.reverse = self.control = None

    # -- playback ---------------------------------------------------------

    def set_documents(self, master: str, media: str) -> None:
        """Set the playlists served through FCUP."""
        self._documents = {
            playlist.MASTER_URL: master.encode(),
            playlist.MEDIA_URL: media.encode(),
        }

    async def play(self, start: float = 0.0, wait: bool = True) -> None:
        """Start playback; with ``wait`` block until the item finishes."""
        if not self._documents:
            raise HlsPlaybackError("set_documents() must be called before play()")
        assert self.control
        body = {
            "Content-Location": playlist.MASTER_URL,
            "Start-Position-Seconds": float(start),
            "uuid": self.playback_uuid,
            "rate": 1.0,
            "clientProcName": "pyatv-hls",
            "clientBundleID": "dev.pyatv.hls",
        }
        resp = await self.control.post(
            "/play",
            headers=self._headers(),
            body=plistlib.dumps(body, fmt=plistlib.FMT_BINARY),
            allow_error=True,
        )
        if resp.code >= 400:
            raise HlsPlaybackError(f"/play refused: {resp.code} {resp.message}")

        try:
            await asyncio.wait_for(self._started.wait(), START_TIMEOUT)
        except asyncio.TimeoutError as ex:
            raise HlsPlaybackError(
                "receiver never requested the playlists "
                f"(asked for: {sorted(self._requested)})"
            ) from ex
        if self._failed:
            raise HlsPlaybackError(self._failed)
        if wait:
            await self._monitor()

    async def _monitor(self) -> None:
        """Poll ``/playback-info`` until the item ends."""
        assert self.control
        seen_playing = False
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            resp = await self.control.get("/playback-info", allow_error=True)
            if resp.code >= 400 or not resp.body:
                if seen_playing:
                    return
                continue
            info = plistlib.loads(
                resp.body if isinstance(resp.body, bytes) else resp.body.encode()
            )
            if self._on_status:
                self._on_status(info)
            if info.get("duration", 0) < 0:
                return
            seen_playing = seen_playing or info.get("rate", 0) > 0

    async def pause(self) -> None:
        """Pause playback."""
        await self._command("/rate?value=0.000000")

    async def resume(self) -> None:
        """Resume playback."""
        await self._command("/rate?value=1.000000")

    async def seek(self, seconds: float) -> None:
        """Seek to ``seconds``."""
        await self._command(f"/scrub?position={seconds:.3f}")

    async def stop(self) -> None:
        """Stop playback."""
        await self._command("/stop")

    async def _command(self, path: str) -> None:
        assert self.control
        await self.control.post(path, headers=self._headers(), allow_error=True)

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-apple-binary-plist",
            "X-Apple-Session-ID": self.session_id,
        }

    # -- reverse channel (FCUP) -------------------------------------------

    async def _on_reverse_request(self, request: HttpRequest) -> None:
        assert self.reverse
        body = request.body.encode() if isinstance(request.body, str) else request.body
        try:
            event = plistlib.loads(body) if body else {}
        except Exception:  # noqa: BLE001 - malformed events must not kill the loop
            _LOGGER.warning("Unparseable reverse request %s", request.path)
            self.reverse.send_response(400, "Bad Request")
            return

        self.reverse.send_response(200, "OK")
        kind = event.get("type")
        _LOGGER.debug("Reverse event %s: %s", kind, event)

        if kind == "unhandledURLRequest":
            await self._answer_fcup(event.get("request", {}))
        elif kind == "playbackError" or kind == "error":
            self._failed = f"receiver reported {event}"
            self._started.set()

    async def _answer_fcup(self, request: Dict[str, Any]) -> None:
        assert self.control
        url = request.get("FCUP_Response_URL", "")
        request_id = request.get("FCUP_Response_RequestID", 0)
        self._requested[url] = request_id

        data = self._documents.get(url)
        params: Dict[str, Any] = {
            "FCUP_Response_URL": url,
            "FCUP_Response_RequestID": request_id,
            "FCUP_Response_StatusCode": 200 if data is not None else 404,
            "FCUP_Response_Data": data or b"",
        }
        action = {"type": "unhandledURLResponse", "params": params}
        resp = await self.control.post(
            "/action",
            headers=self._headers(),
            body=plistlib.dumps(action, fmt=plistlib.FMT_BINARY),
            allow_error=True,
        )
        if resp.code >= 400:
            self._failed = f"/action rejected: {resp.code} {resp.message}"
        if data is None:
            self._failed = f"receiver asked for unknown document {url}"
        if url == playlist.MEDIA_URL or self._failed:
            self._started.set()
