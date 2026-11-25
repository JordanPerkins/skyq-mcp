"""TV listings from Sky's online guide (the box itself doesn't report programme titles).

GET https://atlantis.epgsky.com/as/schedule/<YYYYMMDD>/<sid> with headers
x-skyott-territory (GB/DE/IT), x-skyott-provider: SKY, x-skyott-proposition: SKYQ.
One channel per request, about 7 days ahead. A "day" includes programmes that overlap it, so it
starts the previous evening; neighbouring days are fetched and merged by event id.
Verified 2026-11-26 (UK).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import http
from .errors import ProtocolError, SkyQError
from .rest import Channel, _text

EPG_BASE = "https://atlantis.epgsky.com/as/schedule"
MAX_SCHEDULE_BYTES = 1024 * 1024
CACHE_TTL = 1800.0
MISS_TTL = 600.0  # days the guide has no data for (past its range): don't ask again for a while
CACHE_SIZE = 2400  # channel-days: a 7-day search over ~260 channels with room to spare
DAYS_AHEAD = 7
TERRITORIES = {"GBR": "GB", "IRL": "GB", "DEU": "DE", "AUT": "DE", "ITA": "IT"}


class GuideUnavailable(SkyQError):
    pass


@dataclass(frozen=True)
class LocalTime:
    """The box's time zone: current UTC offset, and the next change (e.g. clocks going forward)."""

    offset: int = 0
    future_offset: int | None = None
    future_from: int | None = None

    def offset_at(self, ts: float) -> int:
        if self.future_offset is not None and self.future_from is not None and ts >= self.future_from:
            return self.future_offset
        return self.offset

    def tz_at(self, ts: float) -> timezone:
        return timezone(timedelta(seconds=self.offset_at(ts)))

    def iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz_at(ts)).isoformat(timespec="minutes")

    def today(self, now: float | None = None) -> date:
        now = time.time() if now is None else now
        return datetime.fromtimestamp(now, self.tz_at(now)).date()

    def day_bounds(self, day: date) -> tuple[float, float]:
        """UTC timestamps for local midnight at the start and end of day."""
        start_guess = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
        start = start_guess - self.offset_at(start_guess)
        end_guess = start + 86400
        return start, end_guess - (self.offset_at(end_guess) - self.offset_at(start))

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> LocalTime:
        def num(k):
            v = d.get(k)
            return int(v) if isinstance(v, (int, float)) else None
        return cls(offset=num("presentLocalTimeOffset") or 0,
                   future_offset=num("futureLocalTimeOffset"), future_from=num("futureTransitionUtc"))


