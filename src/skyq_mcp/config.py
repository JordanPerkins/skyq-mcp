"""Server settings, from environment variables with command-line overrides."""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .store import default_data_dir

DEFAULT_PORT = 8765

# Where agents should look up listings before falling back to Sky's guide (UK; checked 2026-11-26).
# A general web search tends to find a show's regular weekly slot, not tonight's schedule.
DEFAULT_LISTINGS_HINT = (
    "Fetch the schedule pages of https://www.tvguide.co.uk directly: /on-now, /now-and-next or "
    "/on-tonight for what's on across channels, and /channel/<channel-name> for one channel's "
    "listings (e.g. /channel/channel-4-hd, /channel/4seven-hd, /channel/sky-atlantic-hd). A general "
    "web search usually finds only a show's regular weekly slot, not today's schedule, and the "
    "site's search page needs a browser, so use those pages."
)
MIN_TOKEN_LENGTH = 24
TOKEN_FILE = "token"


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    data_dir: Path = field(default_factory=default_data_dir)
    tokens: list[str] = field(default_factory=list)
    allow_unsafe_keys: bool = False
    allowed_hosts: list[str] = field(default_factory=list)  # extra names, added to this machine's own
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    discovery_iface: str | None = None
    log_level: str = "INFO"
    guide_gate: bool = True  # listings tools ask the agent to try its own lookup first
    listings_hint: str = DEFAULT_LISTINGS_HINT  # how agents should look up listings themselves
    allowed_clients: list[str] = field(default_factory=list)  # empty: any address (token still needed)
    trusted_proxies: list[str] = field(default_factory=list)  # e.g. 127.0.0.1 behind Tailscale Funnel

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        tokens = _list(env.get("SKYQ_MCP_TOKENS")) or _list(env.get("SKYQ_MCP_TOKEN"))
        return cls(
            host=env.get("SKYQ_MCP_HOST", "0.0.0.0"),
            port=int(env.get("SKYQ_MCP_PORT", DEFAULT_PORT)),
            data_dir=default_data_dir(),
            tokens=tokens,
            allow_unsafe_keys=_bool(env.get("SKYQ_MCP_ALLOW_UNSAFE_KEYS")),
            allowed_hosts=_list(env.get("SKYQ_MCP_ALLOWED_HOSTS")),
            ssl_certfile=env.get("SKYQ_MCP_SSL_CERTFILE") or None,
            ssl_keyfile=env.get("SKYQ_MCP_SSL_KEYFILE") or None,
            discovery_iface=env.get("SKYQ_MCP_IFACE") or None,
            log_level=env.get("SKYQ_MCP_LOG_LEVEL", "INFO").upper(),
            guide_gate=not _bool(env.get("SKYQ_MCP_NO_GUIDE_GATE")),
            listings_hint=(env.get("SKYQ_MCP_LISTINGS_HINT") or "").strip() or DEFAULT_LISTINGS_HINT,
            allowed_clients=_list(env.get("SKYQ_MCP_ALLOWED_CLIENTS")),
            trusted_proxies=_list(env.get("SKYQ_MCP_TRUSTED_PROXIES")),
        )

    @property
    def token_path(self) -> Path:
        return self.data_dir / TOKEN_FILE

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"


# -- tokens --------------------------------------------------------------------------


def read_token_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def check_tokens(tokens: list[str]) -> None:
    for t in tokens:
        if len(t) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"a configured token is only {len(t)} characters; use at least {MIN_TOKEN_LENGTH} "
                "(run `skyq-mcp token --generate` for a strong one)"
            )
        if len(set(t)) < 10 or any(c.isspace() for c in t):
            raise ValueError("a configured token looks weak or contains spaces; "
                             "run `skyq-mcp token --generate` for a strong one")


def resolve_tokens(config: Config) -> tuple[list[str], str | None]:
    """Tokens from the environment, else the token file, else a new one written to the file.

    Returns (tokens, newly_generated_token_or_None).
    """
    if config.tokens:
        check_tokens(config.tokens)
        return config.tokens, None
    from_file = read_token_file(config.token_path)
    if from_file:
        check_tokens(from_file)
        return from_file, None
    token = generate_token()
    write_token_file(config.token_path, token)
    return [token], token


# -- allowed Host headers (DNS-rebinding protection) ---------------------------------


def lan_ip() -> str | None:
    """This machine's LAN address (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def default_allowed_hosts() -> list[str]:
    names = {"localhost", "127.0.0.1", "[::1]"}
    hostname = socket.gethostname()
    if hostname:
        names.add(hostname)
        names.add(hostname.split(".")[0] + ".local")
    ip = lan_ip()
    if ip:
        names.add(ip)
    return sorted(names)


def host_patterns(hosts: list[str]) -> list[str]:
    out: list[str] = []
    for h in hosts:
        out += [h] if ":" in h and not h.startswith("[") else [h, f"{h}:*"]
    return out
