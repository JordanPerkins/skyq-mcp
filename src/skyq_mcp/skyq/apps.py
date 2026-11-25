"""Apps on the box: list, launch, close, and which one is on screen.

Found on an ES340 (Q370 firmware), 2026-11-26:
- GET  /as/apps                           -> installed apps (id, title, blocked, PIN lock, tray)
- POST /as/apps/action/launch?appId=<id>  -> opens the app (HTTP 200, empty body)
- POST /as/apps/action/close?appId=<id>   -> closes it (HTTP 200, empty body)
- WebSocket /as/apps/status               -> every app's state; the one on screen is "VISIBLE"
  (the normal TV interface is com.bskyb.epgui)
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode

from . import http
from .errors import BoxUnreachable, ProtocolError, SkyQError
from .rest import _text

TV_UI_APP = "com.bskyb.epgui"
APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_APPS_BYTES = 2 * 1024 * 1024
MAX_STATUS_BYTES = 1024 * 1024

# System components the box lists as "apps" but that aren't things to open.
_INTERNAL_PREFIXES = (
    "com.bskyb.metric", "com.bskyb.broadcastmetrics", "com.bskyb.localstore", "com.bskyb.ca-bundle",
    "com.bskyb.dsu", "com.bskyb.launchpad", TV_UI_APP,
)


class AppNotFound(SkyQError):
    pass


class AmbiguousApp(SkyQError):
    def __init__(self, query: str, candidates: list[str]):
        super().__init__(f"'{query}' matches several apps: {', '.join(candidates[:10])}. Use the app id.")
        self.query, self.candidates = query, candidates


@dataclass(frozen=True)
class App:
    app_id: str
    title: str
    blocked: bool
    pin_locked: bool
    on_apps_tray: bool

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> App | None:
        app_id = str(d.get("appId") or "")
        if not APP_ID_RE.match(app_id):
            return None
        return cls(app_id=app_id, title=_text(d.get("title"), 80) or app_id,
                   blocked=bool(d.get("blocked")), pin_locked=bool(d.get("pinLocked")),
                   on_apps_tray=bool(d.get("visibleOnTray")))

    @property
    def internal(self) -> bool:
        """System components (metrics, the TV interface itself, ...), not apps to open."""
        if self.app_id.startswith(_INTERNAL_PREFIXES):
            return True
        # Titles that are just package names ("com.bskyb.something", "@scope/pkg").
        return bool(re.match(r"^(@|[a-z0-9_-]+\.[a-z0-9_.-]+$)", self.title))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower())


class AppList:
    def __init__(self, apps: list[App]):
        self.apps = apps
        self._by_id = {a.app_id: a for a in apps}

    def by_id(self, app_id: str) -> App | None:
        return self._by_id.get(app_id)

    def visible(self, *, include_internal: bool = False) -> list[App]:
        return [a for a in self.apps if include_internal or not a.internal]

    def find(self, query: str) -> App:
        """By id, or by name: exact title, then title starting with the query, then containing it.
        Several apps with the same title (the box has two called "Sky Sports"): the one on the
        Apps tray wins if exactly one is; otherwise the caller must use the id."""
        q = query.strip()
        if q in self._by_id and not self._by_id[q].internal:
            return self._by_id[q]
        nq = _norm(q)
        if not nq:
            raise AppNotFound("empty app name")
        pool = self.visible()
        for match in (lambda a: _norm(a.title) == nq,
                      lambda a: _norm(a.title).startswith(nq),
                      lambda a: nq in _norm(a.title)):
            found = [a for a in pool if match(a)]
            if len(found) == 1:
                return found[0]
            if found:
                on_tray = [a for a in found if a.on_apps_tray]
                if len(on_tray) == 1:
                    return on_tray[0]
                raise AmbiguousApp(q, [f"{a.title} ({a.app_id})" for a in found])
        raise AppNotFound(f"no app called '{q}' on this box; use list_apps to see what's installed")


class AppsClient:
    def __init__(self, host: str, *, port: int, status_port: int | None = None, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.status_port = status_port or port
        self.timeout = timeout

    async def apps(self) -> AppList:
        status, body = await http.request("GET", f"http://{self.host}:{self.port}/as/apps",
                                          timeout=self.timeout, max_bytes=MAX_APPS_BYTES)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise ProtocolError("unexpected non-JSON response from /as/apps") from e
        if status != 200 or not isinstance(data, dict):
            raise ProtocolError(f"/as/apps returned HTTP {status}")
        apps = [a for a in (App.from_api(d) for d in data.get("apps", []) if isinstance(d, dict)) if a]
        return AppList(apps)

    async def _action(self, action: str, app_id: str) -> None:
        if not APP_ID_RE.match(app_id):
            raise SkyQError(f"invalid app id '{app_id[:60]}'")
        url = f"http://{self.host}:{self.port}/as/apps/action/{action}?{urlencode({'appId': app_id})}"
        status, body = await http.request("POST", url, timeout=self.timeout)
        if status != 200:
            msg = ""
            try:
                msg = str(json.loads(body).get("userMessage", ""))[:120]
            except (ValueError, AttributeError):
                pass
            raise SkyQError(f"the box refused to {action} {app_id} (HTTP {status}{': ' + msg if msg else ''})")

    async def launch(self, app_id: str) -> None:
        await self._action("launch", app_id)

    async def close(self, app_id: str) -> None:
        await self._action("close", app_id)

    def watch(self) -> AppStatusWatcher:
        return AppStatusWatcher(self.host, self.status_port, timeout=self.timeout)


def visible_from_message(message: str | bytes) -> list[str] | None:
    try:
        data = json.loads(message)
    except ValueError:
        return None
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return None
    return [str(a.get("appId")) for a in apps
            if isinstance(a, dict) and a.get("status") == "VISIBLE" and APP_ID_RE.match(str(a.get("appId")))]


class AppStatusWatcher:
    """Connection to the box's app-status WebSocket. Open it before launching/closing so no
    update is missed, then wait for the state you expect."""

    def __init__(self, host: str, port: int, *, timeout: float):
        self.url = f"ws://{host}:{port}/as/apps/status"
        self.timeout = timeout
        self._cm = None
        self._ws = None
        self.visible: list[str] = []

    async def __aenter__(self) -> AppStatusWatcher:
        from websockets.asyncio.client import connect

        try:
            self._cm = connect(self.url, open_timeout=self.timeout, close_timeout=1,
                               max_size=MAX_STATUS_BYTES, compression=None)
            self._ws = await self._cm.__aenter__()
            self.visible = await self._next(self.timeout) or []
        except (OSError, asyncio.TimeoutError) as e:
            raise BoxUnreachable(f"couldn't read app status from the box ({type(e).__name__})") from e
        except Exception as e:  # websockets protocol errors
            raise ProtocolError(f"couldn't read app status from the box ({type(e).__name__})") from e
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(*exc)
            except Exception:  # noqa: BLE001 - closing a dead socket isn't worth reporting
                pass

    async def _next(self, timeout: float) -> list[str] | None:
        while True:
            msg = await asyncio.wait_for(self._ws.recv(), timeout)
            v = visible_from_message(msg)
            if v is not None:
                return v

    async def wait_until(self, predicate, timeout: float) -> bool:
        """Wait for an update where predicate(visible_app_ids) is true."""
        if predicate(self.visible):
            return True
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while (left := end - loop.time()) > 0:
            try:
                v = await self._next(left)
            except asyncio.TimeoutError:
                return False
            except Exception:  # noqa: BLE001 - lost the socket: can't confirm
                return False
            self.visible = v
            if predicate(v):
                return True
        return False


def foreground_app(visible: list[str]) -> str | None:
    """The app on screen, ignoring the TV interface that sits underneath apps."""
    others = [a for a in visible if a != TV_UI_APP]
    return others[0] if others else None