@dataclass(frozen=True, slots=True)
class Programme:
    event_id: str
    title: str
    start: float  # UTC timestamp
    duration: int  # seconds
    synopsis: str | None
    season: int | None
    episode: int | None
    new: bool
    subtitles: bool
    audio_description: bool
    can_record: bool
    can_series_link: bool
    channel: Channel | None = None

    @property
    def end(self) -> float:
        return self.start + self.duration

    def on_at(self, ts: float) -> bool:
        return self.start <= ts < self.end

    @classmethod
    def from_api(cls, d: dict[str, Any], channel: Channel | None) -> Programme | None:
        st, dur, eid = d.get("st"), d.get("d"), d.get("eid")
        if not isinstance(st, (int, float)) or not isinstance(dur, (int, float)) or not eid:
            return None
        ints = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None  # noqa: E731
        return cls(event_id=_text(eid, 40) or "", title=_text(d.get("t"), 200) or "(untitled)",
                   start=float(st), duration=int(dur), synopsis=_text(d.get("sy"), 600),
                   season=ints(d.get("seasonnumber")), episode=ints(d.get("episodenumber")),
                   new=d.get("new") is True, subtitles=d.get("s") is True,
                   audio_description=d.get("ad") is True, can_record=d.get("canb") is True,
                   can_series_link=d.get("canl") is True, channel=channel)

    def to_dict(self, tz: LocalTime) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k not in ("start", "duration", "channel")}
        d.update(start=tz.iso(self.start), end=tz.iso(self.end), minutes=self.duration // 60)
        if self.channel:
            d["channel"] = {"number": self.channel.number, "name": self.channel.name}
        return d


class EpgClient:
    def __init__(self, territory: str = "GB", *, base_url: str = EPG_BASE, timeout: float = 10.0,
                 concurrency: int = 16):
        self.territory = territory
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        # Parsed programmes (without channel), not raw JSON: keeps a week of listings small.
        self._cache: OrderedDict[tuple[str, str], tuple[float, list[Programme]]] = OrderedDict()

    @property
    def headers(self) -> dict[str, str]:
        return {"x-skyott-territory": self.territory, "x-skyott-provider": "SKY",
                "x-skyott-proposition": "SKYQ"}

    async def _events(self, sid: str, day: date) -> list[Programme]:
        if not sid.isdigit():
            return []
        key = (sid, day.strftime("%Y%m%d"))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < (CACHE_TTL if hit[1] else MISS_TTL):
            self._cache.move_to_end(key)
            return hit[1]
        async with self._sem:
            try:
                status, body = await http.request("GET", f"{self.base_url}/{key[1]}/{sid}", timeout=self.timeout,
                                                  max_bytes=MAX_SCHEDULE_BYTES, headers=self.headers)
            except SkyQError as e:
                raise GuideUnavailable(f"Sky's TV guide couldn't be reached ({e}); this needs internet access") from e
        if status == 400:  # e.g. a day beyond the guide's range: remember there's nothing
            self._store(key, [])
            return []
        if status != 200:
            raise GuideUnavailable(f"Sky's TV guide returned HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as e:
            raise ProtocolError("unexpected response from Sky's TV guide") from e
        sched = data.get("schedule") if isinstance(data, dict) else None
        events = sched[0].get("events", []) if isinstance(sched, list) and sched and isinstance(sched[0], dict) else []
        parsed = [p for p in (Programme.from_api(e, None) for e in events if isinstance(e, dict)) if p]
        self._store(key, parsed)
        return parsed

    def _store(self, key: tuple[str, str], events: list[Programme]) -> None:
        self._cache[key] = (time.monotonic(), events)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def programmes(self, channel: Channel, start: float, end: float) -> list[Programme]:
        """Programmes on channel overlapping [start, end) (UTC timestamps)."""
        # A guide day also lists programmes that started the evening before, so the days
        # containing start..end (UTC) are enough.
        first = datetime.fromtimestamp(start, timezone.utc).date()
        last = datetime.fromtimestamp(max(start, end - 1), timezone.utc).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        results = await asyncio.gather(*(self._events(channel.sid, d) for d in days), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors and len(errors) == len(results):
            raise errors[0]
        seen: dict[str, Programme] = {}
        for events in results:
            if isinstance(events, BaseException):
                continue
            for p in events:
                if p.end > start and p.start < end:
                    seen[p.event_id] = replace(p, channel=channel)
        return sorted(seen.values(), key=lambda p: p.start)

    async def now_next(self, channel: Channel, at: float | None = None) -> tuple[Programme | None, Programme | None]:
        at = time.time() if at is None else at
        progs = await self.programmes(channel, at - 6 * 3600, at + 6 * 3600)
        now = next((p for p in progs if p.on_at(at)), None)
        later = [p for p in progs if p.start >= (now.end if now else at)]
        return now, (later[0] if later else None)

    async def search(self, query: str, channels: list[Channel], start: float, end: float,
                     *, limit: int = 50) -> list[Programme]:
        q = " ".join(query.lower().split())
        if not q:
            raise SkyQError("empty search")
        results = await asyncio.gather(*(self.programmes(c, start, end) for c in channels), return_exceptions=True)
        if results and all(isinstance(r, BaseException) for r in results):
            raise results[0]
        found = [p for r in results if not isinstance(r, BaseException) for p in r
                 if q in p.title.lower() and p.end > start]
        found.sort(key=lambda p: (p.start, p.channel.sort_key() if p.channel else (0, 0)))
        return found[:limit]


def territory_for(country_code: str | None) -> str:
    return TERRITORIES.get((country_code or "").upper(), "GB")


def search_channels(channels: list[Channel], *, include_plus_one: bool = False, limit: int = 300) -> list[Channel]:
    """A sensible default set to search: TV channels, HD versions preferred over SD duplicates,
    no radio, no +1 channels unless asked."""
    tv = [c for c in channels if not c.radio and (include_plus_one or not c.timeshifted)]
    def base(name: str) -> str:
        n = name.lower().replace(" hd", "").replace(" uhd", "").strip()
        return n
    best: dict[str, Channel] = {}
    for c in tv:
        b = base(c.name)
        keep = best.get(b)
        rank = {"uhd": 0, "hd": 1}.get((c.quality or "").lower(), 2)
        if keep is None or rank < {"uhd": 0, "hd": 1}.get((keep.quality or "").lower(), 2):
            best[b] = c
    return sorted(best.values(), key=Channel.sort_key)[:limit]


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_day(value: str | None, today: date) -> date:
    """'today', 'tomorrow', a weekday ('saturday' = the next one, today included), or YYYY-MM-DD,
    within the guide's range."""
    v = (value or "today").strip().lower()
    if v in ("today", "tonight", "now"):
        d = today
    elif v == "tomorrow":
        d = today + timedelta(days=1)
    elif v[:3] in [w[:3] for w in _WEEKDAYS] and v.isalpha():
        target = [w[:3] for w in _WEEKDAYS].index(v[:3])
        d = today + timedelta(days=(target - today.weekday()) % 7)
    else:
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise SkyQError(f"couldn't understand the day '{value}': use today, tomorrow, a weekday "
                            "or YYYY-MM-DD") from None
    if not today - timedelta(days=1) <= d <= today + timedelta(days=DAYS_AHEAD - 1):
        raise SkyQError(f"{d.isoformat()} is outside the TV guide's range (yesterday to {DAYS_AHEAD - 1} days ahead)")
    return d


def parse_time(value: str | None, day: date, tz: LocalTime) -> float | None:
    """'21:00' / '9pm' on day, in the box's local time -> UTC timestamp. None for 'now'/empty."""
    if not value or value.strip().lower() == "now":
        return None
    v = value.strip().lower().replace(".", ":")
    pm = v.endswith("pm")
    am = v.endswith("am")
    v = v.removesuffix("pm").removesuffix("am").strip()
    try:
        hh, _, mm = v.partition(":")
        h, m = int(hh), int(mm or 0)
    except ValueError:
        raise SkyQError(f"couldn't understand the time '{value}': use e.g. 21:00 or 9pm") from None
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= m < 60):
        raise SkyQError(f"'{value}' isn't a valid time")
    start, _ = tz.day_bounds(day)
    return start + h * 3600 + m * 60
