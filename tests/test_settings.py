"""设置逻辑单测：范围钳制、显示开关、非法输入回落、表单校验。"""
import importlib.util
import pathlib
import sys
from unittest.mock import Mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


@pytest.fixture
def cfg():
    saved = dict(kb.CFG)
    saved_show = dict(kb.CFG.get("show", {}))
    yield
    kb.CFG.clear()
    kb.CFG.update(saved)
    kb.CFG["show"] = saved_show


def values(**over):
    """构造一份合法的设置字典，再按需覆盖。"""
    base = {
        "interval_sec": 60,
        "low_threshold": 20,
        "log_max_mb": 1.0,
        "log_keep": 3,
        "log_retention_days": 14,
        "show": {"device": True, "mouse": True},
    }
    base.update(over)
    return base


def test_values_clamped(cfg):
    kb.apply_settings(values(interval_sec=1, low_threshold=1), persist=False)
    assert kb.CFG["interval_sec"] == 10
    assert kb.CFG["low_threshold"] == 5
    kb.apply_settings(values(interval_sec=9999, low_threshold=999),
                      persist=False)
    assert kb.CFG["interval_sec"] == 600
    assert kb.CFG["low_threshold"] == 50


def test_show_flags_saved(cfg):
    kb.apply_settings(values(show={"device": False, "mouse": True}),
                      persist=False)
    assert kb.CFG["show"] == {"device": False, "mouse": True}


def test_log_settings_clamped(cfg):
    kb.apply_settings(values(log_max_mb=0.001, log_keep=0,
                             log_retention_days=9999), persist=False)
    assert kb.CFG["log_max_mb"] == 0.1
    assert kb.CFG["log_keep"] == 1
    assert kb.CFG["log_retention_days"] == 365
    kb.apply_settings(values(log_max_mb=99), persist=False)
    assert kb.CFG["log_max_mb"] == 10.0


def test_log_settings_accepts_decimals(cfg):
    kb.apply_settings(values(log_max_mb=0.5, log_keep=4), persist=False)
    assert kb.CFG["log_max_mb"] == 0.5
    assert kb.log_limits()[0] == 512 * 1024


def test_reschedules_devices(cfg):
    for d in kb.DEVICES:
        d.fails = 3
        d.next_due = 1e12
    kb.apply_settings(values(interval_sec=20, low_threshold=10),
                      persist=False)
    for d in kb.DEVICES:
        assert d.fails == 0
        assert d.next_due == 0.0


def test_hides_unchecked_panels(cfg):
    class FakePanel:
        def __init__(self, dev):
            self.dev = dev
            self.hidden = False

        def hide(self):
            self.hidden = True

    panels = [FakePanel(kb.KB), FakePanel(kb.Device("logitech_test_1", "鼠标"))]
    saved = kb.PANELS[:]
    kb.PANELS[:] = panels
    try:
        kb.apply_settings(values(show={"device": False, "logitech_test_1": True}),
                          persist=False)
    finally:
        kb.PANELS[:] = saved
    assert panels[0].hidden is True
    assert panels[1].hidden is False


def test_form_applies_visibility_for_discovered_device(cfg):
    key = "logitech_test_2"
    device = kb.Device(key, "鼠标")
    clean, errors = tray_ui_validate({"show": {key: False}})
    assert not errors
    kb.apply_settings(clean, persist=False)
    assert not kb.is_shown(device)


def test_current_settings_roundtrip(cfg):
    kb.apply_settings(values(interval_sec=30, log_max_mb=0.25), persist=False)
    snap = kb.current_settings()
    assert snap["interval_sec"] == 30
    assert snap["log_max_mb"] == 0.25
    assert snap["show"] == {"device": True, "mouse": True}


def test_setting_names_are_saved_with_the_form(cfg, tmp_path, monkeypatch):
    device = kb.Device("logitech_test_1", "鼠标")
    monkeypatch.setattr(kb, "CONFIG", str(tmp_path / "config.json"))
    kb.apply_settings(values(device_names={device.key: "  办公鼠标  "}))
    assert device.display_name() == "办公鼠标"
    assert kb.load_config()[device.key + "_name"] == "办公鼠标"


def test_failed_settings_write_preserves_previous_values(cfg, tmp_path, monkeypatch):
    before = kb.current_settings()
    monkeypatch.setattr(kb, "CONFIG", str(tmp_path / "missing" / "config.json"))
    with pytest.raises(OSError):
        kb.apply_settings(values(interval_sec=110, show={"device": False}))
    assert kb.current_settings() == before


def test_log_limits_reads_config(cfg):
    kb.CFG["log_max_mb"] = 0.25
    kb.CFG["log_keep"] = 5
    kb.CFG["log_retention_days"] = 7
    assert kb.log_limits() == (256 * 1024, 5, 7)


def test_log_limits_survives_bad_config(cfg):
    kb.CFG["log_max_mb"] = "oops"
    kb.CFG["log_keep"] = None
    kb.CFG["log_retention_days"] = "x"
    size, keep, days = kb.log_limits()
    assert size == 1024 * 1024
    assert (keep, days) == (3, 14)


# ------------------------------------------------------------ 表单校验（tray_ui）

def test_validate_form_clean():
    clean, errors = tray_ui_validate({
        "interval": "60", "threshold": "20", "log_max_mb": "1",
        "log_keep": "3", "log_retention_days": "14",
        "show": {"device": True, "mouse": False},
    })
    assert errors == []
    assert clean["interval_sec"] == 60
    assert clean["show"] == {"device": True, "mouse": False}


