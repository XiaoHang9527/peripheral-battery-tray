"""HID 传输层：设备枚举和可取消的读写连接（Win32/SetupAPI/ctypes）。

只负责“怎么和 HID 集合说话”，不关心电量协议，也不依赖 Tk 或 pystray。
"""
import ctypes
import ctypes.wintypes as wt
import math
import threading
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

try:
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PMv2
except Exception:
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass

k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                         ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                   ctypes.POINTER(wt.DWORD), wt.BOOL]
k32.GetOverlappedResultEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                     ctypes.POINTER(wt.DWORD), wt.DWORD, wt.BOOL]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]

hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.c_void_p]
hid.HidD_FlushQueue.argtypes = [wt.HANDLE]
hid.HidD_FlushQueue.restype = ctypes.c_ubyte
hid.HidD_GetProductString.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(ctypes.c_byte),
                                          wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.LPWSTR, wt.DWORD,
    ctypes.POINTER(wt.DWORD)]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class IFDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class DEVDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", GUID),
                ("DevInst", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class ATTRS(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort), ("VersionNumber", ctypes.c_ushort)]


class CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InLen", ctypes.c_ushort), ("OutLen", ctypes.c_ushort),
                ("FeatLen", ctypes.c_ushort), ("Reserved", ctypes.c_ushort * 17),
                ("N1", ctypes.c_ushort), ("N2", ctypes.c_ushort),
                ("N3", ctypes.c_ushort), ("N4", ctypes.c_ushort),
                ("N5", ctypes.c_ushort), ("N6", ctypes.c_ushort),
                ("N7", ctypes.c_ushort), ("N8", ctypes.c_ushort),
                ("N9", ctypes.c_ushort), ("N10", ctypes.c_ushort),
                ("N11", ctypes.c_ushort), ("N12", ctypes.c_ushort)]


GUID_HID = GUID(0x4D1E55B2, 0xF16F, 0x11CF,
                (ctypes.c_ubyte * 8)(0x88, 0xCB, 0, 0x11, 0x11, 0, 0, 0x30))


def hid_path(inst):
    return ("\\\\?\\" + inst.replace("\\", "#").lower() +
            "#{4d1e55b2-f16f-11cf-88cb-001111000030}")


def enum_hid():
    """枚举所有 HID 接口：vid/pid/usage page/报告长度/实例路径。"""
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.cast(ctypes.byref(GUID_HID), ctypes.POINTER(ctypes.c_byte)),
        None, None, 0x02 | 0x10)
    if not hdev:
        return []
    out, i = [], 0
    while True:
        ifd = IFDATA()
        ifd.cbSize = ctypes.sizeof(ifd)
        if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(GUID_HID), i, ctypes.byref(ifd)):
            break
        i += 1
        need = wt.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(
            hdev, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
        if not need.value:
            continue
        buf = ctypes.create_string_buffer(need.value)
        ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = (
            8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
        dd = DEVDATA()
        dd.cbSize = ctypes.sizeof(dd)
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(ifd), buf, need, ctypes.byref(need),
                ctypes.byref(dd)):
            continue
        iid = ctypes.create_unicode_buffer(512)
        n = wt.DWORD(0)
        setupapi.SetupDiGetDeviceInstanceIdW(
            hdev, ctypes.byref(dd), iid, 512, ctypes.byref(n))
        inst = iid.value
        h = k32.CreateFileW(hid_path(inst), 0, 3, None, 3, 0, None)
        if h in (None, wt.HANDLE(-1).value):
            continue
        try:
            a = ATTRS()
            a.Size = ctypes.sizeof(a)
            if not hid.HidD_GetAttributes(h, ctypes.byref(a)):
                continue
            pb = ctypes.create_unicode_buffer(256)
            has_name = bool(hid.HidD_GetProductString(h, pb,
                                                      ctypes.sizeof(pb)))
            pp = ctypes.c_void_p()
            cap = None
            if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)) and pp:
                c = CAPS()
                if hid.HidP_GetCaps(pp, ctypes.byref(c)) == 0x00110000:
                    cap = c
                hid.HidD_FreePreparsedData(pp)
            if cap is None:
                continue
            out.append({"inst": inst, "vid": a.VendorID, "pid": a.ProductID,
                        "page": cap.UsagePage, "usage": cap.Usage,
                        "inl": cap.InLen, "outl": cap.OutLen,
                        "featl": cap.FeatLen,
                        "product": pb.value if has_name else ""})
        finally:
            k32.CloseHandle(h)
    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 258
