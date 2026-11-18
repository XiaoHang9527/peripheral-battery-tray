"""任务栏面板单测：脉冲、复位、渲染参数与重着色。

这些逻辑平时只有真机低电量才会走到，用假 Tk 根窗口 + 拦截 ring_image
把参数记录下来验证，不依赖真实硬件。
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

import tray_ui  # noqa: E402


@pytest.fixture
def panel(monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境没有可用窗口站")
    root.withdraw()

    calls = []
    orig = tray_ui.ring_image

    def rec(*args, **kwargs):
        calls.append((args, kwargs))
        return orig(*args, **kwargs)

    monkeypatch.setattr(tray_ui, "ring_image", rec)

    dev = kb.Device("device", "键盘")
    dev.name = "T"
    dev.present = True
    dev.level = 15
    dev.updated = time.time()
    saved = kb.ROOT
    kb.ROOT = root
    p = kb.TaskbarPanel(root, dev, 0, lambda *a: None, lambda: None)
    p._shown = True
    try:
        yield p, dev, calls
    finally:
        kb.ROOT = saved
        root.destroy()


def test_panel_renders_ring_size(panel):
    p, dev, calls = panel
    assert calls, "构造面板时应渲染一次"
    args, kwargs = calls[-1]
    assert args[0] == tray_ui.RING_SIZE


def test_ring_click_shows_pending_then_updated_feedback(panel, monkeypatch):
    p, dev, _ = panel
    listener = kb.HotplugWatcher()
    monkeypatch.setattr(kb, "HOTPLUG", listener)
    p.win.deiconify()
    p.root.update()
    p._refresh_from_click(listener.request_refresh)
    assert p._tip_visible
    assert p.card.meta.cget("text") == "正在刷新…"
    assert listener.take_refresh()
    dev.updated = time.time()
    listener.finish_refresh(ok=True)
    p.refresh_tip()
    assert p.card.meta.cget("text") == "刚刚更新"


def test_ring_keeps_physical_coordinates_after_tk_idle_layout(panel):
    p, _, _ = panel
    mode, anchor, center = kb.taskbar_anchor()
    if mode != "bar":
        pytest.skip("当前环境没有标准任务栏")
    previous_dpi = tray_ui.apply_dpi_scaling(p.root)
    try:
        p.root.tk.call("tk", "scaling", 144 / 72)
        for x in (anchor, anchor - 36, anchor, anchor - 36, anchor):
            p.place((mode, x, center), 0)
            p.root.update()
            hwnd = p._hwnd()
            assert hwnd and kb.u32.IsWindowVisible(hwnd)
            rect = kb.wt.RECT()
            assert kb.u32.GetWindowRect(hwnd, kb.ctypes.byref(rect))
            assert (rect.left, rect.top) == (x - p.SIZE, center - p.SIZE // 2)
            assert (rect.right - rect.left, rect.bottom - rect.top) == (p.SIZE, p.SIZE)
        p.hide()
        p.place((mode, anchor, center), 0)
        assert kb.u32.IsWindowVisible(p._hwnd())
        p.win.destroy()
        assert not p.alive()
    finally:
        p.root.tk.call("tk", "scaling", previous_dpi / 72)


def test_needs_pulse_only_when_low_and_idle(panel):
    p, dev, _ = panel
    assert p.needs_pulse(20) is True
    dev.level = 80
    assert p.needs_pulse(20) is False
    dev.level = 15
    dev.charging = True
    assert p.needs_pulse(20) is False
    dev.charging = False
    p._shown = False
    assert p.needs_pulse(20) is False


def test_pulse_lowers_alpha_and_reset_restores(panel):
    p, dev, calls = panel
    p.pulse(0.0)                       # 最淡相位
    assert calls[-1][1]["alpha"] < 255
    assert p._pulse < 255
    p.reset_pulse()
    assert calls[-1][1]["alpha"] == 255
    assert p._pulse == 255


def test_reset_without_pulse_does_not_rerender(panel):
    p, dev, calls = panel
    before = len(calls)
    p.reset_pulse()
    assert len(calls) == before        # 本来就不淡，不该多画一张


def test_retint_switches_palette_and_rerenders(panel):
    p, dev, calls = panel
    before_dark = p.dark
    before = len(calls)
    p.retint(not before_dark)
    assert p.dark is (not before_dark)
    assert p.palette.dark is (not before_dark)
    assert len(calls) > before


def test_retint_same_theme_is_noop(panel):
    p, dev, calls = panel
    before = len(calls)
    p.retint(p.dark)
    assert len(calls) == before


def test_panel_is_owned_by_taskbar(panel):
    """回归：圆环必须是任务栏的附属窗口。

    否则点开托盘溢出面板时 explorer 会把任务栏抬到置顶窗口最上面，
    圆环被盖住→再抢回来，用户看到的就是圆环「闪一下」。
    """
    p, dev, _ = panel
    taskbar = kb.taskbar_hwnd()
    if not taskbar:
        pytest.skip("当前环境没有任务栏窗口")
    p.win.deiconify()
    p.root.update_idletasks()
    p._adopt()
    owner = kb.u32.GetWindow(p._hwnd(), kb.GW_OWNER)
    if not owner:
        pytest.skip("当前环境不支持设置宿主窗口")
    assert owner == taskbar


def test_adopt_is_idempotent(panel):
    p, dev, _ = panel
    if not kb.taskbar_hwnd():
        pytest.skip("当前环境没有任务栏窗口")
    p.win.deiconify()
    p.root.update_idletasks()
    p._adopt()
    first = p._owner
    p._adopt()
    assert p._owner == first
