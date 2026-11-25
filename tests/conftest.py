from __future__ import annotations

import pytest

from skyq_mcp.skyq.box import SkyQBox, Timing
from fake_skyq import FakeSkyQ

FAST = Timing(clear_wait=0.05, retry_gap_ms=120, verify_timeout=0.8, poll_interval=0.05,
              power_timeout=1.0, power_repress_after=0.3, wake_settle=0.05, app_timeout=1.0)


@pytest.fixture
def make_fake():
    fakes: list[FakeSkyQ] = []

    def _make(**kw) -> FakeSkyQ:
        fake = FakeSkyQ(**kw).start()
        fakes.append(fake)
        return fake

    yield _make
    for f in fakes:
        f.stop()


@pytest.fixture
def fake(make_fake) -> FakeSkyQ:
    return make_fake()


def box_for(fake: FakeSkyQ, *, key_gap_ms: int = 60, allow_unsafe: bool = False) -> SkyQBox:
    return SkyQBox("127.0.0.1", key_gap_ms=key_gap_ms, timing=FAST, allow_unsafe=allow_unsafe, **fake.ports)
