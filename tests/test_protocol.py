"""协议解析单元测试（用真实抓到的帧）。

运行：
    python -m pytest tests -q
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)

# 协议常量直接从 battery_protocols 取，避免依赖 peripheral_battery_tray 的再导出
sys.path.insert(0, str(ROOT))
import battery_protocols  # noqa: E402


def frame(hexstr):
    data = bytes.fromhex(hexstr)
    assert len(data) == 20
    return data


def fix_checksum(data):
    b = bytearray(data)
    b[19] = sum(b[:19]) & 0xFF
    return bytes(b)


# ---------------------------------------------------------------- Compx 电量

def test_compx_battery_on_battery():
    """实测帧：AULA F75 100%、电池供电。"""
    f = frame("13 4a 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 c5")
    assert kb.parse_compx_battery(f) == (100, battery_protocols.STATE_BATTERY)


def test_compx_battery_97_percent():
    """文档里的 97% 样例。"""
    f = frame("13 4a 01 00 02 61 01 00 00 00 00 00 00 00 00 00 00 00 00 c2")
    assert kb.parse_compx_battery(f) == (97, battery_protocols.STATE_BATTERY)


def test_compx_battery_cable_pins_100():
    """有线模式：状态 0x10，无论电量字节是多少都按 100 处理。"""
    f = fix_checksum(frame("13 4a 01 00 02 5a 10 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    assert kb.parse_compx_battery(f) == (100, battery_protocols.STATE_CABLE)


def test_compx_battery_bad_checksum():
    f = bytearray(frame("13 4a 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 c5"))
    f[19] ^= 0xFF
    with pytest.raises(ValueError):
        kb.parse_compx_battery(bytes(f))


def test_compx_battery_level_out_of_range():
    f = fix_checksum(frame("13 4a 01 00 02 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    with pytest.raises(ValueError):
        kb.parse_compx_battery(f)


def test_compx_battery_unknown_state():
    f = fix_checksum(frame("13 4a 01 00 02 64 07 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    with pytest.raises(ValueError):
        kb.parse_compx_battery(f)


def test_compx_battery_wrong_command():
    f = fix_checksum(frame("13 05 01 00 0a 03 00 00 00 00 cd 01 00 00 00 00 00 00 00 00"))
    with pytest.raises(ValueError):
        kb.parse_compx_battery(f)


# ---------------------------------------------------------------- Compx 型号

def test_compx_model_f75():
    """实测帧：0xCD = AULA F75。"""
    f = frame("13 05 01 00 0a 03 00 00 00 00 cd 01 00 00 00 00 00 00 00 f4")
    assert kb.parse_compx_model(f) == 0xCD
    assert battery_protocols.MODEL_NAMES[0xCD] == "AULA F75"


def test_compx_model_f99():
    f = fix_checksum(frame("13 05 01 00 0a 03 00 00 00 00 a4 01 00 00 00 00 00 00 00 00"))
    assert kb.parse_compx_model(f) == 0xA4


def test_compx_model_wrong_frame():
    f = frame("13 4a 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 c5")
    with pytest.raises(ValueError):
        kb.parse_compx_model(f)


# ---------------------------------------------------------------- Logitech HID++

def test_hidpp_battery_50_percent():
    """实测帧：MX Vertical 50%、状态 0。"""
    f = bytes.fromhex("11 01 08 00 32 14 00")
    assert kb.parse_hidpp_battery(f, dev=1) == (50, 0)


def test_hidpp_battery_device_mismatch():
    f = bytes.fromhex("11 01 08 00 32 14 00")
    with pytest.raises(ValueError):
        kb.parse_hidpp_battery(f, dev=2)


def test_hidpp_battery_not_long_report():
    f = bytes.fromhex("10 01 08 00 32 14")
    with pytest.raises(ValueError):
        kb.parse_hidpp_battery(f)


def test_hidpp_battery_no_level():
    f = bytes.fromhex("11 01 08 00 00 00 00")
    with pytest.raises(ValueError):
        kb.parse_hidpp_battery(f)


def test_hidpp_battery_level_out_of_range():
    f = bytes.fromhex("11 01 08 00 ff 00 00")
    with pytest.raises(ValueError):
        kb.parse_hidpp_battery(f)


def test_hidpp_status_names_cover_protocol():
    """协议定义 0..6 都要有中文名，未知状态走兜底。"""
    for code in range(7):
        assert code in kb.BATTERY_STATUS
    assert 0 not in (kb.BATTERY_STATUS.get(7, None),)


# ---------------------------------------------------------------- 帧匹配

def test_is_reply_matches_command():
    f = frame("13 4a 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 c5")
    assert kb.is_reply(f, battery_protocols.CMD_BAT)
    assert not kb.is_reply(f, battery_protocols.CMD_MODEL)


def test_is_reply_allows_high_bit_on_command():
    """应答可能把命令最高位置 1，解析要照样接受。"""
    f = fix_checksum(frame("13 ca 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    assert kb.is_reply(f, battery_protocols.CMD_BAT)


# ---------------------------------------------------------------- 轮询退避

def test_next_delay_success_uses_base_interval():
    assert kb.next_delay(0, True) == kb.CFG["interval_sec"]
    assert kb.next_delay(5, True) == kb.CFG["interval_sec"]


def test_next_delay_backs_off_and_caps():
    assert kb.next_delay(1, False) == 60
    assert kb.next_delay(2, False) == 120
    assert kb.next_delay(3, False) == 180
    assert kb.next_delay(4, False) == 300
    assert kb.next_delay(99, False) == 300


def test_next_delay_is_monotonic():
    delays = [kb.next_delay(n, False) for n in range(1, 8)]
    assert delays == sorted(delays)


def test_poll_device_backoff_and_reset(monkeypatch):
    """连续失败按 60/120/180/300 递增，成功后退避清零。"""
    d = kb.Device("mouse", "鼠标")

    def fail(devs):
        d.error = "休眠"

    monkeypatch.setattr(kb, "poll_logitech", fail)
    seen = []
    for _ in range(5):
        seen.append(kb.poll_device(d, []))
    assert seen == [60, 120, 180, 300, 300]
    assert d.fails == 5

    def ok(devs):
        d.error = ""

    monkeypatch.setattr(kb, "poll_logitech", ok)
    delay = kb.poll_device(d, [])
    assert delay == kb.CFG["interval_sec"]
    assert d.fails == 0


def test_poll_all_resets_backoff(monkeypatch):
    """手动刷新会把所有设备拉回“立刻轮询 + 退避清零”。"""
    mouse = kb.Device("test-mouse", "鼠标")
    monkeypatch.setattr(kb, "DEVICES", [kb.KB, mouse])
    for dev in kb.DEVICES:
        dev.fails = 4
        dev.next_due = 1e12
        dev.error = ""          # 轮询桩函数不会清错误，先归零
    monkeypatch.setattr(kb, "enum_hid", lambda: [])
    monkeypatch.setattr(kb, "poll_keyboard", lambda devs: None)
    monkeypatch.setattr(kb, "poll_logitech", lambda dev: None)
    kb.poll_all()
    for dev in kb.DEVICES:
        assert dev.fails == 0
        assert dev.next_due <= kb.time.time() + kb.CFG["interval_sec"] + 1


@pytest.mark.parametrize("feature_index", [0, 4])
def test_hidpp_feature_retries_timeout_and_caches_valid_reply(monkeypatch, feature_index):
    replies = iter([None, bytes([0x11, 1, 0, 1, feature_index, 0, 0])])
    calls = []

    def request(*args):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(kb, "_HIDPP_CACHE", {})
    monkeypatch.setattr(kb, "hidpp_request", request)
    assert kb.hidpp_feature("receiver", 1, kb.HIDPP_BATTERY) is None
    assert kb.hidpp_feature("receiver", 1, kb.HIDPP_BATTERY) == feature_index
    assert kb.hidpp_feature("receiver", 1, kb.HIDPP_BATTERY) == feature_index
    assert len(calls) == 2


def test_logitech_polls_each_slot_and_discovers_devices_after_wakeup(monkeypatch):
    receivers = [dict(vid=0x046D, pid=0xC52B, page=0xFF00, inl=20, outl=20, inst=name)
                 for name in ("receiver-a", "receiver-b")]
    first, second = [kb.hid_path(r["inst"]) for r in receivers]
    samples = {(first, 1): (0, 81), (second, 1): (5, 43)}
    keyboard = kb.Device("device", "键盘")
    monkeypatch.setattr(kb, "KB", keyboard)
    monkeypatch.setattr(kb, "DEVICES", [keyboard])
    monkeypatch.setattr(kb, "_HIDPP_CACHE", {})
    monkeypatch.setattr(kb, "_LOGITECH_SCAN_DUE", 0.0)
    monkeypatch.setattr(kb, "enum_hid", lambda: receivers)
    monkeypatch.setattr(kb.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(kb, "save_config", lambda: None)
    monkeypatch.setattr(kb, "CFG", {**kb.CFG, "mouse_name": "旧鼠标名称", "show": {"mouse": False}})

    def request(path, slot, feature, function, params=b"", **kwargs):
        sample = samples.get((path, slot))
        if sample is None:
            return None
        kind, level = sample
        payload = {
            (0, 1): bytes([2]),                 # 协议版本
            (0, 0): bytes([4 if params == b"\x00\x05" else 8]),
            (4, 2): bytes([kind]),
            (4, 0): bytes([4]),
            (4, 1): b"Test",
            (8, 0): bytes([level, 0, 0]),
        }[feature, function]
        return bytes([0x11, slot, feature, (function << 4) | 1]) + payload.ljust(16, b"\0")

    monkeypatch.setattr(kb, "hidpp_request", request)
    kb.poll_all()
    assert [(d.kind, d.level) for d in kb.DEVICES[1:]] == [("键盘", 81), ("轨迹球", 43)]
    primary = kb.DEVICES[2]
    assert primary.display_name() == "旧鼠标名称"
    assert not kb.is_shown(primary)
    assert kb.is_shown(kb.DEVICES[1])

    # 接收器仍在时，休眠设备保留旧读数，后来唤醒的槽位独立增加。
    del samples[second, 1]
    samples[first, 2] = (3, 26)
    kb.poll_all()
    assert primary.present and primary.level == 43 and primary.error
    assert {(d.receiver, d.slot): d.level for d in kb.DEVICES[1:]} == {
        (first, 1): 81, (first, 2): 26, (second, 1): 43,
    }
    assert len({d.key for d in kb.DEVICES}) == 4
    assert kb.is_shown(kb.DEVICES[3])

    receivers.pop()
    kb.poll_all()
    assert not primary.present and primary.disconnected_at is not None
    assert kb.DEVICES[1].present
