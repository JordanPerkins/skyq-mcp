"""Menu navigation: the last-resort way to reach a screen when there's no direct command.

Sky Q menus differ by software version, region, subscription and the household's own settings,
and the server can't see the screen. So:
- the built-in routes below are starting guesses, all marked unverified;
- once the user confirms a route works, it's saved for that box and used from then on.
Apps should be opened with open_app and channels with change_channel, never through menus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SkyQError
from .keys import resolve_key

DESTINATION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_ROUTE_KEYS = 30
MENU_GAP_MS = 400  # menus animate; slower than channel digits

# Unverified guesses at the Sky Q Home menu. Each starts from Home so it doesn't depend on
# where the box was. Correct ones get saved per box with save_menu_route.
BUILT_IN: dict[str, tuple[list[str], str]] = {
    "home": (["home"], "The Home menu"),
    "search": (["home", "select"], "Search (assumed first item on the Home menu)"),
    "tv-guide": (["home", "down", "select"], "TV Guide (assumed second item on the Home menu)"),
    "recordings": (["home", "down", "down", "select"], "Recordings (assumed third item on the Home menu)"),
    "settings": (["home", "up", "select"], "Settings (assumed last item, reached by wrapping up from the top)"),
}


@dataclass(frozen=True)
class Route:
    destination: str
    keys: list[str]
    description: str
    confirmed: bool  # True: the user confirmed it on this box

    def to_dict(self) -> dict:
        return {"destination": self.destination, "keys": self.keys, "description": self.description,
                "confirmed_on_this_box": self.confirmed}


def normalise_destination(value: str) -> str:
    d = re.sub(r"[\s_]+", "-", value.strip().lower())
    if not DESTINATION_RE.match(d):
        raise SkyQError(f"invalid destination name '{value[:40]}': use letters, digits and hyphens")
    return d


def validate_route(keys: list[str], *, allow_unsafe: bool = False) -> list[str]:
    if not keys:
        raise SkyQError("a route needs at least one key")
    if len(keys) > MAX_ROUTE_KEYS:
        raise SkyQError(f"a route can have at most {MAX_ROUTE_KEYS} keys")
    for k in keys:
        resolve_key(k, allow_unsafe=allow_unsafe)  # raises on unknown or unsafe keys
    return [str(k).strip().lower() for k in keys]


def all_routes(saved: dict[str, list[str]]) -> list[Route]:
    routes = {d: Route(d, keys, desc, False) for d, (keys, desc) in BUILT_IN.items()}
    for d, keys in saved.items():
        desc = BUILT_IN[d][1] if d in BUILT_IN else d.replace("-", " ").capitalize()
        routes[d] = Route(d, list(keys), desc, True)
    return sorted(routes.values(), key=lambda r: r.destination)


def find_route(destination: str, saved: dict[str, list[str]]) -> Route:
    d = normalise_destination(destination)
    for r in all_routes(saved):
        if r.destination == d:
            return r
    known = ", ".join(r.destination for r in all_routes(saved))
    raise SkyQError(f"no menu route for '{destination}'. Known: {known}. For apps use open_app; "
                    "for channels use change_channel; otherwise navigate with the arrow keys and save "
                    "the route once it works.")
