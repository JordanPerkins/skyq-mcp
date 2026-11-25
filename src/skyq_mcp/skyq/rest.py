"""Sky Q REST API on port 9006 (box info, channel list, recording details).

`/as/system/information` also returns sensitive data (household token, viewing card number,
serial number, MAC/IP addresses). Only the fields listed in SystemInfo are ever read from it.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import json

from . import http
from .errors import AmbiguousChannel, ChannelNotFound, ProtocolError

REST_PORT = 9006
MAX_SERVICES_BYTES = 4 * 1024 * 1024  # ~400 channels is ~100 KB
HEX_ID_RE = re.compile(r"^[0-9A-Fa-f]{1,16}$")


def _text(value: Any, limit: int) -> str | None:
    """Text supplied by the box (it reaches the AI client): plain string, length-capped."""
    if value is None:
        return None
    s = " ".join(str(value).split())
    return s[:limit] + ("…" if len(s) > limit else "")


@dataclass(frozen=True)
class SystemInfo:
    model: str | None
    hardware_name: str | None
    hardware_version: str | None
    software_version: str | None
    device_type: str | None
    standby: bool
    uhd_capable: bool | None
    hdr_capable: bool | None
    uptime_seconds: int | None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> SystemInfo:
        # Whitelist: never copy other fields (householdToken, serialNumber, viewingCardNumber, …).
        return cls(
            model=_text(d.get("hardwareModel"), 40),
            hardware_name=_text(d.get("hardwareName"), 40),
            hardware_version=_text(d.get("versionNumber"), 40),
            software_version=_text(d.get("ASVersion"), 60),
            device_type=_text(d.get("deviceType"), 40),
            standby=bool(d.get("activeStandby", False)),
            uhd_capable=d.get("uhdCapable"),
            hdr_capable=d.get("hdrCapable"),
            uptime_seconds=d.get("systemUptime"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Channel:
    number: str  # string: radio channels have a leading zero (e.g. "0111")
    name: str
    sid: str
    quality: str | None  # "sd" / "hd" / "uhd"
    timeshifted: bool
    radio: bool

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Channel:
        number = str(d.get("c", ""))
        service_type = str(d.get("servicetype", "")).upper()
        return cls(
            number=number,
            name=_text(d.get("t", ""), 120) or "",
            sid=str(d.get("sid", d.get("sk", ""))),
            quality=_text(d.get("sf"), 10),
            timeshifted=bool(d.get("timeshifted", False)),
            radio=number.startswith("0") or "RADIO" in service_type,
        )

    def sort_key(self) -> tuple[int, int]:
        return (1 if self.radio else 0, int(self.number) if self.number.isdigit() else 10**6)

    def label(self) -> str:
        return f"{self.number} {self.name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower())


class ChannelList:
    def __init__(self, channels: list[Channel]):
        self.channels = sorted(channels, key=Channel.sort_key)
        self._by_number = {c.number: c for c in self.channels}
        self._by_sid = {c.sid: c for c in self.channels}

    def __len__(self) -> int:
        return len(self.channels)

    def by_number(self, number: str | int) -> Channel | None:
        return self._by_number.get(str(number))

    def by_sid(self, sid: str | int) -> Channel | None:
        return self._by_sid.get(str(sid))

    def find(self, query: str | int) -> Channel:
        """Find a channel by number or name.

        Names: an exact match wins; otherwise the lowest-numbered channel whose name starts with
        the query (so "BBC One" gives your regional BBC One on 101); otherwise a single channel
        whose name contains the query. "+1" channels only match if the query mentions "+1".
        """
        q = str(query).strip()
        if q.isdigit():
            ch = self.by_number(q)
            if ch is None:
                raise ChannelNotFound(f"there's no channel {q} on this box")
            return ch
        nq = _norm(q)
        if not nq:
            raise ChannelNotFound("empty channel name")
        pool = self.channels if "+1" in nq else [c for c in self.channels if not c.timeshifted]
        exact = [c for c in pool if _norm(c.name) == nq]
        if exact:
            return exact[0]
        prefix = [c for c in pool if _norm(c.name).startswith(nq)]
        if prefix:
            return prefix[0]
        contains = [c for c in pool if nq in _norm(c.name)]
        if len(contains) == 1:
            return contains[0]
        if contains:
            raise AmbiguousChannel(q, [c.label() for c in contains])
        raise ChannelNotFound(f"no channel name matches '{q}'")

    def search(self, query: str | None = None, *, include_radio: bool = True) -> list[Channel]:
        nq = _norm(query) if query else ""
        return [c for c in self.channels
                if (include_radio or not c.radio) and (not nq or nq in _norm(c.name) or c.number == query)]


@dataclass(frozen=True)
class Recording:
    pvr_id: str
    title: str | None
    channel_number: str | None
    channel_name: str | None
    synopsis: str | None
    start: str | None  # ISO 8601 UTC
    status: str | None
    season: int | None
    episode: int | None

    @classmethod
    def from_api(cls, pvr_id: str, d: dict[str, Any]) -> Recording:
        d = d.get("details", d)
        st = d.get("st")
        start = datetime.fromtimestamp(st, tz=timezone.utc).isoformat() if isinstance(st, (int, float)) else None
        return cls(
            pvr_id=pvr_id,
            title=_text(d.get("t"), 200),
            channel_number=_text(d.get("c"), 8),
            channel_name=_text(d.get("cn"), 120),
            synopsis=_text(d.get("sy"), 1000),
            start=start,
            status=_text(d.get("status"), 30),
            season=d.get("seasonnumber") if isinstance(d.get("seasonnumber"), int) else None,
            episode=d.get("episodenumber") if isinstance(d.get("episodenumber"), int) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RestClient:
    def __init__(self, host: str, *, port: int = REST_PORT, timeout: float = 5.0,
                 channel_ttl: float = 3600.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.channel_ttl = channel_ttl
        self._channels: ChannelList | None = None
        self._channels_at = 0.0

    async def _get(self, path: str, *, max_bytes: int = http.DEFAULT_MAX_BYTES) -> tuple[int, Any]:
        url = f"http://{self.host}:{self.port}{path}"
        status, body = await http.request("GET", url, timeout=self.timeout, max_bytes=max_bytes)
        try:
            return status, json.loads(body)
        except ValueError as e:
            raise ProtocolError(f"unexpected non-JSON response from {path}") from e

    async def system_information(self) -> SystemInfo:
        status, data = await self._get("/as/system/information")
        if status != 200 or not isinstance(data, dict):
            raise ProtocolError(f"/as/system/information returned HTTP {status}")
        return SystemInfo.from_api(data)

    async def is_standby(self) -> bool:
        return (await self.system_information()).standby

    async def channels(self, *, refresh: bool = False) -> ChannelList:
        if (not refresh and self._channels is not None
                and time.monotonic() - self._channels_at < self.channel_ttl):
            return self._channels
        status, data = await self._get("/as/services", max_bytes=MAX_SERVICES_BYTES)
        if status != 200 or not isinstance(data, dict):
            raise ProtocolError(f"/as/services returned HTTP {status}")
        self._channels = ChannelList([Channel.from_api(s) for s in data.get("services", [])])
        self._channels_at = time.monotonic()
        return self._channels

    async def recording(self, pvr_hex_id: str) -> Recording | None:
        """Details of a recording, from the hex id in a `file://pvr/<id>` URI."""
        if not HEX_ID_RE.match(pvr_hex_id):  # comes from the box: never let it shape the URL
            return None
        pvr_id = "P" + pvr_hex_id.upper()  # the P prefix is required (no prefix -> HTTP 400)
        status, data = await self._get(f"/as/pvr/details/{pvr_id}")
        if status != 200 or not isinstance(data, dict):
            return None
        return Recording.from_api(pvr_id, data)
