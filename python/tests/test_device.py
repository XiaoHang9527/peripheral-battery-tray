"""设备模型与显示规则单测：状态文案、圆环可见性（含掉线保留）。"""
import importlib.util
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


@pytest.fixture
def dev():
    d = kb.Device("device", "键盘")
    d.name = "AULA F75"
    d.present = True
    d.level = 80
    d.status = "电池供电"
    d.updated = time.time()
    return d


def test_describe_basic(dev):
    assert kb.describe(dev) == "AULA F75：80% 电池供电"


def test_describe_compact_drops_mode(dev):
    """菜单头部只要名称和百分比。"""
    assert kb.describe(dev, with_mode=False) == "AULA F75：80%"


def test_describe_compact_keeps_charging(dev):
    dev.charging = True
    dev.status = "充电中"
    assert kb.describe(dev, with_mode=False) == "AULA F75：80% 充电中"


def test_describe_with_time(dev):
    out = kb.describe(dev, with_time=True)
    assert out.startswith("AULA F75：80%")
    assert " · " in out


def test_describe_reports_error(dev):
    dev.error = "未发现接收器"
    out = kb.describe(dev)
    assert "未发现接收器" in out


def test_stale_reading_overrides_old_charging_status(dev):
    dev.error = "电量查询无应答"
    dev.status = "充电中"
    dev.charging = True
    reading = kb.tray_ui.battery_reading(dev)
    assert (reading.level, reading.caption, reading.status, reading.historical) == (
        80, "上次电量", "未收到应答", True,
    )
    assert kb.describe(dev) == "AULA F75：上次电量 80% · 未收到应答"


def test_hover_card_refresh_feedback(dev, monkeypatch):
    dev.updated = 120
    monkeypatch.setattr(kb.tray_ui.time, "time", lambda: 122)
    meta = kb.tray_ui.TooltipCard._meta
    assert meta(dev, refresh="pending") == "正在刷新…"
    assert meta(dev, refresh="failed") == "刷新未完成，可再次点击重试"
    assert meta(dev) == "刚刚更新"
    assert meta(dev, historical=True).startswith("上次读到电量 ")


def test_device_overview_includes_devices_with_hidden_rings(dev, monkeypatch):
    monkeypatch.setattr(kb, "DEVICES", [dev])
    monkeypatch.setitem(kb.CFG, "show", {dev.key: False})
    assert kb.overview_rows() == [kb.tray_ui.OverviewRow(dev.key, "AULA F75", 80, "电池供电 · 刚刚更新", False, False, False)]
    dev.error = "无应答"
    row, = kb.overview_rows()
    assert row.historical and row.level == 80
    assert row.detail.startswith("未收到应答 · 上次电量")


def test_hiding_rings_keeps_matching_tray_status(dev, monkeypatch):
    mouse = _mk("mouse", "MX Vertical", 12)
    monkeypatch.setattr(kb, "DEVICES", [dev, mouse])
    monkeypatch.setitem(kb.CFG, "show", {dev.key: False, mouse.key: False})
    assert kb.lowest_level() == (12, mouse)
    assert kb.status_lines() == ["MX Vertical：12% 低电量", "AULA F75：80%"]
    assert {row.key for row in kb.overview_rows()} == {dev.key, mouse.key}


def test_tray_prioritizes_low_discharging_device_over_charging(dev, monkeypatch):
    mouse = _mk("mouse", "MX Vertical", 35)
    dev.level, dev.charging = 12, True
    monkeypatch.setattr(kb, "DEVICES", [dev, mouse])
    monkeypatch.setitem(kb.CFG, "low_threshold", 40)
    assert kb.lowest_level() == (35, mouse)
    assert kb.status_lines()[0] == "MX Vertical：35% 低电量"
    assert kb.level_color(35) == kb.tray_ui.COLOR["low"]


def test_describe_no_reading(dev):
    dev.level = None
    dev.error = ""
    assert kb.describe(dev) == "AULA F75：无读数"


def test_describe_name_limit(dev):
    dev.name = "MX Vertical Advanced Ergonomic Mouse"
    dev.status = "电池供电"
    out = kb.describe(dev, with_mode=False, name_limit=18)
    assert out.startswith("MX Vertical Advan")
    assert "…" in out
    assert "80%" in out


