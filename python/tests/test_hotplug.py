"""热插拔/系统设置监听窗口单测：消息到标志位这条链路，以及窗口能否关闭。

系统消息（WM_SETTINGCHANGE / WM_DEVICECHANGE）不允许 PostMessage，
只能用 SendMessage 同步投递——这也正是退出要用自定义消息的原因。
"""
import importlib.util
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


@pytest.fixture
def watcher():
    w = kb.HotplugWatcher().start()
    if not w._ready.wait(3.0) or not w._hwnd:
        pytest.skip("当前环境无法创建消息窗口")
    try:
        yield w
    finally:
        w.stop()


def send(w, msg):
    kb.u32.SendMessageW(w._hwnd, msg, 0, 0)


def test_settings_broadcast_sets_flag(watcher):
    assert watcher.take_settings_dirty() is False
    send(watcher, kb.WM_SETTINGCHANGE)
    assert watcher.take_settings_dirty() is True
    assert watcher.take_settings_dirty() is False      # 读取即清


def test_theme_changed_message_also_counts(watcher):
    send(watcher, kb.WM_THEMECHANGED)
    assert watcher.take_settings_dirty() is True


def test_display_change_counts_as_settings(watcher):
    send(watcher, kb.WM_DISPLAYCHANGE)
    assert watcher.take_settings_dirty() is True


def test_device_change_sets_device_event(watcher):
    send(watcher, kb.WM_DEVICECHANGE)
    assert watcher.wait(0.2) is True


def test_manual_refresh_tracks_pending_and_coalesces_repeated_clicks():
    listener = kb.HotplugWatcher()
    assert listener.refresh_status() == ""
    listener.request_refresh()
    assert listener.refresh_status() == "pending"
    assert listener.wait(0) is False  # 手动刷新不会被误认为设备变更
    assert listener.take_refresh() is True
    listener.request_refresh()
    assert listener.take_refresh() is False
    assert listener.refresh_status() == "pending"
    listener.finish_refresh(ok=False)
    assert listener.refresh_status() == "failed"
    listener.request_refresh()
    assert listener.take_refresh() is True
    listener.finish_refresh(ok=True)
    assert listener.refresh_status() == ""


@pytest.mark.parametrize("elapsed,name,ring,allowed", [
    (0, "Notepad", False, False),             # Pointer in transit after inactivity.
    (400, "Shell_TrayWnd", False, False),
    (500, "Shell_SecondaryTrayWnd", False, False),
    (1000, "TopLevelWindowForOverflowXamlIsland", False, False),
    (1000, "NotifyIconOverflowWindow", False, False),
    (3000, "TkTopLevel", True, False),
    (1000, "Notepad", False, True),
    (15000, "Notepad", False, False),
])
def test_poll_window_ignores_passive_surfaces(monkeypatch, elapsed, name, ring, allowed):
    def last_input(pointer):
        pointer._obj.dwTime = 1000
        return True

    def window_class(hwnd, buffer, capacity):
        assert hwnd == 42  # Classify the top-level window even if the pointer is over a child.
        buffer.value = name

    monkeypatch.setattr(kb.u32, "GetLastInputInfo", last_input)
    monkeypatch.setattr(kb.u32, "GetCursorPos", lambda pointer: True)
    monkeypatch.setattr(kb.u32, "WindowFromPoint", lambda point: 43)
    monkeypatch.setattr(kb.u32, "GetAncestor", lambda hwnd, kind: 42)
    monkeypatch.setattr(kb.u32, "GetClassNameW", window_class)
    monkeypatch.setattr(kb.k32, "GetTickCount", lambda: 1000 + elapsed)
    monkeypatch.setattr(kb, "PANELS", [SimpleNamespace(_mapped_handle=42)] if ring else [])
    assert (kb.poll_window_remaining() > 0) is allowed


@pytest.mark.parametrize("manual", [False, True])
def test_worker_only_bypasses_activity_check_for_manual_refresh(monkeypatch, manual):
    listener = kb.HotplugWatcher()
    stop = threading.Event()
    calls = []
    activity = {}
    waits = 0

    def wait(timeout):
        nonlocal activity, waits
        waits += 1
        if waits == 1:
            assert calls == ["inventory", "observe"]
            return True
        if waits == 2:
            assert calls == ["inventory", "observe", "rescan", "inventory", "observe"]
            if manual:
                listener.request_refresh()
            else:
                activity = {"keyboard": (100, 100)}
        else:
            stop.set()
        return False

    monkeypatch.setattr(listener, "wait", wait)
    monkeypatch.setattr(kb, "HOTPLUG", listener)
    monkeypatch.setattr(kb, "_STOP_WORKER", stop)
    monkeypatch.setattr(kb, "DEVICES", [])
    monkeypatch.setattr(listener, "recent_input", lambda: activity)
    monkeypatch.setattr(kb, "close_all", lambda: None)
    monkeypatch.setattr(kb, "poll_all", lambda: calls.append("manual"))
    monkeypatch.setattr(kb, "invalidate_devices", lambda: calls.append("rescan"))
    monkeypatch.setattr(kb, "hid_devices", lambda: [])
    monkeypatch.setattr(kb, "update_keyboard", lambda devices: calls.append("inventory"))
    monkeypatch.setattr(kb, "update_logitech", lambda devices: None)
    monkeypatch.setattr(kb, "update_rapoo", lambda devices: None)
    monkeypatch.setattr(kb, "update_vendor_devices", lambda devices: None)
    monkeypatch.setattr(kb, "publish_status", lambda: None)
    monkeypatch.setattr(kb, "refresh_active_devices",
                        lambda devices, inputs: calls.append("keyboard activity" if inputs else "observe"))

    kb.worker()

    assert waits == 3
    assert calls[-1] == ("manual" if manual else "keyboard activity")


