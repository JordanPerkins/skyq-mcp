"""Sky Q remote key codes.

Codes come from community implementations and were confirmed on an ES340 (Q370 firmware)
during Phase 0 (see PLAN.md §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidKey, UnsafeKey


@dataclass(frozen=True)
class Key:
    name: str
    code: int
    description: str
    group: str


_KEY_LIST: list[Key] = [
    Key("power", 0, "Toggle power / standby", "power"),
    Key("select", 1, "Select / OK", "navigation"),
    Key("backup", 2, "Back / dismiss", "navigation"),
    Key("channelup", 6, "Channel up", "channel"),
    Key("channeldown", 7, "Channel down", "channel"),
    Key("interactive", 8, "Interactive / sidebar", "navigation"),
    Key("help", 9, "Help", "navigation"),
    Key("services", 10, "Services / search", "navigation"),
    Key("tvguide", 11, "TV guide / home", "navigation"),
    Key("channelapp", 12, "Open the current channel's app (e.g. iPlayer on BBC, ITVX on ITV)", "navigation"),
    Key("i", 14, "Info banner", "navigation"),
    Key("text", 15, "Text / subtitles", "navigation"),
    Key("up", 16, "Up", "dpad"),
    Key("down", 17, "Down", "dpad"),
    Key("left", 18, "Left", "dpad"),
    Key("right", 19, "Right", "dpad"),
    Key("red", 32, "Red", "colour"),
    Key("green", 33, "Green", "colour"),
    Key("yellow", 34, "Yellow", "colour"),
    Key("blue", 35, "Blue", "colour"),
    *[Key(str(d), 48 + d, f"Digit {d}", "digit") for d in range(10)],
    Key("play", 64, "Play", "transport"),
    Key("pause", 65, "Pause", "transport"),
    Key("stop", 66, "Stop", "transport"),
    Key("record", 67, "Record", "transport"),
    Key("fastforward", 69, "Fast forward", "transport"),
    Key("rewind", 71, "Rewind", "transport"),
    Key("boxoffice", 240, "Sky Box Office", "other"),
    Key("sky", 241, "Sky button", "other"),
]

KEYS: dict[str, Key] = {k.name: k for k in _KEY_LIST}
BY_CODE: dict[int, Key] = {k.code: k for k in _KEY_LIST}

ALIASES: dict[str, str] = {
    "ok": "select",
    "back": "backup",
    "dismiss": "backup",
    "sidebar": "interactive",
    "search": "services",
    "home": "tvguide",
    "guide": "tvguide",
    "info": "i",
    "subtitles": "text",
    "ff": "fastforward",
    "rew": "rewind",
    "chup": "channelup",
    "chdown": "channeldown",
    "apps": "channelapp",
}

# Phase 0: codes 223-239 open remote-control setup / pairing screens, which could unpair
# the household's physical remote. Refused unless explicitly allowed.
UNSAFE_CODES = frozenset(range(223, 240))


def resolve_key(token: str | int, *, allow_unsafe: bool = False) -> int:
    """Turn a key name, alias, digit or raw code into a key code.

    Accepts: "select", "OK", "5" (the digit key), "code:12" / "#12" (raw code), or an int (raw code).
    """
    if isinstance(token, bool):
        raise InvalidKey(f"invalid key {token!r}")
    if isinstance(token, int):
        code = token
    else:
        t = token.strip().lower()
        if t.startswith("code:") or t.startswith("#"):
            raw = t[5:] if t.startswith("code:") else t[1:]
            try:
                code = int(raw, 0)
            except ValueError:
                raise InvalidKey(f"invalid raw key code {token!r}") from None
        else:
            name = ALIASES.get(t, t)
            if name not in KEYS:
                raise InvalidKey(f"unknown key {token!r}; use list_buttons to see valid names")
            return KEYS[name].code
    if not 0 <= code <= 255:
        raise InvalidKey(f"key code {code} is out of range (0-255)")
    if code in UNSAFE_CODES and not allow_unsafe:
        raise UnsafeKey(
            f"key code {code} opens remote-control pairing screens and could unpair your remote; "
            "it's blocked unless the server is started with unsafe keys allowed"
        )
    return code


def key_name(code: int) -> str:
    key = BY_CODE.get(code)
    return key.name if key else f"code:{code}"
