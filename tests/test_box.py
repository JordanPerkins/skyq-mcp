import asyncio

import pytest

from conftest import box_for
from skyq_mcp.skyq.box import parse_uri
from skyq_mcp.skyq.errors import ChannelChangeFailed, UnsafeKey


def test_parse_uri():
    assert parse_uri("xsi://BC9") == ("live", "3017")
    assert parse_uri("file://pvr/290262CD") == ("recording", "290262CD")
    assert parse_uri("xsi://zz") is None
    assert parse_uri(None) is None


async def test_change_channel_by_number(fake):
    result = await box_for(fake).change_channel(102)
    assert result.verified and result.attempts == 1 and not result.woke_box
    assert fake.channel == "102"
    assert fake.received[0] == 2  # backup first, to clear any half-typed number
    assert fake.received[-3:] == [49, 48, 50]


async def test_stray_digit_is_cleared(make_fake):
    # Phase 0: a leftover "1" turned "102" into "110". The clear step prevents it.
    fake = make_fake(stray_digits="1")
    result = await box_for(fake).change_channel("102")
    assert fake.channel == "102" and result.attempts == 1


async def test_change_channel_by_name_and_radio(fake):
    box = box_for(fake)
    assert (await box.change_channel("channel 4")).channel.number == "104"
    assert (await box.change_channel("Radio X")).channel.number == "0111"
    assert fake.channel == "0111"


async def test_already_on_channel_sends_nothing(fake):
    result = await box_for(fake).change_channel("101")
    assert result.already_on and fake.received == []


async def test_retries_with_longer_gap_when_digits_dropped(make_fake):
    # Box drops digits closer than 100 ms; first attempt uses 20 ms, retry uses 120 ms.
    fake = make_fake(drop_under_ms=100)
    result = await box_for(fake, key_gap_ms=20).change_channel("102")
    assert result.verified and result.attempts == 2
    assert fake.channel == "102"


async def test_gives_up_after_two_attempts(make_fake):
    fake = make_fake(drop_under_ms=1000)
    with pytest.raises(ChannelChangeFailed, match="101"):
        await box_for(fake, key_gap_ms=20).change_channel("102")


async def test_unverified_when_upnp_unavailable(make_fake):
    fake = make_fake(upnp_broken=True)
    result = await box_for(fake).change_channel("103")
    assert result.verified is False and fake.channel == "103"


async def test_change_channel_wakes_box(make_fake):
    fake = make_fake(standby=True, power_delay=0.1)
    result = await box_for(fake).change_channel("103")
    assert result.woke_box and fake.channel == "103" and not fake.standby


async def test_power_is_idempotent(fake):
    box = box_for(fake)
    on = await box.power("on")
    assert on.pressed is False and fake.received == []
    off = await box.power("off")
    assert off.pressed and fake.standby
    assert (await box.power("off")).pressed is False
    toggled = await box.power("toggle")
    assert toggled.to_dict() == {"requested": "toggle", "was_on": False, "is_on": True, "pressed_power": True}
    assert fake.received == [0, 0]


async def test_now_playing_live_and_recording(fake):
    box = box_for(fake)
    live = await box.now_playing()
    assert live.source == "live" and live.channel.name == "BBC One Yks HD" and live.state == "PLAYING"
    fake.recording = "290262CD"
    rec = await box.now_playing()
    assert rec.source == "recording" and rec.recording.title == "Gogglebox"
    assert rec.to_dict()["recording"]["channel_name"] == "Channel 4 HD"


async def test_press_names_and_unsafe(fake):
    box = box_for(fake)
    assert await box.press(["i", "back"], repeat=2) == ["i", "backup", "i", "backup"]
    with pytest.raises(UnsafeKey):
        await box.press(["code:224"])
    await asyncio.sleep(0.05)
    assert fake.received == [14, 2, 14, 2]
    assert await box_for(fake, allow_unsafe=True).press([224]) == ["code:224"]


async def test_power_on_right_after_standby_presses_again(make_fake):
    # Phase 1 real-box finding: a power press just after entering standby is ignored.
    fake = make_fake(ignore_power_after_standby=0.2)
    box = box_for(fake)
    await box.power("off")
    result = await box.power("on")
    assert result.is_standby is False and not fake.standby
    assert fake.received.count(0) == 3  # off, ignored on, repeated on


async def test_power_gives_up_after_max_presses(make_fake):
    fake = make_fake(ignore_power_after_standby=60)
    box = box_for(fake)
    await box.power("off")
    with pytest.raises(Exception, match="3 times"):
        await box.power("on")
    assert fake.received.count(0) == 4
