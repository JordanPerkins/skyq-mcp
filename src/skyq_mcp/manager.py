"""Ties saved boxes to live connections, and keeps finding them as their IP addresses change.

How a saved box is reached (DHCP can move it at any time, e.g. after a power cut):
1. resolve its saved hostname (router DNS or mDNS `.local`);
2. otherwise its last known IP;
3. otherwise search the network (SSDP + mDNS) for its identity, then save its new hostname/IP.
An address is only used once the box there proves to be the right one (same UPnP UDN), so a
hostname that has moved to a different box (two boxes both called skyq.local) is never trusted.
The working address is cached for a few minutes and dropped as soon as the box stops answering.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .skyq import discovery, names
from .skyq.box import SkyQBox, Timing
from .skyq.discovery import Candidate
from .skyq.errors import BoxUnreachable, RegistryError, SkyQError
from .skyq.upnp import UpnpClient
from .store import BoxRecord, BoxRegistry, now_iso

log = logging.getLogger(__name__)
T = TypeVar("T")

DiscoverFn = Callable[..., Awaitable[list[Candidate]]]
IdentifyFn = Callable[..., Awaitable[Candidate]]
ResolveFn = Callable[[str], Awaitable[list[str]]]
HostnamesFn = Callable[[str], Awaitable[list[str]]]
VerifyFn = Callable[[BoxRecord, str], Awaitable[bool]]

ADDRESS_TTL = 300.0  # seconds a verified address is reused before resolving the name again
LAST_SEEN_WRITE_INTERVAL = 3600.0  # don't rewrite the state file (SD card) on every call


@dataclass
class _Resolved:
    ip: str
    at: float


async def verify_identity(record: BoxRecord, ip: str) -> bool:
    """Is the box at ip the saved box? (No saved identity: anything that answers counts.)"""
    try:
        dev = await UpnpClient(ip, port=record.upnp_port, timeout=3.0).control_device()
    except SkyQError:
        return False
    return record.id is None or dev.udn == record.id


class BoxManager:
    def __init__(self, registry: BoxRegistry, *, allow_unsafe: bool = False, timing: Timing | None = None,
                 discover_fn: DiscoverFn = discovery.discover, identify_fn: IdentifyFn = discovery.identify,
                 resolve_fn: ResolveFn = names.resolve, hostnames_fn: HostnamesFn = names.find_hostnames,
                 verify_fn: VerifyFn = verify_identity, rediscover_timeout: float = 3.0,
                 allow_loopback: bool = False):
        self.registry = registry
        self.allow_unsafe = allow_unsafe
        self.timing = timing
        self._discover = discover_fn
        self._identify = identify_fn
        self._resolve = resolve_fn
        self._hostnames = hostnames_fn
        self._verify = verify_fn
        self.rediscover_timeout = rediscover_timeout
        # Only local-network addresses are ever contacted. Loopback is for tests/development.
        self.allow_loopback = allow_loopback
        self._resolved: dict[str, _Resolved] = {}
        self._boxes: dict[str, tuple[tuple, SkyQBox]] = {}
        self._last_seen_written: dict[str, float] = {}

    # -- connections ------------------------------------------------------------------

    def box(self, record: BoxRecord, ip: str) -> SkyQBox:
        """A SkyQBox for a record at ip, reused while the address and settings stay the same."""
        key = (ip, record.remote_port, record.rest_port, record.upnp_port, record.key_gap_ms, record.app_status_port)
        cached = self._boxes.get(record.alias)
        if cached and cached[0] == key:
            return cached[1]
        box = SkyQBox(ip, remote_port=record.remote_port, rest_port=record.rest_port,
                      upnp_port=record.upnp_port, key_gap_ms=record.key_gap_ms,
                      timing=self.timing, allow_unsafe=self.allow_unsafe,
                      app_status_port=record.app_status_port)
        self._boxes[record.alias] = (key, box)
        return box

    async def run(self, alias: str | None, fn: Callable[[SkyQBox], Awaitable[T]]) -> T:
        """Run fn against a saved box, finding its current address first. If the box stops
        answering part-way, find it again and retry once."""
        record = self.registry.get(alias)
        ip = await self.locate(record)
        try:
            result = await fn(self.box(record, ip))
        except BoxUnreachable as first_error:
            self._resolved.pop(record.alias, None)
            try:
                new_ip = await self.locate(record, avoid=ip)
            except BoxUnreachable:
                raise first_error from None
            result = await fn(self.box(record, new_ip))
        self._touch(record)
        return result

    async def locate(self, record: BoxRecord, *, avoid: str | None = None) -> str:
        """The IP address the saved box can be reached at right now."""
        cached = self._resolved.get(record.alias)
        if cached and avoid is None and time.monotonic() - cached.at < ADDRESS_TTL:
            return cached.ip

        tried: list[str] = []
        for ip in await self._candidate_ips(record):
            if ip == avoid or ip in tried:
                continue
            tried.append(ip)
            if await self._verify(record, ip):
                return self._use(record, ip)
            log.info("box '%s': %s didn't answer as the saved box", record.alias, ip)

        found = await self._rediscover(record)
        if found is not None:
            return self._use(record, found.host, hostnames=found.hostnames)

        where = " or ".join(filter(None, [record.hostname, record.last_ip]))
        raise BoxUnreachable(
            f"box '{record.alias}' couldn't be reached at {where} or found on the network. "
            "Check it's plugged in and on the same network as this server."
        )

    def _local(self, ip: str) -> bool:
        return names.is_local_ip(ip, allow_loopback=self.allow_loopback)

    async def _candidate_ips(self, record: BoxRecord) -> list[str]:
        ips: list[str] = []
        if record.hostname:
            try:
                ips += await self._resolve(record.hostname)
            except Exception as e:  # noqa: BLE001 - a failed lookup just means "try the next way"
                log.info("box '%s': couldn't resolve %s (%s)", record.alias, record.hostname, e)
        if record.last_ip:
            ips.append(record.last_ip)
        local = [ip for ip in ips if self._local(ip)]
        if len(local) < len(ips):
            log.warning("box '%s': ignoring non-local address(es) %s", record.alias,
                        [ip for ip in ips if not self._local(ip)])
        return local

    async def _rediscover(self, record: BoxRecord) -> Candidate | None:
        if not record.id:
            return None
        try:
            candidates = await self._discover(timeout=self.rediscover_timeout)
        except Exception as e:  # noqa: BLE001 - discovery failing just means "not found"
            log.info("rediscovery failed: %s", e)
            return None
        return next((c for c in candidates if c.id == record.id and self._local(c.host)), None)

    def _use(self, record: BoxRecord, ip: str, *, hostnames: list[str] | None = None) -> str:
        """Remember a verified address; save it (and a better hostname) if it changed."""
        self._resolved[record.alias] = _Resolved(ip, time.monotonic())
        changes: dict[str, Any] = {}
        if ip != record.last_ip:
            changes["last_ip"] = ip
        hostnames = [h for h in (hostnames or []) if names.valid_hostname(h)]
        if hostnames and record.hostname not in hostnames:
            # Found by searching: the saved name (if any) no longer points at this box.
            changes["hostname"] = hostnames[0]
        if changes:
            log.info("box '%s' is now at %s", record.alias, {**changes})
            changes["last_seen"] = now_iso()
            self.registry.update(record.alias, **changes)
            self._last_seen_written[record.alias] = time.monotonic()
        return ip

    def _touch(self, record: BoxRecord) -> None:
        last = self._last_seen_written.get(record.alias, 0.0)
        if time.monotonic() - last >= LAST_SEEN_WRITE_INTERVAL or record.last_seen is None:
            self.registry.update(record.alias, last_seen=now_iso())
            self._last_seen_written[record.alias] = time.monotonic()

    # -- discovery and saving ----------------------------------------------------

    async def discover(self, *, timeout: float = 3.0, iface: str | None = None,
                       subnet: str | None = None) -> list[dict[str, Any]]:
        """Boxes on the network, each marked with the alias it's saved under (if any)."""
        if subnet:
            candidates = await discovery.scan_subnet(subnet, allow_loopback=self.allow_loopback)
        else:
            candidates = await self._discover(timeout=timeout, iface=iface)
        out = []
        for c in candidates:
            saved = (self.registry.find_by_id(c.id) if c.id else None) or self.registry.find_by_address(c.host)
            out.append({**c.to_dict(), "saved_as": saved.alias if saved else None})
        return out

    async def save(self, alias: str, address: str, *, remote_port: int = discovery.REMOTE_PORT,
                   rest_port: int = discovery.REST_PORT, upnp_port: int = discovery.UPNP_PORT,
                   app_status_port: int | None = None) -> tuple[BoxRecord, str | None]:
        """Check the box at address (an IP or a hostname) and save it under alias.

        Saved by hostname whenever one is known, so DHCP address changes don't matter.
        Returns (record, warning).
        """
        address = names.validate_address(address)
        ips = await self._resolve(address)
        if not ips:
            raise BoxUnreachable(f"couldn't resolve '{address}' to an IP address")
        local = [ip for ip in ips if self._local(ip)]
        if not local:
            raise SkyQError(f"'{address}' resolves to {ips[0]}, which isn't on a local network; "
                            "Sky Q boxes must be on your home network")
        ip = local[0]
        cand = await self._identify(ip, rest_port=rest_port, upnp_port=upnp_port)
        if cand.error:
            raise BoxUnreachable(f"couldn't confirm a Sky Q box at {address}: {cand.error}")
        if cand.id and (existing := self.registry.find_by_id(cand.id)):
            raise RegistryError(f"that box is already saved as '{existing.alias}'")

        if names.is_ip(address):
            hostnames = [h for h in await self._hostnames(ip) if names.valid_hostname(h)]
            hostname = hostnames[0] if hostnames else None
        else:
            hostname = names.normalise(address)
        warning = None
        if hostname is None:
            warning = ("No hostname was found for this box, so it's saved by IP address. If its "
                       "address changes it will be found again by searching the network, which needs "
                       "multicast to work between this server and the box. To avoid that, give the "
                       "box a fixed IP (DHCP reservation) or a DNS name on your router, then save it "
                       "by that name.")
        record = BoxRecord(alias=alias, hostname=hostname, last_ip=ip, id=cand.id, model=cand.model,
                           last_seen=now_iso(), remote_port=remote_port, rest_port=rest_port,
                           upnp_port=upnp_port, app_status_port=app_status_port)
        record = self.registry.add(record)
        self._resolved[record.alias] = _Resolved(ip, time.monotonic())
        return record, warning
