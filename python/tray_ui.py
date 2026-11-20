"""UI 层：设计令牌、抗锯齿圆环渲染、悬停卡片、设置对话框。

设计约定
--------
* 本模块**不依赖** peripheral_battery_tray：应用状态一律通过参数传入，动作通过回调返回，
  因此可以单独导入、单独测试（导入时不会创建 Tk 根窗口）。
* 颜色、间距、尺寸只在这里定义一次，任务栏圆环、托盘图标、悬停卡片、
  设置面板全部取同一套令牌，避免各处硬编码漂移。
* 圆环用 PIL 4 倍超采样后缩小绘制；tk 的 Canvas 圆弧没有抗锯齿，
  在高分屏上会和系统图标形成明显反差。
"""
import ctypes
import ctypes.wintypes as wt
import math
import os
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import simpledialog, ttk
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------- 设计令牌

# 间距节奏（4 的倍数），所有内边距/间隔都从这里取
PAD_XS, PAD_SM, PAD_MD, PAD_LG, PAD_XL = 2, 4, 8, 12, 16

RING_WINDOW = 48          # 任务栏圆环窗口边长（像素，物理像素）
RING_SIZE = 44            # 圆环外径
RING_STROKE = 4           # 圆环线宽
RING_GAP = 2              # 相邻圆环之间的缝
TRAY_SIZE = 64            # 托盘图标位图边长（系统会缩放到 16–24）
DEFAULT_LOW_THRESHOLD = 20

# 数值范围集中在 UI 层，apply_settings 直接复用，避免两处各写一套
LIMITS = {
    "interval": (10, 600, 10),      # (最小, 最大, 步进) 秒
    "threshold": (5, 50, 5),        # %
    "log_mb": (0.1, 10.0, 0.1),     # MB
    "log_keep": (1, 10, 1),         # 份
    "log_days": (1, 365, 1),        # 天
    # 设备整个消失（拔掉/被供电切断）后，圆环继续显示最后读数的时长。
    # 只用来吸收「重新枚举」这类短暂断线，所以默认是分钟级。
    "stale_min": (0, 240, 1),       # 分钟，0 = 立即隐藏
}

# 状态色（用户可读的语义色，不随深浅色主题变化）
COLOR = {
    "ok": "#2ecc71",       # 电量充足
    "warn": "#f5a623",     # 偏低
    "low": "#ff4d4f",      # 低电量
    "charge": "#3b9dff",   # 充电中
    "none": "#8c8c8c",     # 无读数
}
LIGHT_TEXT_COLOR = {COLOR["ok"]: "#137541", COLOR["warn"]: "#895200",
                    COLOR["low"]: "#a4262c", COLOR["charge"]: "#1d5ba7"}

FONT_FAMILY = "Microsoft YaHei UI"     # 中文优先，回落到 Tk 默认
FONT_NUM = "Segoe UI Semibold"         # 数字用半粗，小字号更清晰

# 低电量脉冲：周期（毫秒）与透明度下限
PULSE_PERIOD = 2400
PULSE_FLOOR = 0.55


def hex_rgb(value):
    """'#rrggbb' -> (r, g, b)。"""
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def rgba(value, alpha=255):
    """'#rrggbb' -> (r, g, b, a)，PIL 用。"""
    r, g, b = hex_rgb(value)
    return (r, g, b, int(alpha))


def is_low(level, charging=False, stale=False, threshold=DEFAULT_LOW_THRESHOLD):
    return level is not None and not charging and not stale and level <= threshold


def level_color(level, charging=False, stale=False, threshold=DEFAULT_LOW_THRESHOLD):
    """电量 -> 语义色（十六进制）。stale 时压暗但不能失去可辨识度。"""
    if level is None:
        return COLOR["none"]
    if charging:
        base = COLOR["charge"]
    elif is_low(level, threshold=threshold):
        base = COLOR["low"]
    elif level >= 60:
        base = COLOR["ok"]
    else:
        base = COLOR["warn"]
    if stale:
        r, g, b = hex_rgb(base)
        return "#%02x%02x%02x" % (int(r * 0.78), int(g * 0.78),
                                  int(b * 0.78))
    return base


def status_label(level, charging=False, stale=False, status="",
                 disconnected=False):
    """状态短语：先标明读数是否仍有效，再显示设备自报的状态。"""
    if disconnected:
        return "已断开"
    if level is None:
        return "无读数"
    if stale:
        return "数据可能过期"
    if status:
        return status
    if charging:
        return "充电中"
    return "电池供电"


class BatteryReading(NamedTuple):
    level: int | None
    caption: str
    status: str
    historical: bool = False


def battery_reading(dev, threshold=DEFAULT_LOW_THRESHOLD):
    level = getattr(dev, "display_level", dev.level)
    if not getattr(dev, "present", True):
        if level is not None:
            return BatteryReading(level, "上次电量", "已断开", True)
        return BatteryReading(None, "电量未知", "已断开")
    if dev.error:
        status = "未收到应答" if "无应答" in dev.error or "休眠" in dev.error else dev.error
        if level is not None:
            return BatteryReading(level, "上次电量", status, True)
        return BatteryReading(None, "电量未知", status)
    if getattr(dev, "wired", False):
        return BatteryReading(None, "电量未知", "已接线")
    if level is None and dev.charging:
        return BatteryReading(None, "电量未知", "充电中")
    caption = "当前电量" if level is not None else "电量未知"
    status = status_label(level, dev.charging, status=dev.status) if level is not None else ""
    if is_low(level, dev.charging, threshold=threshold):
        status = "低电量"
    return BatteryReading(level, caption, status)


class Palette:
    """一套与深/浅色任务栏匹配的配色。"""

    def __init__(self, dark=True):
        self.dark = dark
        if dark:
            self.bg = "#1f1f1f"           # 任务栏面板底色
            self.fg = "#ffffff"
            self.fg_dim = "#a8a8a8"
            self.hint = "#8a8a8a"
            self.card_bg = "#2b2b2b"
            self.card_border = "#474747"
            self.track = (255, 255, 255, 46)
            self.sep = "#3a3a3a"
            self.control = "#383838"
            self.control_hover = "#494949"
            self.control_focus = "#8cbaff"
            self.primary_hover = "#a7caff"
            self.selection = "#303b49"
        else:
            self.bg = "#f3f3f3"
            self.fg = "#1a1a1a"
            self.fg_dim = "#5f5f5f"
            self.hint = "#7a7a7a"
            self.card_bg = "#fbfbfb"
            self.card_border = "#c9c9c9"
            self.track = (0, 0, 0, 38)
            self.sep = "#dcdcdc"
            self.control = "#eaeaea"
            self.control_hover = "#dcdcdc"
            self.control_focus = "#245da6"
            self.primary_hover = "#194b8a"
            self.selection = "#eaf1fa"

    def text_rgba(self, dim=False):
        return rgba(self.fg_dim if dim else self.fg)

    def reading_color(self, level, charging, historical, threshold):
        if level is None or historical:
            return self.fg_dim
        color = level_color(level, charging, threshold=threshold)
        return color if self.dark else LIGHT_TEXT_COLOR[color]

    @property
    def error(self):
        return self.reading_color(0, False, False, DEFAULT_LOW_THRESHOLD)


# ---------------------------------------------------------------- 字体

_FONT_CACHE = {}


def _font_path():
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"):
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts",
                            name)
        if os.path.exists(path):
            return path
    return None


_FONT_PATH = _font_path()


