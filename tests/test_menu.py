import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


from unittest.mock import Mock


@pytest.mark.parametrize("message", [kb.WM_LBUTTONUP, kb.WM_RBUTTONUP, kb.WM_CONTEXTMENU])
def test_tray_click_opens_overview_on_tk_thread(monkeypatch, message):
    root = Mock()
    monkeypatch.setattr(kb, "ROOT", root)
    icon = kb.build_icon()
    assert icon.name == "外设电量"
    icon._on_notify(0, message)
    root.after.assert_called_once_with(0, kb.open_overview)


def test_overview_actions_and_refresh_state():
    try:
        root = kb.tk.Tk()
    except kb.tk.TclError:
        pytest.skip("当前环境无法启动 Tk")
    root.withdraw()
    try:
        quit_app = Mock()
        toggle = Mock()
        noop = lambda: None
        overview = kb.tray_ui.DeviceOverview(
            root, palette=kb.tray_ui.Palette(False), dpi=96,
            on_refresh=noop, on_settings=lambda anchor: None,
            on_toggle_ring=toggle, on_taskbar=noop, on_autostart=noop,
            on_log=noop, on_quit=quit_app)
        rows = [kb.tray_ui.OverviewRow("mouse", "MX", 30, "上次电量", False, True, True)]
        overview.show((80, 100, 100, 120), "mouse", rows, "pending", 20)
        root.update_idletasks()
        assert overview.reload.cget("state") == "disabled"
        assert overview.reload.cget("text") == "正在刷新…"
        assert overview.quit_button.winfo_ismapped()
        assert not overview._options.winfo_ismapped()
        overview.quit_button.invoke()
        quit_app.assert_called_once_with()
        overview._row_action.invoke()
        toggle.assert_called_once_with("mouse")
        overview.update([], "failed")
        root.update_idletasks()
        assert overview.reload.cget("state") == "normal"
        assert overview.notice.winfo_ismapped()
        assert overview._row_action is None
    finally:
        root.destroy()
