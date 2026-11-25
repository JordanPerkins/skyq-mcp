"""Command line: `skyq-mcp serve | token | discover | boxes`."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import sys
from pathlib import Path

from .config import Config, generate_token, read_token_file, resolve_tokens, write_token_file
from .manager import BoxManager
from .skyq.errors import SkyQError
from .store import BoxRegistry

log = logging.getLogger("skyq_mcp")


def _config(args) -> Config:
    cfg = Config.from_env()
    if getattr(args, "data_dir", None):
        cfg.data_dir = Path(args.data_dir).expanduser()
    for attr in ("host", "port", "ssl_certfile", "ssl_keyfile", "log_level"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    if getattr(args, "allow_unsafe_keys", False):
        cfg.allow_unsafe_keys = True
    if getattr(args, "allowed_host", None):
        cfg.allowed_hosts = args.allowed_host
    if getattr(args, "iface", None):
        cfg.discovery_iface = args.iface
    return cfg


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def cmd_serve(args) -> int:
    import uvicorn

    from .server import build_app

    cfg = _config(args)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if cfg.log_level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request to the box
    if bool(cfg.ssl_certfile) != bool(cfg.ssl_keyfile):
        log.error("TLS needs both a certificate file and a key file")
        return 2
    try:
        tokens, new_token = resolve_tokens(cfg)
    except ValueError as e:
        log.error("%s", e)
        return 2
    if new_token:
        if sys.stderr.isatty():
            print(
                f"\nNo token was configured, so one was generated and saved to {cfg.token_path}:\n\n"
                f"    {new_token}\n\n"
                "Give it to your MCP client as an 'Authorization: Bearer <token>' header.\n"
                "It won't be shown again; run `skyq-mcp token --show` to see it later.\n",
                file=sys.stderr, flush=True,
            )
        else:  # e.g. under systemd: don't put the secret in the system log
            log.warning("no token was configured, so one was generated and saved to %s; "
                        "run `skyq-mcp token --show` as the service user to see it", cfg.token_path)
    tls = bool(cfg.ssl_certfile)
    if not tls and not _is_loopback(cfg.host):
        log.warning("serving over plain HTTP on %s: anyone on your network who can see the traffic "
                    "could read the token. Consider --ssl-certfile/--ssl-keyfile.", cfg.host)
    app = build_app(cfg, tokens)
    scheme = "https" if tls else "http"
    log.info("MCP endpoint: %s://%s:%d/mcp", scheme, cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, ssl_certfile=cfg.ssl_certfile, ssl_keyfile=cfg.ssl_keyfile,
                log_level=cfg.log_level.lower(), proxy_headers=False, server_header=False,
                limit_concurrency=64, timeout_keep_alive=5, ws="none")
    return 0


def cmd_token(args) -> int:
    cfg = _config(args)
    if args.generate:
        if cfg.token_path.exists() and not args.force:
            print(f"{cfg.token_path} already exists; add --force to replace it "
                  "(clients using the old token will stop working).", file=sys.stderr)
            return 1
        token = generate_token()
        write_token_file(cfg.token_path, token)
        print(token)
        print(f"saved to {cfg.token_path}; restart the server to use it", file=sys.stderr)
        return 0
    if cfg.tokens:
        print("the token comes from the SKYQ_MCP_TOKEN / SKYQ_MCP_TOKENS environment variable", file=sys.stderr)
        if args.show:
            print("\n".join(cfg.tokens))
        return 0
    tokens = read_token_file(cfg.token_path)
    if not tokens:
        print(f"no token yet; run `skyq-mcp token --generate` (or start the server, which makes one)",
              file=sys.stderr)
        return 1
    if args.show:
        print("\n".join(tokens))
    else:
        print(f"a token is saved in {cfg.token_path}; add --show to print it", file=sys.stderr)
    return 0


def _manager(cfg: Config) -> BoxManager:
    return BoxManager(BoxRegistry(cfg.state_path), allow_unsafe=cfg.allow_unsafe_keys)


def cmd_discover(args) -> int:
    cfg = _config(args)
    found = asyncio.run(_manager(cfg).discover(timeout=args.timeout, iface=cfg.discovery_iface, subnet=args.subnet))
    print(json.dumps(found, indent=2))
    if not found:
        print("no boxes found; try --iface <this machine's IP> or --subnet 192.168.1.0/24", file=sys.stderr)
    return 0 if found else 1


def cmd_boxes(args) -> int:
    cfg = _config(args)
    manager = _manager(cfg)
    reg = manager.registry
    if args.action == "save":
        if not args.alias or not args.address:
            print("usage: skyq-mcp boxes save ALIAS HOSTNAME_OR_IP", file=sys.stderr)
            return 2
        rec, warning = asyncio.run(manager.save(args.alias, args.address))
        print(f"saved {rec.model or 'box'} as '{rec.alias}': {rec.hostname or 'no hostname'} (currently {rec.last_ip})")
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
    elif args.action == "forget":
        print(f"forgot '{reg.remove(args.alias).alias}'")
    elif args.action == "default":
        print(f"default is now '{reg.set_default(args.alias).alias}'")
    else:
        if not reg.boxes:
            print("no boxes saved; run `skyq-mcp discover` then `skyq-mcp boxes save ALIAS HOSTNAME`")
        for a in sorted(reg.boxes):
            b = reg.boxes[a]
            mark = "*" if a == (reg.default or a) else " "
            print(f"{mark} {a:<20} {b.hostname or '-':<24} {b.last_ip or '-':<16} {b.model or ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="skyq-mcp", description="MCP server for Sky Q boxes")
    p.add_argument("--data-dir", help="where saved boxes and the token live (default ~/.local/share/skyq-mcp)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--host", help="address to listen on (default 0.0.0.0)")
    s.add_argument("--port", type=int, help="port (default 8765)")
    s.add_argument("--ssl-certfile")
    s.add_argument("--ssl-keyfile")
    s.add_argument("--allowed-host", action="append",
                   help="extra hostname clients use to reach this server, e.g. a Tailscale *.ts.net name "
                        "(repeatable); this machine's own names are always allowed")
    s.add_argument("--allow-unsafe-keys", action="store_true", help="allow key codes 223-239 (remote pairing)")
    s.add_argument("--iface", help="local IP to send discovery multicast from")
    s.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("token", help="show or create the bearer token")
    s.add_argument("--generate", action="store_true", help="create a new token")
    s.add_argument("--force", action="store_true", help="replace an existing token")
    s.add_argument("--show", action="store_true", help="print the current token")
    s.set_defaults(func=cmd_token)

    s = sub.add_parser("discover", help="find Sky Q boxes on the network")
    s.add_argument("--timeout", type=float, default=3.0)
    s.add_argument("--subnet", help="scan this subnet instead of using multicast")
    s.add_argument("--iface")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("boxes", help="list, save or forget boxes")
    s.add_argument("action", nargs="?", default="list", choices=["list", "save", "forget", "default"])
    s.add_argument("alias", nargs="?")
    s.add_argument("address", nargs="?", help="hostname (preferred) or IP")
    s.set_defaults(func=cmd_boxes)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except SkyQError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
