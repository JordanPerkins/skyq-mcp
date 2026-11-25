"""High-level control of one Sky Q box, built on the remote, REST and UPnP clients."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .errors import ChannelChangeFailed, SkyQError
from .keys import KEYS, key_name, resolve_key
from .remote import DEFAULT_GAP_MS, REMOTE_PORT, RemoteClient
from .rest import HEX_ID_RE, REST_PORT, Channel, Recording, RestClient
from .upnp import UPNP_PORT, UpnpClient

log = logging.getLogger(__name__)


@dataclass
class Timing:
    """Delays measured in Phase 0 (PLAN.md §3.1); tests shrink them."""

    clear_wait: float = 1.0  # after `backup`, before typing digits
    retry_gap_ms: int = 400  # gap between digits on the second attempt
    verify_timeout: float = 5.0  # how long to wait for the box to land on the channel
    poll_interval: float = 0.5
    power_timeout: float = 20.0  # total time allowed for a power change
    power_repress_after: float = 6.0  # press again if nothing changed by then (see _set_power)
    power_max_presses: int = 3
    wake_settle: float = 3.0  # after waking, before typing digits


@dataclass(frozen=True)
class NowPlaying:
    source: Literal["live", "recording", "unknown"]
    state: str | None
    speed: str | None
    channel: Channel | None = None
    recording: Recording | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "state": self.state,
            "speed": self.speed,
            "channel": self.channel.to_dict() if self.channel else None,
            "recording": self.recording.to_dict() if self.recording else None,
        }


@dataclass(frozen=True)
class ChannelChange:
    channel: Channel
    verified: bool  # False if the box's UPnP couldn't be read to check
    attempts: int
    woke_box: bool
    already_on: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["channel"] = self.channel.to_dict()
        return d


@dataclass(frozen=True)
class PowerChange:
    requested: str
    was_standby: bool
    is_standby: bool
    pressed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"requested": self.requested, "was_on": not self.was_standby,
                "is_on": not self.is_standby, "pressed_power": self.pressed}


def parse_uri(uri: str | None) -> tuple[str, str] | None:
    """('live', sid) for xsi://<hex sid>; ('recording', hex id) for file://pvr/<hex id>."""
    if not uri:
        return None
    if uri.startswith("xsi://"):
        try:
            return "live", str(int(uri[6:], 16))
        except ValueError:
            return None
    if uri.startswith("file://pvr/"):
        pvr = uri[len("file://pvr/"):]
        return ("recording", pvr) if HEX_ID_RE.match(pvr) else None
    return None