def font(px):
    """按像素取半粗数字字体；缓存以字号为键（旧实现只有单个全局缓存，
    先取 30 号后再取 24 号会错误地复用 30 号）。"""
    px = max(1, int(px))
    f = _FONT_CACHE.get(px)
    if f is None:
        if _FONT_PATH:
            try:
                f = ImageFont.truetype(_FONT_PATH, px)
            except OSError:
                f = None
        if f is None:
            try:
                f = ImageFont.load_default(size=px)
            except TypeError:      # 老版本 Pillow
                f = ImageFont.load_default()
        _FONT_CACHE[px] = f
    return f


def fit_font(text, max_width, start_px=14, floor_px=7):
    """在不超过 max_width 的前提下取最大字号，保证 3 位数不被挤扁。"""
    for px in range(int(start_px), int(floor_px) - 1, -1):
        if font(px).getlength(text) <= max_width:
            return px
    return floor_px


# ---------------------------------------------------------------- 圆环渲染

_IMG_CACHE = {}
_IMG_CACHE_MAX = 96
_SUPERSAMPLE = 4


def _arc_caps(draw, box, start_deg, end_deg, color, radius):
    """在圆弧两端补圆头，纯 arc 是平头的，端点看起来像被切断。"""
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    r = (box[2] - box[0]) / 2.0
    for deg in (start_deg, end_deg):
        rad = math.radians(deg)
        x = cx + r * math.cos(rad)
        y = cy + r * math.sin(rad)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=color)


def _draw_bolt(draw, cx, cy, unit, color):
    """充电闪电图标（纯多边形，不依赖图标字体）。"""
    pts = [(cx + unit * 0.18, cy - unit * 1.00),
           (cx - unit * 0.62, cy + unit * 0.14),
           (cx - unit * 0.06, cy + unit * 0.14),
           (cx - unit * 0.18, cy + unit * 1.00),
           (cx + unit * 0.62, cy - unit * 0.14),
           (cx + unit * 0.06, cy - unit * 0.14)]
    draw.polygon(pts, fill=color)