INVALID_HANDLES = (None, wt.HANDLE(-1).value)


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


def _transfer(operation, handle, buf, deadline):
    """执行一次异步读写。取消后等系统归还缓冲区，才允许调用者释放它。"""
    overlap = OVERLAPPED()
    op = ctypes.byref(overlap)
    if not operation(handle, buf, len(buf), None, op):
        error = ctypes.get_last_error()
        if error != ERROR_IO_PENDING:
            raise ctypes.WinError(error)
    count = wt.DWORD()
    timeout_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
    if k32.GetOverlappedResultEx(handle, op, ctypes.byref(count), timeout_ms, False):
        return count.value
    error = ctypes.get_last_error()
    if error in (WAIT_TIMEOUT, ERROR_IO_INCOMPLETE):
        k32.CancelIoEx(handle, op)
        if k32.GetOverlappedResult(handle, op, ctypes.byref(count), True):
            return count.value
        error = ctypes.get_last_error()
        if error == ERROR_OPERATION_ABORTED:
            return None
    raise ctypes.WinError(error)


class HidLink:
    """一个 HID 集合的常驻连接，独立句柄读写，同一时间只处理一个请求。"""

    def __init__(self, path, read_len=256):
        self.path = path
        self.read_len = read_len
        self._io = threading.Lock()
        self._hw = None
        self._hr = None

    def _open(self):
        hw = k32.CreateFileW(self.path, 0x40000000, 3, None, 3,
                             FILE_FLAG_OVERLAPPED, None)
        if hw in INVALID_HANDLES:
            return False
        hr = k32.CreateFileW(self.path, 0x80000000, 3, None, 3,
                             FILE_FLAG_OVERLAPPED, None)
        if hr in INVALID_HANDLES:
            k32.CloseHandle(hw)
            return False
        self._hw, self._hr = hw, hr
        return True

    def exchange(self, payload, match, timeout=0.6):
        """写一帧并等待匹配的回复；返回 (frame|None, 收到的帧列表)。"""
        with self._io:
            if self._hw is None and not self._open():
                return None, []
            got = []
            try:
                # HIDClass 会缓存写入后立即到达的输入报文。
                hid.HidD_FlushQueue(self._hr)
                buf = ctypes.create_string_buffer(bytes(payload), len(payload))
                deadline = time.monotonic() + timeout
                if _transfer(k32.WriteFile, self._hw, buf, deadline) != len(buf):
                    self._close()
                    return None, got
                buf = ctypes.create_string_buffer(self.read_len)
                while time.monotonic() < deadline:
                    count = _transfer(k32.ReadFile, self._hr, buf, deadline)
                    if not count:
                        break
                    frame = buf.raw[:count]
                    got.append(frame)
                    if match(frame):
                        return frame, got
            except OSError:
                self._close()
            return None, got

    def _close(self):
        if self._hr is not None:
            k32.CloseHandle(self._hr)
        if self._hw is not None:
            k32.CloseHandle(self._hw)
        self._hr = self._hw = None

    def close(self):
        with self._io:
            self._close()


_LINKS = {}
_LINKS_LOCK = threading.Lock()


def get_link(path, read_len=256):
    with _LINKS_LOCK:
        link = _LINKS.get(path)
        if link is None:
            link = HidLink(path, read_len)
            _LINKS[path] = link
        return link


def close_all():
    with _LINKS_LOCK:
        links = list(_LINKS.values())
    for link in links:
        link.close()