class SkyQBox:
    def __init__(self, host: str, *, remote_port: int = REMOTE_PORT, rest_port: int = REST_PORT,
                 upnp_port: int = UPNP_PORT, key_gap_ms: int = DEFAULT_GAP_MS,
                 timing: Timing | None = None, allow_unsafe: bool = False):
        self.host = host
        self.timing = timing or Timing()
        self.allow_unsafe = allow_unsafe
        self.remote = RemoteClient(host, port=remote_port, gap_ms=key_gap_ms)
        self.rest = RestClient(host, port=rest_port)
        self.upnp = UpnpClient(host, port=upnp_port)
        self._channel_lock = asyncio.Lock()

    # -- keys ----------------------------------------------------------------------

    async def press(self, keys: list[str | int], *, repeat: int = 1, gap_ms: int | None = None) -> list[str]:
        """Press keys in order (the whole list `repeat` times). Returns the key names sent."""
        if not 1 <= repeat <= 50:
            raise SkyQError("repeat must be between 1 and 50")
        codes = [resolve_key(k, allow_unsafe=self.allow_unsafe) for k in keys] * repeat
        if len(codes) > 200:
            raise SkyQError("too many key presses in one request (max 200)")
        await self.remote.press_many(codes, gap_ms=gap_ms)
        return [key_name(c) for c in codes]

    # -- status --------------------------------------------------------------------

    async def info(self):
        return await self.rest.system_information()

    async def now_playing(self) -> NowPlaying:
        transport = await self.upnp.transport_info()
        media = await self.upnp.media_info()
        parsed = parse_uri(media.uri)
        if parsed and parsed[0] == "live":
            channels = await self.rest.channels()
            return NowPlaying("live", transport.state, transport.speed, channel=channels.by_sid(parsed[1]))
        if parsed and parsed[0] == "recording":
            return NowPlaying("recording", transport.state, transport.speed,
                              recording=await self.rest.recording(parsed[1]))
        return NowPlaying("unknown", transport.state, transport.speed)

    async def current_channel_number(self) -> str | None:
        media = await self.upnp.media_info()
        parsed = parse_uri(media.uri)
        if not parsed or parsed[0] != "live":
            return None
        ch = (await self.rest.channels()).by_sid(parsed[1])
        return ch.number if ch else None

    # -- channels ------------------------------------------------------------------

    async def change_channel(self, query: str | int) -> ChannelChange:
        """Tune by number or name: wake if needed, clear any half-typed number, type the digits,
        check the box got there, and retry once with a longer gap if it didn't.

        Clearing first matters: a stray digit left in the box's number entry shifts every later
        number by one digit (seen in Phase 0: asked for 102, got 110).
        """
        channel = (await self.rest.channels()).find(query)
        async with self._channel_lock:
            woke = False
            if await self.rest.is_standby():
                await self._set_power(want_standby=False)
                woke = True
                await asyncio.sleep(self.timing.wake_settle)

            can_verify = True
            try:
                if await self.current_channel_number() == channel.number:
                    return ChannelChange(channel, True, 0, woke, already_on=True)
            except SkyQError as e:
                log.info("can't read the current channel, so the change won't be checked: %s", e)
                can_verify = False

            digit_codes = [KEYS[d].code for d in channel.number]
            gaps = [self.remote.gap_ms, self.timing.retry_gap_ms]
            last_seen = None
            for attempt, gap in enumerate(gaps, start=1):
                await self.remote.press(KEYS["backup"].code)
                await asyncio.sleep(self.timing.clear_wait)
                await self.remote.press_many(digit_codes, gap_ms=gap)
                if not can_verify:
                    return ChannelChange(channel, False, attempt, woke)
                last_seen = await self._wait_for_channel(channel.number)
                if last_seen == channel.number:
                    return ChannelChange(channel, True, attempt, woke)
                log.info("asked for %s, box is on %s (attempt %d)", channel.number, last_seen, attempt)
            raise ChannelChangeFailed(
                f"sent {channel.label()} twice but the box is showing channel {last_seen or 'unknown'}"
            )

    async def _wait_for_channel(self, number: str) -> str | None:
        deadline = time.monotonic() + self.timing.verify_timeout
        seen = None
        while True:
            try:
                seen = await self.current_channel_number()
            except SkyQError:
                seen = None
            if seen == number or time.monotonic() >= deadline:
                return seen
            await asyncio.sleep(self.timing.poll_interval)

    # -- power ---------------------------------------------------------------------

    async def power(self, action: Literal["on", "off", "toggle"]) -> PowerChange:
        was_standby = await self.rest.is_standby()
        if action == "toggle":
            want_standby = not was_standby
        elif action in ("on", "off"):
            want_standby = action == "off"
        else:
            raise SkyQError(f"unknown power action '{action}'")
        if was_standby == want_standby:
            return PowerChange(action, was_standby, was_standby, pressed=False)
        is_standby = await self._set_power(want_standby=want_standby)
        return PowerChange(action, was_standby, is_standby, pressed=True)

    async def _set_power(self, *, want_standby: bool) -> bool:
        """Press power and wait for activeStandby to change. Sky Q only has a toggle.

        On the ES340 a power press sent within a second or so of the box entering standby is
        ignored, so if nothing has changed after `power_repress_after` seconds, press again.
        State changes showed up in 0.6 s (to standby) and ~3 s (waking), so re-pressing after
        6 s doesn't risk toggling twice.
        """
        t = self.timing
        start = time.monotonic()
        presses = 0
        last_press = 0.0
        while True:
            now = time.monotonic()
            if presses < t.power_max_presses and (presses == 0 or now - last_press >= t.power_repress_after):
                await self.remote.press(KEYS["power"].code)
                presses += 1
                last_press = time.monotonic()
            await asyncio.sleep(t.poll_interval)
            standby = await self.rest.is_standby()
            if standby == want_standby:
                if presses > 1:
                    log.info("power change needed %d presses", presses)
                return standby
            if time.monotonic() - start >= t.power_timeout:
                state = "in standby" if standby else "on"
                raise SkyQError(f"pressed power {presses} times but the box still reports it's {state}")
