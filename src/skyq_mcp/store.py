"""Saved boxes, persisted to a JSON file.

Boxes are keyed by a user-chosen alias ("living-room"). Each record keeps:
- hostname: how the box is normally reached, so DHCP can change its IP freely;
- last_ip: the address it was last seen at (fallback when the name can't be resolved);
- id: the UDN of its SkyControl UPnP device, used to check the right box answered and to
  find it again by searching the network if both of the above fail.
The file is written atomically (temp file + rename) with mode 0600.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from .skyq.errors import BoxNotFound, RegistryError
from .skyq.names import is_ip
from .skyq.remote import DEFAULT_GAP_MS, REMOTE_PORT
from .skyq.rest import REST_PORT
from .skyq.upnp import UPNP_PORT

STATE_VERSION = 2
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def default_data_dir() -> Path:
    env = os.environ.get("SKYQ_MCP_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "skyq-mcp"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class BoxRecord:
    alias: str
    hostname: str | None = None
    last_ip: str | None = None
    id: str | None = None
    model: str | None = None
    remote_port: int = REMOTE_PORT
    rest_port: int = REST_PORT
    upnp_port: int = UPNP_PORT
    key_gap_ms: int = DEFAULT_GAP_MS
    app_status_port: int | None = None  # None: same as rest_port (the real box)
    menu_routes: dict = field(default_factory=dict)  # destination -> keys, confirmed on this box
    added: str = field(default_factory=now_iso)
    last_seen: str | None = None

    def __post_init__(self) -> None:
        if not self.hostname and not self.last_ip:
            raise RegistryError("a box needs a hostname or an IP address")

    @property
    def address(self) -> str:
        """For display: the hostname if there is one, else the last IP."""
        return self.hostname or self.last_ip or "?"

    @classmethod
    def from_dict(cls, d: dict) -> BoxRecord:
        d = dict(d)
        if "host" in d:  # version 1 stored a single host (always an IP in practice)
            host = d.pop("host")
            if is_ip(host):
                d.setdefault("last_ip", host)
            else:
                d.setdefault("hostname", host)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


def validate_alias(alias: str) -> str:
    a = alias.strip().lower()
    if not ALIAS_RE.match(a):
        raise RegistryError(
            f"invalid alias '{alias}': use 1-32 lowercase letters, digits or hyphens, starting with a letter or digit"
        )
    return a


class BoxRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or default_data_dir() / "state.json"
        self.boxes: dict[str, BoxRecord] = {}
        self.default: str | None = None
        self._stamp: tuple | None = None
        self.load()

    # -- persistence ---------------------------------------------------------------

    def _file_stamp(self) -> tuple | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def refresh(self) -> None:
        """Reload if another process (e.g. `skyq-mcp boxes save` while the service runs)
        changed the file, so this process doesn't overwrite that change."""
        if self._file_stamp() != self._stamp:
            self.boxes, self.default = {}, None
            self.load()

    def load(self) -> None:
        self._stamp = self._file_stamp()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise RegistryError(f"couldn't read saved boxes from {self.path}: {e}") from e
        if data.get("version", 1) > STATE_VERSION:
            raise RegistryError(f"{self.path} was written by a newer version of skyq-mcp")
        self.boxes = {a: BoxRecord.from_dict({**b, "alias": a}) for a, b in data.get("boxes", {}).items()}
        self.default = data.get("defaultBox")
        if self.default not in self.boxes:
            self.default = None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "version": STATE_VERSION,
            "defaultBox": self.default,
            "boxes": {a: {k: v for k, v in b.to_dict().items() if k != "alias"} for a, b in self.boxes.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self._stamp = self._file_stamp()
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- queries -------------------------------------------------------------------

    def get(self, alias: str | None = None) -> BoxRecord:
        """The named box, or the default one. With exactly one saved box, that's the default."""
        self.refresh()
        if alias:
            a = alias.strip().lower()
            if a not in self.boxes:
                saved = ", ".join(sorted(self.boxes)) or "none"
                raise BoxNotFound(f"no saved box called '{alias}' (saved: {saved})")
            return self.boxes[a]
        if self.default:
            return self.boxes[self.default]
        if len(self.boxes) == 1:
            return next(iter(self.boxes.values()))
        if not self.boxes:
            raise BoxNotFound("no Sky Q box saved yet; run discovery and save one")
        raise BoxNotFound(f"several boxes are saved ({', '.join(sorted(self.boxes))}); say which one or set a default")

    def find_by_id(self, box_id: str) -> BoxRecord | None:
        self.refresh()
        return next((b for b in self.boxes.values() if b.id and b.id == box_id), None)

    def find_by_address(self, address: str) -> BoxRecord | None:
        self.refresh()
        a = address.strip().rstrip(".").lower()
        return next((b for b in self.boxes.values() if a in (b.hostname, b.last_ip)), None)

    # -- changes (each saves immediately) ------------------------------------------

    def add(self, record: BoxRecord) -> BoxRecord:
        self.refresh()
        record.alias = validate_alias(record.alias)
        if record.alias in self.boxes:
            raise RegistryError(f"a box called '{record.alias}' is already saved")
        if record.id and (existing := self.find_by_id(record.id)):
            raise RegistryError(f"that box is already saved as '{existing.alias}'")
        self.boxes[record.alias] = record
        if self.default is None:
            self.default = record.alias
        self.save()
        return record

    def rename(self, alias: str, new_alias: str) -> BoxRecord:
        self.refresh()
        rec = self.get(alias)
        new = validate_alias(new_alias)
        if new in self.boxes and new != rec.alias:
            raise RegistryError(f"a box called '{new}' is already saved")
        del self.boxes[rec.alias]
        if self.default == rec.alias:
            self.default = new
        rec.alias = new
        self.boxes[new] = rec
        self.save()
        return rec

    def remove(self, alias: str) -> BoxRecord:
        self.refresh()
        rec = self.get(alias)
        del self.boxes[rec.alias]
        if self.default == rec.alias:
            self.default = next(iter(sorted(self.boxes)), None)
        self.save()
        return rec

    def set_default(self, alias: str) -> BoxRecord:
        self.refresh()
        rec = self.get(alias)
        self.default = rec.alias
        self.save()
        return rec

    def update(self, alias: str, **changes) -> BoxRecord:
        self.refresh()
        rec = self.get(alias)
        for k, v in changes.items():
            if k == "alias" or not hasattr(rec, k):
                raise RegistryError(f"can't update field '{k}'")
            setattr(rec, k, v)
        self.save()
        return rec
