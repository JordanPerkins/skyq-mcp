import time

import pytest

from conftest import box_for
from skyq_mcp.skyq.errors import SkyQError


async def upcoming(box, channel="101", hours=2):
    guide = await box.guide()
    ch = (await box.rest.channels()).find(channel)
    return [p for p in await guide.programmes(ch, time.time(), time.time() + hours * 3600) if p.start > time.time()][0]


async def test_book_single_then_no_duplicate(fake):
    box = box_for(fake)
    prog = await upcoming(box)
    first = await box.record(prog)
    assert first.action == "booked" and first.verified and first.recording["status"] == "SCHEDULED"
    assert first.recording["event_id"] == prog.event_id
    again = await box.record(prog)
    assert again.action == "already_booked"
    assert [a for a, _ in fake.pvr_requests].count("bookrecording") == 1


async def test_book_series_and_upgrade_existing(fake):
    box = box_for(fake)
    prog = await upcoming(box)
    await box.record(prog)
    upgraded = await box.record(prog, series=True)
    assert upgraded.action == "series_linked" and upgraded.series
    assert ("serieslink", {"pvrid": upgraded.recording["pvr_id"]}) in fake.pvr_requests
    again = await box.record(prog, series=True)
    assert again.action == "already_booked" and again.series


async def test_find_programme_at(fake):
    box = box_for(fake)
    now = await box.find_programme_at("BBC One")
    assert now.on_at(time.time())
    prog = await upcoming(box)
    by_id = await box.find_programme_at("101", event_id=prog.event_id)
    assert by_id.event_id == prog.event_id
    with pytest.raises(SkyQError, match="no programme"):
        await box.find_programme_at("101", event_id="E9999-1")


async def test_refuses_finished_and_reports_box_refusal(fake):
    box = box_for(fake)
    guide = await box.guide()
    ch = (await box.rest.channels()).find("101")
    past = [p for p in await guide.programmes(ch, time.time() - 6 * 3600, time.time()) if p.end < time.time()][0]
    with pytest.raises(SkyQError, match="already finished"):
        await box.record(past)
    fake.refuse_bookings = True
    with pytest.raises(SkyQError, match="refused 'bookrecording'"):
        await box.record(await upcoming(box))


async def test_cancel_booking(fake):
    box = box_for(fake)
    booked = await box.record(await upcoming(box), series=True)
    pvr = booked.recording["pvr_id"]
    cancelled = await box.cancel_booking(pvr)
    assert cancelled.verified and "series link" in cancelled.note
    assert not any(i["pvrid"] == pvr for i in fake.pvr_items)

    booked = await box.record(await upcoming(box, "102"), series=True)
    whole = await box.cancel_booking(booked.recording["pvr_id"], whole_series=True)
    assert whole.series and whole.note is None
    assert ("seriesunlink", {"pvrid": booked.recording["pvr_id"]}) in fake.pvr_requests


async def test_cancel_only_scheduled(fake):
    box = box_for(fake)
    with pytest.raises(SkyQError, match="not scheduled"):
        await box.cancel_booking("P290262cd")  # a finished recording
    with pytest.raises(SkyQError, match="no recording"):
        await box.cancel_booking("P99999999")
    with pytest.raises(SkyQError, match="invalid value"):
        await box.rest.delete("P1&eraseall=1")
