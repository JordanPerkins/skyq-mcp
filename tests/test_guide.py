import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import box_for, guide_url
from fake_skyq import SENSITIVE
from skyq_mcp.skyq import epg
from skyq_mcp.skyq.errors import SkyQError
from skyq_mcp.skyq.rest import Channel, RestClient

BST = epg.LocalTime(offset=3600)
UK_WINTER = epg.LocalTime(offset=0, future_offset=3600, future_from=10**12)


def test_local_time_and_day_bounds():
    start, end = BST.day_bounds(date(2026, 7, 4))
    assert datetime.fromtimestamp(start, timezone.utc).isoformat() == "2026-07-03T23:00:00+00:00"
    assert end - start == 86400
    assert BST.iso(start) == "2026-07-04T00:00+01:00"
    # The day the clocks go forward is 23 hours long.
    change = epg.LocalTime(offset=0, future_offset=3600,
                           future_from=int(datetime(2027, 3, 28, 1, tzinfo=timezone.utc).timestamp()))
    s, e = change.day_bounds(date(2027, 3, 28))
    assert e - s == 23 * 3600


@pytest.mark.parametrize("text,expected", [
    ("today", date(2026, 11, 26)), ("tonight", date(2026, 11, 26)), ("tomorrow", date(2026, 11, 27)),
    ("saturday", date(2026, 11, 28)), ("Thu", date(2026, 11, 26)), ("wednesday", date(2026, 12, 2)),
    ("2026-11-30", date(2026, 11, 30)), (None, date(2026, 11, 26)),
])
def test_parse_day(text, expected):
    assert epg.parse_day(text, date(2026, 11, 26)) == expected  # a Thursday


@pytest.mark.parametrize("text", ["2027-01-01", "2026-11-01", "someday", "13/12"])
def test_parse_day_rejects(text):
    with pytest.raises(SkyQError):
        epg.parse_day(text, date(2026, 11, 26))


@pytest.mark.parametrize("text,hm", [("21:00", (21, 0)), ("9pm", (21, 0)), ("9.30pm", (21, 30)),
                                     ("12am", (0, 0)), ("12pm", (12, 0)), ("7", (7, 0))])
def test_parse_time(text, hm):
    ts = epg.parse_time(text, date(2026, 7, 4), BST)
    local = datetime.fromtimestamp(ts, BST.tz_at(ts))
    assert (local.hour, local.minute, local.date()) == (*hm, date(2026, 7, 4))
    assert epg.parse_time("now", date(2026, 7, 4), BST) is None
    with pytest.raises(SkyQError):
        epg.parse_time("25:00", date(2026, 7, 4), BST)


def test_search_channels_prefers_hd_and_skips_radio_and_plus_one():
    chans = [Channel("101", "BBC One HD", "1", "hd", False, False), Channel("141", "BBC One", "2", "sd", False, False),
             Channel("203", "ITV1+1", "3", "sd", True, False), Channel("0111", "Radio X", "4", None, False, True),
             Channel("401", "Sky Sports F1 UHD", "5", "uhd", False, False), Channel("406", "Sky Sports F1 HD", "6", "hd", False, False)]
    names = [c.name for c in epg.search_channels(chans)]
    assert names == ["BBC One HD", "Sky Sports F1 UHD"]
    assert "ITV1+1" in [c.name for c in epg.search_channels(chans, include_plus_one=True)]


def test_territories():
    assert [epg.territory_for(c) for c in ("GBR", "IRL", "DEU", "ITA", None, "XYZ")] == ["GB", "GB", "DE", "IT", "GB", "GB"]


async def test_country_code_never_exposes_token(fake):
    client = RestClient("127.0.0.1", port=fake.http_port)
    assert await client.country_code() == "GBR"


async def test_guide_programmes_merge_days_and_cache(fake):
    box = box_for(fake)
    guide = await box.guide()
    assert guide.territory == "GB"
    ch = (await box.rest.channels()).by_number("104")
    start, end = UK_WINTER.day_bounds(datetime.now(timezone.utc).date())
    progs = await guide.programmes(ch, start, end)
    assert len({p.event_id for p in progs}) == len(progs) == 24  # overlap from the previous day removed
    assert all(p.channel.number == "104" for p in progs)
    n = len(fake.guide_requests)
    await guide.programmes(ch, start, end)
    assert len(fake.guide_requests) == n  # cached


async def test_now_next_and_search(fake):
    box = box_for(fake)
    guide = await box.guide()
    ch = (await box.rest.channels()).by_number("101")
    now, nxt = await guide.now_next(ch)
    assert now.on_at(time.time()) and nxt.start == now.end
    chans = (await box.rest.channels()).channels
    found = await guide.search("gogglebox", chans, time.time(), time.time() + 2 * 86400)
    assert found and all(p.channel.number == "104" and p.title == "Gogglebox" for p in found)


async def test_guide_down_is_reported(fake):
    fake.guide_down = True
    box = box_for(fake)
    ch = (await box.rest.channels()).by_number("101")
    with pytest.raises(epg.GuideUnavailable):
        await (await box.guide()).now_next(ch)
    playing = await box.now_playing()  # still works without the guide
    assert playing.channel.number == "101" and playing.programme is None and "guide" in playing.guide_note.lower()
