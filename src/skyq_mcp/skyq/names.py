"""Hostnames for boxes, so they can be found again after DHCP gives them a new IP address.

Two sources, because home networks differ:
- the router's DNS (reverse lookup of the box's IP), when the router publishes DHCP names;
- mDNS: Sky Q boxes announce AirPlay/RAOP services with their own `.local` hostname
  (e.g. `skyq.local`). This uses the zeroconf library directly, so it works without
  avahi / nss-mdns on the host.

Hostnames aren't unique: two boxes both called `skyq.local` get renamed by mDNS
(`skyq-2.local`), and which box gets which name can change after a power cut. Callers must
check a box's identity (UPnP UDN) after resolving a name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MDNS_SERVICE_TYPES = ["_airplay._tcp.local.", "_raop._tcp.local."]


LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918 home/office networks
    "169.254.0.0/16",  # link-local (no DHCP server)
    "fc00::/7", "fe80::/10",  # IPv6 unique-local and link-local
)]

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


def is_local_ip(value: str, *, allow_loopback: bool = False) -> bool:
    """True for addresses a Sky Q box can have: private LAN ranges and link-local.
    Public internet addresses are never contacted (no request forgery via saved boxes)."""
    try:
        a = ipaddress.ip_address(value)
    except ValueError:
        return False
    if a.is_loopback:
        return allow_loopback
    # An explicit list: ipaddress's is_private also counts documentation and other special ranges.
    return any(a in net for net in LOCAL_NETWORKS)


def valid_hostname(value: str) -> bool:
    return bool(HOSTNAME_RE.match(normalise(value)))


def validate_address(value: str) -> str:
    """An IP address or a well-formed hostname, normalised; raises SkyQError otherwise."""
    from .errors import SkyQError

    v = value.strip()
    if is_ip(v):
        return v
    v = normalise(v)
    if not HOSTNAME_RE.match(v):
        raise SkyQError(f"'{value[:80]}' isn't a valid hostname or IP address")
    return v


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def normalise(hostname: str) -> str:
    return hostname.strip().rstrip(".").lower()


@dataclass
class MdnsDevice:
    hostname: str
    addresses: list[str] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)
    manufacturer: str | None = None
    model: str | None = None

    @property
    def is_sky(self) -> bool:
        return (self.manufacturer or "").lower() == "sky"


async def mdns_browse(timeout: float = 3.0, *, iface: str | None = None) -> list[MdnsDevice]:
    """Devices announcing AirPlay/RAOP over mDNS, grouped by hostname."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:  # pragma: no cover - declared dependency
        log.warning("zeroconf isn't installed; skipping mDNS")
        return []

    azc = AsyncZeroconf(interfaces=[iface]) if iface else AsyncZeroconf()
    found: set[tuple[str, str]] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found.add((service_type, name))

    browser = AsyncServiceBrowser(azc.zeroconf, MDNS_SERVICE_TYPES, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        devices: dict[str, MdnsDevice] = {}
        for service_type, name in sorted(found):
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(azc.zeroconf, 2000) or not info.server:
                continue
            props = {k.decode(errors="replace"): (v.decode(errors="replace") if v else None)
                     for k, v in (info.properties or {}).items()}
            dev = devices.setdefault(normalise(info.server), MdnsDevice(hostname=normalise(info.server)))
            for addr in info.parsed_addresses():
                if is_ip(addr) and ipaddress.ip_address(addr).version == 4 and addr not in dev.addresses:
                    dev.addresses.append(addr)
            dev.instances.append(name)
            dev.manufacturer = dev.manufacturer or props.get("manufacturer")
            dev.model = dev.model or props.get("model") or props.get("am")
        return list(devices.values())
    finally:
        await browser.async_cancel()
        await azc.async_close()


async def reverse_dns(ip: str, timeout: float = 2.0) -> str | None:
    """The name the network's DNS gives this IP (usually the router's DHCP name), if any."""
    loop = asyncio.get_running_loop()
    try:
        host, _ = await asyncio.wait_for(loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    host = normalise(host)
    if not host or is_ip(host) or host.endswith(".arpa"):
        return None
    return host


def hostnames_from_mdns(ip: str, devices: list[MdnsDevice]) -> list[str]:
    return [d.hostname for d in devices if ip in d.addresses]


async def find_hostnames(ip: str, *, timeout: float = 3.0, iface: str | None = None,
                         mdns: list[MdnsDevice] | None = None) -> list[str]:
    """Hostnames that currently point at ip: router DNS name first, then mDNS names."""
    if mdns is None:
        dns, mdns = await asyncio.gather(reverse_dns(ip), mdns_browse(timeout, iface=iface))
    else:
        dns = await reverse_dns(ip)
    names = ([dns] if dns else []) + hostnames_from_mdns(ip, mdns)
    out: list[str] = []
    for n in names:
        if n not in out and valid_hostname(n):  # names come from the network: don't trust them
            out.append(n)
    return out


async def resolve(hostname: str, timeout: float = 3.0) -> list[str]:
    """IPv4 addresses for a hostname: the OS resolver first (DNS, /etc/hosts, nss-mdns),
    then mDNS directly for `.local` names. An IP address resolves to itself."""
    name = hostname.strip()
    if is_ip(name):
        return [name]
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(name, None, family=socket.AF_INET), timeout)
        addrs = []
        for info in infos:
            a = info[4][0]
            if a not in addrs:
                addrs.append(a)
        if addrs:
            return addrs
    except (OSError, asyncio.TimeoutError):
        pass
    if normalise(name).endswith(".local"):
        return await _mdns_resolve(normalise(name) + ".", timeout)
    return []


async def _mdns_resolve(fqdn: str, timeout: float) -> list[str]:
    try:
        from zeroconf import AddressResolverIPv4
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:  # pragma: no cover
        return []
    azc = AsyncZeroconf()
    try:
        resolver = AddressResolverIPv4(fqdn)
        if await resolver.async_request(azc.zeroconf, int(timeout * 1000)):
            return list(resolver.parsed_addresses())
        return []
    finally:
        await azc.async_close()
