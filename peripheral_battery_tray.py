"""外设电量（Peripheral Battery Tray）：任务栏圆环 + 托盘图标。

支持两类设备：
  1) Compx 2.4G 接收器 (3554:FA09) —— 键盘电量，命令 0x4A；型号，命令 0x05
  2) Logitech Unifying (046D:C52B) —— HID++ 2.0：0x1000 电量、0x0005 设备名

任务栏上每个设备一个圆环，贴着通知区域左侧依次排列；悬停显示型号/电量/状态。
"""
import ctypes
import ctypes.wintypes as wt
import collections
import hashlib
import json
import os
import sys
import threading
import time
import traceback
import winreg

import pystray

from battery_protocols import (BATTERY_STATUS, CMD_BAT, CMD_MODEL, MODEL_NAMES,
                     REPORT_ID, REPORT_LEN, STATE_CABLE,
                     checksum, is_reply, parse_compx_battery,
                     parse_compx_model, parse_hidpp_battery)
import tkinter as tk
from PIL import ImageTk

import tray_ui
from tray_ui import LIMITS, RING_WINDOW

APP_NAME = "PeripheralBatteryTray"
DISPLAY_NAME = "外设电量"

# --- Compx 2.4G 接收器（协议常量与解析在 battery_protocols）
VID, PID = 0x3554, 0xFA09

# --- Logitech Unifying / HID++
LOGITECH_VID = 0x046D
HIDPP_BATTERY = 0x1000
HIDPP_NAME = 0x0005

APPDIR = os.path.join(os.environ["APPDATA"], APP_NAME)
LOCALDIR = os.path.join(os.environ["LOCALAPPDATA"], APP_NAME)
CONFIG = os.path.join(APPDIR, "config.json")
LOG = os.path.join(LOCALDIR, "tray.log")
LAST = os.path.join(LOCALDIR, "last.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

os.makedirs(APPDIR, exist_ok=True)

from hid_transport import GUID, close_all, enum_hid, get_link, hid_path, k32

u32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

try:
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------- 日志/配置

_log_lock = threading.Lock()
_config_lock = threading.Lock()

MB_KB = 1024                  # 1 MB 记作 1024 KB
LOG_MAX_MB = 1.0              # 单份上限默认 1 MB（范围见 tray_ui.LIMITS["log_mb"]）
LOG_KEEP = 3                  # 保留 3 份历史（tray.log.1 ~ .3）
LOG_RETENTION_DAYS = 14       # 超过 14 天的分片直接删除

# 设备整个消失（拔掉 / 被供电切断）后，圆环继续显示最后读数的时长。
# 只用来吸收「重新枚举」这类短暂断线，所以默认是分钟级：拔掉的设备
# 不该在任务栏上留很久。
STALE_MIN_DEFAULT = 2
STALE_MIN_LEGACY_HOURS = 6      # 旧配置是 stale_hours，迁移时按小时换算

_INF = float("inf")


def _to_float(value):
    """转 float；非法输入、NaN、Inf 一律返回 None。"""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val != val or val in (_INF, -_INF):
        return None
    return val


def clamp_mb(value, fallback=LOG_MAX_MB, lo=None, hi=None):
    """把任意输入压到 [lo, hi] MB，保留两位小数；非法输入用 fallback。

    lo/hi 缺省取 tray_ui.LIMITS，保证「面板范围」和「落盘范围」只有一处定义。
    """
    if lo is None or hi is None:
        lim_lo, lim_hi, _ = LIMITS["log_mb"]
        lo = lim_lo if lo is None else lo
        hi = lim_hi if hi is None else hi
    val = _to_float(value)
    if val is None:
        val = _to_float(fallback)
    if val is None:
        val = LOG_MAX_MB
    return round(max(lo, min(hi, val)), 2)


def fmt_mb(value):
    """MB 数值 → 显示文本（1.0 → '1'，0.50 → '0.5'）。"""
    text = ("%.2f" % clamp_mb(value, lo=0.0, hi=1e6)).rstrip("0")
    return text.rstrip(".") or "0"


def mb_bytes(value):
    """MB → 字节，按整 KB 取整，保证上限始终是 KB 的整数倍。"""
    return int(round(clamp_mb(value, lo=0.01) * MB_KB)) * 1024


def rotate_log(path=None, keep=None):
    """把 path 轮转成 path.1，旧分片依次后移，超出 keep 的丢掉。"""
    path = path or LOG
    keep = LOG_KEEP if keep is None else keep
    try:
        if not os.path.exists(path):
            return False
        oldest = "%s.%d" % (path, keep)
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src_i = "%s.%d" % (path, i)
            if os.path.exists(src_i):
                os.replace(src_i, "%s.%d" % (path, i + 1))
        os.replace(path, path + ".1")
        return True
    except OSError:
        return False


def prune_logs(directory=None, days=None):
    """删除超过保留期的日志分片，返回删除个数。"""
    directory = directory or LOCALDIR
    if days is None:
        days = log_limits()[2]
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(os.path.basename(LOG) + "."):
            continue
        p = os.path.join(directory, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


def log_limits():
    """当前生效的日志参数：(单份字节数, 保留份数, 保留天数)。

    范围同样取自 tray_ui.LIMITS，配置被手改成离谱值也不会撑爆磁盘。
    """
    size = mb_bytes(CFG.get("log_max_mb", LOG_MAX_MB))
    keep = _to_float(CFG.get("log_keep", LOG_KEEP))
    days = _to_float(CFG.get("log_retention_days", LOG_RETENTION_DAYS))
    keep = int(keep) if keep is not None else LOG_KEEP
    days = int(days) if days is not None else LOG_RETENTION_DAYS
    k_lo, k_hi, _ = LIMITS["log_keep"]
    d_lo, d_hi, _ = LIMITS["log_days"]
    return size, max(k_lo, min(k_hi, keep)), max(d_lo, min(d_hi, days))


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    max_bytes, keep, _ = log_limits()
    with _log_lock:
        try:
            os.makedirs(LOCALDIR, exist_ok=True)
            if os.path.exists(LOG) and os.path.getsize(LOG) > max_bytes:
                rotate_log(LOG, keep)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def log_crash(kind, text):
    """pythonw 没有 stderr，未捕获异常写入同一份可轮转的诊断日志。"""
    log("%s 未捕获异常\n%s" % (kind, text.rstrip()))


def install_crash_hooks(root=None):
    """接管三条异常通道：主线程、子线程、Tk 回调。"""
    def hook(exc_type, exc, tb):
        log_crash("主线程", "".join(traceback.format_exception(
            exc_type, exc, tb)))

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        def thook(args):
            log_crash("线程 %s" % getattr(args.thread, "name", "?"),
                      "".join(traceback.format_exception(
                          args.exc_type, args.exc_value, args.exc_traceback)))
        threading.excepthook = thook
    if root is not None:
        def tk_hook(exc_type, exc, tb):
            log_crash("Tk 回调", "".join(traceback.format_exception(
                exc_type, exc, tb)))
        root.report_callback_exception = tk_hook


def load_config():
    cfg = {"interval_sec": 60, "low_threshold": 20, "taskbar": True,
           "device_name": "", "mouse_name": "",
           "show": {"device": True, "mouse": True},
           "stale_min": STALE_MIN_DEFAULT,
           "log_max_mb": LOG_MAX_MB, "log_keep": LOG_KEEP,
           "log_retention_days": LOG_RETENTION_DAYS}
    raw = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cfg.update(raw)
    if "log_max_mb" not in raw:
        # 旧版本用 KB 记这个值，迁移一次（1 MB = 1024 KB）
        old_kb = _to_float(raw.get("log_max_kb"))
        if old_kb is not None:
            cfg["log_max_mb"] = clamp_mb(old_kb / MB_KB)
    cfg.pop("log_max_kb", None)
    if "stale_min" not in raw:
        # 旧版本用「小时」且默认 6 小时；换算成分钟，顺手夹到新范围
        old_h = _to_float(raw.get("stale_hours"))
        if old_h is not None:
            lo, hi, _ = LIMITS["stale_min"]
            cfg["stale_min"] = int(max(lo, min(hi, round(old_h * 60))))
    cfg.pop("stale_hours", None)
    return cfg


def save_config(changes=None):
    with _config_lock:
        try:
            snapshot = dict(CFG)
            if changes is not None:
                snapshot.update(changes)
            snapshot["show"] = dict(CFG["show"])
            if changes is not None:
                snapshot["show"].update(changes["show"])
            with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(CONFIG + ".tmp", CONFIG)
        except OSError:
            if changes is not None:
                raise
        else:
            if changes is not None:
                CFG.update({**changes, "show": snapshot["show"]})


def load_last():
    try:
        with open(LAST, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_LAST_SIG = None


def save_last():
    global _LAST_SIG
    data = {}
    for d in DEVICES:
        if d.present and (d.level is not None or d.name or d.model_id is not None or d.slot):
            data[d.key] = {"level": d.level, "charging": d.charging,
                           "status": d.status, "ts": d.updated,
                           "name": d.name, "model_id": d.model_id,
                           "wired": d.wired,
                           "receiver": d.receiver, "slot": d.slot, "kind": d.kind}
    sig = json.dumps(data, sort_keys=True)
    if sig == _LAST_SIG:
        return
    try:
        os.makedirs(LOCALDIR, exist_ok=True)
        with open(LAST + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(LAST + ".tmp", LAST)
        _LAST_SIG = sig
    except OSError:
        pass


# ---------------------------------------------------------------- Win32

u32.FindWindowW.restype = wt.HWND
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.FindWindowExW.restype = wt.HWND
u32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.IsWindow.argtypes = [wt.HWND]
u32.GetWindow.restype = wt.HWND
u32.GetWindow.argtypes = [wt.HWND, wt.UINT]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.GetForegroundWindow.restype = wt.HWND
u32.GetSystemMetrics.restype = ctypes.c_int
u32.GetSystemMetrics.argtypes = [ctypes.c_int]
u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
HWND_TOPMOST = wt.HWND(-1)
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010 | 0x0200
# 只移动/改尺寸，不碰 z 序（层级由 _topmost/adopt 单独负责）
GW_HWNDPREV = 3
GW_CHILD = 5
GW_HWNDNEXT = 2
GW_OWNER = 4
GWL_HWNDPARENT = -8             # 顶层窗口用它设「宿主」关系
COVER_WALK_LIMIT = 24          # 检查「谁压在我上面」时最多往上找多少个窗口


def window_pid(hwnd):
    """窗口所属进程 ID。"""
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _set_window_long(hwnd, index, value):
    """SetWindowLongPtrW 在 32 位系统不存在，这里做一次兼容。"""
    fn = getattr(u32, "SetWindowLongPtrW", None)
    if fn is not None:
        fn.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        fn.restype = ctypes.c_void_p
        return fn(hwnd, index, ctypes.c_void_p(value))
    u32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
    return u32.SetWindowLongW(hwnd, index, ctypes.c_void_p(value))


def taskbar_hwnd():
    return u32.FindWindowW("Shell_TrayWnd", None)

shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("guidItem", GUID)]


shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wt.RECT)]


def tray_icon_rect():
    # pystray Win32 以自己的消息窗口和 id(icon) 作为通知区图标标识。
    hwnd = getattr(ICON, "_hwnd", None)
    if hwnd:
        icon_id = NOTIFYICONIDENTIFIER()
        icon_id.cbSize = ctypes.sizeof(icon_id)
        icon_id.hWnd = hwnd
        icon_id.uID = id(ICON) & 0xffffffff
        r = wt.RECT()
        if shell32.Shell_NotifyIconGetRect(ctypes.byref(icon_id), ctypes.byref(r)) == 0:
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom

# ---- 热插拔（WM_DEVICECHANGE）与系统设置变化
WM_DEVICECHANGE = 0x0219
WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A      # 主题/个性化变化时系统广播
WM_THEMECHANGED = 0x031A
WM_DISPLAYCHANGE = 0x007E      # 分辨率/任务栏位置变化
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
# WM_USER 以下的系统消息不能 PostMessage（会返回 ERROR_MESSAGE_SYNC_ONLY），
# 所以退出用一个自定义消息，绝不能直接 Post WM_DESTROY。
WM_APP_STOP = 0x8000 + 1
DBT_DEVTYP_DEVICEINTERFACE = 0x0005
DEVICE_NOTIFY_ALL_INTERFACE_CLASSES = 0x4

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM,
                             wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [("dbcc_size", wt.DWORD), ("dbcc_devicetype", wt.DWORD),
                ("dbcc_reserved", wt.DWORD), ("dbcc_classguid", GUID),
                ("dbcc_name", wt.WCHAR * 1)]


k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
                                ctypes.c_void_p]
