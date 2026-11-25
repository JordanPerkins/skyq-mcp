import json
import stat

import pytest

from conftest import FAST
from fake_skyq import CONTROL_UDN
from skyq_mcp.manager import BoxManager
from skyq_mcp.skyq.discovery import Candidate
from skyq_mcp.skyq.errors import BoxNotFound, BoxUnreachable, RegistryError
from skyq_mcp.store import BoxRecord, BoxRegistry


@pytest.fixture
def registry(tmp_path):
    return BoxRegistry(tmp_path / "data" / "state.json")


# -- registry ----------------------------------------------------------------------------


def test_first_box_becomes_default_and_persists(registry):
    registry.add(BoxRecord(alias="Living-Room", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    assert registry.get().alias == "living-room"
    reloaded = BoxRegistry(registry.path)
    rec = reloaded.get("living-room")
    assert (rec.hostname, rec.last_ip) == ("skyq.local", "10.0.0.5")
    assert reloaded.default == "living-room"
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600
    assert json.loads(registry.path.read_text())["version"] == 2


def test_record_needs_an_address():
    with pytest.raises(RegistryError):
        BoxRecord(alias="x")


def test_registry_rules(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", id="uuid:a"))
    registry.add(BoxRecord(alias="bedroom", hostname="skyq-2.local", id="uuid:b"))
    assert registry.get().alias == "lounge"
    with pytest.raises(RegistryError):
        registry.add(BoxRecord(alias="lounge", last_ip="10.0.0.7"))
    with pytest.raises(RegistryError, match="already saved as 'bedroom'"):
        registry.add(BoxRecord(alias="other", last_ip="10.0.0.7", id="uuid:b"))
    with pytest.raises(RegistryError):
        registry.add(BoxRecord(alias="Bad Alias!", last_ip="10.0.0.7"))
    assert registry.find_by_address("SKYQ-2.local.").alias == "bedroom"
    registry.rename("lounge", "living-room")
    assert registry.default == "living-room"
    registry.remove("bedroom")
    with pytest.raises(BoxNotFound, match="saved: living-room"):
        registry.get("bedroom")


def test_changes_from_another_process_are_not_lost(registry):
    # The service holds the registry in memory; `skyq-mcp boxes save` writes the file meanwhile.
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", id="uuid:a"))
    other = BoxRegistry(registry.path)
    other.add(BoxRecord(alias="bedroom", hostname="skyq-2.local", id="uuid:b"))
    assert registry.get("bedroom").hostname == "skyq-2.local"
    registry.update("lounge", last_ip="10.0.0.5")  # service writes afterwards
    assert set(BoxRegistry(registry.path).boxes) == {"lounge", "bedroom"}


def test_get_without_alias(registry):
    with pytest.raises(BoxNotFound, match="no Sky Q box saved"):
        registry.get()


def test_version_1_files_are_migrated(registry):
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text(json.dumps({"version": 1, "defaultBox": "a", "boxes": {
        "a": {"host": "10.0.0.5", "id": "uuid:a", "futureField": 1},
        "b": {"host": "skyq.local"}}}))
    reg = BoxRegistry(registry.path)
    assert (reg.get("a").hostname, reg.get("a").last_ip) == (None, "10.0.0.5")
    assert reg.get("b").hostname == "skyq.local"
    registry.path.write_text(json.dumps({"version": 99, "boxes": {}}))
    with pytest.raises(RegistryError, match="newer version"):
        BoxRegistry(registry.path)


# -- finding a saved box -------------------------------------------------------------------


class Net:
    """A pretend network: which IP each hostname resolves to and which box is at each IP."""

    def __init__(self, dns=None, boxes=None, discoverable=None):
        self.dns = dns or {}  # hostname -> [ips]
        self.boxes = boxes or {}  # ip -> box id
        self.discoverable = discoverable or []  # Candidates an SSDP/mDNS search would find
        self.searches = 0

    async def resolve(self, name):
        return [name] if name[0].isdigit() else list(self.dns.get(name, []))

    async def verify(self, record, ip):
        return self.boxes.get(ip) == record.id

    async def discover(self, **_):
        self.searches += 1
        return self.discoverable


def manager_for(registry, net: Net) -> BoxManager:
    return BoxManager(registry, resolve_fn=net.resolve, verify_fn=net.verify, discover_fn=net.discover)


async def located(manager, alias="lounge"):
    return await manager.run(alias, lambda box: _host(box))


async def _host(box):
    return box.host


async def test_hostname_followed_when_dhcp_moves_the_box(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    net = Net(dns={"skyq.local": ["10.0.0.9"]}, boxes={"10.0.0.9": "uuid:a"})
    assert await located(manager_for(registry, net)) == "10.0.0.9"
    assert BoxRegistry(registry.path).get("lounge").last_ip == "10.0.0.9"
    assert net.searches == 0  # no network search needed


async def test_hostname_pointing_at_another_box_is_not_trusted(registry):
    # Two boxes swapped mDNS names after a power cut: skyq.local is now the bedroom box.
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    net = Net(dns={"skyq.local": ["10.0.0.6"]},
              boxes={"10.0.0.6": "uuid:b", "10.0.0.7": "uuid:a"},
              discoverable=[Candidate(host="10.0.0.7", id="uuid:a", hostnames=["skyq-2.local"]),
                            Candidate(host="10.0.0.6", id="uuid:b", hostnames=["skyq.local"])])
    assert await located(manager_for(registry, net)) == "10.0.0.7"
    rec = BoxRegistry(registry.path).get("lounge")
    assert (rec.hostname, rec.last_ip) == ("skyq-2.local", "10.0.0.7")  # learned its new name


async def test_last_ip_used_when_name_doesnt_resolve(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    net = Net(boxes={"10.0.0.5": "uuid:a"})
    assert await located(manager_for(registry, net)) == "10.0.0.5"


async def test_box_without_hostname_found_by_search(registry):
    registry.add(BoxRecord(alias="lounge", last_ip="10.0.0.5", id="uuid:a"))
    net = Net(boxes={"10.0.0.8": "uuid:a"}, discoverable=[Candidate(host="10.0.0.8", id="uuid:a")])
    assert await located(manager_for(registry, net)) == "10.0.0.8"
    assert BoxRegistry(registry.path).get("lounge").last_ip == "10.0.0.8"


async def test_unreachable_everywhere(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    with pytest.raises(BoxUnreachable, match="skyq.local or 10.0.0.5"):
        await located(manager_for(registry, Net()))


async def test_address_cached_then_refound_after_failure(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))
    net = Net(dns={"skyq.local": ["10.0.0.5"]}, boxes={"10.0.0.5": "uuid:a"})
    manager = manager_for(registry, net)
    assert await located(manager) == "10.0.0.5"

    # Power cut: DHCP gives the box a new address; the cached one now fails.
    net.dns = {"skyq.local": ["10.0.0.20"]}
    net.boxes = {"10.0.0.20": "uuid:a"}
    calls = []

    async def op(box):
        calls.append(box.host)
        if box.host == "10.0.0.5":
            raise BoxUnreachable("gone")
        return "ok"

    assert await manager.run("lounge", op) == "ok"
    assert calls == ["10.0.0.5", "10.0.0.20"]


# -- saving -----------------------------------------------------------------------------------


async def test_save_by_ip_learns_hostname(registry, fake):
    async def hostnames(ip):
        return ["skyq.local"]

    manager = BoxManager(registry, timing=FAST, hostnames_fn=hostnames, allow_loopback=True)
    rec, warning = await manager.save("lounge", "127.0.0.1", **fake.ports)
    assert (rec.hostname, rec.last_ip, rec.id) == ("skyq.local", "127.0.0.1", CONTROL_UDN)
    assert warning is None
    with pytest.raises(RegistryError, match="already saved as 'lounge'"):
        await manager.save("again", "127.0.0.1", **fake.ports)


async def test_save_by_hostname(registry, fake):
    rec, warning = await BoxManager(registry, allow_loopback=True).save("lounge", "localhost", **fake.ports)
    assert rec.hostname == "localhost" and rec.last_ip == "127.0.0.1" and warning is None


async def test_save_without_hostname_warns(registry, fake):
    async def none(ip):
        return []

    rec, warning = await BoxManager(registry, hostnames_fn=none, allow_loopback=True).save("lounge", "127.0.0.1", **fake.ports)
    assert rec.hostname is None and "DHCP reservation" in warning


async def test_save_rejects_non_box(registry):
    with pytest.raises(BoxUnreachable):
        await BoxManager(registry, allow_loopback=True).save("lounge", "127.0.0.1", rest_port=1, upnp_port=1)


async def test_run_controls_the_saved_box(registry, fake):
    async def none(ip):
        return []

    manager = BoxManager(registry, timing=FAST, hostnames_fn=none, allow_loopback=True)
    await manager.save("lounge", "127.0.0.1", **fake.ports)
    registry.update("lounge", key_gap_ms=60)
    result = await manager.run(None, lambda box: box.change_channel("102"))
    assert result.verified and fake.channel == "102"


async def test_discover_marks_saved_boxes(registry):
    registry.add(BoxRecord(alias="lounge", hostname="skyq.local", last_ip="10.0.0.5", id="uuid:a"))

    async def found(**_):
        return [Candidate(host="10.0.0.9", id="uuid:a", hostnames=["skyq.local"]),
                Candidate(host="10.0.0.6", id="uuid:b")]

    result = await BoxManager(registry, discover_fn=found).discover()
    assert [(r["host"], r["hostnames"], r["saved_as"]) for r in result] == [
        ("10.0.0.9", ["skyq.local"], "lounge"), ("10.0.0.6", [], None)]