def test_validate_form_reports_and_clamps():
    clean, errors = tray_ui_validate({
        "interval": "0", "threshold": "abc", "log_max_mb": "99",
        "log_keep": "0", "log_retention_days": "7", "show": {},
    })
    assert clean["interval_sec"] == 10
    assert clean["low_threshold"] == 5
    assert clean["log_max_mb"] == 10.0
    assert clean["log_keep"] == 1
    # 三个越界 + 一个非数字，都要有明确提示
    assert len(errors) == 4
    assert any("刷新间隔" in e for e in errors)
    assert any("低电量阈值" in e and "整数" in e for e in errors)


def test_validate_form_accepts_comma_decimal():
    clean, errors = tray_ui_validate({
        "interval": "60", "threshold": "20", "log_max_mb": "0,5",
        "log_keep": "3", "log_retention_days": "14", "show": {},
    })
    assert errors == []
    assert clean["log_max_mb"] == 0.5


def test_settings_show_field_errors_and_focus_first_invalid_control():
    dialog = kb.tray_ui.SettingsDialog.__new__(kb.tray_ui.SettingsDialog)
    dialog._raw = Mock(return_value={"interval": "0", "log_max_mb": "99"})
    interval, interval_label = Mock(), Mock()
    log_size, log_label = Mock(), Mock()
    dialog._fields = {"interval": (interval, interval_label), "log_max_mb": (log_size, log_label)}
    dialog._field_errors = {}
    dialog._device_names = {"device": "办公键盘"}
    dialog.error_label = Mock()
    dialog._show_advanced = Mock()
    dialog._reposition = Mock()
    dialog._scroll_to = Mock()
    dialog.palette = kb.tray_ui.Palette()
    dialog.on_save = Mock()
    dialog.close = Mock()
    dialog.save()
    dialog._show_advanced.assert_called_once_with(True)
    interval.focus_set.assert_called_once()
    interval_label.configure.assert_called_once_with(text="刷新间隔应在 10–600 之间")
    log_label.grid.assert_called_once()
    dialog._scroll_to.assert_called_once_with(interval, interval_label)
    dialog.on_save.assert_not_called()

    dialog._raw.return_value = {"interval": "60", "log_max_mb": "1"}
    dialog.save()
    assert dialog.on_save.call_args.args[0]["device_names"] == {"device": "办公键盘"}
    assert not dialog._field_errors
    dialog.close.assert_called_once()


def test_settings_window_expands_advanced_for_invalid_log_field():
    try:
        root = kb.tk.Tk()
    except kb.tk.TclError:
        pytest.skip("当前环境无法启动 Tk")
    root.withdraw()
    saved = []
    try:
        dialog = kb.tray_ui.SettingsDialog(root, kb.current_settings(), kb._device_rows(), saved.append)
        dialog.show()
        root.update()
        assert not dialog.advanced.winfo_ismapped()
        dialog.vars["log_max_mb"].set("22")
        dialog.save()
        root.update()
        entry, label = dialog._fields["log_max_mb"]
        assert dialog.advanced.winfo_ismapped() and label.winfo_ismapped()
        assert entry.cget("highlightbackground") == label.cget("fg") and entry.selection_present()
        assert not saved
        dialog.vars["log_max_mb"].set("0.5")
        assert not label.winfo_ismapped()
        dialog.save()
        assert saved[0]["log_max_mb"] == 0.5
    finally:
        root.destroy()


def test_settings_and_overview_align_to_same_tray_edge(monkeypatch):
    try:
        root = kb.tk.Tk()
    except kb.tk.TclError:
        pytest.skip("当前环境无法启动 Tk")
    root.withdraw()
    area, _ = kb.tray_ui.cursor_monitor()
    if area is None:
        root.destroy()
        pytest.skip("当前环境没有显示器工作区")
    _, _, right, bottom = area
    anchor = (right - 126, bottom + 4, right - 102, bottom + 28)
    monkeypatch.setattr(kb, "ROOT", root)
    monkeypatch.setattr(kb, "_SETTINGS", None)
    monkeypatch.setattr(kb, "_OVERVIEW", None)
    monkeypatch.setattr(kb, "tray_icon_rect", lambda: anchor)

    def bottom_right(win):
        root.update_idletasks()
        rect = kb.wt.RECT()
        hwnd = kb.tray_ui.window_handle(win)
        assert hwnd and kb.u32.GetWindowRect(hwnd, kb.ctypes.byref(rect))
        return rect.right, rect.bottom

    try:
        kb.open_overview()
        expected = bottom_right(kb._OVERVIEW.win)
        monkeypatch.setattr(kb, "tray_icon_rect", lambda: None)
        kb._OVERVIEW._open_settings(kb.open_settings)
        assert not kb._OVERVIEW.win.winfo_viewable()
        assert bottom_right(kb._SETTINGS.win) == expected
        kb._SETTINGS._toggle_advanced()
        assert bottom_right(kb._SETTINGS.win) == expected
        kb._SETTINGS.close()

        monkeypatch.setattr(kb, "tray_icon_rect", lambda: anchor)
        kb.open_settings()
        assert bottom_right(kb._SETTINGS.win) == expected
        kb._SETTINGS.close()
    finally:
        root.destroy()


def tray_ui_validate(raw):
    import tray_ui

    return tray_ui.validate_form(raw)


def test_storage_and_autostart_use_current_application_name():
    assert kb.APP_NAME == "PeripheralBatteryTray"
    assert pathlib.Path(kb.CONFIG).parent.name == kb.APP_NAME
    assert pathlib.Path(kb.LOG).parent.name == kb.APP_NAME
    assert kb.autostart_command().endswith(f' "{pathlib.Path(kb.__file__).resolve()}"')
