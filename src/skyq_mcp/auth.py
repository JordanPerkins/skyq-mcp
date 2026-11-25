"""Bearer-token (shared secret) authentication as ASGI middleware.

Every request except GET/HEAD /healthz must carry `Authorization: Bearer <token>`.
- Only HTTP and lifespan traffic is handled; anything else (e.g. WebSocket, which uvicorn
  enables whenever a WebSocket library is installed) is refused, never passed through.
- Tokens are compared as SHA-256 digests with hmac.compare_digest, so neither the content
  nor the length of a token leaks through timing. Tokens are never logged.
- Rejected attempts are logged at most once per interval, so a flood of bad requests
  (e.g. through Tailscale Funnel) can't fill the disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

log = logging.getLogger(__name__)

REALM = "skyq-mcp"
HEALTH_PATH = "/healthz"
REJECT_LOG_INTERVAL = 60.0


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


class BearerAuthMiddleware:
    def __init__(self, app, tokens: list[str]):
        if not tokens:
            raise ValueError("at least one token is required")
        self.app = app
        self._digests = [_digest(t.encode()) for t in tokens]
        self._rejected = 0
        self._last_reject_log = 0.0

    def _valid(self, presented: bytes) -> bool:
        d = _digest(presented)
        ok = False
        for t in self._digests:  # check all, so timing doesn't reveal which matched
            ok |= hmac.compare_digest(d, t)
        return ok

    def _note_rejection(self, scope) -> None:
        self._rejected += 1
        now = time.monotonic()
        if now - self._last_reject_log >= REJECT_LOG_INTERVAL:
            client = (scope.get("client") or ("?", 0))[0]
            log.warning("rejected %d request(s) with a missing or invalid token (latest from %s)",
                        self._rejected, client)
            self._rejected = 0
            self._last_reject_log = now

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            raise RuntimeError(f"unsupported ASGI scope type {kind!r}")

        method = scope.get("method")
        if scope.get("path") == HEALTH_PATH and method in ("GET", "HEAD"):
            await _json(send, 200, {"status": "ok"}, head=method == "HEAD")
            return
        header = next((v for k, v in scope.get("headers", []) if k == b"authorization"), None)
        scheme, _, presented = (header or b"").partition(b" ")
        presented = presented.strip()
        if header is None or scheme.lower() != b"bearer" or not presented or not self._valid(presented):
            self._note_rejection(scope)
            await _unauthorized(send, "invalid_token" if header is not None else None, head=method == "HEAD")
            return
        await self.app(scope, receive, send)


async def _json(send, status: int, body: dict, extra_headers: list[tuple[bytes, bytes]] | None = None,
                *, head: bool = False) -> None:
    data = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
               (b"cache-control", b"no-store")]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra_headers or [])})
    await send({"type": "http.response.body", "body": b"" if head else data})


async def _unauthorized(send, error: str | None, *, head: bool = False) -> None:
    challenge = f'Bearer realm="{REALM}"' + (f', error="{error}"' if error else "")
    await _json(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", challenge.encode())], head=head)
