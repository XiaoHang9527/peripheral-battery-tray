"""HID 句柄清理与异步 I/O 取消回归测试。"""
import ctypes
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

    def transfer(operation, handle, buf, deadline):
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
        calls.append(("wait", handle, overlap))
        ctypes.set_last_error(timeout_error)
        return False

    def drain(handle, overlap, transferred, blocking):
        assert blocking
        calls.append(("drain", handle, overlap))
        ctypes.set_last_error(hid_transport.ERROR_OPERATION_ABORTED)
        return False

    monkeypatch.setattr(hid_transport, "k32", SimpleNamespace(
        GetOverlappedResultEx=wait, GetOverlappedResult=drain,
        CancelIoEx=lambda handle, overlap: calls.append(("cancel", handle, overlap)),
    ))
    assert hid_transport._transfer(read, 101, ctypes.create_string_buffer(20), time.monotonic()) is None
    overlap = calls[0][2]
    assert calls == [(action, 101, overlap) for action in ("read", "wait", "cancel", "drain")]
