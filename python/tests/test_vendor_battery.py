from functools import reduce
from operator import xor

import vendor_battery as vendor


def test_discovery_matches_feature_length_and_razer_interface():
    correct = "HID\\VID_1532&PID_007B&MI_02&COL01\\1&ABC&0&0000"
    match = dict(vid=0x1532, pid=0x007B, featl=91, inst=correct)
    paths = vendor.receivers([match, dict(match, inst=correct.replace("MI_02", "MI_01")),
                              dict(match, pid=0x9999), dict(match, featl=65)])
    assert paths == {vendor.hid_path(correct): vendor.MODELS[0x1532, 0x007B]}
    assert not vendor.receivers([match, dict(match, inst=correct.replace("COL01", "COL02")
                                            .replace("0000", "0001"))])


def test_razer_reads_level_and_charging_only_from_matching_replies(monkeypatch):
    def exchange(path, request, delay):
        assert request[2] == 0x3F and request[7] == 7
        reply = bytearray(request)
        reply[1] = 2
        reply[10] = 128 if request[8] == 0x80 else 1
        reply[89] = reduce(xor, reply[3:89])
        return bytes(reply)

    monkeypatch.setattr(vendor, "feature_exchange", exchange)
    model = vendor.MODELS[0x1532, 0x007B]
    assert vendor.read_battery("razer", model) == (50, True)
    monkeypatch.setattr(vendor, "feature_exchange", lambda *args: b"\0" * 91)
    assert vendor.read_battery("razer", model) is None


def test_sinowealth_query_and_sleep_status(monkeypatch):
    reply = bytearray(65)
    reply[1], reply[4], reply[6:9] = 0xA1, 2, bytes((0x83, 1, 76))

    def exchange(path, request, delay):
        assert request[:7] == bytes.fromhex("00 00 00 02 02 00 83")
        assert len(request) == 65
        return bytes(reply)

    monkeypatch.setattr(vendor, "feature_exchange", exchange)
    for identity in ((0x258A, 0x2022), (0x36A7, 0xA874)):
        assert vendor.read_battery("receiver", vendor.MODELS[identity]) == (76, True)
    reply[1] = 0xA4
    assert vendor.read_battery("receiver", vendor.MODELS[0x258A, 0x2022]) is None
