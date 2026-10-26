"""DLNA / UPnP AV (MediaRenderer) playback, an alternative to AirPlay.

The renderer is controlled with SOAP calls to its AVTransport service
(``SetAVTransportURI``, ``Play``, ``Pause``, ``Seek``, ``Stop``) and then fetches the
media itself over HTTP from this machine, exactly like the AirPlay path. It is
unauthenticated, so nothing needs to be paired.
"""

import asyncio
import logging
import mimetypes
import socket
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SSDP_GROUP = ("239.255.255.250", 1900)
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
POLL_INTERVAL = 2.0
POLL_FAILURES = 5
PLAY_WAIT = 15.0

_DEVICE_NS = {"d": "urn:schemas-upnp-org:device-1-0"}
_ENDED_STATES = ("STOPPED", "NO_MEDIA_PRESENT")
_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class DlnaError(Exception):
    """Raised when the renderer rejects a request or cannot be found."""


class Renderer:
    """A discovered MediaRenderer."""

    def __init__(self, location: str, friendly_name: str, udn: str, control_url: str):
        """Initialize a new Renderer."""
        self.location = location
        self.friendly_name = friendly_name
        self.udn = udn
        self.control_url = control_url

    @property
    def host(self) -> str:
        """Address of the renderer."""
        return urlparse(self.location).hostname or ""


def parse_description(location: str, xml_text: str) -> Renderer:
    """Parse a UPnP device description and find the AVTransport control URL."""
    root = ET.fromstring(xml_text)
    device = root.find("d:device", _DEVICE_NS)
    if device is None:
        raise DlnaError(f"{location}: not a UPnP device description")
    base = root.findtext("d:URLBase", default=location, namespaces=_DEVICE_NS)
    control = None
    for service in root.iter("{%s}service" % _DEVICE_NS["d"]):
        if service.findtext("d:serviceType", namespaces=_DEVICE_NS) == AV_TRANSPORT:
            control = urljoin(base, service.findtext("d:controlURL", namespaces=_DEVICE_NS))
    if control is None:
        raise DlnaError(f"{location}: device has no AVTransport service")
    return Renderer(
        location,
        device.findtext("d:friendlyName", default="", namespaces=_DEVICE_NS),
        device.findtext("d:UDN", default="", namespaces=_DEVICE_NS),
        control,
    )


