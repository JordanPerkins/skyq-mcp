import json

import pytest

from fake_skyq import CONTROL_UDN, SENSITIVE
from skyq_mcp.skyq import discovery
from skyq_mcp.skyq.errors import AmbiguousChannel, ChannelNotFound
from skyq_mcp.skyq.rest import Channel, ChannelList, RestClient
from skyq_mcp.skyq.upnp import UpnpClient, parse_description


def rest(fake):
    return RestClient("127.0.0.1", port=fake.http_port)


async def test_system_information_never_exposes_sensitive_fields(fake):
    info = await rest(fake).system_information()
    assert info.model == "ES340d8UKIE"
    assert info.hardware_version == "32B206"
    assert info.standby is False
    dumped = json.dumps(info.to_dict()) + repr(info)
    for secret in SENSITIVE.values():
        assert secret not in dumped


async def test_standby_flag(make_fake):
    fake = make_fake(standby=True)
    assert await rest(fake).is_standby() is True


@pytest.fixture
async def channels(fake) -> ChannelList:
    return await rest(fake).channels()


async def test_channels_sorted_tv_then_radio(channels):
    numbers = [c.number for c in channels.channels]
    assert numbers[0] == "101" and numbers[-1] == "0111"
    assert channels.by_number("0111").radio
    assert channels.by_sid(0xBC9).name == "BBC One Yks HD"


@pytest.mark.parametrize("query,number", [
    ("101", "101"),
    (102, "102"),
    ("0111", "0111"),
    ("bbc two hd", "102"),          # exact, case-insensitive
    ("BBC One", "101"),             # prefix: lowest number = the regional one
    ("itv1", "103"),                # +1 channel excluded unless asked for
    ("ITV1 +1", "203"),
    ("radio x", "0111"),
    ("channel 4", "104"),
    ("Premiere", "301"),            # single "contains" match
])
async def test_find_channel(channels, query, number):
    assert channels.find(query).number == number


async def test_find_channel_errors(channels):
    with pytest.raises(ChannelNotFound):
        channels.find("999")
    with pytest.raises(ChannelNotFound):
        channels.find("Nonexistent TV")
    with pytest.raises(AmbiguousChannel) as e:
        channels.find("cinema")  # contained in two names, prefix of none
    assert "301 Sky Cinema Premiere HD" in e.value.candidates


async def test_channels_cached(fake):
    client = rest(fake)
    first = await client.channels()
    assert await client.channels() is first
    assert await client.channels(refresh=True) is not first


async def test_recording_needs_p_prefix(fake):
    rec = await rest(fake).recording("290262CD")
    assert rec.title == "Gogglebox" and rec.channel_number == "104" and rec.episode == 3
    assert rec.start.startswith("2026-")
    assert await rest(fake).recording("DEADBEEF") is None


def test_parse_description_resolves_control_url():
    xml = b"""<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
      <deviceType>urn:schemas-nds-com:device:GatewaySkyControl:2</deviceType><UDN>uuid:x</UDN>
      <serviceList><service><serviceType>urn:schemas-nds-com:service:SkyPlay:2</serviceType>
      <controlURL>/abcSkyPlay</controlURL></service></serviceList></device></root>"""
    dev = parse_description(xml, "http://10.0.0.5:49153/description1.xml")
    assert dev.udn == "uuid:x"
    assert dev.service("SkyPlay").control_url == "http://10.0.0.5:49153/abcSkyPlay"


async def test_upnp_uses_skycontrol_not_skyserve(fake):
    client = UpnpClient("127.0.0.1", port=fake.http_port)
    dev = await client.control_device()
    assert dev.udn == CONTROL_UDN
    assert (await client.transport_info()).state == "PLAYING"
    assert (await client.media_info()).uri == "xsi://BC9"


# Header shapes copied from the ES340's real SSDP answers in Phase 0.
SSDP_RESPONSES = [
    ("192.168.1.104", {"ST": "urn:schemas-nds-com:device:GatewaySkyControl:2",
                       "USN": "uuid:444D5276-3247::urn:schemas-nds-com:device:GatewaySkyControl:2",
                       "LOCATION": "http://192.168.1.104:49153/description1.xml",
                       "SERVER": "GW-Device/1.0.0 (Sky, ES140, )"}),
    ("192.168.1.104", {"ST": "urn:schemas-nds-com:device:GatewaySkyServe:2",
                       "USN": "uuid:444D5376-3247::urn:schemas-nds-com:device:GatewaySkyServe:2",
                       "LOCATION": "http://192.168.1.104:49153/description0.xml"}),
    ("192.168.1.1", {"ST": "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                     "USN": "uuid:router::urn:schemas-upnp-org:device:InternetGatewayDevice:1"}),
]


def test_parse_ssdp_response():
    raw = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLocation: http://192.168.1.104:49153/description1.xml\r\n"
           b"ST: urn:schemas-nds-com:device:GatewaySkyControl:2\r\n\r\n")
    h = discovery.parse_ssdp_response(raw)
    assert h["LOCATION"].endswith("description1.xml") and h["ST"].endswith("GatewaySkyControl:2")


def test_group_ssdp_responses_keeps_sky_boxes_only():
    [box] = discovery.group_responses(SSDP_RESPONSES)
    assert box.host == "192.168.1.104"
    assert box.id == "uuid:444D5276-3247"  # the SkyControl UDN
    assert len(box.locations) == 2


async def test_identify(fake):
    cand = await discovery.identify("127.0.0.1", rest_port=fake.http_port, upnp_port=fake.http_port)
    assert cand.error is None
    assert cand.id == CONTROL_UDN and cand.model == "ES340d8UKIE" and cand.standby is False
    assert "SECRET" not in json.dumps(cand.to_dict())


async def test_identify_nothing_there():
    cand = await discovery.identify("127.0.0.1", rest_port=1, upnp_port=1, timeout=0.5)
    assert cand.error and cand.id is None


async def test_scan_subnet_rejects_huge_ranges():
    with pytest.raises(Exception, match="too large"):
        await discovery.scan_subnet("10.0.0.0/8")


# -- hostnames ------------------------------------------------------------------------------

from skyq_mcp.skyq import names


async def test_resolve_ip_and_hostname():
    assert await names.resolve("192.168.1.104") == ["192.168.1.104"]
    assert "127.0.0.1" in await names.resolve("localhost")
    assert await names.resolve("no-such-host.invalid", timeout=2) == []


def test_hostnames_from_mdns_match_by_ip():
    devices = [names.MdnsDevice("samsung.local", ["192.168.1.177"], manufacturer="Samsung"),
               names.MdnsDevice("skyq.local", ["192.168.1.104"], manufacturer="Sky", model="ES340"),
               names.MdnsDevice("skyq-2.local", ["192.168.1.105"], manufacturer="Sky")]
    assert names.hostnames_from_mdns("192.168.1.104", devices) == ["skyq.local"]
    assert names.hostnames_from_mdns("192.168.1.50", devices) == []
    assert [d.is_sky for d in devices] == [False, True, True]


async def test_find_hostnames_puts_dns_name_first(monkeypatch):
    async def fake_reverse(ip, timeout=2.0):
        return "skyq.home.lan"

    monkeypatch.setattr(names, "reverse_dns", fake_reverse)
    mdns = [names.MdnsDevice("skyq.local", ["192.168.1.104"])]
    assert await names.find_hostnames("192.168.1.104", mdns=mdns) == ["skyq.home.lan", "skyq.local"]


def test_normalise():
    assert names.normalise("SkyQ.Local.") == "skyq.local"
