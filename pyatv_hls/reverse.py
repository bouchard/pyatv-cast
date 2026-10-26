"""Reverse (PTTH) connection.

After ``POST /reverse`` with ``Upgrade: PTTH/1.0`` the receiver answers ``101`` and
the roles on that socket flip: the receiver sends HTTP *requests* (``POST /event``)
and the sender answers them. pyatv's ``HttpConnection`` only parses responses, so
this subclass switches parser once the upgrade has happened.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from pyatv.support.http import HttpConnection, HttpRequest, parse_request

_LOGGER = logging.getLogger(__name__)

RequestHandler = Callable[[HttpRequest], Awaitable[None]]


class ReverseConnection(HttpConnection):
    """HTTP connection that can be upgraded to a receiver-initiated channel."""

    def __init__(self) -> None:
        """Initialize a new ReverseConnection."""
        super().__init__()
        self.upgraded = False
        self.request_handler: Optional[RequestHandler] = None
        self._request_buffer = b""

    def mark_upgraded(self) -> None:
        """Switch to request parsing; keep any bytes already received."""
        self.upgraded = True
        self._request_buffer, self._buffer = self._buffer, b""

    def data_received(self, data: bytes) -> None:
        """Parse responses until upgraded, requests afterwards."""
        if not self.upgraded:
            super().data_received(data)
            return

        self._request_buffer += self.receive_processor(data)
        while self._request_buffer:
            request, self._request_buffer = parse_request(self._request_buffer)
            if request is None:
                break
            _LOGGER.debug("Reverse request: %s %s", request.method, request.path)
            if self.request_handler is not None:
                asyncio.ensure_future(self.request_handler(request))

    def send_response(
        self, code: int = 200, message: str = "OK", body: bytes = b""
    ) -> None:
        """Answer a request from the receiver."""
        head = f"HTTP/1.1 {code} {message}\r\nContent-Length: {len(body)}\r\n\r\n"
        if self.transport is None:
            raise RuntimeError("reverse connection is closed")
        self.transport.write(self.send_processor(head.encode() + body))


async def connect_reverse(address: str, port: int) -> ReverseConnection:
    """Open a TCP connection that can later be upgraded."""
    loop = asyncio.get_event_loop()
    _, connection = await loop.create_connection(ReverseConnection, address, port)
    return connection
