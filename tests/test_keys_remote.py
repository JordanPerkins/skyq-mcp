import asyncio
import socket
import time

import pytest

from skyq_mcp.skyq.errors import BoxUnreachable, InvalidKey, ProtocolError, UnsafeKey
from skyq_mcp.skyq.keys import KEYS, key_name, resolve_key
from skyq_mcp.skyq.remote import RemoteClient, encode_key, send_key


@pytest.mark.parametrize("token,code", [
    ("select", 1), ("OK", 1), ("back", 2), ("i", 14), ("info", 14), ("5", 53), ("0", 48),
    ("channelapp", 12), ("apps", 12), ("sky", 241), ("code:20", 20), ("#0x14", 20), (64, 64),
])
def test_resolve_key(token, code):
    assert resolve_key(token) == code


@pytest.mark.parametrize("token", ["nope", "code:256", -1, 300, "code:abc", True])
def test_resolve_key_rejects_invalid(token):
    with pytest.raises(InvalidKey):
        resolve_key(token)


def test_pairing_codes_blocked_unless_allowed():
    for code in (223, 224, 231, 239):
        with pytest.raises(UnsafeKey):
            resolve_key(code)
        assert resolve_key(f"code:{code}", allow_unsafe=True) == code
    assert resolve_key(240) == 240  # boxoffice is just past the range


def test_key_names():
    assert key_name(14) == "i"
    assert key_name(99) == "code:99"
    assert len({k.code for k in KEYS.values()}) == len(KEYS)


def test_encode_key_matches_phase0_trace():
    down, up = encode_key(14)
    assert down.hex(" ") == "04 01 00 00 00 00 e0 0e"
    assert up.hex(" ") == "04 00 00 00 00 00 e0 0e"
    assert encode_key(49)[0][6:] == bytes([0xE3, 0x01])  # digit 1 seen as e3 01 in Phase 0


async def test_send_key_handshake(fake):
    await send_key("127.0.0.1", 14, port=fake.remote_port)
    await send_key("127.0.0.1", 200, port=fake.remote_port)
    await asyncio.sleep(0.05)
    assert fake.received == [14, 200]
    assert fake.handshake_errors == []


async def test_send_key_unreachable():
    with socket.socket() as s:  # find a closed port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(BoxUnreachable):
        await send_key("127.0.0.1", 1, port=port, timeout=1)


async def test_send_key_silent_box_times_out():
    async def accept_and_say_nothing(reader, writer):
        await asyncio.sleep(2)
        writer.close()

    srv = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ProtocolError):
            await send_key("127.0.0.1", 1, port=port, timeout=0.3)
    finally:
        srv.close()


async def test_press_many_keeps_order_and_gap(fake):
    client = RemoteClient("127.0.0.1", port=fake.remote_port, gap_ms=80)
    t0 = time.monotonic()
    await client.press_many([49, 48, 49])
    elapsed = time.monotonic() - t0
    await asyncio.sleep(0.05)
    assert fake.received == [49, 48, 49]
    assert elapsed >= 0.16  # two gaps


async def test_concurrent_sequences_dont_interleave(fake):
    client = RemoteClient("127.0.0.1", port=fake.remote_port, gap_ms=10)
    await asyncio.gather(client.press_many([16, 16, 16]), client.press_many([17, 17, 17]))
    await asyncio.sleep(0.05)
    assert fake.received in ([16, 16, 16, 17, 17, 17], [17, 17, 17, 16, 16, 16])
