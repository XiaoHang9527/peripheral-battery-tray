"""定时循环单测：每个 tick 跑一次不能抛异常，而且必须重新排程。

这类循环的异常都是在 `finally` 里重新排程之前抛出的——pythonw 没有 stderr，
一旦名字写错/引用失效，循环就静默死掉，界面停在最后一帧。
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


class FakeRoot:
    def __init__(self):
        self.calls = []

    def after(self, ms, fn=None):
        self.calls.append((ms, fn))
        return "job"


class FakeHotplug:
    def take_settings_dirty(self):
        return False


@pytest.fixture
def env(monkeypatch):
    root = FakeRoot()
    monkeypatch.setattr(kb, "ROOT", root)
    monkeypatch.setattr(kb, "PANELS", [])
    monkeypatch.setattr(kb, "HOTPLUG", FakeHotplug())
    monkeypatch.setattr(kb, "foreground_fullscreen", lambda: False)
    monkeypatch.setitem(kb.CFG, "taskbar", True)
    kb._TICKS["n"] = 0
    kb._TICKS["ts"] = time.time()
    return root


def test_ui_tick_reschedules(env):
    kb.ui_tick()
    assert env.calls, "ui_tick 必须重新排程"
    assert env.calls[-1][1] is kb.ui_tick


def test_ui_tick_survives_exception_in_body(env, monkeypatch):
    """主体抛异常也必须继续排程，否则界面冻结。"""
    monkeypatch.setattr(kb.ANCHOR, "update",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    kb.ui_tick()
    assert env.calls and env.calls[-1][1] is kb.ui_tick


def test_ui_tick_survives_exception_in_finally(env, monkeypatch):
    """收尾（主题复核/心跳）抛异常同样不能吞掉排程。"""
    monkeypatch.setattr(kb, "check_theme",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    kb._TICKS["n"] = 299            # 下一拍会触发 check_theme
    kb.ui_tick()
    assert env.calls and env.calls[-1][1] is kb.ui_tick


def test_ui_tick_skips_placement_without_anchor(env, monkeypatch):
    monkeypatch.setattr(kb.ANCHOR, "update", lambda: None)
    placed = []
    monkeypatch.setattr(kb, "TaskbarPanel", kb.TaskbarPanel)
    kb.ui_tick()                     # 只要求不炸、仍排程
    assert env.calls and env.calls[-1][1] is kb.ui_tick
    assert placed == []


def test_ui_tick_creates_one_panel_per_discovered_device(env, monkeypatch):
    mouse = kb.Device("logitech_test_1", "鼠标")
    keyboard = kb.Device("logitech_test_2", "键盘")
    mouse.present, mouse.level = True, 30
    keyboard.present, keyboard.level = True, 55
    monkeypatch.setattr(kb, "DEVICES", [mouse])
    monkeypatch.setattr(kb.ANCHOR, "update", lambda: ("bar", 100, 100))

    class FakePanel:
        def __init__(self, root, dev, index, on_refresh, menu_builder):
            self.dev = dev

        def update(self, level, charging, stale, disconnected):
            self.level = level

        def place(self, anchor, slot):
            self.slot = slot

        def refresh_tip(self):
            pass

        def alive(self):
            return True

    monkeypatch.setattr(kb, "TaskbarPanel", FakePanel)
    kb.ui_tick()
    kb.DEVICES.append(keyboard)
    kb.ui_tick()
    assert [(p.dev, p.level, p.slot) for p in kb.PANELS] == [(mouse, 30, 0), (keyboard, 55, 1)]


def test_ui_tick_rebuilds_destroyed_device_window(env, monkeypatch):
    dev = kb.Device("device", "键盘")
    dev.present = True
    monkeypatch.setattr(kb, "DEVICES", [dev])
    monkeypatch.setattr(kb.ANCHOR, "update", lambda: ("bar", 100, 100))
    from unittest.mock import Mock

    broken = Mock(dev=dev)
    broken.alive.return_value = False
    replacement = Mock(dev=dev)
    monkeypatch.setattr(kb, "PANELS", [broken])
    monkeypatch.setattr(kb, "TaskbarPanel", Mock(return_value=replacement))
    kb.ui_tick()
    broken.destroy.assert_called_once()
    replacement.place.assert_called_once_with(("bar", 100, 100), 0)
    assert kb.PANELS == [replacement]


def test_topmost_tick_reschedules(env):
    kb.topmost_tick()
    assert env.calls and env.calls[-1][1] is kb.topmost_tick


def test_pulse_tick_reschedules(env):
    kb.pulse_tick()
    assert env.calls and env.calls[-1][1] is kb.pulse_tick


def test_pulse_tick_watchdog_revives_ui_loop(env, monkeypatch):
    """界面循环停滞时，pulse_tick 要把它重新拉起来。"""
    kb._TICKS["ts"] = time.time() - (kb._UI_STALL_SEC + 1)
    revived = []
    monkeypatch.setattr(kb, "ui_tick", lambda: revived.append(1))
    kb.pulse_tick()
    armed = [c for c in env.calls if c[0] == 0 and c[1] is kb.ui_tick]
    assert armed, "应当 after(0, ui_tick) 重新拉起"
    # 拉起后立刻更新心跳时间，避免每 800ms 反复重启
    assert time.time() - kb._TICKS["ts"] < 1


def test_pulse_tick_watchdog_quiet_when_healthy(env, monkeypatch):
    monkeypatch.setattr(kb, "ui_tick", lambda: None)
    kb.pulse_tick()
    assert not [c for c in env.calls if c[0] == 0]