def ring_image(size=RING_SIZE, level=None, charging=False, stale=False,
               dark=True, alpha=255, stroke=None, palette=None, text=True,
               disconnected=False, threshold=DEFAULT_LOW_THRESHOLD):
    """渲染抗锯齿圆环，返回 RGBA 图像。

    level=None 表示无读数：只画轨道和 "--"。
    charging=True 追加右下角闪电徽标；stale=True 淡化弧线。
    disconnected=True 在淡化之上再画一条斜杠——设备已经不在（拔掉/断电），
    数字只是「最后一次读数」，不能和实时数据长得一样。
    alpha 用于低电量脉冲（0–255）。
    text=False 只画环不画数字——托盘图标只有 16–24px，数字必然糊成一团，
    精确百分比由任务栏圆环和悬停卡片承担。
    """
    palette = palette or Palette(dark)
    stroke = RING_STROKE if stroke is None else stroke
    alpha = max(0, min(255, int(alpha)))
    if disconnected:
        stale = True
    key = (int(size), level, bool(charging), bool(stale),
           palette.track, palette.bg, palette.fg, palette.fg_dim, alpha,
           stroke, bool(text), bool(disconnected), threshold)
    hit = _IMG_CACHE.get(key)
    if hit is not None:
        return hit

    ss = _SUPERSAMPLE
    w = size * ss
    # 弧线和数字分两层画：脉冲/过期只淡化弧线，数字保持可读
    arc_layer = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(arc_layer)
    pad = ss + stroke * ss // 2
    box = (pad, pad, w - pad, w - pad)
    cap_r = stroke * ss / 2.0

    # 轨道：半透明，任何任务栏底色上都不会显脏
    d.arc(box, 0, 360, fill=palette.track, width=stroke * ss)

    frac = 0.0 if level is None else max(0.0, min(1.0, level / 100.0))
    if frac > 0:
        color = rgba(level_color(level, charging, threshold=threshold))
        if frac >= 0.999:
            d.arc(box, 0, 360, fill=color, width=stroke * ss)
        else:
            d.arc(box, -90, -90 + 360 * frac, fill=color, width=stroke * ss)
            _arc_caps(d, box, -90, -90 + 360 * frac, color, cap_r)

    if charging and not stale:
        b = max(11, int(size * 0.34)) * ss        # 徽标直径
        r = b / 2.0
        cx = w - r - ss * 0.5
        cy = w - r - ss * 0.5
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgba(palette.bg),
                  outline=rgba(COLOR["charge"]), width=ss)
        _draw_bolt(d, cx, cy, r * 0.78, rgba(COLOR["charge"]))

    if disconnected:
        # 从左下到右上的斜杠：一眼看出「这数字是旧的，设备已经不在」。
        # 画在弧线层里，这样数字叠在上面仍然可读。
        inset = pad + stroke * ss
        d.line((inset, w - inset, w - inset, inset),
               fill=rgba(COLOR["none"], 200),
               width=max(2, int(stroke * ss * .65)))

    arc_alpha = alpha
    if stale:
        arc_alpha = (alpha * 150) // 255
    if arc_alpha < 255:
        arc_layer.putalpha(
            arc_layer.getchannel("A").point(lambda v: v * arc_alpha // 255))

    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    img.alpha_composite(arc_layer)
    d = ImageDraw.Draw(img)

    if text:
        label = "--" if level is None else str(int(level))
        inner = size - 2 * stroke - PAD_SM
        num_px = fit_font(label, inner, start_px=max(9, int(inner * 0.42)))
        f = font(num_px * ss)
        fill = palette.text_rgba(dim=bool(level is None))
        if level is not None and charging:
            fill = rgba(COLOR["charge"])
        if stale:
            fill = fill[:3] + (150,)
        d.text((w // 2, w // 2 + ss), label, font=f, fill=fill, anchor="mm")

    out = img.resize((size, size), Image.LANCZOS)
    if len(_IMG_CACHE) > _IMG_CACHE_MAX:
        _IMG_CACHE.clear()
    _IMG_CACHE[key] = out
    return out


def pulse_alpha(phase, floor=PULSE_FLOOR):
    """脉冲透明度：phase 为 0–1 的周期位置。"""
    wave = (1.0 - math.cos(phase * 2 * math.pi)) / 2.0      # 0→1→0
    return int(round(255 * (floor + (1.0 - floor) * wave)))


def tray_image(level=None, charging=False, stale=False, dark=False, threshold=DEFAULT_LOW_THRESHOLD):
    """托盘图标：只有环没有数字（16–24px 下数字不可读），线宽更厚。"""
    return ring_image(TRAY_SIZE, level, charging, stale, dark=dark,
                      stroke=max(7, TRAY_SIZE // 8), text=False, threshold=threshold)


# ---------------------------------------------------------------- DPI

def apply_dpi_scaling(root):
    """为使用 Tk 默认尺寸的控件设置系统 DPI。"""
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetDpiForSystem.restype = ctypes.c_uint
        dpi = u32.GetDpiForSystem()
    except Exception:
        dpi = 96
    if not dpi or dpi < 72:
        dpi = 96
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    return dpi


# ---------------------------------------------------------------- 悬停卡片

_U32 = None


def _u32():
    global _U32
    if _U32 is None:
        _U32 = ctypes.WinDLL("user32", use_last_error=True)
        _U32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        _U32.MonitorFromPoint.restype = wt.HMONITOR
        _U32.GetMonitorInfoW.argtypes = [wt.HMONITOR,
                                         ctypes.POINTER(_MONITORINFO)]
        _U32.GetMonitorInfoW.restype = wt.BOOL
        _U32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        _U32.GetAncestor.restype = wt.HWND
        _U32.IsWindow.argtypes = [wt.HWND]
        _U32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        _U32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.UINT]
    return _U32


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


def _monitor_info(x, y):
    try:
        u32 = _u32()
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        mon = u32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)
        if mon and u32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return info
    except Exception:
        pass
    return None


def _monitor_work_area(x, y):
    info = _monitor_info(x, y)
    if info is not None:
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom


def monitor_bounds(x, y):
    info = _monitor_info(x, y)
    if info is not None:
        r = info.rcMonitor
        return r.left, r.top, r.right, r.bottom


def window_handle(win):
    """查找 Tk 内容窗口当前所属的原生顶层窗口。"""
    u32 = _u32()
    inner = win.winfo_id()
    hwnd = u32.GetAncestor(inner, 2)  # GA_ROOT 只沿父窗口查找
    if hwnd and hwnd != inner and u32.IsWindow(hwnd):
        return hwnd


def move_window(win, x, y, width=0, height=0):
    hwnd = window_handle(win)
    if hwnd:
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        if not width or not height:
            flags |= 0x0001  # SWP_NOSIZE
        return bool(_u32().SetWindowPos(hwnd, None, x, y, width, height, flags))


def _monitor_dpi(mon):
    """某个 HMONITOR 的有效 DPI；取不到返回 None。"""
    if not mon:
        return None
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        # MDT_EFFECTIVE_DPI = 0
        if shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0,
                                   ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return dx.value or None
    except Exception:
        pass
    return None


def monitor_at(x, y):
    """返回给定屏幕位置对应的 (工作区, DPI)。"""
    try:
        u32 = _u32()
        mon = u32.MonitorFromPoint(wt.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        if not mon:
            return None, None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        area = None
        if u32.GetMonitorInfoW(mon, ctypes.byref(info)):
            r = info.rcWork
            area = (r.left, r.top, r.right, r.bottom)
        return area, _monitor_dpi(mon)
    except Exception:
        return None, None


def cursor_monitor():
    """返回鼠标所在显示器的 (工作区, DPI)。"""
    try:
        pt = wt.POINT()
        if _u32().GetCursorPos(ctypes.byref(pt)):
            return monitor_at(pt.x, pt.y)
    except Exception:
        pass
    return None, None


def anchor_center(anchor):
    left, top, right, bottom = anchor
    return (left + right) // 2, (top + bottom) // 2


def ellipsize(text, limit=24):
    """超长设备名截断加省略号：卡片宽度不该由最长的那台设备决定。"""
    text = str(text or "")
    if limit <= 1 or len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


class TooltipCard:
    """圆环悬停卡片：设备名 / 大号电量 / 状态 / 更新时间 / 操作提示。

    比系统 tooltip 多几行结构化信息，超过 125 字符的截断限制也随之消失。
    """

    def __init__(self, root, palette=None):
        self.root = root
        self.palette = palette or Palette()
        self._key = None
        self._rect = None
        p = self.palette

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.win.configure(bg=p.card_border)

        # 1px 外框靠底色露边实现，避免额外线条控件
        self.body = tk.Frame(self.win, bg=p.card_bg)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        self.accent = tk.Frame(self.body, bg=COLOR["ok"], width=3)
        self.accent.pack(side="left", fill="y")

        self.content = tk.Frame(self.body, bg=p.card_bg)
        self.content.pack(side="left", fill="both", expand=True,
                          padx=(PAD_MD, PAD_LG), pady=PAD_MD)

        self.title = tk.Label(self.content, text="", bg=p.card_bg, fg=p.fg,
                              font=(FONT_FAMILY, 10, "bold"), anchor="w")
        self.title.pack(fill="x")

        self.row = tk.Frame(self.content, bg=p.card_bg)
        self.row.pack(fill="x", pady=(PAD_SM, 0))
        self.percent = tk.Label(self.row, text="", bg=p.card_bg, fg=p.fg,
                                font=(FONT_FAMILY, 18, "bold"), anchor="w")
        self.percent.pack(side="left")
        self.status = tk.Label(self.row, text="", bg=p.card_bg, fg=p.fg_dim,
                               font=(FONT_FAMILY, 9), anchor="w", justify="left")
        self.status.pack(side="left", padx=(PAD_SM, 0), pady=(0, 4))

        self.meta = tk.Label(self.content, text="", bg=p.card_bg, fg=p.fg_dim,
                             font=(FONT_FAMILY, 9), anchor="w")
        self.meta.pack(fill="x", pady=(PAD_XS, 0))

        self.hint = tk.Label(self.content, text="左键刷新 · 右键菜单", bg=p.card_bg,
                             fg=p.hint, font=(FONT_FAMILY, 8), anchor="w")
        self.hint.pack(fill="x", pady=(PAD_SM, 0))

    def _apply_palette(self, p):
        """切换深浅色时统一改底色/前景，只在这里维护控件清单。"""
        self.win.configure(bg=p.card_border)
        for w in (self.body, self.content, self.row, self.title, self.percent,
                  self.status, self.meta, self.hint):
            w.configure(bg=p.card_bg)
        self.title.configure(fg=p.fg)
        self.status.configure(fg=p.fg_dim)
        self.meta.configure(fg=p.fg_dim)
        self.hint.configure(fg=p.hint)
        self._key = None            # 颜色变了，强制重画一次

    def set_palette(self, palette):
        """外部（主题切换）直接换配色。"""
        if palette is not self.palette:
            self.palette = palette
            self._apply_palette(palette)

    def show_for(self, dev, name, palette=None, refresh="", threshold=DEFAULT_LOW_THRESHOLD):
        """按设备填充内容；内容没变时不重复配置控件（每秒刷新也不闪）。"""
        p = palette or self.palette
        if p is not self.palette:
            self.set_palette(p)

        level, caption, state, historical = battery_reading(dev, threshold)
        color = p.reading_color(level, dev.charging, historical, threshold)
        status = "\n".join(filter(None, (caption, state)))
        meta = self._meta(dev, historical, refresh)
        title = ellipsize(name, 26)
        key = (title, level, color, status, meta)
        if key != self._key:
            self._key = key
            self.title.configure(text=title)
            self.percent.configure(text="--" if level is None
                                   else "%d%%" % level, fg=color)
            self.status.configure(text=status)
            self.meta.configure(text=meta)
            self.accent.configure(bg=color)
            self.win.update_idletasks()

        w = max(self.win.winfo_reqwidth(), 148)
        h = self.win.winfo_reqheight()
        return w, h

    @staticmethod
    def _meta(dev, historical=False, refresh=""):
        if refresh == "pending":
            return "正在刷新…"
        if refresh == "failed":
            return "刷新未完成，可再次点击重试"
        if not dev.updated:
            return "尚未读取电量"
        if historical or getattr(dev, "wired", False):
            return "上次读到电量 %s" % time_hhmm(dev.updated)
        if 0 <= time.time() - dev.updated < 10:
            return "刚刚更新"
        return "更新于 %s" % time_hhmm(dev.updated)

    def place(self, ring_x, ring_y, ring_size, width, height):
        """默认贴在圆环上方；越界则翻到下方，并夹在该圆环所在显示器内。"""
        cx = ring_x + ring_size // 2
        area = _monitor_work_area(cx, ring_y + ring_size // 2)
        if area:
            left, top, right, bottom = area
        else:
            left, top = 0, 0
            right = self.win.winfo_screenwidth()
            bottom = self.win.winfo_screenheight()
        x = cx - width // 2
        y = ring_y - height - PAD_MD
        if y < top + PAD_SM:
            y = ring_y + ring_size + PAD_MD
        x = max(left + PAD_SM, min(right - width - PAD_SM, x))
        y = max(top + PAD_SM, min(bottom - height - PAD_SM, y))
        self._rect = (x, y, width, height)
        if self.win.winfo_viewable():
            move_window(self.win, *self._rect)

    def show(self):
        self.win.deiconify()
        self.win.update_idletasks()
        if self._rect:
            move_window(self.win, *self._rect)
        self.win.lift()
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass

    def hide(self):
        try:
            self.win.withdraw()
        except Exception:
            pass


def time_hhmm(ts):
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except (TypeError, ValueError, OSError):
        return "--:--"


def center_window(win, area):
    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    if area is None:
        area = _monitor_work_area(win.winfo_screenwidth() // 2, win.winfo_screenheight() // 2)
    left, top, right, bottom = area or (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
    x = max(left, (left + right - width) // 2)
    y = max(top, (top + bottom - height) // 2)
    move_window(win, x, y)


def _popup_above(anchor, area):
    return anchor[1] - area[1] >= area[3] - anchor[3]


def popup_position(anchor, area, width, height):
    left, top, right, bottom = area
    anchor_left, anchor_top, anchor_right, anchor_bottom = anchor
    y = anchor_top - height - PAD_MD if _popup_above(anchor, area) else anchor_bottom + PAD_MD
    x = anchor_right - width if anchor_right - left >= right - anchor_left else anchor_left
    return (max(left, min(right - width, x)), max(top, min(bottom - height, y)))


def place_by_anchor(win, anchor):
    """用原生窗口外框定位，让有标题栏的设置也和概览对齐。"""
    win.update_idletasks()
    hwnd = window_handle(win)
    rect = wt.RECT()
    if not hwnd or not _u32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return
    area = _monitor_work_area(*anchor_center(anchor))
    if area is None:
        area = (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
    x, y = popup_position(anchor, area, rect.right - rect.left, rect.bottom - rect.top)
    move_window(win, x, y)


class _PanelControls:
    """两个面板共用的控件、配色和 DPI 尺寸。"""

    def _px(self, value):
        return max(1, round(value * self.dpi / 96))

    def _font(self, size, bold=False, family=FONT_FAMILY):
        return family, -self._px(size), "bold" if bold else "normal"

    def _theme_widget(self, widget, **colors):
        widget.configure(**{option: (widget.master.cget("bg") if role == "parent"
                                     else getattr(self.palette, role))
                            for option, role in colors.items()})
        return widget

    def _scrollbar_style(self, frame):
        p = self.palette
        style = ttk.Style(frame)
        name = f"Battery{self.dpi}{str(p.dark)}.Vertical.TScrollbar"
        # 系统主题的轨道保持浅色，这里复用可着色的 ttk 元素。
        for part in ("trough", "thumb"):
            element = f"BatteryOverview.Scrollbar.{part}"
            if element not in style.element_names():
                style.element_create(element, "from", "clam", f"Vertical.Scrollbar.{part}")
        style.layout(name, [("BatteryOverview.Scrollbar.trough", {
            "sticky": "ns", "children": [("BatteryOverview.Scrollbar.thumb", {
                "expand": True, "sticky": "nswe"})]})])
        style.configure(name, troughcolor=p.card_bg, background=p.hint, bordercolor=p.card_bg,
                        lightcolor=p.hint, darkcolor=p.hint, gripcount=0,
                        arrowsize=self._px(10), borderwidth=0)
        style.map(name, background=[("active", p.fg_dim)])
        return name

    def _button(self, parent, text, command, *, primary=False):
        bg = "control_focus" if primary else "control"
        fg = "card_bg" if primary else "fg"
        button = tk.Button(parent, text=text, command=command, font=self._font(13),
                           relief="flat", bd=0, highlightthickness=self._px(1), takefocus=True,
                           padx=self._px(PAD_MD), pady=self._px(PAD_SM))
        self._theme_widget(button, bg=bg, fg=fg,
                           activebackground="primary_hover" if primary else "control_hover",
                           activeforeground=fg, disabledforeground="fg_dim",
                           highlightbackground="parent", highlightcolor="control_focus")
        button.bind("<Enter>", lambda _: button.configure(bg=button.cget("activebackground"))
                    if button.cget("state") != "disabled" else None)
        button.bind("<Leave>", lambda _: button.configure(bg=getattr(self.palette, bg)))
        button.bind("<Return>", lambda _: self._invoke(button))
        return button

    def _switch(self, parent, text, selected, command=None, textvariable=None):
        switch = tk.Checkbutton(parent, text=text, textvariable=textvariable,
                                variable=selected, command=command,
                                indicatoron=False, image=self._switch_off,
                                selectimage=self._switch_on, compound="right",
                                font=self._font(13), relief="flat", offrelief="flat",
                                bd=0, highlightthickness=self._px(1), takefocus=True,
                                padx=self._px(PAD_MD), pady=self._px(PAD_MD), cursor="hand2")
        self._theme_widget(switch, bg="control", fg="fg", activebackground="control_hover",
                           activeforeground="fg", selectcolor="selection",
                           highlightbackground="parent", highlightcolor="control_focus")
        switch.bind("<Return>", lambda _: self._invoke(switch))
        return switch

    @staticmethod
    def _invoke(widget):
        widget.invoke()
        return "break"

    def _switch_image(self, selected):
        p = self.palette
        width, height = self._px(38), self._px(22)
        scale = 3
        w, h = width * scale, height * scale
        image = Image.new("RGBA", (w, h))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2,
                               fill=p.control_focus if selected else p.bg,
                               outline=None if selected else p.hint,
                               width=self._px(1) * scale)
        inset = self._px(4) * scale
        knob = h - 2 * inset
        x = w - inset - knob if selected else inset
        draw.ellipse((x, inset, x + knob - 1, inset + knob - 1),
                     fill=p.card_bg if selected else p.fg_dim)
        return ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.LANCZOS), master=self.win)

    def _fit_text(self, text, font, width):
        if font.measure(text) <= width:
            return text
        text = text[:80]
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"


class OverviewRow(NamedTuple):
    key: str
    name: str
    level: int | None
    detail: str
    charging: bool
    historical: bool
    ring_shown: bool | None


class DeviceOverview(_PanelControls):
    """共用的电量面板，贴近图标，失去焦点即收起。"""

    def __init__(self, root, *, palette, dpi, on_refresh, on_settings, on_toggle_ring,
                 on_taskbar, on_autostart, on_log, on_quit, on_update=None):
        self.palette = p = palette
        self.dpi = dpi
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.title("设备电量")
        self.win.configure(bg=p.card_border)
        self.win.bind("<Escape>", lambda _: self.hide())
        self.win.bind("<FocusOut>", self._focus_out)
        self.win.bind("<MouseWheel>", self._scroll)
        self._anchor = None
        self._area = None
        self._selected = None
        self._rows = None
        self._refresh = None
        self._threshold = None
        self._row_action = None
        self._on_toggle_ring = on_toggle_ring
        self._list_width = self._px(344)
        self._name_font = tkfont.Font(root=self.win, font=self._font(14, bold=True))
        self._detail_font = tkfont.Font(root=self.win, font=self._font(12))

        body = tk.Frame(self.win, bg=p.card_bg)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(body, bg=p.card_bg, padx=self._px(PAD_XL), pady=self._px(PAD_LG))
        header.pack(fill="x")
        tk.Label(header, text="外设电量", bg=p.card_bg, fg=p.fg, padx=0,
                 font=self._font(18, bold=True)).pack(side="left")
        self.reload = self._button(header, "立即刷新", on_refresh)
        self.reload.pack(side="right")
        self.notice = tk.Label(body, anchor="w", bg=p.card_bg,
                               fg=p.reading_color(0, False, False, DEFAULT_LOW_THRESHOLD),
                               font=self._detail_font)

        frame = tk.Frame(body, bg=p.card_bg)
        self._list_frame = frame
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg=p.card_bg, width=self._list_width, height=1,
                                highlightthickness=0, takefocus=False)
        self.scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview,
                                       style=self._scrollbar_style(frame))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list = tk.Frame(self.canvas, bg=p.card_bg)
        self._list_window = self.canvas.create_window((0, 0), window=self.list, anchor="nw",
                                                      width=self._list_width)
        self.canvas.bind("<Configure>", self._resize_list)
        self.list.bind("<Configure>", self._resize_scrollregion)
        for key, direction, unit in (("Up", -1, "units"), ("Down", 1, "units"),
                                     ("Prior", -1, "pages"), ("Next", 1, "pages")):
            self.win.bind(f"<{key}>", lambda _, n=direction, u=unit: self.canvas.yview_scroll(n, u))

        tk.Frame(body, bg=p.sep, height=1).pack(fill="x")
        footer = tk.Frame(body, bg=p.bg, padx=self._px(PAD_XL), pady=self._px(PAD_MD))
        footer.pack(fill="x")
        actions = self._actions = tk.Frame(footer, bg=p.bg)
        actions.pack(fill="x")
        self.more = self._button(actions, "更多操作", self._toggle_options)
        self.more.configure(width=10)
        self.more.pack(side="left")
        settings_button = self._button(actions, "设置…", lambda: self._open_settings(on_settings))
        self.quit_button = self._button(actions, "退出程序", on_quit)
        self.quit_button.pack(side="right")
        settings_button.pack(side="right", padx=(0, self._px(PAD_MD)))

        self._options = tk.Frame(footer, bg=p.bg)
        switches = tk.Frame(self._options, bg=p.bg)
        switches.pack(fill="x")
        switches.columnconfigure((0, 1), weight=1, uniform="switches")
        self._switch_off = self._switch_image(False)
        self._switch_on = self._switch_image(True)
        self._taskbar = tk.BooleanVar(master=self.win)
        self._autostart = tk.BooleanVar(master=self.win)
        gap = self._px(PAD_SM)
        self._switch(switches, "任务栏圆环", self._taskbar, on_taskbar).grid(
            row=0, column=0, sticky="ew", padx=(0, gap))
        self._switch(switches, "开机自启", self._autostart, on_autostart).grid(
            row=0, column=1, sticky="ew", padx=(gap, 0))
        tools = tk.Frame(self._options, bg=p.bg)
        tools.pack(fill="x", pady=(self._px(PAD_SM), 0))
        self._button(tools, "打开日志", on_log).pack(side="left")
        if on_update is not None:
            self._button(tools, "检查更新", on_update).pack(side="right")

    def update_options(self, taskbar, autostart):
        self._taskbar.set(taskbar)
        self._autostart.set(autostart)

    def _toggle_options(self):
        expanded = not self._options.winfo_manager()
        if expanded:
            gap = self._px(PAD_MD)
            if _popup_above(self._anchor, self._area):
                self._options.pack(fill="x", before=self._actions, pady=(0, gap))
            else:
                self._options.pack(fill="x", after=self._actions, pady=(gap, 0))
        else:
            self._options.pack_forget()
        self.more.configure(text="收起操作" if expanded else "更多操作")
        self._reposition()

    def update(self, rows, refresh, threshold=DEFAULT_LOW_THRESHOLD):
        if rows != self._rows or threshold != self._threshold:
            self._rows = rows
            self._threshold = threshold
            self._render_rows()
        if refresh != self._refresh:
            self._refresh = refresh
            pending = refresh == "pending"
            self.reload.configure(text="正在刷新…" if pending else "立即刷新",
                                  state="disabled" if pending else "normal")
            if refresh == "failed":
                self.notice.configure(text="刷新未完成。可以再次点击「立即刷新」。")
                self.notice.pack(fill="x", padx=self._px(PAD_XL), before=self._list_frame,
                                 pady=(0, self._px(PAD_MD)))
            else:
                self.notice.pack_forget()
            if self.win.winfo_viewable():
                self._reposition()

    def _render_rows(self):
        p = self.palette
        scroll = self.canvas.yview()[0]
        row_action_focused = self._row_action is not None and self.win.focus_get() == self._row_action
        if row_action_focused:
            self.reload.focus_set()
        self._row_action = None
        for child in self.list.winfo_children():
            child.destroy()
        if not self._rows:
            empty = tk.Frame(self.list, bg=p.card_bg, padx=self._px(PAD_XL), pady=self._px(PAD_XL))
            empty.pack(fill="x")
            tk.Label(empty, text="未检测到设备", font=self._name_font, anchor="w",
                     bg=p.card_bg, fg=p.fg).pack(fill="x")
            tk.Label(empty, text="连接接收器或唤醒外设后，点击「立即刷新」。",
                     font=self._detail_font, anchor="w", justify="left", bg=p.card_bg, fg=p.fg_dim,
                     wraplength=self._px(300)).pack(fill="x", pady=(self._px(PAD_SM), 0))
        for index, item in enumerate(sorted(self._rows, key=lambda row: row.key != self._selected)):
            if index:
                tk.Frame(self.list, height=1, bg=p.sep).pack(fill="x", padx=self._px(PAD_XL))
            self._render_row(item)
        if self.win.winfo_viewable():
            self._reposition()
        else:
            self._fit_list()
        self.canvas.yview_moveto(scroll)
        if row_action_focused and self._row_action is not None:
            self._row_action.focus_set()

    def _render_row(self, item):
        p = self.palette
        selected = item.key == self._selected
        bg = p.selection if selected else p.card_bg
        row = tk.Frame(self.list, bg=bg, padx=self._px(PAD_XL), pady=self._px(PAD_LG))
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        text_width = self._list_width - self._px(108)
        tk.Label(row, text=self._fit_text(item.name, self._name_font, text_width),
                 bg=bg, fg=p.fg, font=self._name_font, padx=0,
                 anchor="w").grid(row=0, column=0, sticky="ew")
        tk.Label(row, text=self._fit_text(item.detail, self._detail_font, text_width),
                 bg=bg, fg=p.fg_dim, font=self._detail_font, padx=0,
                 anchor="w").grid(row=1, column=0, sticky="ew")
        value = "—" if item.level is None else "%d%%" % item.level
        color = p.reading_color(item.level, item.charging, item.historical, self._threshold)
        tk.Label(row, text=value, bg=bg, fg=color, font=self._font(23, family=FONT_NUM),
                 padx=0).grid(row=0, column=1, rowspan=2, sticky="e", padx=(self._px(PAD_MD), 0))
        bar = tk.Canvas(row, width=1, height=self._px(4), bg=p.sep,
                        highlightthickness=0, takefocus=False)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(self._px(PAD_MD), 0))
        if item.level is not None:
            bar.bind("<Configure>", lambda event: self._draw_bar(event, item.level, color))
        if selected and item.ring_shown is not None:
            text = "隐藏圆环" if item.ring_shown else "显示圆环"
            self._row_action = self._button(row, text, lambda: self._on_toggle_ring(item.key))
            self._row_action.grid(row=3, column=0, columnspan=2, sticky="w", pady=(self._px(PAD_MD), 0))

    @staticmethod
    def _draw_bar(event, level, color):
        event.widget.delete("level")
        if level > 0:
            event.widget.create_rectangle(0, 0, round(event.width * min(level, 100) / 100),
                                          event.height, fill=color, width=0, tags="level")

    def _resize_list(self, event):
        self.canvas.itemconfigure(self._list_window, width=event.width)

    def _resize_scrollregion(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _fit_list(self):
        self.win.update_idletasks()
        natural = self.list.winfo_reqheight()
        cap = self._px(304)
        if self._anchor is not None:
            above = _popup_above(self._anchor, self._area)
            available = (self._anchor[1] - self._area[1] if above
                         else self._area[3] - self._anchor[3])
            chrome = self.win.winfo_reqheight() - self.canvas.winfo_reqheight()
            if not above and not self._options.winfo_manager():
                # 下方展开时预留操作区高度，设备列表就不会把按钮往上推。
                chrome += self._options.winfo_reqheight() + self._px(PAD_MD)
            cap = min(cap, max(self._px(50), available - chrome - PAD_MD))
        clipped = natural > cap
        if clipped:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        else:
            self.scrollbar.pack_forget()
        self.canvas.configure(width=self._list_width - (self.scrollbar.winfo_reqwidth() if clipped else 0),
                              height=max(1, min(natural, cap)))

    def _reposition(self):
        if self._anchor is not None:
            self._fit_list()
            place_by_anchor(self.win, self._anchor)

    def show(self, anchor, selected, rows, refresh, threshold):
        self._anchor = anchor
        self._area = (_monitor_work_area(*anchor_center(anchor))
                      or (0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight()))
        if selected != self._selected:
            self._selected = selected
            self._rows = None
        self.update(rows, refresh, threshold)
        self.canvas.yview_moveto(0)
        self.win.deiconify()
        self._reposition()
        self.win.lift()
        self.win.focus_force()

    def _focus_out(self, _event):
        self.win.after_idle(self._hide_if_unfocused)

    def _hide_if_unfocused(self):
        focused = self.win.focus_displayof()
        if focused is None or focused.winfo_toplevel() != self.win:
            self.hide()

    def _scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _open_settings(self, on_settings):
        self.hide()
        on_settings(self._anchor)

    def hide(self):
        self.win.withdraw()
        if self._options.winfo_manager():
            self._options.pack_forget()
            self.more.configure(text="更多操作")

    def visible(self):
        return self.alive() and self.win.winfo_viewable()

    def alive(self):
        return self.win.winfo_exists()


# ---------------------------------------------------------------- 设置面板

DEFAULT_THEME = "system"
THEME_OPTIONS = {
    DEFAULT_THEME: "跟随 Windows",
    "light": "浅色",
    "dark": "深色",
}


def normalize_theme(value):
    return value if isinstance(value, str) and value in THEME_OPTIONS else DEFAULT_THEME


FORM_DEFAULTS = {
    "interval": "60",
    "threshold": str(DEFAULT_LOW_THRESHOLD),
    "stale_min": "2",
    "log_max_mb": "1",
    "log_keep": "3",
    "log_retention_days": "14",
}

FORM_FIELDS = {
    "interval": ("interval_sec", "interval", "刷新间隔", int),
    "threshold": ("low_threshold", "threshold", "低电量阈值", int),
    "stale_min": ("stale_min", "stale_min", "掉线保留时长", int),
    "log_max_mb": ("log_max_mb", "log_mb", "单份日志上限", float),
    "log_keep": ("log_keep", "log_keep", "日志保留份数", int),
    "log_retention_days": ("log_retention_days", "log_days", "日志保留天数", int),
}


def validate_form(raw, limits=None):
    """缺字段按默认值补齐。返回规整后的值和待修正字段的提示。"""
    values, errors = _validate_fields(raw, limits)
    return values, list(errors.values())


def _validate_fields(raw, limits=None):
    lim = dict(LIMITS)
    if limits:
        lim.update(limits)
    out, errors = {}, {}
    for form_key, (setting, limit_key, label, cast) in FORM_FIELDS.items():
        lo, hi, _ = lim[limit_key]
        value = str(raw.get(form_key, FORM_DEFAULTS[form_key])).strip()
        limits_text = "%s–%s" % (_fmt(lo, cast), _fmt(hi, cast))
        try:
            v = cast(value.replace(",", ".") if cast is float else value)
            if cast is float and not math.isfinite(v):
                raise ValueError
        except ValueError:
            errors[form_key] = "%s需要填%s（%s）" % (
                label, "整数" if cast is int else "数字", limits_text)
            out[setting] = lo
            continue
        if v < lo or v > hi:
            errors[form_key] = "%s应在 %s 之间" % (label, limits_text)
            v = max(lo, min(hi, v))
        out[setting] = v
    out["log_max_mb"] = round(out["log_max_mb"], 2)
    if "theme" in raw:
        out["theme"] = normalize_theme(raw["theme"])
    show = raw.get("show") or {}
    out["show"] = {"device": True, "mouse": True}
    out["show"].update({key: bool(value) for key, value in show.items()})
    return out, errors


def _fmt(value, cast):
    if cast is float:
        text = ("%.2f" % value).rstrip("0").rstrip(".")
        return text or "0"
    return str(int(value))


class SettingsDeviceRow(NamedTuple):
    key: str
    name: str
    automatic_name: str
    level: int | None
    charging: bool
    stale: bool


class SettingsDialog(_PanelControls):
    """可保存或取消的设置面板，使用与电量面板相同的控件。"""

    def __init__(self, root, values, devices, on_save, palette=None, anchor=None, *,
                 system_theme_dark=None):
        self.on_save = on_save
        self.palette = palette or Palette()
        initial_dark = self.palette.dark
        self._system_theme_dark = system_theme_dark or (lambda: initial_dark)
        self._anchor = anchor
        self._preview_threshold = values["low_threshold"]
        self._fields = {}
        self._field_errors = {}
        self._device_names = {}
        self._device_controls = {}
        self._devices = None
        self._advanced_open = False

        self._area, dpi = monitor_at(*anchor_center(anchor)) if anchor is not None else cursor_monitor()
        self.dpi = dpi or round(root.winfo_fpixels("1i"))
        self._width = self._px(368)
        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.title("设置")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)

        self.vars = {
            field: tk.StringVar(master=win, value=_fmt(cast(values.get(setting, FORM_DEFAULTS[field])), cast))
            for field, (setting, _, _, cast) in FORM_FIELDS.items()
        }
        self.vars["theme"] = tk.StringVar(master=win, value=normalize_theme(values.get("theme")))
        self.vars["show"] = {}
        self.log_hint = tk.StringVar(master=win)
        self.error = tk.StringVar(master=win)
        self._device_font = tkfont.Font(root=win, font=self._font(13))
        self._build(devices, values["show"])

        for field in FORM_FIELDS:
            self.vars[field].trace_add("write", lambda *_, key=field: self._field_changed(key))
        self._refresh_hint()

        win.bind("<Return>", lambda _: self.save())
        win.bind("<Escape>", lambda _: self.close())
        win.bind("<Alt-F4>", lambda _: self.close())
        win.bind("<MouseWheel>", self._scroll)
        win.bind("<FocusIn>", self._keep_focused_visible)

    def _build(self, devices, show):
        win = self._theme_widget(self.win, bg="card_border")
        surface = self._theme_widget(tk.Frame(win), bg="card_bg")
        surface.pack(fill="both", expand=True, padx=1, pady=1)
        self._label(surface, "设置", size=18, bold=True).pack(
            fill="x", padx=self._px(PAD_XL), pady=self._px(PAD_LG))

        frame = self._theme_widget(tk.Frame(surface), bg="card_bg")
        frame.pack(fill="both", expand=True)
        self.canvas = self._theme_widget(
            tk.Canvas(frame, width=self._width, height=1, highlightthickness=0, takefocus=False),
            bg="card_bg")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview,
                                       style=self._scrollbar_style(frame))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = body = self._theme_widget(
            tk.Frame(self.canvas, padx=self._px(PAD_XL), pady=self._px(PAD_MD)), bg="card_bg")
        body.columnconfigure(0, weight=1)
        content = self.canvas.create_window((0, 0), window=body, anchor="nw", width=self._width)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(content, width=e.width))
        body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self._switch_off = self._switch_image(False)
        self._switch_on = self._switch_image(True)

        row = self._section(body, 0, "外观")
        choices = self._theme_widget(tk.Frame(body), bg="card_bg")
        choices.grid(row=row, column=0, columnspan=3, sticky="ew")
        choices.columnconfigure((0, 1, 2), weight=1, uniform="themes")
        self._theme_buttons = {}
        for column, (theme, label) in enumerate(THEME_OPTIONS.items()):
            option = tk.Radiobutton(
                choices, text=label, variable=self.vars["theme"], value=theme,
                command=self._preview_theme, indicatoron=False, relief="flat", offrelief="flat",
                font=self._font(13, bold=True), bd=0, takefocus=True,
                highlightthickness=self._px(1), padx=self._px(PAD_SM),
                pady=self._px(PAD_LG + PAD_XS))
            self._theme_widget(option, selectcolor="control_focus",
                               highlightbackground="parent", highlightcolor="control_focus")
            option.grid(row=0, column=column, sticky="ew",
                        padx=(self._px(PAD_SM) if column else 0, 0))
            option.bind("<Return>", lambda event: self._invoke(event.widget))
            self._theme_buttons[theme] = option
        self._style_theme_buttons()

        row = self._section(body, row + 1, "任务栏圆环")
        self.device_list = self._theme_widget(tk.Frame(body), bg="card_bg")
        self.device_list.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.device_list.columnconfigure(0, weight=1)
        row = self._number_row(body, row + 1, "stale_min", "分钟")
        self._label(body, "设为 0 分钟会在断开后立即隐藏圆环", muted=True).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, self._px(PAD_SM)))

        row = self._section(body, row + 1, "刷新和提醒")
        row = self._number_row(body, row, "interval", "秒")
        row = self._number_row(body, row, "threshold", "%")
        self._label(body, "达到低电量阈值时圆环会缓慢脉冲", muted=True).grid(
            row=row, column=0, columnspan=3, sticky="w")

        self.advanced_button = self._button(body, "高级日志设置", self._toggle_advanced)
        self.advanced_button.grid(row=row + 1, column=0, columnspan=3, sticky="w",
                                  pady=(self._px(PAD_XL), self._px(PAD_SM)))
        self.advanced = self._theme_widget(tk.Frame(body), bg="card_bg")
        self.advanced.grid(row=row + 2, column=0, columnspan=3, sticky="ew")
        self.advanced.columnconfigure(0, weight=1)
        advanced_row = self._number_row(self.advanced, 0, "log_max_mb", "MB")
        advanced_row = self._number_row(self.advanced, advanced_row, "log_keep", "份")
        advanced_row = self._number_row(self.advanced, advanced_row, "log_retention_days", "天")
        self._label(self.advanced, muted=True, textvariable=self.log_hint).grid(
            row=advanced_row, column=0, columnspan=3, sticky="w")
        self._show_advanced(self._advanced_open)

        self._theme_widget(tk.Frame(surface, height=1), bg="sep").pack(fill="x")
        footer = self._theme_widget(
            tk.Frame(surface, padx=self._px(PAD_XL), pady=self._px(PAD_MD)), bg="bg")
        footer.pack(fill="x")
        self.error_label = self._label(footer, textvariable=self.error, error=True,
                                       wraplength=self._width - self._px(2 * PAD_XL))
        buttons = self._footer_buttons = self._theme_widget(tk.Frame(footer), bg="bg")
        buttons.pack(fill="x")
        self._button(buttons, "恢复默认", self.reset).pack(side="left")
        cancel = self._button(buttons, "取消", self.close)
        self._button(buttons, "保存", self.save, primary=True).pack(side="right")
        cancel.pack(side="right", padx=(0, self._px(PAD_MD)))

        self.update_devices(devices, show)

    def _theme_widget(self, widget, **colors):
        widget._palette_roles = colors
        return super()._theme_widget(widget, **colors)

    def _refresh_widget_colors(self, widget):
        colors = getattr(widget, "_palette_roles", None)
        if colors:
            super()._theme_widget(widget, **colors)
        for child in widget.winfo_children():
            self._refresh_widget_colors(child)

    def _style_theme_buttons(self):
        p = self.palette
        theme = self.vars["theme"].get()
        for value, option in self._theme_buttons.items():
            selected = value == theme
            fg = p.card_bg if selected else p.fg
            option.configure(bg=p.control_focus if selected else p.control, fg=fg,
                             activebackground=p.primary_hover if selected else p.control_hover,
                             activeforeground=fg)

    def _preview_theme(self):
        theme = self.vars["theme"].get()
        dark = self._system_theme_dark() if theme == DEFAULT_THEME else theme == "dark"
        if dark != self.palette.dark:
            self.palette = Palette(dark)
            self._refresh_widget_colors(self.win)
            self.scrollbar.configure(style=self._scrollbar_style(self.scrollbar.master))
            for field in self._fields:
                self._style_field_border(field)

            switch_off = self._switch_image(False)
            switch_on = self._switch_image(True)
            for device in self._devices or ():
                switch, _, preview = self._device_controls[device.key]
                switch.configure(image=switch_off, selectimage=switch_on)
                self._render_ring_preview(preview, device)
            self._switch_off, self._switch_on = switch_off, switch_on
        self._style_theme_buttons()

    def _label(self, parent, text="", *, size=13, bold=False, muted=False, error=False, **kwargs):
        label = tk.Label(parent, text=text,
                        font=self._font(12 if muted or error else size, bold), anchor="w",
                        justify="left", padx=0, pady=0, **kwargs)
        return self._theme_widget(label, bg="parent", fg="error" if error else ("fg_dim" if muted else "fg"))

    def _section(self, parent, row, text):
        if row:
            self._theme_widget(tk.Frame(parent, height=1), bg="sep").grid(
                row=row, column=0, columnspan=3, sticky="ew",
                pady=(self._px(PAD_LG), self._px(PAD_LG)))
            row += 1
        self._label(parent, text, size=14, bold=True).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, self._px(PAD_SM)))
        return row + 1

    def _number_row(self, parent, row, field, unit):
        _, limit_key, label, cast = FORM_FIELDS[field]
        lo, hi, step = LIMITS[limit_key]
        self._label(parent, label).grid(row=row, column=0, sticky="w", pady=self._px(PAD_XS))
        entry = tk.Spinbox(parent, from_=lo, to=hi, increment=step, width=8,
                           textvariable=self.vars[field],
                           format="%.2f" if cast is float else "%.0f", justify="left",
                           font=self._font(14, family=FONT_NUM), relief="flat", bd=0,
                           highlightthickness=self._px(1))
        self._theme_widget(entry, bg="bg", fg="fg", insertbackground="fg", buttonbackground="control",
                           selectbackground="control_focus", selectforeground="card_bg")
        entry.grid(row=row, column=1, sticky="e", ipady=self._px(PAD_XS),
                   padx=(self._px(PAD_LG), self._px(PAD_MD)), pady=self._px(PAD_SM))
        entry.bind("<MouseWheel>", self._scroll)
        self._label(parent, unit, muted=True, width=4).grid(row=row, column=2, sticky="w")
        error = self._label(parent, error=True)
        error.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(0, self._px(PAD_SM)))
        error.grid_remove()
        self._fields[field] = (entry, error)
        self._style_field_border(field)
        return row + 2

    def update_devices(self, devices, show):
        if devices == self._devices or self.win.grab_current() not in (None, self.win):
            return
        if self._devices is not None:
            identity = lambda device: (device.key, device.name, device.automatic_name)
            if tuple(map(identity, devices)) == tuple(map(identity, self._devices)):
                for before, device in zip(self._devices, devices):
                    if before != device:
                        self._render_ring_preview(self._device_controls[device.key][2], device)
                self._devices = devices
                return
        layout_changed = self._devices is None or len(devices) != len(self._devices)
        self._devices = devices
        focused = self.win.focus_get()
        focus_target = next(((key, controls.index(focused))
                             for key, controls in self._device_controls.items() if focused in controls), None)
        self._device_controls.clear()
        for widget in self.device_list.winfo_children():
            widget.destroy()
        if not devices:
            self._label(self.device_list, "未检测到设备", muted=True).grid(
                row=0, column=0, sticky="w", pady=self._px(PAD_SM))
        for row, device in enumerate(devices):
            if device.key not in self.vars["show"]:
                self.vars["show"][device.key] = tk.BooleanVar(
                    master=self.win, value=bool(show.get(device.key, True)))
            self._device_row(row, device)
        if layout_changed and self.win.winfo_viewable():
            self._reposition()
        if focus_target is not None:
            key, index = focus_target
            if key in self._device_controls:
                self._device_controls[key][index].focus_set()

    def _device_row(self, row, device):
        key = device.key
        frame = self._theme_widget(tk.Frame(self.device_list), bg="card_bg")
        frame.grid(row=row, column=0, sticky="ew", pady=self._px(PAD_XS))
        frame.columnconfigure(1, weight=1)
        preview = self._label(frame)
        self._render_ring_preview(preview, device)
        preview.grid(row=0, column=0, padx=(0, self._px(PAD_SM)))
        label = tk.StringVar(master=self.win)
        self._set_device_label(label, self._device_names.get(key, device.name) or device.automatic_name)
        switch = self._switch(frame, "", self.vars["show"][key], textvariable=label)
        switch.configure(anchor="w")
        switch.grid(row=0, column=1, sticky="ew")
        rename = self._button(frame, "重命名",
                              lambda: self._rename_device(key, device.name, device.automatic_name, label))
        rename.grid(row=0, column=2, sticky="e", padx=(self._px(PAD_MD), 0))
        self._device_controls[key] = (switch, rename, preview)

    def _render_ring_preview(self, preview, device):
        p = self.palette
        image = ring_image(self._px(36), device.level, device.charging, device.stale, dark=p.dark,
                           palette=p, stroke=self._px(3), threshold=self._preview_threshold)
        photo = ImageTk.PhotoImage(image, master=self.win)
        preview.configure(image=photo)
        preview.image = photo

    def _set_device_label(self, label, name):
        label.set(self._fit_text(name, self._device_font, self._width - self._px(218)))

    def _rename_device(self, key, original, automatic_name, label):
        initial = self._device_names.get(key, original)
        new = simpledialog.askstring("重命名设备", "留空使用自动识别的设备名称",
                                     initialvalue=initial, parent=self.win)
        self.win.grab_set()
        if new is not None:
            self._device_names[key] = new.strip()
            self._set_device_label(label, self._device_names[key] or automatic_name)

    # -- 行为 --------------------------------------------------------

    def _raw(self):
        raw = {key: self.vars[key].get() for key in FORM_FIELDS}
        raw["theme"] = self.vars["theme"].get()
        raw["show"] = {key: var.get() for key, var in self.vars["show"].items()}
        return raw

    def _refresh_hint(self):
        values, errors = _validate_fields(self._raw())
        if "log_max_mb" in errors or "log_keep" in errors:
            self.log_hint.set("")
            return
        mb, keep = values["log_max_mb"], values["log_keep"]
        self.log_hint.set("≈ %d KB/份，最多占用约 %s MB"
                          % (int(round(mb * 1024)),
                             _fmt(mb * (keep + 1), float)))

    def reset(self):
        for field, value in FORM_DEFAULTS.items():
            self.vars[field].set(value)
        self.vars["theme"].set(DEFAULT_THEME)
        for var in self.vars["show"].values():
            var.set(True)
        self.error.set("")
        self.error_label.pack_forget()
        self._preview_theme()
        self._reposition()

    def _show_advanced(self, show):
        self._advanced_open = show
        self.advanced_button.configure(text="收起日志设置" if show else "高级日志设置")
        if show:
            self.advanced.grid()
        else:
            self.advanced.grid_remove()

    def _toggle_advanced(self):
        self._show_advanced(not self._advanced_open)
        self._reposition()
        return "break"

    def _set_field_error(self, field, message):
        _, label = self._fields[field]
        if message:
            self._field_errors[field] = message
            label.configure(text=message)
            label.grid()
        else:
            self._field_errors.pop(field, None)
            label.grid_remove()
        self._style_field_border(field)

    def _style_field_border(self, field):
        entry, _ = self._fields[field]
        p = self.palette
        invalid = field in self._field_errors
        entry.configure(highlightbackground=p.error if invalid else p.card_border,
                        highlightcolor=p.error if invalid else p.control_focus)

    def _field_changed(self, field):
        if field in self._field_errors:
            _, errors = _validate_fields(self._raw())
            self._set_field_error(field, errors.get(field))
            self._reposition()
        if field in ("log_max_mb", "log_keep"):
            self._refresh_hint()

    def save(self):
        values, errors = _validate_fields(self._raw())
        self.error_label.pack_forget()
        for field in self._fields:
            self._set_field_error(field, errors.get(field))
        if errors:
            if any(key.startswith("log_") for key in errors):
                self._show_advanced(True)
            self._reposition()
            first, message = next(self._fields[key] for key in self._fields if key in errors)
            first.focus_set()
            first.selection_range(0, "end")
            self._scroll_to(first, message)
            return
        try:
            values["device_names"] = dict(self._device_names)
            self.on_save(values)
        except Exception as exc:        # pythonw 下异常没有窗口，直接写在面板里
            self._show_error("保存失败：%s" % exc)
            return
        self.close()

    def _show_error(self, text):
        self.error.set(text)
        self.error_label.pack(fill="x", before=self._footer_buttons, pady=(0, self._px(PAD_SM)))
        self._reposition()

    def _scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _keep_focused_visible(self, event):
        widget = event.widget
        if str(widget).startswith(f"{self.body}."):
            self._scroll_to(widget)

    def _scroll_to(self, widget, last=None):
        last = last or widget
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = last.winfo_rooty() - self.body.winfo_rooty() + last.winfo_height()
        scroll_y = self.canvas.canvasy(0)
        viewport = self.canvas.winfo_height()
        height = self.body.winfo_height()
        if top < scroll_y:
            self.canvas.yview_moveto(top / height)
        elif bottom > scroll_y + viewport:
            self.canvas.yview_moveto((bottom - viewport) / height)

    def _fit_content(self):
        self.win.update_idletasks()
        height = self.body.winfo_reqheight()
        screen_height = (self._area[3] - self._area[1]
                         if self._area else self.win.winfo_screenheight())
        chrome = self.win.winfo_reqheight() - self.canvas.winfo_reqheight()
        available = max(self._px(52), screen_height - chrome - self._px(2 * PAD_XL))
        clipped = height > available
        if clipped:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        else:
            self.scrollbar.pack_forget()
        self.canvas.configure(width=self._width - (self.scrollbar.winfo_reqwidth() if clipped else 0),
                              height=max(1, min(height, available)))

    def _reposition(self):
        self._fit_content()
        if self._anchor is None:
            center_window(self.win, self._area)
        else:
            place_by_anchor(self.win, self._anchor)

    def show(self):
        if self.win.grab_current() not in (None, self.win):
            return
        was_visible = self.win.winfo_viewable()
        if not was_visible:
            self._fit_content()
        self.win.deiconify()
        self.win.update_idletasks()
        if not was_visible:
            self._reposition()
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()

    def alive(self):
        try:
            return bool(self.win) and self.win.winfo_exists()
        except Exception:
            return False
