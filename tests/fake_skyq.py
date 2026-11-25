"""A fake Sky Q box that behaves like the ES340 measured in Phase 0 (PLAN.md §3).

- Remote protocol on TCP with the real handshake; records every key code received.
- Digits sent less than `drop_under_ms` apart are dropped; a half-typed number stays until
  `backup`; 3-digit numbers tune, numbers starting with 0 need 4 digits (radio).
- REST (/as/system/information, /as/services, /as/pvr/details/P…) including the sensitive
  fields the real box returns, so tests can check they're never passed on.
- UPnP: description0 = GatewaySkyServe (its SkyPlay fails, like the real one),
  description1 = GatewaySkyControl (working SkyPlay).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICES = [
    {"c": "101", "t": "BBC One Yks HD", "sid": "3017", "sf": "hd", "servicetype": "DSAT"},
    {"c": "102", "t": "BBC Two HD", "sid": "3018", "sf": "hd", "servicetype": "DSAT"},
    {"c": "103", "t": "ITV1 HD", "sid": "6000", "sf": "hd", "servicetype": "DSAT"},
    {"c": "104", "t": "Channel 4 HD", "sid": "4000", "sf": "hd", "servicetype": "DSAT"},
    {"c": "110", "t": "U&Gold HD", "sid": "110", "sf": "hd", "servicetype": "DSAT"},
    {"c": "141", "t": "BBC One Lon", "sid": "2000", "sf": "sd", "servicetype": "DSAT"},
    {"c": "203", "t": "ITV1+1", "sid": "6355", "sf": "sd", "servicetype": "DSAT", "timeshifted": True},
    {"c": "210", "t": "U&Gold+1", "sid": "210", "sf": "sd", "servicetype": "DSAT", "timeshifted": True},
    {"c": "301", "t": "Sky Cinema Premiere HD", "sid": "1409", "sf": "hd", "servicetype": "DSAT"},
    {"c": "302", "t": "Sky Cinema Select HD", "sid": "1001", "sf": "hd", "servicetype": "DSAT"},
    {"c": "0111", "t": "Radio X", "sid": "7111", "servicetype": "RADIO"},
]

RECORDINGS = {
    "P290262CD": {"details": {"t": "Gogglebox", "c": 104, "cn": "Channel 4 HD", "sy": "The sofa critics return.",
                              "st": 1794351900, "status": "RECORDED", "seasonnumber": 26, "episodenumber": 3,
                              "pvrid": "P290262cd"}},
}

# 250 entries so clients must page (the real box pages with limit/offset).
PVR_ITEMS = [
    {"pvrid": "P290262cd", "t": "Gogglebox", "cn": "Channel 4 HD", "c": "104", "st": 1794351900,
     "ast": 1794351780, "status": "RECORDED", "sy": "The sofa critics return."},
    {"pvrid": "P29026b37", "t": "The Office", "cn": "U&Gold HD", "c": "110", "st": 1795645200,
     "ast": 1795645987, "status": "RECORDING"},
    {"pvrid": "P29026b39", "t": "The Office", "cn": "U&Gold HD", "c": "110", "st": 1795654800,
     "status": "SCHEDULED"},
    {"pvrid": "P2900ef07", "t": "Live League 2: Harrogate v Bromley", "cn": "SkySp+ HD", "c": "412",
     "st": 1723297500, "ast": 1723297500, "status": "FAILED"},
    {"pvrid": "P29022821", "t": "Live: 24 Hours of Le Mans", "cn": "TNT Sports 3", "c": "413",
     "st": 1781413200, "ast": 1781413200, "status": "PART REC"},
] + [
    {"pvrid": f"P2901{n:04x}", "t": f"Old Episode {n}", "cn": "BBC Two HD", "c": "102",
     "st": 1700000000 + n * 3600, "ast": 1700000000 + n * 3600, "status": "RECORDED"}
    for n in range(245)
]
STORAGE = {"userQuotaMax": 651040, "userQuotaUsed": 638398}

APPS = [
    {"appId": "apple.tv", "title": "Apple TV", "blocked": False, "pinLocked": False, "visibleOnTray": False},
    {"appId": "Netflix", "title": "Netflix", "blocked": False, "pinLocked": False, "visibleOnTray": True},
    {"appId": "iPlayer", "title": "BBC iPlayer", "blocked": False, "pinLocked": False, "visibleOnTray": True},
    {"appId": "com.bskyb.sports", "title": "Sky Sports", "blocked": False, "pinLocked": False, "visibleOnTray": False},
    {"appId": "com.sky.sports.six", "title": "Sky Sports", "blocked": False, "pinLocked": False, "visibleOnTray": True},
    {"appId": "com.bskyb.news", "title": "Sky News", "blocked": False, "pinLocked": False, "visibleOnTray": False},
    {"appId": "com.sky.news.two", "title": "Sky News", "blocked": False, "pinLocked": False, "visibleOnTray": False},
    {"appId": "nba", "title": "NBA", "blocked": True, "pinLocked": False, "visibleOnTray": False},
    {"appId": "com.sky.qkids", "title": "Sky Kids Games", "blocked": False, "pinLocked": True, "visibleOnTray": True},
    {"appId": "com.bskyb.epgui", "title": "EPG_UI", "blocked": False, "pinLocked": False, "visibleOnTray": False},
    {"appId": "com.bskyb.metricstore.titan", "title": "com.bskyb.metricstore", "blocked": False,
     "pinLocked": False, "visibleOnTray": False},
    {"appId": "com.juno.fanview", "title": "@juno-ui/juno-fanview", "blocked": True, "pinLocked": False,
     "visibleOnTray": False},
]

def fake_schedule(day: str, sid: str) -> list[dict]:
    """Hourly programmes for a UTC day, starting the evening before (like Sky's guide), plus
    Gogglebox on Channel 4 at 21:00 UTC every day."""
    import datetime as _dt
    ch = next((c for c in SERVICES if c["sid"] == sid), None)
    if ch is None:
        return []
    midnight = int(_dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=_dt.timezone.utc).timestamp())
    events = []
    for h in range(-2, 24):
        st = midnight + h * 3600
        title = "Gogglebox" if (ch["c"] == "104" and h == 21) else f"{ch['t']} Show {h % 24:02d}"
        events.append({"eid": f"E{sid}-{st}", "t": title, "st": st, "d": 3600, "sy": f"About {title}.",
                       "seasonnumber": 1, "episodenumber": h % 24, "new": h == 20, "s": True, "ad": False,
                       "canb": True, "canl": True})
    return events


SENSITIVE = {
    "householdToken": "SECRET-HOUSEHOLD-TOKEN",
    "serialNumber": "SECRET-SERIAL",
    "viewingCardNumber": "SECRET-CARD",
    "receiverID": "SECRET-RECEIVER",
    "MACAddress": "SECRET-MAC",
    "IPAddress": "SECRET-IP",
}

CONTROL_UDN = "uuid:444D5276-fake-control"


def _description(device_type: str, udn: str, control_path: str) -> bytes:
    return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>{device_type}</deviceType><friendlyName>BSKYB_GATEWAY</friendlyName>
<manufacturer>Sky</manufacturer><modelName>Sky+HD</modelName><UDN>{udn}</UDN>
<serviceList><service><serviceType>urn:schemas-nds-com:service:SkyPlay:2</serviceType>
<controlURL>{control_path}</controlURL><SCPDURL>/x.xml</SCPDURL></service></serviceList>
</device></root>""".encode()


FAULT = b"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring></s:Fault></s:Body></s:Envelope>"""


class FakeSkyQ:
    def __init__(self, *, channel: str = "101", stray_digits: str = "", drop_under_ms: int = 40,
                 standby: bool = False, upnp_broken: bool = False, power_delay: float = 0.0,
                 ignore_power_after_standby: float = 0.0, ignore_app_launch: bool = False,
                 app_status_broken: bool = False):
        self.channel = channel
        self.buffer = stray_digits
        self.drop_under_ms = drop_under_ms
        self.standby = standby
        self.upnp_broken = upnp_broken
        self.power_delay = power_delay
        self.ignore_power_after_standby = ignore_power_after_standby
        self._standby_since = 0.0
        self.recording: str | None = None  # hex pvr id when "playing a recording"
        self.foreground_app: str | None = None  # app on screen, over the TV interface
        self.ignore_app_launch = ignore_app_launch
        self.app_status_broken = app_status_broken
        self.app_requests: list[tuple[str, str]] = []
        self.guide_requests: list[tuple[str, str]] = []
        self.guide_down = False
        self.received: list[int] = []
        self.handshake_errors: list[str] = []
        self._last_digit = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> FakeSkyQ:
        self._remote = socket.socket()
        self._remote.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._remote.bind(("127.0.0.1", 0))
        self._remote.listen()
        self._remote.settimeout(0.2)
        self.remote_port = self._remote.getsockname()[1]
        threading.Thread(target=self._remote_loop, daemon=True).start()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._http_get(self)

            def do_POST(self):
                fake._http_post(self)

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self._start_app_status()
        return self

    # -- app status WebSocket (the real box serves it on the REST port) ------------------

    def _start_app_status(self) -> None:
        import asyncio

        from websockets.asyncio.server import serve

        ready = threading.Event()
        self._ws_clients = set()

        async def handler(ws):
            if self.app_status_broken:
                await ws.close(1011)
                return
            self._ws_clients.add(ws)
            try:
                await ws.send(self._status_message())
                await ws.wait_closed()
            finally:
                self._ws_clients.discard(ws)

        async def main():
            self._ws_server = await serve(handler, "127.0.0.1", 0)
            self.ws_port = self._ws_server.sockets[0].getsockname()[1]
            self._ws_stop = asyncio.Event()
            ready.set()
            await self._ws_stop.wait()
            self._ws_server.close()

        self._ws_loop = asyncio.new_event_loop()
        threading.Thread(target=self._ws_loop.run_until_complete, args=(main(),), daemon=True).start()
        ready.wait(5)

    def _status_message(self) -> str:
        visible = {"com.bskyb.epgui"} | ({self.foreground_app} if self.foreground_app else set())
        apps = [{"appId": a["appId"], "status": "VISIBLE" if a["appId"] in visible else "STOPPED"} for a in APPS]
        apps.append({"appId": "com.bskyb.metricstore.titan", "status": "RUNNING"})
        return json.dumps({"apps": apps})

    def _push_status(self, delay: float = 0.05) -> None:
        async def push():
            import asyncio
            await asyncio.sleep(delay)
            msg = self._status_message()
            for ws in list(self._ws_clients):
                try:
                    await ws.send(msg)
                except Exception:
                    pass
        self._ws_loop.call_soon_threadsafe(lambda: self._ws_loop.create_task(push()))

    def stop(self) -> None:
        self._stop.set()
        self._ws_loop.call_soon_threadsafe(self._ws_stop.set)
        self._http.shutdown()
        self._http.server_close()
        self._remote.close()

    @property
    def ports(self) -> dict[str, int]:
        return {"remote_port": self.remote_port, "rest_port": self.http_port, "upnp_port": self.http_port,
                "app_status_port": self.ws_port}

    # -- remote protocol -------------------------------------------------------------

    def _remote_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._remote.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle_remote, args=(conn,), daemon=True).start()

    def _handle_remote(self, c: socket.socket) -> None:
        try:
            c.settimeout(2)
            c.sendall(b"SKY 000.001\n")
            if c.recv(12) != b"SKY 000.001\n":
                self.handshake_errors.append("bad version echo")
                return
            c.sendall(b"\x01\x01")
            if c.recv(1) != b"\x01":
                self.handshake_errors.append("bad 2-byte echo")
                return
            c.sendall(b"\x00\x00\x00\x00")
            if c.recv(1) != b"\x00":
                self.handshake_errors.append("bad 4-byte echo")
                return
            c.sendall(b"\x00" * 24)
            data = b""
            while len(data) < 16:
                chunk = c.recv(16 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) != 16 or data[:2] != b"\x04\x01" or data[8:10] != b"\x04\x00" or data[6:8] != data[14:16]:
                self.handshake_errors.append(f"bad key packets {data.hex()}")
                return
            self._key(((data[6] - 0xE0) << 4) | data[7])
        except OSError as e:
            self.handshake_errors.append(str(e))
        finally:
            c.close()

    def _key(self, code: int) -> None:
        with self._lock:
            self.received.append(code)
            if code == 0:
                if self.standby and time.monotonic() - self._standby_since < self.ignore_power_after_standby:
                    return  # ES340 ignores power right after entering standby
                threading.Thread(target=self._toggle_power, daemon=True).start()
                return
            if self.standby:
                return
            if code == 2:  # backup clears a half-typed number
                self.buffer = ""
                return
            if 48 <= code <= 57:
                now = time.monotonic()
                too_soon = self.buffer and (now - self._last_digit) * 1000 < self.drop_under_ms
                self._last_digit = now
                if too_soon:
                    return
                self.buffer += str(code - 48)
                needed = 4 if self.buffer.startswith("0") else 3
                if len(self.buffer) == needed:
                    if any(s["c"] == self.buffer for s in SERVICES):
                        self.channel = self.buffer
                        self.recording = None
                    self.buffer = ""

    def _toggle_power(self) -> None:
        time.sleep(self.power_delay)
        with self._lock:
            self.standby = not self.standby
            self._standby_since = time.monotonic()
            self.buffer = ""

    # -- HTTP: REST + UPnP -------------------------------------------------------------

    def _send(self, h: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str = "application/json") -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _http_get(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path
        if path == "/as/system/information":
            info = {"hardwareModel": "ES340d8UKIE", "hardwareName": "Titan", "versionNumber": "32B206",
                    "ASVersion": "Q370.000.09.00-AS_37.2.1", "deviceType": "GATEWAYSTB",
                    "activeStandby": self.standby, "uhdCapable": True, "hdrCapable": True,
                    "systemUptime": 64000, **SENSITIVE}
            return self._send(h, 200, json.dumps(info).encode())
        if path == "/as/services":
            return self._send(h, 200, json.dumps({"documentId": "1", "services": SERVICES, "version": 1}).encode())
        if path == "/as/system/time":
            import time as _t
            return self._send(h, 200, json.dumps({"utc": int(_t.time()), "presentLocalTimeOffset": 0,
                                                  "futureLocalTimeOffset": 3600,
                                                  "futureTransitionUtc": int(_t.time()) + 400 * 86400}).encode())
        if path == "/as/system/deviceinformation":
            return self._send(h, 200, json.dumps({"countryCode": "GBR", "bouquet": 4103,
                                                  "householdToken": SENSITIVE["householdToken"],
                                                  "IPAddress": SENSITIVE["IPAddress"]}).encode())
        if path.startswith("/as/schedule/"):
            if (h.headers.get("x-skyott-territory") != "GB" or h.headers.get("x-skyott-provider") != "SKY"
                    or h.headers.get("x-skyott-proposition") != "SKYQ"):
                return self._send(h, 400, b'{"errorCode": "missing headers"}')
            parts = path.split("/")
            day, sid = parts[3], parts[4]
            self.guide_requests.append((day, sid))
            if self.guide_down:
                return self._send(h, 503, b"")
            if "," in sid:
                return self._send(h, 400, b'{"errorCode": "bad sid"}')
            return self._send(h, 200, json.dumps({"schedule": [{"sid": sid, "events": fake_schedule(day, sid)}]}).encode())
        if path == "/as/apps":
            return self._send(h, 200, json.dumps({"apps": APPS, "version": 1}).encode())
        if path == "/as/pvr/storage":
            return self._send(h, 200, json.dumps(STORAGE).encode())
        if path.startswith("/as/pvr?"):
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(path).query)
            limit, offset = int(q["limit"][0]), int(q["offset"][0])
            page = PVR_ITEMS[offset:offset + min(limit, 200)]
            return self._send(h, 200, json.dumps({"documentId": "1", "offset": offset, "pvrItems": page,
                                                  "totalPvrItems": len(PVR_ITEMS), "version": 1}).encode())
        if path.startswith("/as/pvr/details/"):
            pvr = path.rsplit("/", 1)[1]
            if pvr.startswith("P") and pvr.upper() in RECORDINGS:
                return self._send(h, 200, json.dumps(RECORDINGS[pvr.upper()]).encode())
            return self._send(h, 400, json.dumps({"errorCode": 1, "userMessage": "Bad request"}).encode())
        if path == "/description0.xml":
            return self._send(h, 200, _description("urn:schemas-nds-com:device:GatewaySkyServe:2",
                                                   "uuid:444D5376-fake-serve", "/serve-ctl"), "text/xml")
        if path == "/description1.xml":
            return self._send(h, 200, _description("urn:schemas-nds-com:device:GatewaySkyControl:2",
                                                   CONTROL_UDN, "/control-ctl"), "text/xml")
        return self._send(h, 404, b'{"userMessage": "Invalid URL"}')

    def _http_post(self, h: BaseHTTPRequestHandler) -> None:
        h.rfile.read(int(h.headers.get("Content-Length", 0)))
        if h.path.startswith("/as/apps/action/"):
            from urllib.parse import parse_qs, urlsplit
            u = urlsplit(h.path)
            action, app_id = u.path.rsplit("/", 1)[1], (parse_qs(u.query).get("appId") or [""])[0]
            self.app_requests.append((action, app_id))
            if action not in ("launch", "close"):
                return self._send(h, 404, b'{"errorCode": 101, "userMessage": "Invalid URL"}')
            if not any(a["appId"] == app_id for a in APPS):
                return self._send(h, 400, b'{"errorCode": 102, "userMessage": "Invalid Parameters"}')
            if action == "launch" and not self.ignore_app_launch:
                self.foreground_app = app_id
                self._push_status(0.1)
            elif action == "close" and self.foreground_app == app_id:
                self.foreground_app = None
                self._push_status(0.1)
            return self._send(h, 200, b"")
        if h.headers.get("User-Agent") != "SKYPLUS_skyplus" or h.path != "/control-ctl" or self.upnp_broken:
            return self._send(h, 500, FAULT, "text/xml")
        action = h.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        if action == "GetTransportInfo":
            fields = {"CurrentTransportState": "PLAYING", "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}
        elif action == "GetMediaInfo":
            if self.recording:
                uri = f"file://pvr/{self.recording}"
            else:
                sid = next(s["sid"] for s in SERVICES if s["c"] == self.channel)
                uri = f"xsi://{int(sid):X}"
            fields = {"NrTracks": "0", "CurrentURI": uri, "PlayMedium": "NONE"}
        else:
            return self._send(h, 500, FAULT, "text/xml")
        inner = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
        body = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action}Response xmlns:u="urn:schemas-nds-com:service:SkyPlay:2">{inner}'
                f"</u:{action}Response></s:Body></s:Envelope>")
        return self._send(h, 200, body.encode(), "text/xml")
