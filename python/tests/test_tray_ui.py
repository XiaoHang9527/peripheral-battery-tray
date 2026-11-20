"""UI 层单测：设计令牌、状态色、圆环渲染、脉冲、设置表单。

tray_ui 不创建 Tk 根窗口，因此这些用例可以无界面运行。
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tray_ui  # noqa: E402


def test_limits_have_sane_ranges():
    for key in ("interval", "threshold", "log_mb", "log_keep", "log_days"):
        lo, hi, step = tray_ui.LIMITS[key]
        assert lo < hi
        assert step > 0
        assert (hi - lo) / step <= 1000        # 免得 Spinbox 步进几万次


def test_level_color_semantics():
    assert tray_ui.level_color(None) == tray_ui.COLOR["none"]
    assert tray_ui.level_color(100) == tray_ui.COLOR["ok"]
    assert tray_ui.level_color(50) == tray_ui.COLOR["warn"]
    assert tray_ui.level_color(5) == tray_ui.COLOR["low"]
    assert tray_ui.level_color(5, charging=True) == tray_ui.COLOR["charge"]


def test_stale_color_dimmed_but_same_hue():
    full = tray_ui.level_color(50)
    dim = tray_ui.level_color(50, stale=True)
    assert dim != full
    r1, g1, b1 = tray_ui.hex_rgb(full)
    r2, g2, b2 = tray_ui.hex_rgb(dim)
    # 三个通道同比压暗，色相不变
    assert r2 <= r1 and g2 <= g1 and b2 <= b1
    assert abs(g2 / max(g1, 1) - b2 / max(b1, 1)) < 0.05


def test_status_label():
    assert tray_ui.status_label(None) == "无读数"
    assert tray_ui.status_label(50, charging=True) == "充电中"
    assert tray_ui.status_label(50) == "电池供电"
    assert tray_ui.status_label(50, stale=True) == "数据可能过期"
    assert tray_ui.status_label(50, status="放电中") == "放电中"


def test_ring_image_size_and_cache():
    img = tray_ui.ring_image(44, 50)
    assert img.size == (44, 44)
    assert img.mode == "RGBA"
    # 同一组参数复用缓存对象
    assert tray_ui.ring_image(44, 50) is img


def test_ring_image_disconnected_differs_from_stale():
    """断开态要有可区分的视觉（斜杠），不能只是淡化。"""
    stale = tray_ui.ring_image(44, 90, stale=True, dark=True)
    gone = tray_ui.ring_image(44, 90, disconnected=True, dark=True)
    assert stale.tobytes() != gone.tobytes()


def test_status_label_disconnected():
    assert tray_ui.status_label(90, disconnected=True) == "已断开"
    assert tray_ui.status_label(90, stale=True) == "数据可能过期"


def test_ring_image_states_differ():
    normal = tray_ui.ring_image(44, 50, dark=True)
    stale = tray_ui.ring_image(44, 50, stale=True, dark=True)
    charging = tray_ui.ring_image(44, 50, charging=True, dark=True)
    assert normal.tobytes() != stale.tobytes()
    assert normal.tobytes() != charging.tobytes()


def test_ring_image_has_transparent_corners():
    img = tray_ui.ring_image(44, 100)
    assert img.getpixel((0, 0))[3] == 0
    # 圆心区域应有内容（数字或数字间隙），不能整块透明
    box = [img.getpixel((x, y))[3] for x in range(18, 27) for y in range(18, 27)]
    assert max(box) > 0


def test_ring_image_track_visible_without_reading():
    """无读数时轨道仍要画出来，否则任务栏上空一块。"""
    img = tray_ui.ring_image(44, None)
    alphas = [img.getpixel((x, 3))[3] for x in range(8, 36)]
    assert max(alphas) > 0


def test_tray_image_has_no_digits():
    """托盘图标只有 16–24px，画数字糊成一团：应当只有环。"""
    tray = tray_ui.tray_image(50)
    ring = tray_ui.ring_image(tray_ui.TRAY_SIZE, 50, stroke=tray_ui.TRAY_SIZE // 8,
                           text=True, dark=False)
    assert tray.tobytes() != ring.tobytes()
    centre = [tray.getpixel((x, y))[3] for x in range(28, 36)
              for y in range(28, 36)]
    assert max(centre) == 0


def test_ring_light_and_dark_differ():
    dark = tray_ui.ring_image(44, None, dark=True)
    light = tray_ui.ring_image(44, None, dark=False)
    assert dark.tobytes() != light.tobytes()


def test_pulse_alpha_bounds():
    values = [tray_ui.pulse_alpha(i / 16.0) for i in range(16)]
    assert min(values) >= int(255 * tray_ui.PULSE_FLOOR) - 1
    assert max(values) == 255
    assert tray_ui.pulse_alpha(0.0) < tray_ui.pulse_alpha(0.5)


def test_font_cache_is_keyed_by_size():
    """旧实现只有一个全局缓存，先取 30 号后取 24 号会错误复用。"""
    big = tray_ui.font(30)
    small = tray_ui.font(12)
    assert big is not small
    assert tray_ui.font(30) is big


def test_fit_font_shrinks_for_long_text():
    """同样宽度上限下，3 位数要比 1 位数用更小字号才塞得进圆环。"""
    assert (tray_ui.fit_font("100", 24, start_px=20)
            < tray_ui.fit_font("1", 24, start_px=20))
    assert tray_ui.fit_font("100", 24, start_px=20) >= 7


def test_hex_helpers():
    assert tray_ui.hex_rgb("#ffffff") == (255, 255, 255)
    assert tray_ui.rgba("#000000", 128) == (0, 0, 0, 128)


def test_palette_dark_and_light_are_distinct():
    dark, light = tray_ui.Palette(True), tray_ui.Palette(False)
    assert dark.bg != light.bg
    assert dark.track != light.track
    assert dark.card_bg != light.card_bg


def test_ellipsize_keeps_short_names():
    assert tray_ui.ellipsize("AULA F75", 26) == "AULA F75"
    assert tray_ui.ellipsize("", 26) == ""


def test_ellipsize_truncates_long_names():
    out = tray_ui.ellipsize("MX Vertical Advanced Ergonomic Mouse", 26)
    assert out.endswith("…")
    assert len(out) <= 26
    assert out.startswith("MX Vertical")


def test_ellipsize_handles_none():
    assert tray_ui.ellipsize(None, 26) == ""


def test_ellipsize_does_not_leave_trailing_space():
    assert not tray_ui.ellipsize("abc def ghi jkl mno pqr stu", 12).endswith(" …")


def test_cursor_monitor_returns_rect_and_dpi():
    """鼠标所在显示器与 DPI（取不到时允许 (None, None)）。"""
    area, dpi = tray_ui.cursor_monitor()
    if area is None:
        pytest.skip("当前环境拿不到显示器信息")
    left, top, right, bottom = area
    assert right > left and bottom > top
    assert dpi is None or dpi >= 72


def test_monitor_work_area_excludes_taskbar():
    area = tray_ui._monitor_work_area(10, 10)
    if area is None:
        pytest.skip("当前环境拿不到显示器信息")
    left, top, right, bottom = area
    assert right > left and bottom > top


def test_validate_form_clamps_stale_min():
    clean, errors = tray_ui.validate_form({"stale_min": "999"})
    assert clean["stale_min"] == tray_ui.LIMITS["stale_min"][1]
    assert any("掉线保留" in e for e in errors)
    clean, _ = tray_ui.validate_form({"stale_min": "-3"})
    assert clean["stale_min"] == tray_ui.LIMITS["stale_min"][0]


def test_validate_form_returns_errors_in_chinese():
    clean, errors = tray_ui.validate_form({
        "interval": "x", "threshold": "20", "log_max_mb": "1",
        "log_keep": "3", "log_retention_days": "14", "show": {},
    })
    assert errors and "刷新间隔" in errors[0]
    assert clean["interval_sec"] == tray_ui.LIMITS["interval"][0]


def test_validate_form_handles_missing_keys():
    """缺字段按默认值补齐、不报错（加新字段时老调用方不会被打脸）。"""
    clean, errors = tray_ui.validate_form({})
    assert errors == []
    assert clean["interval_sec"] == 60
    assert clean["stale_min"] == 2
    assert set(clean) >= {"interval_sec", "low_threshold", "stale_min",
                          "log_max_mb", "log_keep", "log_retention_days",
                          "show"}


def test_validate_form_empty_string_is_an_error():
    """字段在、但内容为空，属于用户输入问题，要报出来。"""
    clean, errors = tray_ui.validate_form({"interval": "", "threshold": "20"})
    assert errors and "刷新间隔" in errors[0]


def test_popup_uses_the_tray_side_and_stays_inside_second_monitor():
    area = (-1920, 0, 0, 1040)
    assert tray_ui.popup_position((-60, 1044, -12, 1080), area, 330, 170) == (-342, 866)
    assert tray_ui.popup_position((-1918, 0, -1880, 40), (-1920, 40, 0, 1080), 330, 170) == (-1918, 48)