u32.DefWindowProcW.restype = ctypes.c_void_p
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.RegisterDeviceNotificationW.restype = wt.HANDLE
u32.RegisterDeviceNotificationW.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                            wt.DWORD]
u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.PostQuitMessage.argtypes = [ctypes.c_int]


WS_CAPTION = 0x00C00000
GWL_STYLE = -16
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "Windows.UI.Core.CoreWindow", "MultitaskingViewFrame"}
QUNS_FULLSCREEN, QUNS_PRESENTATION = 3, 4


def _ask(inst, req, cmd, attempts=3, wait=0.7):
    link = get_link(hid_path(inst), REPORT_LEN)
    frames = []
    for _ in range(attempts):
        r, frames = link.exchange(req, lambda f: is_reply(f, cmd), wait)
        if r is not None:
            return r, frames, None
        time.sleep(0.2)
    return None, frames, "无应答（键盘休眠/关机？）"


def find_compx(devs=None):
    for d in (devs if devs is not None else enum_hid()):
        if (d["vid"], d["pid"]) == (VID, PID) and d["page"] == 0xFF02 \
                and d["inl"] == REPORT_LEN and d["outl"] == REPORT_LEN:
            return d["inst"]
    return None


def compx_battery(inst):
    req = bytearray(REPORT_LEN)
    req[0], req[1] = REPORT_ID, CMD_BAT
    req[REPORT_LEN - 1] = checksum(req)
    r, _, err = _ask(inst, req, CMD_BAT)
    if r is None:
        return None, err
    try:
        return parse_compx_battery(r), None
    except ValueError as e:
        return None, str(e)


def compx_model(inst):
    req = bytearray(REPORT_LEN)
    req[0], req[1], req[2] = REPORT_ID, CMD_MODEL, 1
    req[REPORT_LEN - 1] = checksum(req)
    r, _, err = _ask(inst, req, CMD_MODEL, attempts=2, wait=0.6)
    if r is None:
        return None, err
    try:
        return parse_compx_model(r), None
    except (ValueError, IndexError) as e:
        return None, str(e)


# ---------------------------------------------------------------- Logitech HID++

_HIDPP_CACHE = {}
_LOGITECH_SCAN_DUE = 0.0
LOGITECH_SCAN_INTERVAL = 60
HIDPP_TYPES = {0: "键盘", 1: "遥控器", 2: "数字键盘", 3: "鼠标",
               4: "触控板", 5: "轨迹球", 6: "演示器"}


def _set_error(dev, msg):
    if dev.error != msg and not msg.startswith("未发现") and "无应答" not in msg:
        log("读取%s失败：%s" % ("Compx" if dev is KB else "Logitech", msg))
    dev.error = msg




def hidpp_request(path, dev, feat, fn, params=b"", swid=1, timeout=0.35):
    # 用非零软件 ID 区分查询应答和设备主动上报。
    p = bytearray(20)
    p[0], p[1], p[2], p[3] = 0x11, dev, feat, (fn << 4) | swid
    p[4:4 + len(params)] = params

    def match(f):
        return (len(f) >= 7 and f[0] == 0x11 and f[1] == dev and f[2] == feat
                and (f[3] >> 4) == fn and (f[3] & 0x0F) == swid)

    r, _ = get_link(path, 20).exchange(bytes(p), match, timeout)
    return r


def hidpp_feature(path, dev, feature):
    key = (path, dev, feature)
    if key in _HIDPP_CACHE:
        return _HIDPP_CACHE[key]
    r = hidpp_request(path, dev, 0x00, 0x00,
                      bytes([feature >> 8, feature & 0xFF]))
    if r is None or len(r) <= 4:
        return None
    _HIDPP_CACHE[key] = r[4]
    return r[4]


def hidpp_name(path, dev):
    idx = hidpp_feature(path, dev, HIDPP_NAME)
    if not idx:
        return ""
    r = hidpp_request(path, dev, idx, 0x00)
    if not r or len(r) < 5:
        return ""
    length = r[4]
    name = b""
    for off in range(0, max(length, 1), 16):
        rr = hidpp_request(path, dev, idx, 0x01, bytes([off]))
        if not rr:
            return ""
        name += bytes(rr[4:4 + min(16, length - len(name))])
    return name.decode("utf-8", "replace").strip("\x00 ")


