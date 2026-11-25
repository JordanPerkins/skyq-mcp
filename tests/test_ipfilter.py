import pytest

from skyq_mcp.ipfilter import ClientIPFilter, client_address, parse_networks

ANTHROPIC = parse_networks(["160.79.104.0/21", "153.61.208.0/20"])
LOOPBACK = parse_networks(["127.0.0.1", "::1"])


def scope(peer, xff=None, kind="http"):
    headers = [(b"host", b"pi.example.ts.net")]
    for v in ([xff] if isinstance(xff, str) else xff or []):
        headers.append((b"x-forwarded-for", v.encode()))
    return {"type": kind, "client": (peer, 50000), "headers": headers, "path": "/mcp", "method": "POST"}


def test_parse_networks():
    assert [str(n) for n in parse_networks(["160.79.104.0/21", " 192.168.1.10 ", "", "::1"])] == \
        ["160.79.104.0/21", "192.168.1.10/32", "::1/128"]
    with pytest.raises(ValueError, match="isn't an IP"):
        parse_networks(["anthropic.com"])


@pytest.mark.parametrize("peer,xff,trusted,expected", [
    ("192.168.1.50", None, LOOPBACK, "192.168.1.50"),             # direct LAN client
    ("192.168.1.50", "160.79.104.9", LOOPBACK, "192.168.1.50"),   # forged header from untrusted peer: ignored
    ("127.0.0.1", "160.79.104.9", LOOPBACK, "160.79.104.9"),      # Funnel: real client from the header
    ("127.0.0.1", "160.79.104.9", [], "127.0.0.1"),               # no trusted proxies configured: header ignored
    ("127.0.0.1", "8.8.8.8, 160.79.104.9", LOOPBACK, "160.79.104.9"),  # rightmost hop wins
    ("127.0.0.1", ["160.79.104.9", "203.0.113.7"], LOOPBACK, "203.0.113.7"),  # multiple headers: last one
    ("127.0.0.1", None, LOOPBACK, "127.0.0.1"),                    # local request, no header
    ("127.0.0.1", "127.0.0.1", LOOPBACK, "127.0.0.1"),             # only proxies in the chain
    ("::ffff:160.79.104.9", None, [], "160.79.104.9"),             # IPv4-mapped IPv6
    ("127.0.0.1", "not-an-ip", LOOPBACK, None),                    # malformed: no address
])
def test_client_address(peer, xff, trusted, expected):
    got = client_address(scope(peer, xff), trusted)
    assert (str(got) if got else None) == expected


class Recorder:
    def __init__(self):
        self.reached, self.sent = [], []

    async def app(self, scope, receive, send):
        self.reached.append(scope["type"])

    async def send(self, message):
        self.sent.append(message)

    def statuses(self):
        return [m["status"] for m in self.sent if m["type"] == "http.response.start"]


async def test_filter_allows_anthropic_via_proxy():
    r = Recorder()
    await ClientIPFilter(r.app, ANTHROPIC, LOOPBACK)(scope("127.0.0.1", "160.79.105.200"), None, r.send)
    assert r.reached == ["http"] and r.sent == []


@pytest.mark.parametrize("peer,xff", [
    ("127.0.0.1", "8.8.8.8"),        # via the proxy, but not an allowed address
    ("192.168.1.50", "160.79.104.9"),  # forged header from a client that isn't a trusted proxy
    ("127.0.0.1", None),             # the proxy itself, with no client address
    ("127.0.0.1", "garbage"),        # malformed header
])
async def test_filter_refuses(peer, xff):
    r = Recorder()
    await ClientIPFilter(r.app, ANTHROPIC, LOOPBACK)(scope(peer, xff), None, r.send)
    assert r.reached == [] and r.statuses() == [403]


async def test_filter_closes_websockets_and_passes_lifespan():
    r = Recorder()
    f = ClientIPFilter(r.app, ANTHROPIC, LOOPBACK)
    await f(scope("127.0.0.1", "8.8.8.8", kind="websocket"), None, r.send)
    assert r.sent == [{"type": "websocket.close", "code": 1008}]
    await f({"type": "lifespan"}, None, r.send)
    assert r.reached == ["lifespan"]


def test_filter_needs_networks():
    with pytest.raises(ValueError):
        ClientIPFilter(None, [])
