"""HID 句柄清理与异步 I/O 取消回归测试。"""
import ctypes
import itertools
import threading
import time
from types import SimpleNamespace

import pytest

import hid_transport


@pytest.mark.parametrize("failed_op", ["read", "write"])
def test_exchange_closes_failed_connection_and_reopens(monkeypatch, failed_op):
    handles = iter([100, 101, 200, 201])
    closed = []
    flushed = []
    write, read = object(), object()
    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(
        CreateFileW=lambda *args: next(handles), CloseHandle=closed.append,
        WriteFile=write, ReadFile=read,
    ))
    monkeypatch.setattr(hid_transport, "hid", SimpleNamespace(HidD_FlushQueue=flushed.append))

    def transfer(operation, handle, buf, deadline, path):
        if ((handle == 100 and failed_op == "write")
                or (handle == 101 and failed_op == "read")):
            raise OSError("设备已断开")
        if operation is write:
            return len(buf)
        buf.raw = b"reply"
        return 5

    monkeypatch.setattr(hid_transport, "_transfer", transfer)
    link = hid_transport.HidLink("device", 5)
    assert link.exchange(b"req", lambda frame: frame == b"reply") == (None, [])
    assert closed == [101, 100]
    assert link.exchange(b"req", lambda frame: frame == b"reply") == (b"reply", [b"reply"])
    assert flushed == [101, 201]
    link.close()
    link.close()
    assert closed == [101, 100, 201, 200]


@pytest.mark.parametrize("timeout_error", [hid_transport.WAIT_TIMEOUT, hid_transport.ERROR_IO_INCOMPLETE])
def test_transfer_drains_cancelled_request(monkeypatch, timeout_error):
    calls = []

    def read(handle, buf, size, transferred, overlap):
        calls.append(("read", handle, overlap))
        ctypes.set_last_error(hid_transport.ERROR_IO_PENDING)
        return False

    def wait(handle, overlap, transferred, timeout, alertable):
        if timeout == hid_transport._CANCEL_TIMEOUT_MS:
            calls.append(("drain", handle, overlap))
            ctypes.set_last_error(hid_transport.ERROR_OPERATION_ABORTED)
        else:
            calls.append(("wait", handle, overlap))
            ctypes.set_last_error(timeout_error)
        return False

    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(
        GetOverlappedResultEx=wait, CreateEventW=lambda *args: 51, CloseHandle=lambda _: None,
        CancelIoEx=lambda handle, overlap: calls.append(("cancel", handle, overlap)),
    ))
    assert hid_transport._transfer(read, 101, ctypes.create_string_buffer(20), time.monotonic(), "device") is None
    overlap = calls[0][2]
    assert calls == [(action, 101, overlap) for action in ("read", "wait", "cancel", "drain")]


def test_transfer_holds_storage_after_cancel_timeout(monkeypatch):
    pending = {}
    closed = []
    events = iter([51, 52])
    pins = []

    def wait(*_):
        ctypes.set_last_error(hid_transport.WAIT_TIMEOUT)
        return False

    def read(*_):
        ctypes.set_last_error(hid_transport.ERROR_IO_PENDING)
        return False

    monkeypatch.setattr(hid_transport, "_pending_io", pending)
    monkeypatch.setattr(hid_transport, "_pin", pins.append)
    monkeypatch.setattr(hid_transport, "_unpin", pins.remove)
    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(
        CreateEventW=lambda *_: next(events), CloseHandle=closed.append,
        GetOverlappedResultEx=wait, CancelIoEx=lambda *_: None,
        WaitForSingleObject=lambda event, timeout: 0,
    ))
    first = ctypes.create_string_buffer(20)
    with pytest.raises(TimeoutError):
        hid_transport._transfer(read, 101, first, time.monotonic(), "device")
    assert pending["device"][1] is first
    assert closed == []
    with pytest.raises(TimeoutError):
        hid_transport._transfer(read, 102, ctypes.create_string_buffer(20), time.monotonic(), "device")
    assert closed == [51]
    assert pending["device"][0].hEvent == 52
    assert pins == list(pending.values())