def hidpp_battery(path, dev):
    idx = hidpp_feature(path, dev, HIDPP_BATTERY)
    if idx is None:
        return None, "电量特性查询无应答"
    if idx == 0:
        return None, "不支持 0x1000 电量特性"
    r = hidpp_request(path, dev, idx, 0x00)
    if not r:
        return None, "电量查询无应答"
    try:
        return parse_hidpp_battery(r, dev), None
    except ValueError as e:
        return None, str(e)


def logitech_paths(devs):
    return {hid_path(d["inst"]) for d in devs
            if d["vid"] == LOGITECH_VID and d["page"] == 0xFF00
            and d["inl"] == 20 and d["outl"] == 20}


# ---------------------------------------------------------------- 设备模型

class Device:
    def __init__(self, key, kind, receiver=None, slot=None):
        self.key = key
        self.kind = kind
        self.receiver = receiver
        self.slot = slot
        self.present = False
        self.disconnected_at = None
        self.next_due = 0.0        # 下次轮询时间戳
        self.fails = 0             # 连续失败次数（用于退避）
        self.clear_reading()

    def clear_reading(self):
        self.level = None
        self.charging = False
        self.wired = False
        self.status = ""
        # error 只表示「这次没读到」的具体原因，作为「是否过期」的判据；
        # 初始为空，不要塞「启动中」这类状态文案，否则新设备一上来就算过期。
        self.error = ""
        self.updated = None
        self.model_id = None
        self.name = ""
        self.alerted = False

    @property
    def display_level(self):
        return None if self.present and self.wired else self.level

    def set_present(self, present):
        if present and not self.present:
            self.next_due = 0.0
            self.fails = 0
        if present:
            self.disconnected_at = None
        elif self.present:
            self.disconnected_at = time.time()
        self.present = present

    def display_name(self):
        custom = (CFG.get(("%s_name" % self.key)) or "").strip()
        return custom or self.automatic_name()

    def automatic_name(self):
        if self.name:
            return self.name
        if self.model_id in MODEL_NAMES:
            return MODEL_NAMES[self.model_id]
        if self.model_id is not None:
            return "型号 0x%02X" % self.model_id
        return "2.4G %s" % self.kind


CFG = load_config()
KB = Device("device", "键盘")
DEVICES = [KB]
ICON = None
ROOT = None
PANELS = []


def logitech_key(receiver, slot):
    digest = hashlib.sha256(receiver.encode("utf-8")).hexdigest()[:16]
    return "logitech_%s_%d" % (digest, slot)


def restore_devices(last, hid_devs):
    """只有本机此次枚举到同一接收器时，才恢复它的上次读数。"""
    inst = find_compx(hid_devs)
    compx = hid_path(inst) if inst else None
    paths = logitech_paths(hid_devs)
    for key, saved in last.items():
        if not isinstance(saved, dict):
            continue
        receiver = saved.get("receiver")
        if key == KB.key:
            if compx is None or receiver != compx:
                continue
            dev = KB
            dev.receiver = compx
        else:
            slot = saved.get("slot")
            if (not isinstance(receiver, str) or receiver not in paths or type(slot) is not int
                    or not 1 <= slot <= 6 or key != logitech_key(receiver, slot)):
                continue
            dev = Device(key, saved.get("kind") or "Logitech 设备", receiver, slot)
            DEVICES.append(dev)
        dev.level = saved.get("level")
        dev.charging = bool(saved.get("charging"))
        dev.wired = bool(saved.get("wired"))
        dev.status = saved.get("status") or ""
        dev.updated = saved.get("ts")
        dev.name = saved.get("name") or ""
        dev.model_id = saved.get("model_id")
        dev.error = "等待读取"
        dev.set_present(True)


def is_shown(dev):
    return bool(CFG.get("show", {}).get(dev.key, True))


# 设备消失后在任务栏保留圆环的秒数，0 表示立即隐藏。
def stale_window():
    """当前配置下的掉线保留秒数。"""
    minutes = _to_float(CFG.get("stale_min", STALE_MIN_DEFAULT))
    if minutes is None:
        minutes = STALE_MIN_DEFAULT
    lo, hi, _ = LIMITS["stale_min"]
    minutes = max(lo, min(hi, minutes))
    return int(minutes * 60)


def is_visible(dev):
    """是否应该在任务栏占一个圆环位。

    分两种情况，不能一刀切：
      * 设备还在、只是没回应（鼠标休眠）——圆环一直留着，淡化了而已；
      * 设备整个从设备树上消失（拔掉/被供电切断）——只保留 STALE_MIN 分钟，
        用来吸收重新枚举的短暂断线，之后收起，避免拔掉后还挂着幽灵圆环。
    """
    return is_shown(dev) and has_status(dev)


def has_status(dev):
    if dev.present:
        return True
    expiry = stale_expiry(dev)
    return expiry is not None and time.time() < expiry


def stale_expiry(dev):
    if dev.present or dev.level is None or dev.updated is None:
        return None
    window = stale_window()
    if window <= 0:
        return None
    if dev.disconnected_at is None:
        return None
    return dev.disconnected_at + window


def describe(dev, with_mode=True, with_time=False, name_limit=None):
    """单个设备的状态文案；调用方只决定要多详细。

    原来悬停卡片、菜单标题、托盘 tooltip 各写了一份拼装逻辑，
    措辞和字段容易各改各的。name_limit 用于受长度限制的场合
    （Windows 通知图标 tooltip 只有 127 字符）。
    """
    name = dev.display_name()
    if name_limit:
        name = tray_ui.ellipsize(name, name_limit)
    reading = tray_ui.battery_reading(dev, CFG["low_threshold"])
    if reading.level is None:
        if reading.status == "已接线":
            return "%s：已接线（电量未知）" % name
        return "%s：%s" % (name, reading.status or "无读数")
    if reading.historical:
        line = "%s：上次电量 %d%% · %s" % (name, reading.level, reading.status)
        if with_time and dev.updated:
            line += " · %s" % tray_ui.time_hhmm(dev.updated)
        return line
    line = "%s：%d%%" % (name, reading.level)
    if with_mode:
        line += " " + reading.status
    elif reading.status == "低电量":
        line += " 低电量"
    elif dev.charging:
        line += " 充电中"
    if with_time and dev.updated:
        line += " · %s" % time.strftime("%H:%M", time.localtime(dev.updated))
    return line


TOOLTIP_LIMIT = 125          # Windows 通知图标 tooltip 上限 127 字符


def status_lines():
    return tray_status_lines(18)


def tray_status_lines(name_limit):
    _, indicated = lowest_level()
    devices = sorted((d for d in DEVICES if has_status(d)), key=lambda d: d is not indicated)
    return [describe(d, with_mode=False, name_limit=name_limit) for d in devices] or ["未发现 2.4G 外设"]


def status_text():
    text = " | ".join(status_lines())
    if len(text) > TOOLTIP_LIMIT:       # 真超了才截，截完仍能看出两台设备
        text = text[:TOOLTIP_LIMIT - 1] + "…"
    return text


# ---------------------------------------------------------------- 图标

# 图标/圆环的绘制全部在 tray_ui（设计令牌、抗锯齿、状态语义）；
# 这里只保留应用侧的薄封装，避免同一套视觉在两处各写一遍。
def level_color(level, charging=False):
    return tray_ui.level_color(level, charging, threshold=CFG["low_threshold"])


def make_image(level, charging=False, stale=False):
    """托盘图标（Windows 会把 64px 缩到 16–24px，需留足线宽）。"""
    return tray_ui.tray_image(level, charging, stale, dark=pystray_dark(),
                           threshold=CFG["low_threshold"])


def pystray_dark():
    try:
        return taskbar_theme_dark()
    except Exception:
        return True


# ---------------------------------------------------------------- 开机自启

def autostart_command():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def is_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(val)
    except OSError:
        return False


