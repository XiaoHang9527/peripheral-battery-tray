"""任务栏几何单测：托盘实际左边界与锚点计算。

这些函数直接读系统窗口，取不到任务栏时跳过。
"""
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


@pytest.fixture
def tray():
    tb = kb.u32.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        pytest.skip("当前环境没有任务栏")
    t = kb.u32.FindWindowExW(tb, None, "TrayNotifyWnd", None)
    if not t:
        pytest.skip("当前环境没有通知区域")
    return tb, t


def test_tray_left_edge_not_right_of_container(tray):
    """实际左边界必须 ≤ 容器外框左边界（相等是常态）。

    开机时容器外框会滞后，子窗口已经画到左边了——取 min 才能拿到真实位置，
    否则圆环会算到图标右边、造成重叠。
    """
    tb, t = tray
    edge = kb.tray_left_edge(t)
    assert edge is not None
    r = kb.wt.RECT()
    kb.u32.GetWindowRect(t, kb.ctypes.byref(r))
    assert edge <= r.left


def test_tray_left_edge_counts_children(tray):
    """至少要考虑容器自身的矩形；有可见子窗口时应取到更左的值或相等。"""
    tb, t = tray
    edge = kb.tray_left_edge(t)
    r = kb.wt.RECT()
    kb.u32.GetWindowRect(t, kb.ctypes.byref(r))
    lefts = [r.left]
    for child in kb._child_windows(t):
        if kb.u32.IsWindowVisible(child) and kb.u32.GetWindowRect(
                child, kb.ctypes.byref(r)):
            lefts.append(r.left)
    assert edge == min(lefts)


def test_taskbar_anchor_matches_tray_edge(tray):
    tb, t = tray
    mode, x, _ = kb.taskbar_anchor()
    if mode != "bar":
        pytest.skip("任务栏当前不在标准位置")
    edge = kb.tray_left_edge(t)
    assert x == edge - 6


def test_child_windows_returns_direct_children(tray):
    tb, _ = tray
    kids = kb._child_windows(tb)
    assert kids != []
    assert all(isinstance(k, int) for k in kids)


def test_panel_anchor_falls_back_to_taskbar_not_cursor(monkeypatch):
    monkeypatch.setattr(kb, "tray_icon_rect", lambda: None)
    monkeypatch.setattr(kb, "taskbar_anchor", lambda: ("bar", 1720, 1056))
    monkeypatch.setattr(kb, "taskbar_hwnd", lambda: 42)

    def get_rect(hwnd, pointer):
        assert hwnd == 42
        rect = pointer._obj
        rect.top, rect.bottom = 1032, 1080
        return True

    monkeypatch.setattr(kb, "u32", SimpleNamespace(GetWindowRect=get_rect))
    assert kb.tray_panel_anchor() == (1720, 1032, 1720, 1080)


def test_notification_busy_does_not_hide_ring_for_normal_window(monkeypatch):
    foreground = (80, 80, 1200, 820)

    def get_rect(hwnd, ptr):
        r = ptr._obj
        r.left, r.top, r.right, r.bottom = foreground if hwnd == 1 else (0, 1040, 1920, 1080)
        return True

    def get_class(hwnd, buf, length):
        buf.value = "Chrome_WidgetWin_1"

    monkeypatch.setattr(kb, "taskbar_hwnd", lambda: 2)
    monkeypatch.setattr(kb, "query_notification_state", lambda: 2)
    monkeypatch.setattr(kb.tray_ui, "monitor_bounds", lambda x, y: (0, 0, 1920, 1080))
    monkeypatch.setattr(kb, "u32", SimpleNamespace(
        GetForegroundWindow=lambda: 1, GetClassNameW=get_class,
        GetWindowRect=get_rect, GetWindowLongW=lambda hwnd, index: 0,
    ))
    assert not kb.foreground_fullscreen()
    foreground = (0, 0, 1920, 1080)
    assert kb.foreground_fullscreen()
