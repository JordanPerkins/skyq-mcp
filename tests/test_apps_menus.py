import asyncio

import pytest

from conftest import box_for
from skyq_mcp.skyq import menus
from skyq_mcp.skyq.apps import App, AppList, AmbiguousApp, AppNotFound, foreground_app, visible_from_message
from skyq_mcp.skyq.errors import SkyQError, UnsafeKey
from fake_skyq import APPS


def app_list() -> AppList:
    return AppList([a for a in (App.from_api(d) for d in APPS) if a])


# -- finding apps -----------------------------------------------------------------------------


@pytest.mark.parametrize("query,app_id", [
    ("Apple TV", "apple.tv"), ("apple tv", "apple.tv"), ("appletv", "apple.tv"), ("apple", "apple.tv"),
    ("netflix", "Netflix"), ("iplayer", "iPlayer"), ("BBC iPlayer", "iPlayer"),
    ("Sky Sports", "com.sky.sports.six"),  # two called "Sky Sports": the one on the Apps tray wins
    ("com.bskyb.sports", "com.bskyb.sports"),  # an id picks exactly that app
])
def test_find_app(query, app_id):
    assert app_list().find(query).app_id == app_id


def test_find_app_errors():
    with pytest.raises(AmbiguousApp) as e:
        app_list().find("Sky News")  # two, neither on the tray
    assert "com.sky.news.two" in str(e.value)
    with pytest.raises(AppNotFound):
        app_list().find("Disney+")
    with pytest.raises(AppNotFound):
        app_list().find("EPG_UI")  # the TV interface isn't an app to open
    with pytest.raises(AppNotFound):
        app_list().find("com.bskyb.metricstore.titan")


def test_internal_components_hidden():
    ids = {a.app_id for a in app_list().visible()}
    assert {"apple.tv", "Netflix", "nba"} <= ids
    assert not ids & {"com.bskyb.epgui", "com.bskyb.metricstore.titan", "com.juno.fanview"}


def test_status_message_parsing():
    msg = '{"apps": [{"appId": "com.bskyb.epgui", "status": "VISIBLE"}, {"appId": "apple.tv", "status": "VISIBLE"},' \
          ' {"appId": "x", "status": "RUNNING"}, {"appId": "../evil", "status": "VISIBLE"}]}'
    assert visible_from_message(msg) == ["com.bskyb.epgui", "apple.tv"]
    assert foreground_app(["com.bskyb.epgui", "apple.tv"]) == "apple.tv"
    assert foreground_app(["com.bskyb.epgui"]) is None
    assert visible_from_message("not json") is None


# -- opening and closing on the fake box ------------------------------------------------------


async def test_open_and_close_app(fake):
    box = box_for(fake)
    assert await box.app_on_screen() is None
    opened = await box.open_app("apple tv")
    assert opened.verified and not opened.already and fake.foreground_app == "apple.tv"
    assert (await box.app_on_screen()).title == "Apple TV"
    again = await box.open_app("Apple TV")
    assert again.already and fake.app_requests.count(("launch", "apple.tv")) == 1
    closed = await box.close_app()  # whatever is on screen
    assert closed.verified and closed.app.app_id == "apple.tv" and fake.foreground_app is None
    with pytest.raises(SkyQError, match="no app is open"):
        await box.close_app()


async def test_blocked_and_pin_locked_apps(fake):
    box = box_for(fake)
    with pytest.raises(SkyQError, match="blocked"):
        await box.open_app("NBA")
    assert fake.app_requests == []
    kids = await box.open_app("Sky Kids Games")
    assert kids.verified and "PIN" in kids.note


async def test_open_app_wakes_box(make_fake):
    fake = make_fake(standby=True, power_delay=0.1)
    result = await box_for(fake).open_app("netflix")
    assert result.woke_box and result.verified and not fake.standby


async def test_open_app_not_appearing_is_an_error(make_fake):
    fake = make_fake(ignore_app_launch=True)
    with pytest.raises(SkyQError, match="didn't appear on screen"):
        await box_for(fake).open_app("netflix")


async def test_open_app_without_status_is_unverified(make_fake):
    fake = make_fake(app_status_broken=True)
    result = await box_for(fake).open_app("netflix")
    assert result.verified is False and fake.foreground_app == "Netflix"


async def test_launch_refused_by_box(fake, monkeypatch):
    box = box_for(fake)
    from skyq_mcp.skyq.apps import App as A

    async def fake_apps():
        return AppList([A("not.installed", "Ghost", False, False, True)])

    monkeypatch.setattr(box.apps, "apps", fake_apps)
    with pytest.raises(SkyQError, match="refused to launch"):
        await box.open_app("ghost")


# -- menu routes ------------------------------------------------------------------------------


def test_built_in_routes_are_unverified_and_saved_ones_win():
    routes = {r.destination: r for r in menus.all_routes({})}
    assert routes["recordings"].keys == ["home", "down", "down", "select"]
    assert routes["settings"].keys == ["home"] + ["down"] * 12 + ["select"]
    assert routes["my-sky"].keys == ["home"] + ["down"] * 14 + ["select"]
    assert routes["home"].keys == ["home"] and routes["search"].keys == ["search"]
    assert len(routes) == 16
    assert not any(r.confirmed for r in routes.values())
    saved = menus.all_routes({"recordings": ["home", "down", "down", "down", "select"], "sky-sports": ["home"]})
    by = {r.destination: r for r in saved}
    assert by["recordings"].confirmed and by["recordings"].keys[-2] == "down" and len(by["recordings"].keys) == 5
    assert by["sky-sports"].confirmed


def test_route_validation():
    assert menus.normalise_destination("TV Guide") == "tv-guide"
    with pytest.raises(SkyQError):
        menus.normalise_destination("../../etc")
    with pytest.raises(UnsafeKey):
        menus.validate_route(["home", "code:224"])
    with pytest.raises(SkyQError):
        menus.validate_route(["home"] * 31)
    with pytest.raises(SkyQError):
        menus.validate_route(["teleport"])
    with pytest.raises(SkyQError, match="open_app"):
        menus.find_route("netflix", {})
