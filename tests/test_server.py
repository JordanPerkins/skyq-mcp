"""End to end: a real HTTP server (uvicorn) with bearer auth, an MCP client, and the fake box."""

import asyncio
import json
import threading

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from conftest import FAST
from fake_skyq import CONTROL_UDN, SENSITIVE
from skyq_mcp.config import Config
from skyq_mcp.manager import BoxManager
from skyq_mcp.server import build_app
from skyq_mcp.store import BoxRecord, BoxRegistry

TOKEN = "test-token-0123456789-abcdefghij"
OTHER_TOKEN = "second-token-0123456789-abcdefgh"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class ServerThread:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            threading.Event().wait(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def served(fake, tmp_path):
    registry = BoxRegistry(tmp_path / "state.json")
    registry.add(BoxRecord(alias="lounge", hostname="localhost", last_ip="127.0.0.1", id=CONTROL_UDN,
                           key_gap_ms=60, **fake.ports))
    manager = BoxManager(registry, timing=FAST, allow_loopback=True,
                         epg_base_url=f"http://127.0.0.1:{fake.http_port}/as/schedule")
    config = Config(host="127.0.0.1", data_dir=tmp_path, allowed_hosts=["pi.tail1234.ts.net"])
    with ServerThread(build_app(config, [TOKEN, OTHER_TOKEN], manager=manager)) as srv:
        srv.fake = fake
        srv.registry = registry
        yield srv


# -- auth & transport security -------------------------------------------------------


async def test_healthz_needs_no_token(served):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{served.url}/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token", f"Basic {TOKEN}", "Bearer", f"Bearer {TOKEN}x"])
async def test_mcp_rejects_missing_or_bad_tokens(served, auth):
    headers = dict(MCP_HEADERS)
    if auth is not None:
        headers["Authorization"] = auth
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{served.url}/mcp", json=INIT, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith('Bearer realm="skyq-mcp"')
    assert TOKEN not in r.text


@pytest.mark.parametrize("token", [TOKEN, OTHER_TOKEN])
async def test_mcp_accepts_each_configured_token(served, token):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{served.url}/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"bearer {token}"})
    assert r.status_code == 200


async def test_other_paths_also_need_token(served):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{served.url}/anything")
    assert r.status_code == 401


async def test_extra_allowed_host_for_tailscale_funnel(served):
    # Funnel forwards with the public *.ts.net name as Host; this machine's own names still work.
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient() as c:
        funnel = await c.post(f"{served.url}/mcp", json=INIT, headers={**auth, "Host": "pi.tail1234.ts.net"})
        local = await c.post(f"{served.url}/mcp", json=INIT, headers=auth)
    assert funnel.status_code == 200 and local.status_code == 200


async def test_dns_rebinding_protection(served):
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient() as c:
        bad_host = await c.post(f"{served.url}/mcp", json=INIT, headers={**auth, "Host": "evil.example:8765"})
        browser = await c.post(f"{served.url}/mcp", json=INIT, headers={**auth, "Origin": "http://evil.example"})
    assert bad_host.status_code == 421
    assert browser.status_code == 403


# -- MCP tools -----------------------------------------------------------------------


async def call(served, tool: str, args: dict | None = None, *, token: str = TOKEN):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(f"{served.url}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args or {})


async def guide_call(served, tool: str, args: dict):
    """Listings tools first reply with a fallback code; call again with it (as an agent would)."""
    first = await call(served, tool, args)
    assert first.structured_content["status"] == "use_your_own_listings_first", text(first)
    return await call(served, tool, {**args, "fallback_code": first.structured_content["fallback_code"]})


def text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "text", None))