def _mk(key, name, level, status="电池供电"):
    d = kb.Device(key, "键盘")
    d.name = name
    d.present = True
    d.level = level
    d.status = status
    d.updated = time.time()
    return d


def test_status_text_fits_tooltip_budget(monkeypatch):
    """两台设备都要能出现在 125 字符内。"""
    monkeypatch.setitem(kb.CFG, "show",
                        {"device": True, "mouse": True})
    monkeypatch.setattr(kb, "DEVICES", [
        _mk("device", "MX Vertical Advanced Ergonomic Mouse", 50),
        _mk("mouse", "AULA F75 Ultra Long Name Keyboard Model", 100),
    ])
    text = kb.status_text()
    assert len(text) <= kb.TOOLTIP_LIMIT
    assert "|" in text
    assert "%" in text


# ------------------------------------------------------------ 圆环可见性

def test_visible_when_present(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    assert kb.is_visible(dev) is True


def test_hidden_when_user_unchecks(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": False})
    assert kb.is_visible(dev) is False


def test_absent_device_keeps_ring_with_fresh_reading(dev, monkeypatch):
    """接收器被 USB 选择性挂起时枚举不到，但刚读到过读数——
    圆环要留着（淡化），否则会反复闪现。"""
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    dev.set_present(False)
    dev.error = "未发现接收器"
    assert kb.is_visible(dev) is True


def test_absent_device_hides_after_window(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    monkeypatch.setitem(kb.CFG, "stale_min", 2)
    dev.present = False
    dev.updated = time.time() - 2 * 60 - 1
    assert kb.is_visible(dev) is False


def test_stale_min_zero_hides_immediately(dev, monkeypatch):
    """拔掉后立即收起，不留「幽灵圆环」。"""
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    monkeypatch.setitem(kb.CFG, "stale_min", 0)
    dev.present = False
    dev.updated = time.time()
    assert kb.is_visible(dev) is False


def test_default_window_is_minutes_not_hours(dev, monkeypatch):
    """默认 2 分钟：拔掉 5 分钟后不该还挂着圆环。"""
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    monkeypatch.setitem(kb.CFG, "stale_min", kb.STALE_MIN_DEFAULT)
    dev.present = False
    dev.updated = time.time() - 5 * 60
    assert kb.is_visible(dev) is False


def test_stale_window_upper_bound(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    monkeypatch.setitem(kb.CFG, "stale_min", 240)
    dev.present = False
    dev.disconnected_at = time.time() - 200 * 60
    assert kb.is_visible(dev) is True


def test_stale_window_clamps_bad_config(monkeypatch):
    monkeypatch.setitem(kb.CFG, "stale_min", "oops")
    assert kb.stale_window() == kb.STALE_MIN_DEFAULT * 60
    monkeypatch.setitem(kb.CFG, "stale_min", 9999)
    assert kb.stale_window() == kb.LIMITS["stale_min"][1] * 60


def test_present_device_keeps_ring_regardless_of_window(dev, monkeypatch):
    """设备还在、只是没回应（鼠标休眠）时，圆环不该按掉线规则收起。"""
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    monkeypatch.setitem(kb.CFG, "stale_min", 0)
    dev.present = True
    dev.error = "鼠标休眠，等待唤醒"
    dev.updated = time.time() - 5 * 3600
    assert kb.is_visible(dev) is True


def test_config_migrates_stale_hours(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"stale_hours": 1, "interval_sec": 30}', encoding="utf-8")
    monkeypatch.setattr(kb, "CONFIG", str(path))
    cfg = kb.load_config()
    assert cfg["stale_min"] == 60          # 1 小时 -> 60 分钟
    assert "stale_hours" not in cfg


def test_config_migration_clamps_old_hours(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"stale_hours": 24}', encoding="utf-8")
    monkeypatch.setattr(kb, "CONFIG", str(path))
    assert kb.load_config()["stale_min"] == kb.LIMITS["stale_min"][1]


def test_absent_device_without_reading_is_hidden(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    dev.present = False
    dev.level = None
    assert kb.is_visible(dev) is False


def test_absent_device_without_timestamp_is_hidden(dev, monkeypatch):
    monkeypatch.setitem(kb.CFG, "show", {"device": True})
    dev.present = False
    dev.updated = None
    assert kb.is_visible(dev) is False


def test_unplug_retains_an_old_sample_until_disconnect_deadline(dev, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(kb, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(kb, "KB", dev)
    monkeypatch.setattr(kb, "log", lambda msg: None)
    monkeypatch.setitem(kb.CFG, "stale_min", 2)
    dev.updated = now - 300
    kb.poll_keyboard([])
    assert kb.is_visible(dev)
    now += 119
    kb.poll_keyboard([])
    assert kb.is_visible(dev)
    now += 1
    assert not kb.is_visible(dev)
    dev.set_present(True)
    assert kb.is_visible(dev) and dev.disconnected_at is None


def test_compx_wired_reading_preserves_last_measured_level(dev, monkeypatch):
    import battery_protocols

    readings = iter([(100, battery_protocols.STATE_CABLE), (72, battery_protocols.STATE_BATTERY)])
    monkeypatch.setattr(kb, "KB", dev)
    monkeypatch.setattr(kb, "DEVICES", [dev])
    monkeypatch.setattr(kb, "find_compx", lambda devices: "receiver")
    monkeypatch.setattr(kb, "compx_battery", lambda inst: (next(readings), None))
    dev.receiver = kb.hid_path("receiver")
    dev.model_id = 0xCD
    previous = dev.updated
    kb.poll_keyboard([])
    assert dev.wired and not dev.charging
    assert (dev.level, dev.updated) == (80, previous)
    assert dev.display_level is None
    assert "已接线（电量未知）" in kb.status_text()
    assert kb.lowest_level() == (None, None)
    kb.poll_keyboard([])
    assert not dev.wired and dev.display_level == 72


def test_restore_preserves_receiver_slots_and_drops_ambiguous_old_samples(dev, tmp_path, monkeypatch):
    path = kb.hid_path("receiver")
    mouse = kb.Device(kb.logitech_key(path, 2), "鼠标", path, 2)
    mouse.level = 44
    mouse.updated = time.time() - 300
    mouse.set_present(True)
    monkeypatch.setattr(kb, "LAST", str(tmp_path / "last.json"))
    monkeypatch.setattr(kb, "_LAST_SIG", None)
    monkeypatch.setattr(kb, "DEVICES", [dev, mouse])
    kb.save_last()
    last = kb.load_last()
    last["mouse"] = {"level": 100, "name": "不确定属于哪个槽位"}
    last[dev.key] = {"level": 100, "charging": True, "name": "AULA F75"}
    monkeypatch.setattr(kb, "KB", kb.Device("device", "键盘"))
    monkeypatch.setattr(kb, "DEVICES", [kb.KB])
    last[kb.logitech_key("old receiver", 2)] = {**last[mouse.key], "receiver": "old receiver"}
    kb.restore_devices(last, [{"vid": kb.LOGITECH_VID, "pid": 0xC52B, "page": 0xFF00,
                               "inl": 20, "outl": 20, "inst": "receiver"}])
    assert kb.KB.level is None and not kb.KB.present
    _, restored_mouse = kb.DEVICES
    assert (restored_mouse.key, restored_mouse.level) == (mouse.key, 44)
    assert restored_mouse.present and restored_mouse.error == "等待读取"


# ------------------------------------------------------------ 日志范围

def test_log_limits_clamp_uses_shared_limits(monkeypatch):
    monkeypatch.setitem(kb.CFG, "log_keep", 99)
    monkeypatch.setitem(kb.CFG, "log_retention_days", 9999)
    _, keep, days = kb.log_limits()
    assert keep == kb.LIMITS["log_keep"][1]
    assert days == kb.LIMITS["log_days"][1]


def test_log_limits_min_from_shared_limits(monkeypatch):
    monkeypatch.setitem(kb.CFG, "log_keep", 0)
    monkeypatch.setitem(kb.CFG, "log_retention_days", 0)
    _, keep, days = kb.log_limits()
    assert keep == kb.LIMITS["log_keep"][0]
    assert days == kb.LIMITS["log_days"][0]