def set_autostart(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        log("开机自启 -> %s" % on)
    except OSError as e:
        log("写注册表失败: %s" % e)


# ---------------------------------------------------------------- 任务栏

def taskbar_theme_dark():
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 0
    except OSError:
        return True


def query_notification_state():
    st = ctypes.c_int(-1)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0:
            return st.value
    except Exception:
        pass
    return -1


def foreground_fullscreen():
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(128)
    u32.GetClassNameW(hwnd, cls, 128)
    if cls.value in SHELL_CLASSES:
        return False
    r = wt.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    bar = wt.RECT()
    bounds = None
    if u32.GetWindowRect(taskbar_hwnd(), ctypes.byref(bar)):
        bounds = tray_ui.monitor_bounds((bar.left + bar.right) // 2, (bar.top + bar.bottom) // 2)
    l, t, right, bottom = bounds or (0, 0, u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))
    if not (r.left <= l + 2 and r.top <= t + 2 and r.right >= right - 2
            and r.bottom >= bottom - 2):
        return False
    if not (u32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION):
        return True
    return query_notification_state() in (QUNS_FULLSCREEN, QUNS_PRESENTATION)


def _child_windows(hwnd):
    """直接子窗口列表（不含更深层）。"""
    out = []
    cur = u32.GetWindow(hwnd, GW_CHILD)
    hops = 0
    while cur and hops < 64:
        out.append(cur)
        cur = u32.GetWindow(cur, GW_HWNDNEXT)
        hops += 1
    return out


def tray_left_edge(tray):
    """通知区域实际渲染出来的最左边界（含其子窗口、孙窗口）。

    开机登录阶段 TrayNotifyWnd 的外框会短暂滞后于它内部已经画好的图标，
    这时只用外框会把圆环算到图标右边、造成重叠；取子窗口的最小左边界
    就能立刻拿到真实位置。
    """
    lefts = []
    r = wt.RECT()
    if u32.GetWindowRect(tray, ctypes.byref(r)):
        lefts.append(r.left)
    for child in _child_windows(tray):
        if not u32.IsWindowVisible(child):
            continue
        if u32.GetWindowRect(child, ctypes.byref(r)) and r.right > r.left + 2:
            lefts.append(r.left)
        for grand in _child_windows(child):
            if not u32.IsWindowVisible(grand):
                continue
            if u32.GetWindowRect(grand, ctypes.byref(r)) and r.right > r.left + 2:
                lefts.append(r.left)
    return min(lefts) if lefts else None


def taskbar_anchor():
    sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    tb = u32.FindWindowW("Shell_TrayWnd", None)
    if tb and u32.IsWindowVisible(tb):
        r = wt.RECT()
        if u32.GetWindowRect(tb, ctypes.byref(r)) \
                and 24 < (r.bottom - r.top) < 200 and r.top < sh - 4:
            anchor = r.right - 10
            tray = u32.FindWindowExW(tb, None, "TrayNotifyWnd", None)
            if tray:
                edge = tray_left_edge(tray)
                if edge is not None and edge > r.left + 100:
                    anchor = edge - 6
            return "bar", anchor, (r.top + r.bottom) // 2
    return "corner", sw - 8, sh - 18


class Anchor:
    """任务栏锚点（通知区域左边界），所有圆环共用。

    通知区域宽度会随图标增删整块平移（每次约一个图标宽），也常出现单拍抖动。
    策略：先取最近 5 次采样的中位数滤掉单拍毛刺，再要求新值连续 2 拍确认——
    真的移动了约 4 秒内跟上，来回横跳时保持不动。

    首次放置额外要求 FIRST_STABLE 拍一致：开机登录时托盘是一张一张冒出来的，
    第一拍就采信会把圆环放到图标右边（重叠），而那时又没有历史值可比对。
    """

    SAMPLES = 5
    CONFIRM = 2
    FIRST_STABLE = 3

    def __init__(self):
        self.value = None
        self.samples = collections.deque(maxlen=self.SAMPLES)
        self._pending = None
        self._pending_n = 0
        self._noted = 0.0

    def update(self):
        mode, x, cy = taskbar_anchor()
        if mode != "bar":
            # 任务栏找不到/隐藏时保持上一次的位置，不要跳到屏幕角落
            if self.value is None:
                self.value = (mode, x, cy)
            return self.value

        self.samples.append((x, cy))
        xs = sorted(s[0] for s in self.samples)
        med = xs[len(xs) // 2]
        prev = self.value
        if prev is None:
            # 还没定过位置：等连续 FIRST_STABLE 拍给出同一个值再采信
            if len(self.samples) >= self.FIRST_STABLE and xs.count(med) >= self.FIRST_STABLE:
                self.value = ("bar", med, cy)
                log("锚点确定: ('bar', %d, %d)（等待 %d 拍稳定）"
                    % (med, cy, self.FIRST_STABLE))
                return self.value
            return None
        elif med == prev[1]:
            if cy != prev[2]:          # 任务栏上下移动时纵向跟着变
                self.value = ("bar", med, cy)
            self._pending, self._pending_n = None, 0
        else:
            if self._pending == med:
                self._pending_n += 1
            else:
                self._pending, self._pending_n = med, 1
            if self._pending_n >= self.CONFIRM:
                self.value = ("bar", med, cy)
                self._pending, self._pending_n = None, 0

        if self.value != prev:
            log("锚点更新: %s -> %s" % (prev, self.value))
        elif prev is not None and x != prev[1]:
            now = time.time()
            if now - self._noted > 30:
                self._noted = now
                log("锚点采样偏移: 观测 %d，当前 %d" % (x, prev[1]))
        return self.value


ANCHOR = Anchor()


class TaskbarPanel:
    """任务栏上的一个电量圆环。

    渲染交给 tray_ui（PIL 超采样抗锯齿），本类只负责：状态缓存、位置、
    顶层保持、悬停卡片与低电量脉冲。
    """

    SIZE = RING_WINDOW
    GAP = tray_ui.RING_GAP

    def __init__(self, root, dev, index, on_refresh, on_overview):
        self.root = root
        self.dev = dev
        self.index = index
        self._shown = False
        self._last_rect = None
        self._last_key = None
        self._photo = None
        self._mapped_handle = None
        self._owner = None
        self._misplace_log = 0.0
        self._tip_job = None
        self._tip_visible = False
        self._pulse = 255
        self.dark = taskbar_theme_dark()
        self.palette = tray_ui.Palette(self.dark)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry("%dx%d" % (self.SIZE, self.SIZE))
        self.win.configure(bg=self.palette.bg)

        # 用单个 Label 显示位图：不做 Canvas 绘制，避免 tk 圆弧锯齿。
        # expand=True 让 44px 位图在 48px 窗口里居中——否则 pack 默认顶对齐，
        # 圆环会整体比任务栏中心高 2–3px。
        self.label = tk.Label(self.win, bd=0, bg=self.palette.bg,
                              highlightthickness=0, padx=0, pady=0)
        self.label.pack(expand=True)
        self.win.withdraw()

        self.card = tray_ui.TooltipCard(root, self.palette)
        self.win.bind("<ButtonRelease-1>", lambda _: self._refresh_from_click(on_refresh))
        self.win.bind("<ButtonRelease-3>", lambda event: self._open_overview(event, on_overview))
        self.label.bind("<Enter>", lambda e: self._schedule_tip())
        self.label.bind("<Leave>", lambda e: self._hide_tip())

        self._render(None, False, False)

    def _refresh_from_click(self, request_refresh):
        request_refresh()
        self._cancel_tip()
        self._show_tip()

    def _open_overview(self, event, on_overview):
        x, y = self._last_rect or (event.x_root, event.y_root)
        on_overview((x, y, x + self.SIZE, y + self.SIZE), self.dev.key)

    # -- 渲染 --------------------------------------------------------

    def _stale(self):
        return bool(self.dev.error)

    def _gone(self):
        """设备整个不在了（拔掉/断电），此时画成带斜杠的断开态。"""
        return not self.dev.present

    def _render(self, level, charging, stale, alpha=255, disconnected=False):
        img = tray_ui.ring_image(tray_ui.RING_SIZE, level, charging, stale,
                              dark=self.dark, alpha=alpha,
                              palette=self.palette,
                              disconnected=disconnected, threshold=CFG["low_threshold"])
        self._photo = ImageTk.PhotoImage(img, master=self.win)
        self.label.configure(image=self._photo)

    def update(self, level, charging, stale, disconnected=False):
        key = (level, charging, stale, disconnected, CFG["low_threshold"])
        if key == self._last_key:
            return
        self._last_key = key
        self._pulse = 255
        self._render(level, charging, stale, disconnected=disconnected)

    def needs_pulse(self, threshold):
        """低于阈值且未充电时才脉冲，避免常态动画干扰。"""
        level = self.dev.display_level
        return (self._shown and self.dev.present
                and tray_ui.is_low(level, self.dev.charging, bool(self.dev.error), threshold))

    def pulse(self, phase):
        """低电量脉冲：只淡化弧线，数字保持清晰。"""
        alpha = tray_ui.pulse_alpha(phase)
        if alpha == self._pulse:
            return
        self._pulse = alpha
        self._render(self.dev.display_level, self.dev.charging, self._stale(), alpha,
                     disconnected=self._gone())

    def reset_pulse(self):
        """退出脉冲态时把弧线恢复成不透明，否则会一直停在最后一帧的淡化上。"""
        if self._pulse != 255:
            self._pulse = 255
            self._render(self.dev.display_level, self.dev.charging, self._stale(),
                         disconnected=self._gone())

    def retint(self, dark):
        """系统在深浅色主题之间切换后重新着色（否则窗口底会留一块异色方块）。"""
        if dark == self.dark:
            return
        self.dark = dark
        self.palette = tray_ui.Palette(dark)
        self.win.configure(bg=self.palette.bg)
        self.label.configure(bg=self.palette.bg)
        self.card.set_palette(self.palette)
        self._last_key = None            # 强制按新配色重画
        self.update(self.dev.display_level, self.dev.charging, self._stale(),
                    disconnected=self._gone())

    # -- 位置 --------------------------------------------------------

    def _hwnd(self):
        return tray_ui.window_handle(self.win)

    def alive(self):
        try:
            return bool(self.win.winfo_exists() and (self._mapped_handle is None or self._hwnd()))
        except tk.TclError:
            return False

    def destroy(self):
        self._cancel_tip()
        for win in (self.card.win, self.win):
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _topmost(self):
        hwnd = self._hwnd()
        if hwnd:
            u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)

    def _adopt(self):
        """把圆环挂成任务栏的附属窗口（owned window）。

        owned window 永远排在宿主之上，所以 explorer 把任务栏抬到置顶窗口
        最上面时（点托盘溢出面板就会这样），圆环跟着一起上去，不用再靠
        定时 SetWindowPos 抢层级——那种抢法每次都会造成一帧重绘空档，
        看起来就是圆环「闪一下」。
        """
        tb, hwnd = taskbar_hwnd(), self._hwnd()
        if not tb or not hwnd:
            self._owner = None
            return
        if u32.GetWindow(hwnd, GW_OWNER) != tb:
            _set_window_long(hwnd, GWL_HWNDPARENT, tb)
        self._owner = u32.GetWindow(hwnd, GW_OWNER)

    def covered(self):
        """是否有「别的进程」的可见窗口压在我们上面。

        典型场景：点开托盘溢出面板后，explorer 会把任务栏抬到所有置顶窗口
        之上，圆环被任务栏背景盖住，要等下一次置顶才回来——那就是肉眼看到的
        「闪一下」。这里按 z 序往上找，一旦被外部窗口压住就立刻重新置顶。
        本进程的窗口（悬停卡片、右键菜单）不算，避免自己抢自己的层级。
        """
        x, y = self._last_rect or (0, 0)
        x1, y1 = x + self.SIZE, y + self.SIZE
        try:
            me = os.getpid()
            prev = u32.GetWindow(self._hwnd(), GW_HWNDPREV)
            hops = 0
            while prev and hops < COVER_WALK_LIMIT:
                if u32.IsWindowVisible(prev):
                    r = wt.RECT()
                    if (u32.GetWindowRect(prev, ctypes.byref(r))
                            and r.left < x1 and r.right > x
                            and r.top < y1 and r.bottom > y):
                        if window_pid(prev) != me:
                            return True
                prev = u32.GetWindow(prev, GW_HWNDPREV)
                hops += 1
        except Exception:
            pass
        return False

    def place(self, anchor, slot):
        mode, anchor_x, cy = anchor
        x = anchor_x - self.SIZE * (slot + 1) - self.GAP * slot
        y = cy - self.SIZE // 2
        bounds = tray_ui.monitor_bounds(anchor_x, cy)
        if bounds:
            x, y = max(bounds[0], x), max(bounds[1], y)
        rect = (x, y)
        hwnd = self._hwnd()
        first = not self._shown or not (hwnd and u32.IsWindowVisible(hwnd))
        if first:
            self.win.deiconify()
            self.win.update_idletasks()
            hwnd = self._hwnd()
        if not hwnd:
            return
        new_window = hwnd != self._mapped_handle
        self._shown = bool(u32.IsWindowVisible(hwnd))
        self._mapped_handle = hwnd
        if first or new_window or rect != self._last_rect:
            if self._last_rect is not None and rect != self._last_rect:
                log("圆环移动: %s -> %s" % (self._last_rect, rect))
            self._last_rect = rect
            self._apply_rect(x, y)
        self._verify_rect(hwnd)
        self._adopt()
        if first or new_window:
            self._topmost()

    def _apply_rect(self, x, y):
        # 锚点来自 Win32，坐标和尺寸直接用物理像素交给同一套 API。
        tray_ui.move_window(self.win, x, y, self.SIZE, self.SIZE)

    def _verify_rect(self, hwnd):
        """实际位置和请求不一致时纠正一次，并留一条日志。"""
        if not self._last_rect:
            return
        x, y = self._last_rect
        r = wt.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return
        if (abs(r.left - x) <= 1 and abs(r.top - y) <= 1
                and r.right - r.left == self.SIZE and r.bottom - r.top == self.SIZE):
            return
        self._apply_rect(x, y)
        now = time.time()
        if now - self._misplace_log > 60:
            self._misplace_log = now
            log("圆环位置纠偏: 期望(%d,%d) 实际(%d,%d)" % (x, y, r.left, r.top))

    def hide(self):
        self._hide_tip()
        try:
            self.win.withdraw()
        except Exception:
            pass
        self._shown = False

    # -- 悬停卡片 ----------------------------------------------------

    def _schedule_tip(self):
        self._cancel_tip()
        # 200ms 防抖：扫过任务栏时不会闪出一堆卡片
        self._tip_job = self.root.after(180, self._show_tip)

    def _cancel_tip(self):
        if self._tip_job is not None:
            try:
                self.root.after_cancel(self._tip_job)
            except Exception:
                pass
            self._tip_job = None

    def _show_tip(self):
        self._tip_job = None
        if not self._shown or (_OVERVIEW is not None and _OVERVIEW.visible()):
            return
        self._refresh_tip()
        self._tip_visible = True
        self.card.show()

    def _refresh_tip(self):
        """按最新读数重绘卡片内容；卡片开着时电量变化也能立刻反映。"""
        w, h = self.card.show_for(self.dev, self.dev.display_name(), self.palette,
                                  refresh=HOTPLUG.refresh_status(), threshold=CFG["low_threshold"])
        x, y = self._last_rect or (self.win.winfo_rootx(), self.win.winfo_rooty())
        self.card.place(x, y, self.SIZE, w, h)

    def refresh_tip(self):
        if self._tip_visible:
            self._refresh_tip()

    def _hide_tip(self):
        self._cancel_tip()
        self._tip_visible = False
        self.card.hide()


_HOTPLUG_WINDOWS = {}      # hwnd -> watcher；按窗口查实例，不能只留一个「当前」
_HOTPLUG_PROC = None


def _hotplug_wnd_proc(hwnd, msg, wparam, lparam):
    """模块级窗口回调：窗口类只注册一次，回调必须活得比实例久。

    之前把 WNDPROC 绑定在实例上：第一个 watcher 结束后回调被回收，而窗口类
    仍指向它，后续再创建窗口就收不到消息。实例与窗口用字典对应，多个窗口
    （测试里会连续创建）互不干扰。
    """
    w = _HOTPLUG_WINDOWS.get(int(hwnd) if hwnd else 0)
    if w is not None:
        return w._wnd_proc(hwnd, msg, wparam, lparam)
    return u32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_hotplug_class(hinst, class_name):
    global _HOTPLUG_PROC
    if _HOTPLUG_PROC is None:
        _HOTPLUG_PROC = WNDPROC(_hotplug_wnd_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(_HOTPLUG_PROC, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        u32.RegisterClassW(ctypes.byref(wc))
    return _HOTPLUG_PROC


class HotplugWatcher:
    """隐藏窗口收系统广播：设备热插拔、主题/分辨率变化。

    消息在后台线程到达，回调里只置标志位，真正的界面更新由 Tk 线程的
    ui_tick 读取——跨线程碰 Tk 会直接崩。
    """

    CLASS = "KBTrayHotplugWnd"

    def __init__(self):
        self._event = threading.Event()
        self._state_lock = threading.Lock()
        self._device_changed = False
        self._refresh_phase = ""
        self._refresh_failed_at = 0.0
        self._settings = threading.Event()
        self._hwnd = None
        self._ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        try:
            hinst = k32.GetModuleHandleW(None)
            _ensure_hotplug_class(hinst, self.CLASS)
            hwnd = u32.CreateWindowExW(0, self.CLASS, "kb-battery-hotplug", 0,
                                       0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                log("热插拔窗口创建失败 err=%s" % ctypes.get_last_error())
                return
            self._hwnd = hwnd
            _HOTPLUG_WINDOWS[int(hwnd)] = self
            dbi = DEV_BROADCAST_DEVICEINTERFACE()
            dbi.dbcc_size = ctypes.sizeof(dbi)
            dbi.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
            u32.RegisterDeviceNotificationW(
                hwnd, ctypes.byref(dbi), DEVICE_NOTIFY_ALL_INTERFACE_CLASSES)
            self._ready.set()
            log("热插拔监听就绪 hwnd=%#x" % hwnd)
            msg = wt.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log("热插拔线程异常: " + traceback.format_exc())

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DEVICECHANGE:
            with self._state_lock:
                self._device_changed = True
                self._event.set()
        elif msg in (WM_SETTINGCHANGE, WM_THEMECHANGED, WM_DISPLAYCHANGE):
            self._settings.set()               # 主题/分辨率变了，让 ui_tick 复核
        elif msg == WM_APP_STOP:
            u32.DestroyWindow(hwnd)            # 触发 WM_DESTROY → PostQuitMessage
            return 0
        elif msg == WM_DESTROY:
            self._hwnd = None
            _HOTPLUG_WINDOWS.pop(int(hwnd) if hwnd else 0, None)
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def wait(self, timeout):
        """设备变更或手动刷新均可唤醒轮询，返回值仅代表设备变更。"""
        self._event.wait(timeout)
        with self._state_lock:
            changed = self._device_changed
            self._device_changed = False
            self._event.clear()
            return changed

    def request_refresh(self):
        with self._state_lock:
            if self._refresh_phase not in ("queued", "running"):
                self._refresh_phase = "queued"
                self._event.set()

    def wake(self):
        self._event.set()

    def take_refresh(self):
        with self._state_lock:
            if self._refresh_phase == "queued":
                self._refresh_phase = "running"
                return True
            return False

    def finish_refresh(self, ok):
        with self._state_lock:
            self._refresh_phase = "" if ok else "failed"
            if not ok:
                self._refresh_failed_at = time.monotonic()

    def refresh_status(self):
        with self._state_lock:
            if self._refresh_phase in ("queued", "running"):
                return "pending"
            if (self._refresh_phase == "failed"
                    and time.monotonic() - self._refresh_failed_at < 8):
                return "failed"
            return ""

    def take_settings_dirty(self):
        """系统设置（主题/分辨率）是否有变化；读取即清。"""
        if self._settings.is_set():
            self._settings.clear()
            return True
        return False

    def stop(self):
        """关闭监听窗口；用自定义消息，系统消息不允许跨线程 Post。"""
        if self._hwnd:
            u32.PostMessageW(self._hwnd, WM_APP_STOP, 0, 0)


HOTPLUG = HotplugWatcher()
_HID_DEVS = None


_HID_DEVS_AT = 0.0


def hid_devices(force=False):
    """枚举结果缓存 30 秒：热插拔事件即时失效，超时也会自愈重扫。"""
    global _HID_DEVS, _HID_DEVS_AT
    if force or _HID_DEVS is None or time.time() - _HID_DEVS_AT > 30:
        _HID_DEVS = enum_hid()
        _HID_DEVS_AT = time.time()
    return _HID_DEVS


def invalidate_devices():
    global _HID_DEVS, _LOGITECH_SCAN_DUE
    _HID_DEVS = None
    _LOGITECH_SCAN_DUE = 0.0
    _HIDPP_CACHE.clear()
    close_all()
    for d in DEVICES:
        d.next_due = 0.0
        d.fails = 0


# ---------------------------------------------------------------- 轮询

def poll_keyboard(devs=None):
    inst = find_compx(devs)
    KB.set_present(bool(inst))
    if not inst:
        _set_error(KB, "未发现接收器")
        return
    receiver = hid_path(inst)
    if KB.receiver != receiver:
        KB.receiver = receiver
        KB.clear_reading()
    if KB.model_id is None:
        mid, _ = compx_model(inst)
        if mid is not None:
            KB.model_id = mid
    res, err = compx_battery(inst)
    if not res:
        _set_error(KB, err)
        return
    level, state = res
    KB.wired = state == STATE_CABLE
    KB.charging = False
    KB.error = ""
    if KB.wired:
        KB.status = "已接线"
        return
    KB.level = level
    KB.status = "电池供电"
    KB.updated = time.time()


def update_logitech(hid_devs, force=False):
    """更新已知槽位的连接状态，并定期探测之前未应答的槽位。"""
    global _LOGITECH_SCAN_DUE
    paths = logitech_paths(hid_devs)
    known = {(d.receiver, d.slot): d for d in DEVICES if d.slot is not None}
    removed = {d.receiver for d in known.values() if d.present} - paths
    for dev in known.values():
        dev.set_present(dev.receiver in paths)
        if not dev.present:
            _set_error(dev, "未发现 Logitech 接收器")
    for path in removed:
        get_link(path, 20).close()
    if removed:
        for key in list(_HIDPP_CACHE):
            if key[0] in removed:
                del _HIDPP_CACHE[key]
    if not force and time.time() < _LOGITECH_SCAN_DUE:
        return
    for path in sorted(paths):
        for slot in range(1, 7):
            if (path, slot) in known:
                continue
            reply = hidpp_request(path, slot, 0, 1, timeout=0.2)
            if reply and reply[4] in (2, 4):
                dev = Device(logitech_key(path, slot), "Logitech 设备", path, slot)
                dev.set_present(True)
                DEVICES.append(dev)
    _LOGITECH_SCAN_DUE = time.time() + LOGITECH_SCAN_INTERVAL


def poll_logitech(dev):
    if not dev.present:
        return
    path, slot = dev.receiver, dev.slot
    if dev.kind == "Logitech 设备":
        idx = hidpp_feature(path, slot, HIDPP_NAME)
        reply = hidpp_request(path, slot, idx, 2) if idx else None
        if reply:
            dev.kind = HIDPP_TYPES.get(reply[4], "Logitech 外设")
    if dev.kind in ("鼠标", "触控板", "轨迹球") and "logitech_mouse_key" not in CFG:
        CFG["logitech_mouse_key"] = dev.key
        if CFG.get("mouse_name"):
            CFG.setdefault(dev.key + "_name", CFG["mouse_name"])
        show = CFG.setdefault("show", {})
        show.setdefault(dev.key, show.get("mouse", True))
        save_config()
    if not dev.name:
        dev.name = hidpp_name(path, slot)
    res, err = hidpp_battery(path, slot)
    if not res:
        _set_error(dev, err)
        return
    if dev.updated is None:
        # 首读可能不准，复读一次再采用。
        time.sleep(0.15)
        again, _ = hidpp_battery(path, slot)
        res = again or res
    dev.level, state = res
    dev.charging = state in (1, 2, 3, 4)
    dev.status = BATTERY_STATUS.get(state, "状态 0x%02X" % state)
    dev.updated = time.time()
    dev.error = ""


BACKOFF = (60, 120, 180, 300)     # 连续失败后的间隔：1/2/3/5 分钟


def next_delay(fails, ok):
    """成功用基础间隔；连续失败按 1/2/3/5 分钟退避（封顶）。"""
    if ok:
        return CFG["interval_sec"]
    return BACKOFF[min(max(fails, 1), len(BACKOFF)) - 1]


def poll_device(dev, hid_devs):
    if dev is KB:
        poll_keyboard(hid_devs)
    else:
        poll_logitech(dev)
    if dev.error:
        dev.fails += 1
    else:
        dev.fails = 0
    delay = next_delay(dev.fails, not dev.error)
    dev.next_due = time.time() + delay
    return delay


def poll_all():
    """立即轮询所有设备（手动刷新用），并重置退避。"""
    invalidate_devices()
    devs = hid_devices(force=True)
    update_logitech(devs, force=True)
    for d in DEVICES:
        poll_device(d, devs)


def lowest_level():
    vals = [(level, d) for d in DEVICES if d.present
            and (level := d.display_level) is not None]
    if not vals:
        return None, None
    def priority(item):
        level, dev = item
        low = tray_ui.is_low(level, dev.charging, bool(dev.error), CFG["low_threshold"])
        return not low, bool(dev.error), level

    return min(vals, key=priority)


_TRAY_KEY = {"value": None}


def publish_status():
    if ICON is not None:
        level, dev = lowest_level()
        key = (level, bool(dev and dev.charging), bool(dev and dev.error),
               pystray_dark(), CFG["low_threshold"])
        if key != _TRAY_KEY["value"]:
            ICON.icon = make_image(*key[:3])
            _TRAY_KEY["value"] = key
        ICON.title = status_text()
        for dev in DEVICES:
            level = dev.display_level
            if level is None:
                continue
            if level > CFG["low_threshold"] + 5:
                dev.alerted = False
            if (dev.present and not dev.alerted
                    and tray_ui.is_low(level, dev.charging, bool(dev.error), CFG["low_threshold"])):
                try:
                    ICON.notify("电量 %d%%，该充电了" % level, dev.display_name())
                except Exception:
                    continue
                dev.alerted = True
    save_last()


def worker():
    rescan = False
    while True:
        try:
            if HOTPLUG.take_refresh():
                try:
                    poll_all()
                except Exception:
                    HOTPLUG.finish_refresh(ok=False)
                    log("手动刷新异常: " + traceback.format_exc())
                else:
                    HOTPLUG.finish_refresh(ok=True)
            else:
                if rescan:
                    invalidate_devices()
                now = time.time()
                if now >= _LOGITECH_SCAN_DUE or any(now >= d.next_due for d in DEVICES):
                    devs = hid_devices()
                    update_logitech(devs)
                    for dev in DEVICES:
                        if time.time() >= dev.next_due:
                            poll_device(dev, devs)
            publish_status()
        except Exception:
            log("worker 异常: " + traceback.format_exc())
        # 睡到最近一次轮询或设备发现；插拔时立即唤醒。
        now = time.time()
        deadlines = [_LOGITECH_SCAN_DUE, *(d.next_due for d in DEVICES)]
        deadlines.extend(expiry for d in DEVICES
                         if (expiry := stale_expiry(d)) is not None and expiry > now)
        nxt = min(deadlines)
        rescan = HOTPLUG.wait(max(1.0, min(60.0, nxt - now)))


# ---------------------------------------------------------------- 菜单

# ---------------------------------------------------------------- 设置面板

_SETTINGS = None
_OVERVIEW = None


def overview_rows():
    rows = []
    taskbar = CFG.get("taskbar", True)
    for dev in DEVICES:
        if not has_status(dev):
            continue
        reading = tray_ui.battery_reading(dev, CFG["low_threshold"])
        if reading.level is None:
            time_or_label = reading.caption
        elif reading.historical:
            time_or_label = "上次电量"
            if dev.updated is not None:
                time_or_label += " " + tray_ui.time_hhmm(dev.updated)
        else:
            time_or_label = tray_ui.TooltipCard._meta(dev) if dev.updated else ""
        detail = " · ".join(filter(None, (reading.status, time_or_label)))
        ring_shown = is_shown(dev) if dev.present and taskbar else None
        rows.append(tray_ui.OverviewRow(dev.key, dev.display_name(), reading.level, detail,
                                     dev.charging, reading.historical, ring_shown))
    return rows


def _update_overview():
    if _OVERVIEW is not None and _OVERVIEW.visible():
        _OVERVIEW.update(overview_rows(), HOTPLUG.refresh_status(), CFG["low_threshold"])


def _refresh_from_overview():
    on_refresh()
    _update_overview()


def tray_panel_anchor():
    rect = tray_icon_rect()
    if rect is not None:
        return rect
    mode, x, y = taskbar_anchor()
    bar = wt.RECT()
    if mode == "bar" and u32.GetWindowRect(taskbar_hwnd(), ctypes.byref(bar)):
        return x, bar.top, x, bar.bottom
    return x, y, x, y


def open_overview(anchor=None, selected=None):
    global _OVERVIEW
    if _SETTINGS is not None and _SETTINGS.alive():
        _SETTINGS.show()
        return
    anchor = anchor or tray_panel_anchor()
    _, dpi = tray_ui.monitor_at(*tray_ui.anchor_center(anchor))
    dpi = dpi or round(ROOT.winfo_fpixels("1i"))
    dark = taskbar_theme_dark()
    for panel in PANELS:
        panel._hide_tip()
    if (_OVERVIEW is not None and _OVERVIEW.alive()
            and (_OVERVIEW.palette.dark != dark or _OVERVIEW.dpi != dpi)):
        _OVERVIEW.win.destroy()
    if _OVERVIEW is None or not _OVERVIEW.alive():
        _OVERVIEW = tray_ui.DeviceOverview(
            ROOT, palette=tray_ui.Palette(dark), dpi=dpi, on_refresh=_refresh_from_overview,
            on_settings=open_settings, on_toggle_ring=toggle_device_ring,
            on_taskbar=on_taskbar, on_autostart=on_autostart, on_log=on_log, on_quit=on_quit)
    _update_overview_options()
    _OVERVIEW.show(anchor, selected, overview_rows(), HOTPLUG.refresh_status(), CFG["low_threshold"])


def _update_overview_options():
    if _OVERVIEW is not None and _OVERVIEW.alive():
        _OVERVIEW.update_options(CFG.get("taskbar", True), is_autostart())


def current_settings():
    """导出当前设置，供设置面板回显。"""
    show = {"device": True, "mouse": True}
    show.update(CFG.get("show", {}))
    for dev in DEVICES:
        show.setdefault(dev.key, True)
    return {
        "interval_sec": CFG["interval_sec"],
        "low_threshold": CFG["low_threshold"],
        "stale_min": CFG.get("stale_min", STALE_MIN_DEFAULT),
        "log_max_mb": CFG.get("log_max_mb", LOG_MAX_MB),
        "log_keep": CFG.get("log_keep", LOG_KEEP),
        "log_retention_days": CFG.get("log_retention_days",
                                      LOG_RETENTION_DAYS),
        "show": {key: bool(value) for key, value in show.items()},
    }


def apply_settings(values, persist=True):
    """应用已校验的设置字典；返回实际生效的 (间隔, 阈值)。

    字典字段与 tray_ui.LIMITS 对应，校验在 tray_ui.validate_form 里做一次，
    这里只负责落到 CFG 并触发副作用。
    """
    changes = {"show": {key: bool(value) for key, value in values.get("show", {}).items()}}
    for setting, limit, _, cast in tray_ui.FORM_FIELDS.values():
        if setting == "stale_min" and setting not in values:
            continue
        lo, hi, _ = LIMITS[limit]
        value = max(lo, min(hi, cast(values[setting])))
        changes[setting] = round(value, 2) if cast is float else value
    for key, name in values.get("device_names", {}).items():
        changes[key + "_name"] = name.strip()
    if persist:
        save_config(changes)
    else:
        CFG.update({**changes, "show": {**CFG.get("show", {}), **changes["show"]}})
    for d in DEVICES:          # 立即按新间隔重新排程
        d.next_due = 0.0
        d.fails = 0
    HOTPLUG.wake()
    for p in PANELS:           # 显示开关即时生效，不等下一次 tick
        if not is_shown(p.dev):
            p.hide()
    if ICON:
        try:
            ICON.title = status_text()
        except Exception:
            pass
    log("设置更新: 间隔=%ss 阈值=%s%% 掉线保留=%s分钟 日志=%sMB×%s份/%s天" % (
            CFG["interval_sec"], CFG["low_threshold"],
            CFG.get("stale_min", STALE_MIN_DEFAULT),
            fmt_mb(CFG.get("log_max_mb")),
            CFG.get("log_keep"), CFG.get("log_retention_days")))
    return CFG["interval_sec"], CFG["low_threshold"]


def _device_rows():
    """返回当前设备，供设置页绘制圆环预览。"""
    return [tray_ui.SettingsDeviceRow(d.key, d.display_name(), d.automatic_name(),
                                  d.display_level, d.charging, bool(d.error))
            for d in DEVICES if d.present]


def _update_settings():
    if _SETTINGS is not None and _SETTINGS.alive():
        _SETTINGS.update_devices(_device_rows(), CFG["show"])


def _save_from_dialog(values):
    apply_settings(values)
    return []


def open_settings(anchor=None):
    """打开设置对话框（必须在主线程调用）。"""
    global _SETTINGS
    if _SETTINGS is not None and _SETTINGS.alive():
        _SETTINGS.show()
        return
    if anchor is None:
        anchor = tray_panel_anchor()
    if _OVERVIEW is not None and _OVERVIEW.alive():
        _OVERVIEW.hide()
    for p in PANELS:
        p._hide_tip()
    log("设置面板 -> 托盘锚点 %s" % (anchor,))
    _SETTINGS = tray_ui.SettingsDialog(
        ROOT, current_settings(), _device_rows(),
        on_save=_save_from_dialog,
        palette=tray_ui.Palette(taskbar_theme_dark()), anchor=anchor)
    _SETTINGS.show()
    return


def on_refresh():
    HOTPLUG.request_refresh()


def on_autostart():
    set_autostart(not is_autostart())
    _update_overview_options()


def on_taskbar():
    CFG["taskbar"] = not CFG.get("taskbar", True)
    save_config()
    if not CFG["taskbar"]:
        for p in PANELS:
            p.hide()
    _update_overview_options()
    _update_overview()


def on_log():
    os.startfile(LOG)


def on_quit():
    log("退出")
    HOTPLUG.stop()
    close_all()
    if ROOT:
        try:
            ROOT.quit()
        except Exception:
            pass
    os._exit(0)


def toggle_device_ring(key):
    dev = next((d for d in DEVICES if d.key == key and d.present), None)
    if dev is not None:
        save_config({"show": {key: not is_shown(dev)}})
    _update_overview()


class TrayIcon(pystray.Icon):
    def _on_notify(self, wparam, lparam):
        # Win32 托盘点击交给 Tk 主线程显示共用面板。
        if lparam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
            ROOT.after(0, open_overview)
        else:
            super()._on_notify(wparam, lparam)


def build_icon():
    return TrayIcon(DISPLAY_NAME, make_image(None), DISPLAY_NAME)


_TICKS = {"n": 0, "ts": 0.0, "visibility": None}
_TOPMOST_TICK_MS = 300         # 兜底检查周期（正常路径靠 owned window，不会触发）
_UI_TICK_MS = 1000
_UI_STALL_SEC = 6              # 超过这么久没更新就认为界面循环死了
_TOP = {"raise": 0}            # 累计抢回次数，用于心跳日志


def ui_tick():
    try:
        _update_overview()
        _update_settings()
        anchor = ANCHOR.update()
        if anchor is None:
            # 锚点还没稳定（开机登录阶段托盘仍在加载）：先不摆放，宁可晚一两秒出现
            return
        visible = [d for d in DEVICES if is_visible(d)]
        reason = "已显示"
        if not CFG.get("taskbar", True):
            reason = "任务栏圆环已关闭"
        elif not visible:
            reason = "无可显示设备"
        elif foreground_fullscreen():
            reason = "任务栏所在屏幕正在全屏显示"
        if reason != _TICKS["visibility"]:
            log("任务栏圆环: " + reason)
            _TICKS["visibility"] = reason
        if reason != "已显示":
            for p in PANELS:
                p.hide()
        else:
            for p in tuple(PANELS):
                if not p.alive():
                    log("圆环窗口失效，重新创建")
                    p.destroy()
                    PANELS.remove(p)
            with_panel = {p.dev for p in PANELS}
            for dev in visible:
                if dev not in with_panel:
                    PANELS.append(TaskbarPanel(ROOT, dev, len(PANELS), on_refresh, open_overview))
            for p in PANELS:
                if p.dev in visible:
                    p.update(p.dev.display_level, p.dev.charging, bool(p.dev.error),
                             disconnected=not p.dev.present)
                    p.place(anchor, visible.index(p.dev))
                    p.refresh_tip()      # 卡片开着时跟着读数走
                else:
                    p.hide()
    except Exception:
        log("ui_tick 异常: " + traceback.format_exc())
    finally:
        # 收尾里的任何异常都不能吃掉重新排程，否则界面就静默冻结在最后一帧
        try:
            _TICKS["n"] += 1
            _TICKS["ts"] = time.time()
            # 主题/分辨率变化由隐藏窗口的广播立刻置标志，这里只做读取（1 秒内生效）；
            # 每 5 分钟再兜底核一次，防止广播被吞掉。
            if HOTPLUG.take_settings_dirty() or _TICKS["n"] % 300 == 0:
                check_theme()
            if _TICKS["n"] % 300 == 0:     # 每 5 分钟留一行心跳
                log("心跳: tick=%d 锚点=%s 显示=%d 抢回=%d" % (
                    _TICKS["n"], ANCHOR.value,
                    sum(1 for p in PANELS if p._shown), _TOP["raise"]))
        except Exception:
            log("ui_tick 收尾异常: " + traceback.format_exc())
        finally:
            ROOT.after(_UI_TICK_MS, ui_tick)


def check_theme():
    """系统切换深/浅色后，面板底色和卡片配色跟着变，不用重启。

    只做「值变了才重画」：广播会在拖动主题色板时连续到达，每次都重画
    会产生一堆无意义的图像分配。
    """
    dark = taskbar_theme_dark()
    if all(p.dark == dark for p in PANELS):
        return
    log("系统主题切换 -> %s" % ("深色" if dark else "浅色"))
    for q in PANELS:
        q.retint(dark)


def topmost_tick():
    """兜底：真被外部窗口压住时才重新置顶。

    正常情况下圆环是任务栏的附属窗口（见 TaskbarPanel._adopt），
    explorer 抬高任务栏时圆环跟着上去，这里什么都不会做；只有宿主关系丢了
    （比如 explorer 重启）或者被别的置顶窗口压住时才抢回来。
    不做「无条件高频置顶」——每次 SetWindowPos 都会带来一帧重绘空档，
    反而制造出可见的闪烁。
    """
    try:
        for p in PANELS:
            if p._shown and p.covered():
                p._adopt()
                p._topmost()
                _TOP["raise"] += 1
    except Exception:
        pass
    finally:
        ROOT.after(_TOPMOST_TICK_MS, topmost_tick)


def pulse_tick():
    """低电量脉冲；顺带当界面循环的看门狗。"""
    try:
        # 看门狗：ui_tick 若是被异常打断而没能重新排程，界面会静默停在最后一帧
        # （圆环位置不再更新、锚点变了也不跟）。这里发现超时就重新拉起。
        if _TICKS["ts"] and time.time() - _TICKS["ts"] > _UI_STALL_SEC:
            log("界面循环停滞 %.1fs，重新拉起" % (time.time() - _TICKS["ts"]))
            _TICKS["ts"] = time.time()
            ROOT.after(0, ui_tick)
        threshold = CFG.get("low_threshold", 20)
        period = tray_ui.PULSE_PERIOD / 1000.0
        phase = (time.time() % period) / period
        active = False
        for p in PANELS:
            if p.needs_pulse(threshold):
                p.pulse(phase)
                active = True
            else:
                p.reset_pulse()      # 充电/回血后不能停在最淡的那一帧
    except Exception:
        log("pulse_tick 异常: " + traceback.format_exc())
        active = False
    finally:
        ROOT.after(150 if active else 800, pulse_tick)


def main():
    mutex = k32.CreateMutexW(None, False, "Local\\" + APP_NAME)
    if ctypes.get_last_error() == 183:
        log("已有实例在运行，退出")
        return 0
    removed = prune_logs()
    size, keep, days = log_limits()
    log("启动，间隔 %ss，阈值 %s%%，任务栏=%s，日志=%sMB(%dKB)×%d份/%d天%s" % (
        CFG["interval_sec"], CFG["low_threshold"], CFG.get("taskbar"),
        fmt_mb(size / float(MB_KB * MB_KB)), size // 1024, keep, days,
        "，清理旧日志 %d 份" % removed if removed else ""))

    global ICON, ROOT
    last = load_last()
    if last:
        restore_devices(last, hid_devices(force=True))
    ROOT = tk.Tk()
    ROOT.withdraw()
    install_crash_hooks(ROOT)               # pythonw 下没有 stderr，异常必须落盘
    dpi = tray_ui.apply_dpi_scaling(ROOT)      # 高分屏下让 Tk 控件不再偏小
    log("界面 DPI=%s 缩放=%.2f 圆环=%dpx"
        % (dpi, dpi / 72.0, tray_ui.RING_SIZE))
    ICON = build_icon()
    ICON.run_detached()
    HOTPLUG.start()
    threading.Thread(target=worker, daemon=True).start()
    _TICKS["ts"] = time.time()
    ROOT.after(300, ui_tick)
    ROOT.after(400, pulse_tick)
    ROOT.after(500, topmost_tick)
    try:
        ROOT.mainloop()
    finally:
        log("主循环结束")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("致命错误: " + traceback.format_exc())
        raise