def test_pending_cancel_blocks_only_its_own_interface(monkeypatch):
    pending = {}
    events = itertools.count(51)
    attempts = []

    def transfer(handle, *_):
        attempts.append(handle)
        ctypes.set_last_error(hid_transport.ERROR_IO_PENDING)
        return False

    def wait(handle, op, count, *_):
        if handle == 99:
            ctypes.cast(count, ctypes.POINTER(hid_transport.wt.DWORD))[0] = 1
            return True
        ctypes.set_last_error(hid_transport.WAIT_TIMEOUT)
        return False

    monkeypatch.setattr(hid_transport, "_pending_io", pending)
    monkeypatch.setattr(hid_transport, "_pin", lambda _: None)
    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(
        CreateEventW=lambda *_: next(events), CloseHandle=lambda _: None,
        GetOverlappedResultEx=wait, CancelIoEx=lambda *_: None,
        WaitForSingleObject=lambda *_: hid_transport.WAIT_TIMEOUT,
    ))
    for number in range(33):
        with pytest.raises(TimeoutError):
            hid_transport._transfer(transfer, 101, ctypes.create_string_buffer(20), time.monotonic(), f"device-{number}")
    with pytest.raises(TimeoutError):
        hid_transport._transfer(transfer, 202, ctypes.create_string_buffer(20), time.monotonic(), "DEVICE-0")
    assert 202 not in attempts
    assert hid_transport._transfer(transfer, 99, ctypes.create_string_buffer(20), time.monotonic(), "healthy") == 1
    assert len(pending) == 33


def test_windows_overlapped_layout():
    assert hid_transport.OVERLAPPED.hEvent.offset == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    assert ctypes.sizeof(hid_transport.OVERLAPPED) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)


@pytest.mark.parametrize("read_write", [False, True])
def test_feature_query_opens_with_read_write_then_falls_back(monkeypatch, read_write):
    access_attempts, closed = [], []

    def open_device(path, access, *_):
        access_attempts.append(access)
        return 51 if access == 0 or read_write else None

    def read_feature(handle, report, length):
        assert report[0] == b"\x01"
        report[1] = b"\x52"
        return True

    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(CreateFileW=open_device, CloseHandle=closed.append))
    monkeypatch.setattr(hid_transport, "hid", SimpleNamespace(
        HidD_SetFeature=lambda *_: True, HidD_GetFeature=read_feature))
    assert hid_transport.feature_exchange("device", b"\x01\x00", 0) == b"\x01\x52"
    assert access_attempts == ([hid_transport.READ_WRITE_ACCESS] if read_write else [hid_transport.READ_WRITE_ACCESS, 0])
    assert closed == [51]


def test_feature_timeout_isolated_and_healthy_worker_reused(monkeypatch, caplog):
    release = threading.Event()
    healthy_workers = []

    def query(path, *_):
        if path == "stalled-feature":
            release.wait(timeout=3)
        else:
            healthy_workers.append(threading.current_thread())
        return b"reply"

    monkeypatch.setattr(hid_transport, "_feature_query", query)
    monkeypatch.setattr(hid_transport, "_FEATURE_TIMEOUT", 0.05)
    try:
        assert hid_transport.feature_exchange("stalled-feature", b"a", 0) is None
        assert hid_transport.feature_exchange("STALLED-FEATURE", b"b", 0) is None
        monkeypatch.setattr(hid_transport, "_FEATURE_TIMEOUT", 1)
        assert hid_transport.feature_exchange("healthy-feature", b"c", 0) == b"reply"
        assert hid_transport.feature_exchange("healthy-feature", b"d", 0) == b"reply"
        assert healthy_workers[0] is healthy_workers[1]
        assert sum("Feature 读取超时" in record.message for record in caplog.records) == 1
    finally:
        release.set()