def test_direct_hid_refresh_uses_only_the_active_device(monkeypatch):
    common = dict(vid=23, pid=42, inl=7, outl=7)
    keyboard = b"a" * 16
    mouse = b"b" * 16
    interfaces = [
        dict(common, inst="keyboard-input", page=1, usage=6, container=keyboard),
        dict(common, inst="keyboard-battery", page=0xFF00, usage=2, container=keyboard),
        dict(common, inst="mouse-input", page=1, usage=2, container=mouse),
        dict(common, inst="mouse-battery", page=0xFF00, usage=2, container=mouse),
    ]
    devices = [kb.Device(name, name, kb.hid_path(name + "-battery")) for name in ("keyboard", "mouse")]
    for dev in devices:
        dev.set_present(True)
        dev.last_attempt = 70
        dev.next_due = float("inf")
    calls = []
    monkeypatch.setattr(kb, "DEVICES", devices)
    monkeypatch.setattr(kb.time, "monotonic", lambda: 100)
    monkeypatch.setattr(kb, "poll_device", lambda dev, _: calls.append(dev.key))
    monkeypatch.setattr(kb, "poll_window_remaining", lambda: 1)
    kb.refresh_active_devices(interfaces, {kb.hid_path("keyboard-input"): (98, 97)})
    assert calls == ["keyboard"]
    kb.refresh_active_devices(interfaces, {kb.hid_path("mouse-input"): (94, 94)})
    assert calls == ["keyboard"]  # An expired signal does not enable the mouse.
    monkeypatch.setattr(kb, "poll_window_remaining", lambda: 0)
    kb.refresh_active_devices(interfaces, {kb.hid_path("mouse-input"): (99, 99)})
    assert calls == ["keyboard"]


def test_unifying_activity_is_scoped_to_its_slot_and_ignores_query_replies(monkeypatch):
    common = dict(vid=0x046D, pid=0xC52B, page=0xFF00, usage=2, container=b"a" * 16)
    interfaces = [dict(common, inst="long", inl=20, outl=20), dict(common, inst="short", usage=1, inl=7, outl=7)]
    path = kb.hid_path("long")
    devices = [kb.Device(str(slot), "Logitech", path, slot) for slot in (1, 2)]
    for dev in devices:
        dev.set_present(True)
    now = 100.0
    counts = bytes(6)
    calls = []
    receivers = {}

    def poll(dev, _):
        calls.append(dev.slot)
        dev.last_attempt = now
        receivers[path].queried_device(dev.slot)

    monkeypatch.setattr(kb, "DEVICES", devices)
    monkeypatch.setattr(kb, "_RECEIVER_ACTIVITY", receivers)
    monkeypatch.setattr(kb.time, "monotonic", lambda: now)
    monkeypatch.setattr(kb, "poll_window_remaining", lambda: 1)
    monkeypatch.setattr(kb, "read_unifying_activity", lambda *_: counts)
    monkeypatch.setattr(kb, "poll_device", poll)
    kb.refresh_active_devices(interfaces, {})
    assert calls == []
    now += 1
    counts = bytes([0, 1, 0, 0, 0, 0])
    kb.refresh_active_devices(interfaces, {})
    assert calls == [2]
    now += 1
    counts = bytes([0, 3, 0, 0, 0, 0])  # Only replies to the last query.
    kb.refresh_active_devices(interfaces, {})
    assert calls == [2]
    now += 1
    counts = bytes([1, 3, 0, 0, 0, 0])
    kb.refresh_active_devices(interfaces, {})
    assert calls == [2, 1]


def test_settings_message_does_not_wake_device_poll(watcher):
    """主题变化不该被当成设备变更，否则会白扫一轮 HID。"""
    send(watcher, kb.WM_SETTINGCHANGE)
    assert watcher.wait(0.05) is False


def test_system_messages_cannot_be_posted(watcher):
    """记录这个前提：能 Post 成功的是自定义消息，系统消息不行。"""
    import ctypes

    ctypes.set_last_error(0)
    ok = kb.u32.PostMessageW(watcher._hwnd, kb.WM_SETTINGCHANGE, 0, 0)
    assert ok == 0
    assert ctypes.get_last_error() == 1159        # ERROR_MESSAGE_SYNC_ONLY


def test_stop_destroys_window(watcher):
    hwnd = watcher._hwnd
    watcher.stop()
    for _ in range(60):
        if not kb.u32.IsWindow(hwnd):
            break
        time.sleep(0.05)
    assert not kb.u32.IsWindow(hwnd), "监听窗口应被销毁，否则句柄泄漏到进程退出"
