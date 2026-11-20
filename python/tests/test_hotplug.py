"""热插拔/系统设置监听窗口单测：消息到标志位这条链路，以及窗口能否关闭。

系统消息（WM_SETTINGCHANGE / WM_DEVICECHANGE）不允许 PostMessage，
只能用 SendMessage 同步投递——这也正是退出要用自定义消息的原因。
"""
import importlib.util
import pathlib
import sys
import time

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
