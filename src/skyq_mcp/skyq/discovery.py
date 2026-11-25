"""Finding Sky Q boxes: SSDP multicast, with a subnet scan as a fallback.

Phase 0 (PLAN.md §3.5): an ES340 answers to the Gateway* device types and not to the plain
SkyServe/SkyControl ones; both are searched for compatibility with other models.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import names
from .errors import SkyQError
from .remote import REMOTE_PORT
from .rest import REST_PORT, RestClient
from .upnp import UPNP_PORT, UpnpClient

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = [
    "urn:schemas-nds-com:device:GatewaySkyControl:2",
    "urn:schemas-nds-com:device:GatewaySkyServe:2",
    "urn:schemas-nds-com:device:SkyControl:2",
    "urn:schemas-nds-com:device:SkyServe:2",
]
SKY_CONTROL_ST_FRAGMENT = "SkyControl"


@dataclass
class Candidate:
    """A box found on the network."""

    host: str  # current IP address
    id: str | None = None  # UDN of the SkyControl device: stable across IP changes
    hostnames: list[str] = field(default_factory=list)  # names that currently point at host
    model: str | None = None
    hardware_name: str | None = None
    standby: bool | None = None
    locations: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "locations"}


def parse_ssdp_response(data: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in data.decode("utf-8", errors="replace").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    return headers


def is_sky_response(headers: dict[str, str]) -> bool:
    blob = " ".join(headers.get(k, "") for k in ("ST", "NT", "USN", "SERVER")).lower()
    return "nds-com" in blob


def _url_host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def group_responses(responses: list[tuple[str, dict[str, str]]]) -> list[Candidate]:
    by_host: dict[str, Candidate] = {}
    for host, h in responses:
        if not is_sky_response(h):
            continue
        cand = by_host.setdefault(host, Candidate(host=host))
        loc = h.get("LOCATION")
        if loc and loc not in cand.locations and _url_host(loc) == host:  # no fetching elsewhere
            cand.locations.append(loc)
        if SKY_CONTROL_ST_FRAGMENT in h.get("ST", "") and h.get("USN"):
            cand.id = h["USN"].split("::", 1)[0]
    return list(by_host.values())



class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.responses: list[tuple[str, dict[str, str]]] = []

    def datagram_received(self, data: bytes, addr) -> None:
        self.responses.append((addr[0], parse_ssdp_response(data)))

    def error_received(self, exc: Exception) -> None:
        log.debug("SSDP socket error: %s", exc)


async def ssdp_search(timeout: float = 3.0, *, iface: str | None = None,
                      targets: list[str] | None = None) -> list[tuple[str, dict[str, str]]]:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _SsdpProtocol, local_addr=(iface or "0.0.0.0", 0), family=socket.AF_INET)
    try:
        sock = transport.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if iface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        sent = 0
        last_err: Exception | None = None
        for _ in range(2):  # UDP is lossy
            for st in targets or SEARCH_TARGETS:
                msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                       f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n')
                try:
                    transport.sendto(msg.encode("ascii"), SSDP_ADDR)
                    sent += 1
                except OSError as e:
                    last_err = e
        if not sent:
            raise SkyQError(f"couldn't send SSDP multicast ({last_err}); try setting the network interface")
        await asyncio.sleep(timeout)
        return list(proto.responses)
    finally:
        transport.close()


async def identify(host: str, *, rest_port: int = REST_PORT, upnp_port: int = UPNP_PORT,
                   locations: list[str] | None = None, timeout: float = 4.0) -> Candidate:
    """Fill in a candidate's identity (UDN) and model by asking the box itself."""
    cand = Candidate(host=host, locations=list(locations or []))
    try:
        info = await RestClient(host, port=rest_port, timeout=timeout).system_information()
        cand.model, cand.hardware_name, cand.standby = info.model, info.hardware_name, info.standby
        dev = await UpnpClient(host, port=upnp_port, timeout=timeout, locations=locations or None).control_device()
        cand.id = dev.udn
    except SkyQError as e:
        cand.error = str(e)
    return cand


async def _with_hostnames(cands: list[Candidate], mdns: list[names.MdnsDevice]) -> list[Candidate]:
    found = await asyncio.gather(*(names.find_hostnames(c.host, mdns=mdns) for c in cands))
    for c, hostnames in zip(cands, found):
        c.hostnames = hostnames
    return cands


async def _safe_mdns(timeout: float, iface: str | None) -> list[names.MdnsDevice]:
    try:
        return await names.mdns_browse(timeout, iface=iface)
    except Exception as e:  # noqa: BLE001 - mDNS is a bonus; SSDP still works without it
        log.info("mDNS browse failed: %s", e)
        return []


async def discover(timeout: float = 3.0, *, iface: str | None = None) -> list[Candidate]:
    """Find boxes with SSDP and mDNS together, then ask each for its identity and model and
    look up the hostnames that point at it."""
    ssdp_result, mdns = await asyncio.gather(
        ssdp_search(timeout, iface=iface), _safe_mdns(timeout, iface), return_exceptions=True)
    if isinstance(mdns, BaseException):
        mdns = []
    if isinstance(ssdp_result, BaseException):
        if not mdns:
            raise ssdp_result
        log.info("SSDP search failed (%s); using mDNS only", ssdp_result)
        ssdp_result = []
    found = group_responses(ssdp_result)
    seen = {c.host for c in found}
    for dev in mdns:  # Sky boxes that answered mDNS but not SSDP
        for addr in dev.addresses:
            if dev.is_sky and addr not in seen:
                found.append(Candidate(host=addr))
                seen.add(addr)
    results = await asyncio.gather(*(identify(c.host, locations=c.locations or None) for c in found))
    for c, r in zip(found, results):
        r.id = r.id or c.id
    boxes = [r for r in results if (r.id or not r.error) and names.is_local_ip(r.host, allow_loopback=True)]
    return await _with_hostnames(boxes, mdns)


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def scan_subnet(cidr: str, *, timeout: float = 0.4, concurrency: int = 64,
                      remote_port: int = REMOTE_PORT, allow_loopback: bool = False) -> list[Candidate]:
    """Fallback when multicast is blocked: probe every host for the remote port, then identify.
    Only private (home/office) networks can be scanned."""
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise SkyQError(f"'{cidr[:60]}' isn't a network range like 192.168.1.0/24") from None
    if net.version != 4:
        raise SkyQError("only IPv4 network ranges can be scanned")
    if not names.is_local_ip(str(net.network_address), allow_loopback=allow_loopback) or \
            not names.is_local_ip(str(net.broadcast_address), allow_loopback=allow_loopback):
        raise SkyQError(f"{cidr} isn't a private home network range; only those can be scanned")
    if net.num_addresses > 1024:
        raise SkyQError(f"{cidr} is too large to scan; use a /22 or smaller")
    sem = asyncio.Semaphore(concurrency)

    async def probe(h: str) -> str | None:
        async with sem:
            return h if await _port_open(h, remote_port, timeout) else None

    hosts = [h for h in await asyncio.gather(*(probe(str(h)) for h in net.hosts())) if h]
    cands = list(await asyncio.gather(*(identify(h) for h in hosts)))
    return await _with_hostnames(cands, await _safe_mdns(3.0, None))
