"""Optional client IP allowlist, as ASGI middleware (runs before the token check).

The client address is the TCP peer, unless the peer is a trusted proxy (e.g. Tailscale Funnel,
which connects from 127.0.0.1 and sets X-Forwarded-For to the real client). Then the address is
taken from X-Forwarded-For: the rightmost entry that isn't itself a trusted proxy. The header is
never trusted from anyone else, so a client can't fake its address by sending it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
REJECT_LOG_INTERVAL = 60.0


def parse_networks(values: list[str]) -> list[Network]:
    """'160.79.104.0/21', '192.168.1.10' (a single address) -> networks. Raises ValueError."""
    out = []
    for v in values:
        v = v.strip()
        if not v:
            continue
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            raise ValueError(f"'{v}' isn't an IP address or network like 192.168.1.0/24") from None
    return out


def _addr(value: str) -> Address | None:
    try:
        a = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        return a.ipv4_mapped
    return a


def _in(addr: Address, nets: list[Network]) -> bool:
    return any(addr.version == n.version and addr in n for n in nets)


def client_address(scope, trusted_proxies: list[Network]) -> Address | None:
    peer = _addr((scope.get("client") or ("", 0))[0] or "")
    if peer is None or not _in(peer, trusted_proxies):
        return peer
    forwarded = [v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"x-forwarded-for"]
    hops = [h for line in forwarded for h in line.split(",") if h.strip()]
    for hop in reversed(hops):
        addr = _addr(hop)
        if addr is None:
            return None  # malformed: refuse rather than guess
        if not _in(addr, trusted_proxies):
            return addr
    return peer  # only proxies in the chain (e.g. a local request with no header)


class ClientIPFilter:
    def __init__(self, app, allowed: list[Network], trusted_proxies: list[Network] | None = None):
        if not allowed:
            raise ValueError("an allowlist needs at least one network")
        self.app = app
        self.allowed = allowed
        self.trusted_proxies = trusted_proxies or []
        self._rejected = 0
        self._last_log = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        addr = client_address(scope, self.trusted_proxies)
        if addr is not None and _in(addr, self.allowed):
            await self.app(scope, receive, send)
            return
        self._note(addr)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": "forbidden"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    def _note(self, addr: Address | None) -> None:
        self._rejected += 1
        now = time.monotonic()
        if now - self._last_log >= REJECT_LOG_INTERVAL:
            log.warning("refused %d request(s) from addresses not on the allowlist (latest: %s)",
                        self._rejected, addr or "unknown")
            self._rejected = 0
            self._last_log = now
