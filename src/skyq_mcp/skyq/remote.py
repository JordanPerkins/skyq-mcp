"""Sky Q remote-control protocol (TCP port 49160).

Each key press is its own connection: the box sends a version string and a few short frames
that the client echoes; once it sends a frame of 24+ bytes the client sends key-down and
key-up packets and closes. Confirmed on an ES340 in Phase 0 (PLAN.md §3.1).

The box accepts keys faster than it acts on them (a 0 ms gap between digits is lost), so
presses to one box go through a lock with a configurable gap between them.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .errors import BoxUnreachable, ProtocolError

log = logging.getLogger(__name__)

REMOTE_PORT = 49160
DEFAULT_GAP_MS = 150  # measured minimum on ES340: 50 ms
VERSION_ECHO_LEN = 12
READY_FRAME_LEN = 24


def encode_key(code: int) -> tuple[bytes, bytes]:
    """Key-down and key-up packets for a key code (0-255)."""
    if not 0 <= code <= 255:
        raise ValueError("key code must be 0-255")
    hi, lo = 0xE0 + (code >> 4), code & 0x0F
    return bytes([4, 1, 0, 0, 0, 0, hi, lo]), bytes([4, 0, 0, 0, 0, 0, hi, lo])


async def send_key(host: str, code: int, *, port: int = REMOTE_PORT, timeout: float = 5.0) -> None:
    """Open a connection, complete the handshake, send one key press, close."""
    down, up = encode_key(code)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise BoxUnreachable(f"couldn't connect to the box at {host}:{port} ({e or 'timed out'})") from e
    try:
        await asyncio.wait_for(_handshake_and_send(reader, writer, down, up), timeout)
    except asyncio.TimeoutError as e:
        raise ProtocolError(f"the box at {host} didn't complete the remote handshake in {timeout}s") from e
    except (ConnectionError, OSError) as e:
        raise BoxUnreachable(f"connection to the box at {host} dropped during the handshake ({e})") from e
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _handshake_and_send(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                              down: bytes, up: bytes) -> None:
    echo_len = VERSION_ECHO_LEN
    while True:
        data = await reader.read(1024)
        if not data:
            raise ConnectionError("box closed the connection")
        if len(data) < READY_FRAME_LEN:
            writer.write(data[:echo_len])
            await writer.drain()
            echo_len = 1
        else:
            writer.write(down + up)
            await writer.drain()
            return


class RemoteClient:
    """Sends key presses to one box, one at a time, with a gap between presses."""

    def __init__(self, host: str, *, port: int = REMOTE_PORT, gap_ms: int = DEFAULT_GAP_MS,
                 timeout: float = 5.0, retries: int = 1):
        self.host = host
        self.port = port
        self.gap_ms = gap_ms
        self.timeout = timeout
        self.retries = retries
        self._lock = asyncio.Lock()
        self._last_sent = 0.0

    async def press(self, code: int) -> None:
        await self.press_many([code])

    async def press_many(self, codes: list[int], *, gap_ms: int | None = None) -> None:
        """Send keys in order. Holds the lock for the whole sequence so sequences don't interleave."""
        gap = (self.gap_ms if gap_ms is None else gap_ms) / 1000
        async with self._lock:
            for code in codes:
                wait = self._last_sent + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._send_with_retry(code)
                self._last_sent = time.monotonic()

    async def _send_with_retry(self, code: int) -> None:
        for attempt in range(self.retries + 1):
            try:
                await send_key(self.host, code, port=self.port, timeout=self.timeout)
                return
            except (BoxUnreachable, ProtocolError) as e:
                if attempt == self.retries:
                    raise
                log.debug("key %s to %s failed (%s); retrying", code, self.host, e)
                await asyncio.sleep(0.3)
