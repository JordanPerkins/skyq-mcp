"""Errors raised by the Sky Q client library.

Messages are written to be shown to an MCP client as-is, so they say what went wrong and
what to do next, and never include sensitive box data.
"""


class SkyQError(Exception):
    """Base class for all library errors."""


class BoxUnreachable(SkyQError):
    """The box didn't answer on the network (off at the wall, wrong IP, or not on this LAN)."""


class ProtocolError(SkyQError):
    """The box answered, but not in the way the protocol expects."""


class InvalidKey(SkyQError):
    """Unknown key name or out-of-range key code."""


class UnsafeKey(SkyQError):
    """Key code in the remote-pairing range, which could unpair the physical remote."""


class ChannelNotFound(SkyQError):
    """No channel matches the requested number or name."""


class AmbiguousChannel(SkyQError):
    """A channel name matches several channels."""

    def __init__(self, query: str, candidates: list[str]):
        self.query = query
        self.candidates = candidates
        shown = ", ".join(candidates[:10]) + (" …" if len(candidates) > 10 else "")
        super().__init__(f"'{query}' matches several channels: {shown}. Use the channel number.")


class ChannelChangeFailed(SkyQError):
    """Digits were sent but the box didn't end up on the requested channel."""


class BoxNotFound(SkyQError):
    """No saved box with that alias (or no boxes saved yet)."""


class RegistryError(SkyQError):
    """Invalid change to the saved-box registry (bad alias, duplicate, …)."""
