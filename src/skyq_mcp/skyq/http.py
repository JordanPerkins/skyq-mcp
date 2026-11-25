"""HTTP to the box, with limits: anything on the LAN can pretend to be a Sky Q box, so
responses are size-capped (a huge reply can't exhaust a Raspberry Pi's memory) and redirects
are never followed (a reply can't send the server off to another address)."""

from __future__ import annotations

import httpx

from .errors import BoxUnreachable, ProtocolError

DEFAULT_MAX_BYTES = 256 * 1024


async def request(method: str, url: str, *, timeout: float, max_bytes: int = DEFAULT_MAX_BYTES,
                  headers: dict[str, str] | None = None, content: bytes | None = None) -> tuple[int, bytes]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(method, url, headers=headers, content=content) as resp:
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ProtocolError(f"response from {url} is too large ({declared} bytes)")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ProtocolError(f"response from {url} is larger than {max_bytes} bytes")
                return resp.status_code, bytes(body)
    except httpx.HTTPError as e:
        raise BoxUnreachable(f"couldn't reach {url} ({type(e).__name__})") from e
