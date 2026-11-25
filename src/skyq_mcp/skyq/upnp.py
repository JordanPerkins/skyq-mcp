"""Sky Q UPnP (SOAP) on port 49153: device descriptions and SkyPlay transport/media info.

Phase 0 (PLAN.md §3.4): use the SkyPlay service on the GatewaySkyControl device; the one on
GatewaySkyServe rejects InstanceID 0. Requests need the SKYPLUS_skyplus user agent.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

from . import http
from .errors import ProtocolError

UPNP_PORT = 49153
USER_AGENT = "SKYPLUS_skyplus"
NS = "urn:schemas-upnp-org:device-1-0"
SKY_CONTROL = "SkyControl"
SKY_PLAY = "SkyPlay"


@dataclass(frozen=True)
class Service:
    service_type: str
    control_url: str


@dataclass(frozen=True)
class Device:
    location: str
    device_type: str
    udn: str | None
    model_name: str | None
    services: list[Service] = field(default_factory=list)

    def service(self, fragment: str) -> Service | None:
        return next((s for s in self.services if fragment in s.service_type), None)


def _same_origin(url: str, base: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(base)
        return a.scheme == "http" and (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)
    except ValueError:
        return False


def parse_description(xml: bytes, location: str) -> Device:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ProtocolError(f"invalid UPnP description at {location}") from e
    dev = root.find(f"{{{NS}}}device")
    if dev is None:
        raise ProtocolError(f"no <device> in UPnP description at {location}")

    def text(el: ET.Element, tag: str) -> str | None:
        found = el.find(f"{{{NS}}}{tag}")
        return found.text.strip() if found is not None and found.text else None

    services = []
    for s in dev.iter(f"{{{NS}}}service"):
        control = urljoin(location, text(s, "controlURL") or "")
        if not _same_origin(control, location):
            # A device could point us anywhere (another service on the LAN or this machine).
            continue
        services.append(Service(text(s, "serviceType") or "", control))
    return Device(location, text(dev, "deviceType") or "", text(dev, "UDN"), text(dev, "modelName"), services)


@dataclass(frozen=True)
class TransportInfo:
    state: str | None  # PLAYING, PAUSED_PLAYBACK, STOPPED, …
    speed: str | None


@dataclass(frozen=True)
class MediaInfo:
    uri: str | None  # xsi://<hex sid> for live TV, file://pvr/<hex id> for recordings


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


class UpnpClient:
    def __init__(self, host: str, *, port: int = UPNP_PORT, timeout: float = 5.0,
                 locations: list[str] | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        default = [f"http://{host}:{port}/description{i}.xml" for i in range(4)]
        # Only fetch descriptions from the box itself, whatever SSDP advertised.
        self.locations = [u for u in (locations or []) if _host_of(u) == host] or default
        self._control: Service | None = None
        self._control_device: Device | None = None

    async def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                       content: bytes | None = None) -> tuple[int, bytes]:
        return await http.request(method, url, timeout=self.timeout, content=content,
                                  headers={"User-Agent": USER_AGENT, **(headers or {})})

    async def devices(self) -> list[Device]:
        out = []
        for loc in self.locations:
            status, body = await self._request("GET", loc)
            if status == 200:
                out.append(parse_description(body, loc))
        return out

    async def control_device(self) -> Device:
        """The GatewaySkyControl device (its UDN is the box's stable identity)."""
        if self._control_device is None:
            for dev in await self.devices():
                if SKY_CONTROL in dev.device_type and dev.service(SKY_PLAY):
                    self._control_device = dev
                    self._control = dev.service(SKY_PLAY)
                    break
            else:
                raise ProtocolError(f"no SkyControl/SkyPlay UPnP service found on {self.host}")
        return self._control_device

    async def _soap(self, action: str, args: dict[str, str]) -> dict[str, str]:
        await self.control_device()
        assert self._control is not None
        st = self._control.service_type
        arg_xml = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{st}">{arg_xml}</u:{action}></s:Body></s:Envelope>'
        )
        status, resp_body = await self._request(
            "POST", self._control.control_url, content=body.encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{st}#{action}"'},
        )
        try:
            root = ET.fromstring(resp_body)
        except ET.ParseError as e:
            raise ProtocolError(f"invalid SOAP response to {action}") from e
        for el in root.iter():
            if el.tag.endswith(f"{action}Response"):
                return {child.tag.split("}")[-1]: (child.text or "").strip()[:500] for child in el}
        raise ProtocolError(f"{action} failed (HTTP {status})")

    async def transport_info(self) -> TransportInfo:
        r = await self._soap("GetTransportInfo", {"InstanceID": "0"})
        return TransportInfo(r.get("CurrentTransportState"), r.get("CurrentSpeed"))

    async def media_info(self) -> MediaInfo:
        r = await self._soap("GetMediaInfo", {"InstanceID": "0"})
        return MediaInfo(r.get("CurrentURI") or None)