class _Search(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.locations: List[str] = []

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        for line in data.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()
                if location not in self.locations:
                    self.locations.append(location)


async def ssdp_search(
    timeout: float = 3.0, target: Tuple[str, int] = SSDP_GROUP
) -> List[str]:
    """Return the description URLs of every MediaRenderer that answers an M-SEARCH."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _Search, family=socket.AF_INET, local_addr=("0.0.0.0", 0)
    )
    try:
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_GROUP[0]}:{SSDP_GROUP[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {max(1, int(timeout))}\r\n"
            f"ST: {MEDIA_RENDERER}\r\n\r\n"
        )
        transport.sendto(message.encode(), target)
        await asyncio.sleep(timeout)
        return protocol.locations
    finally:
        transport.close()


async def _fetch(http: aiohttp.ClientSession, location: str) -> Optional[Renderer]:
    try:
        async with http.get(location, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return parse_description(location, await resp.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, DlnaError) as ex:
        _LOGGER.debug("ignoring %s: %s", location, ex)
        return None


async def find_renderer(
    host: Optional[str] = None,
    name: Optional[str] = None,
    location: Optional[str] = None,
    timeout: float = 3.0,
    target: Tuple[str, int] = SSDP_GROUP,
) -> Renderer:
    """Find one renderer by description URL, address, or (part of) its name or UDN."""
    locations = [location] if location else await ssdp_search(timeout, target)
    async with aiohttp.ClientSession() as http:
        found = [r for r in [await _fetch(http, loc) for loc in locations] if r]
    for renderer in found:
        if host and renderer.host == host:
            return renderer
        if name and name.lower() in (renderer.friendly_name + renderer.udn).lower():
            return renderer
        if location:
            return renderer
    seen = ", ".join(f"{r.friendly_name} ({r.host})" for r in found) or "none"
    raise DlnaError(f"no matching DLNA renderer found; saw: {seen}")


def didl(url: str, title: str, mime: str) -> str:
    """Build the DIDL-Lite metadata describing the item."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:{_FLAGS}">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def clock(seconds: float) -> str:
    """Format seconds as the ``H:MM:SS`` used by Seek."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class DlnaPlayer:
    """Plays a URL on a DLNA MediaRenderer. Same interface as :class:`HlsPlayer`."""

    def __init__(self, renderer: Renderer, mime: str = "application/vnd.apple.mpegurl"):
        """Initialize a new DlnaPlayer."""
        self.renderer = renderer
        self.mime = mime
        self.ended = asyncio.Event()
        self._http: Optional[aiohttp.ClientSession] = None
        self._poll: Optional["asyncio.Task[None]"] = None
        self.local_ip = ""

    async def connect(self) -> None:
        """Open the HTTP session and work out which local address reaches the renderer."""
        self._http = aiohttp.ClientSession()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((self.renderer.host, 9))  # no packet is sent
            self.local_ip = probe.getsockname()[0]
        finally:
            probe.close()

    async def close(self) -> None:
        """Stop polling and close the HTTP session."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        if self._http:
            await self._http.close()
            self._http = None

    async def play(self, url: str, title: str = "", start: float = 0.0) -> None:
        """Load ``url`` on the renderer and start playing, optionally from ``start``."""
        self.ended.clear()
        await self._action(
            "SetAVTransportURI",
            CurrentURI=escape(url),
            CurrentURIMetaData=escape(didl(url, title, self.mime)),
        )
        await self._action("Play", Speed="1")
        self._poll = asyncio.ensure_future(self._poll_state())
        if start > 0:
            await self._wait_for("PLAYING", PLAY_WAIT)
            await self._action("Seek", Unit="REL_TIME", Target=clock(start))

    async def pause(self) -> None:
        """Pause playback."""
        await self._action("Pause")

    async def resume(self) -> None:
        """Resume playback."""
        await self._action("Play", Speed="1")

    async def wait_until_ended(self) -> None:
        """Block until playback has stopped."""
        await self.ended.wait()

    async def stop(self) -> None:
        """Stop playback."""
        if self._http is None:
            return
        try:
            await self._action("Stop")
        except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
            _LOGGER.debug("renderer did not acknowledge Stop")

    # -- internals --------------------------------------------------------

    async def _action(self, name: str, **arguments: str) -> Dict[str, str]:
        assert self._http is not None
        args = "".join(f"<{k}>{v}</{k}>" for k, v in {"InstanceID": "0", **arguments}.items())
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{name} xmlns:u="{AV_TRANSPORT}">{args}</u:{name}>'
            "</s:Body></s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{AV_TRANSPORT}#{name}"',
        }
        async with self._http.post(
            self.renderer.control_url, data=body.encode(), headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:  # fmt: skip
            text = await resp.text()
            if resp.status != 200:
                raise DlnaError(f"{name} failed: HTTP {resp.status} {_upnp_error(text)}")
        reply = ET.fromstring(text).find(".//{%s}%sResponse" % (AV_TRANSPORT, name))
        return {child.tag.split("}")[-1]: child.text or "" for child in (reply or [])}

    async def _state(self) -> str:
        info = await self._action("GetTransportInfo")
        return info.get("CurrentTransportState", "")

    async def _wait_for(self, state: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while await self._state() != state:
            if asyncio.get_running_loop().time() > deadline:
                raise DlnaError(f"renderer never reached {state}")
            await asyncio.sleep(POLL_INTERVAL / 4)

    async def _poll_state(self) -> None:
        """Mark playback ended once the renderer stops after having played."""
        played = False
        failures = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                state = await self._state()
                failures = 0
            except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
                failures += 1
                if failures >= POLL_FAILURES:
                    _LOGGER.debug("renderer stopped answering")
                    self.ended.set()
                    return
                continue
            played = played or state in ("PLAYING", "TRANSITIONING")
            if played and state in _ENDED_STATES:
                self.ended.set()
                return


def _upnp_error(text: str) -> str:
    try:
        root = ET.fromstring(text)
        code = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
        desc = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorDescription")
        return f"UPnP error {code}: {desc}" if code else ""
    except ET.ParseError:
        return ""


def guess_mime(path: Path) -> str:
    """Best-effort media type for a video file."""
    return mimetypes.guess_type(path.name)[0] or "video/mp4"


class DirectFileServer:
    """Serve one file unchanged, with Range support, for renderers that play it as is."""

    def __init__(self, path: Path, address: str, port: int = 0) -> None:
        """Initialize a new DirectFileServer."""
        self._path = path
        self._address = address
        self._port = port
        self._runner: Optional[web.AppRunner] = None

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self._path, headers={"transferMode.dlna.org": "Streaming"})

    async def start(self) -> str:
        """Start serving and return the file's URL."""
        app = web.Application()
        app.router.add_get("/media{ext:\\.[A-Za-z0-9]+}", self._serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/media{self._path.suffix or '.mp4'}"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