async def test_lists_all_tools(served):
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with streamable_http_client(f"{served.url}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                resources = {str(r.uri) for r in (await session.list_resources()).resources}
    assert {"discover_boxes", "save_box", "change_channel", "power_on", "power_off", "press_button",
            "get_now_playing", "list_channels"} <= tools
    assert resources == {"skyq://buttons", "skyq://boxes"}


async def test_change_channel_tool(served):
    result = await call(served, "change_channel", {"channel": "channel 4"})
    assert not result.is_error, text(result)
    assert result.structured_content["channel"]["number"] == "104"
    assert result.structured_content["verified"] is True
    assert served.fake.channel == "104"


async def test_now_playing_and_status_tools(served):
    now = await call(served, "get_now_playing")
    assert now.structured_content["channel"]["name"] == "BBC One Yks HD"
    status = await call(served, "get_box_status", {"box": "lounge"})
    assert status.structured_content["power"] == "on" and status.structured_content["model"] == "ES340d8UKIE"
    assert status.structured_content["hostname"] == "localhost"
    for secret in SENSITIVE.values():
        assert secret not in json.dumps(status.structured_content) + text(status)


async def test_power_tools(served):
    off = await call(served, "power_off")
    assert off.structured_content["is_on"] is False and served.fake.standby
    on = await call(served, "power_on")
    assert on.structured_content["is_on"] is True and not served.fake.standby


async def test_press_tools(served):
    r = await call(served, "navigate", {"direction": "down", "count": 2, "select": True})
    assert r.structured_content["pressed"] == ["down", "down", "select"]
    await asyncio.sleep(0.05)
    assert served.fake.received[-3:] == [17, 17, 1]


async def test_errors_are_readable(served):
    unsafe = await call(served, "press_button", {"button": "code:224"})
    assert unsafe.is_error and "unpair your remote" in text(unsafe)
    missing = await call(served, "change_channel", {"channel": "999"})
    assert missing.is_error and "no channel 999" in text(missing)
    no_box = await call(served, "get_now_playing", {"box": "bedroom"})
    assert no_box.is_error and "no saved box called 'bedroom'" in text(no_box)
    bad_arg = await call(served, "press_button", {"button": "i", "repeat": 500})
    assert bad_arg.is_error


async def test_box_management_tools(served):
    listed = await call(served, "list_boxes")
    assert listed.structured_content["default"] == "lounge"
    renamed = await call(served, "rename_box", {"alias": "lounge", "new_alias": "living-room"})
    assert renamed.structured_content["alias"] == "living-room"
    assert BoxRegistry(served.registry.path).get().alias == "living-room"
    dup = await call(served, "save_box", {"alias": "again", "address": "127.0.0.1"})
    assert dup.is_error  # fake isn't on the default ports, so it can't be confirmed there


async def test_list_channels_tool(served):
    r = await call(served, "list_channels", {"search": "bbc", "include_radio": False})
    names = [c["name"] for c in r.structured_content["channels"]]
    assert names == ["BBC One Yks HD", "BBC Two HD", "BBC One Lon"]


async def test_list_recordings_tool(served):
    r = await call(served, "list_recordings", {"search": "office"})
    assert not r.is_error, text(r)
    data = r.structured_content
    assert data["storage"]["percent_used"] == 98.1
    assert [x["status"] for x in data["recordings"]] == ["SCHEDULED", "RECORDING"]  # newest first
    failed = await call(served, "list_recordings", {"status": "failed"})
    assert [x["title"] for x in failed.structured_content["recordings"]] == ["Live League 2: Harrogate v Bromley"]
    everything = await call(served, "list_recordings", {"limit": 10})
    assert everything.structured_content["total_matching"] == 250
    assert len(everything.structured_content["recordings"]) == 10


async def test_app_tools(served):
    listed = await call(served, "list_apps")
    titles = [a["title"] for a in listed.structured_content["apps"]]
    assert "Apple TV" in titles and "EPG_UI" not in titles and listed.structured_content["on_screen"] is None
    opened = await call(served, "open_app", {"app": "Apple TV"})
    assert not opened.is_error, text(opened)
    assert opened.structured_content["verified"] is True and served.fake.foreground_app == "apple.tv"
    now = await call(served, "get_now_playing")
    assert now.structured_content["app_on_screen"]["app_id"] == "apple.tv"
    status = await call(served, "get_box_status")
    assert status.structured_content["app_on_screen"] == "Apple TV"
    closed = await call(served, "close_app")
    assert closed.structured_content["app"]["app_id"] == "apple.tv" and served.fake.foreground_app is None
    blocked = await call(served, "open_app", {"app": "NBA"})
    assert blocked.is_error and "blocked" in text(blocked)


async def test_menu_tools(served):
    routes = await call(served, "list_menu_routes")
    recs = next(r for r in routes.structured_content["routes"] if r["destination"] == "recordings")
    assert recs["confirmed_on_this_box"] is False
    went = await call(served, "navigate_to", {"destination": "recordings"})
    assert went.structured_content["pressed"] == ["tvguide", "down", "down", "select"]
    assert "Unverified" in went.structured_content["note"]
    saved = await call(served, "save_menu_route", {"destination": "Recordings",
                                                   "keys": ["home", "down", "down", "down", "select"]})
    assert saved.structured_content["saved"] == "recordings"
    went = await call(served, "navigate_to", {"destination": "recordings"})
    assert went.structured_content["confirmed_on_this_box"] is True and went.structured_content["note"] is None
    assert served.registry.get().menu_routes["recordings"][3] == "down"
    unsafe = await call(served, "save_menu_route", {"destination": "pair", "keys": ["code:224"]})
    assert unsafe.is_error
    gone = await call(served, "forget_menu_route", {"destination": "recordings"})
    assert gone.structured_content["forgotten"] == "recordings"


async def test_guide_tools(served):
    now = await call(served, "get_now_playing")
    assert now.structured_content["programme"]["title"].startswith("BBC One Yks HD Show")
    assert now.structured_content["next_programme"] is not None
    listing = await guide_call(served, "tv_listings", {"channel": "channel 4", "day": "today"})
    assert not listing.is_error, text(listing)
    titles = [p["title"] for p in listing.structured_content["programmes"]]
    assert "Gogglebox" in titles and len(titles) == 24
    assert sum(p["on_now"] for p in listing.structured_content["programmes"]) == 1
    later = await guide_call(served, "tv_listings", {"channel": "104", "from_time": "21:00"})
    assert later.structured_content["programmes"][0]["title"] == "Gogglebox"
    on = await guide_call(served, "whats_on", {"channels": ["101", "Channel 4"], "at": "21:30"})
    rows = on.structured_content["channels"]
    assert [r["channel"]["number"] for r in rows] == ["101", "104"] and rows[1]["now"]["title"] == "Gogglebox"
    found = await guide_call(served, "find_programme", {"query": "gogglebox", "days": 2})
    assert found.structured_content["matches"][0]["channel"]["name"] == "Channel 4 HD"
    bad = await guide_call(served, "tv_listings", {"channel": "101", "day": "next year"})
    assert bad.is_error and "couldn't understand the day" in text(bad)
    for r in (now, listing, on, found):
        assert SENSITIVE["householdToken"] not in json.dumps(r.structured_content)


async def test_booking_tools(served):
    listing = await guide_call(served, "tv_listings", {"channel": "104", "day": "tomorrow", "from_time": "21:00"})
    goggle = listing.structured_content["programmes"][0]
    booked = await call(served, "record_programme", {"channel": "104", "event_id": goggle["event_id"]})
    assert not booked.is_error, text(booked)
    assert booked.structured_content["action"] in ("booked", "already_booked")
    pvr = booked.structured_content["recording"]["pvr_id"]
    by_time = await call(served, "record_programme", {"channel": "Channel 4", "day": "tomorrow",
                                                       "start_time": "21:30", "series": True})
    assert by_time.structured_content["action"] == "series_linked"
    scheduled = await call(served, "list_recordings", {"status": "scheduled", "search": "gogglebox"})
    assert pvr in [r["pvr_id"] for r in scheduled.structured_content["recordings"]]
    cancelled = await call(served, "cancel_recording", {"recording": pvr, "whole_series": True})
    assert cancelled.structured_content["action"] == "cancelled" and cancelled.structured_content["verified"]
    finished = await call(served, "cancel_recording", {"recording": "P290262cd"})
    assert finished.is_error and "not scheduled" in text(finished)



async def test_recording_a_finished_programme_says_so(served):
    from datetime import datetime, timezone

    listing = await guide_call(served, "tv_listings", {"channel": "101", "day": "yesterday"
                                                 if datetime.now(timezone.utc).hour < 2 else "today"})
    done = next(p for p in listing.structured_content["programmes"]
                if datetime.fromisoformat(p["end"]) < datetime.now(timezone.utc))
    r = await call(served, "record_programme", {"channel": "101", "event_id": done["event_id"]})
    assert r.is_error and "already finished" in text(r)



async def test_guide_tools_ask_for_own_lookup_first(served):
    for tool, args in [("tv_listings", {"channel": "101"}), ("whats_on", {}),
                       ("find_programme", {"query": "gogglebox"})]:
        first = await call(served, tool, args)
        data = first.structured_content
        assert not first.is_error and data["status"] == "use_your_own_listings_first"
        assert "web search" in data["message"] and "programmes" not in data and "matches" not in data
        wrong = await call(served, tool, {**args, "fallback_code": "nope"})
        assert wrong.structured_content["status"] == "use_your_own_listings_first"
        ok = await call(served, tool, {**args, "fallback_code": data["fallback_code"].upper()})
        assert "status" not in ok.structured_content


async def test_gate_replies_dont_fetch_the_guide(served):
    await call(served, "find_programme", {"query": "anything"})
    assert served.fake.guide_requests == []


async def test_guide_gate_can_be_switched_off(fake, tmp_path):
    registry = BoxRegistry(tmp_path / "state.json")
    registry.add(BoxRecord(alias="lounge", hostname="localhost", last_ip="127.0.0.1", id=CONTROL_UDN,
                           key_gap_ms=60, **fake.ports))
    manager = BoxManager(registry, timing=FAST, allow_loopback=True,
                         epg_base_url=f"http://127.0.0.1:{fake.http_port}/as/schedule")
    config = Config(host="127.0.0.1", data_dir=tmp_path, allowed_hosts=["127.0.0.1"], guide_gate=False)
    with ServerThread(build_app(config, [TOKEN], manager=manager)) as srv:
        r = await call(srv, "tv_listings", {"channel": "101"})
        assert "status" not in r.structured_content and r.structured_content["programmes"]



async def test_gate_names_a_listings_site(served):
    first = await call(served, "whats_on", {})
    msg = first.structured_content["message"]
    assert "tvguide.co.uk" in msg and "/on-now" in msg and "/channel/" in msg


async def test_custom_listings_hint(fake, tmp_path):
    from skyq_mcp.server import build_server

    hint = "Fetch https://www.tvspielfilm.de/tv-programm/ for today's listings."
    server = build_server(BoxManager(BoxRegistry(tmp_path / "s.json")),
                          Config(data_dir=tmp_path, listings_hint=hint))
    assert hint in server.instructions and "tvguide.co.uk" not in server.instructions
    r = await server.call_tool("tv_listings", {"channel": "101"})
    data = r.structured_content if hasattr(r, "structured_content") else r
    assert hint in data["message"]
