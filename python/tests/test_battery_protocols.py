"""确认协议模块可独立导入（不依赖 Win32 主程序）。"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import battery_protocols  # noqa: E402


def test_standalone_import_and_parse():
    frame = bytes.fromhex("13 4a 01 00 02 64 01 00 00 00 00 00 00 00 00 00 00 00 00 c5")
    assert battery_protocols.parse_compx_battery(frame) == (100, battery_protocols.STATE_BATTERY)


def test_no_windows_dependency():
    assert not hasattr(battery_protocols, "ctypes"), "协议模块不应依赖 ctypes/Win32"


def test_rapoo_vt_only_decodes_known_reports():
    parse = battery_protocols.parse_rapoo_vt_battery
    assert parse(bytes.fromhex("BB B0 71 84 03 01 64"), 7) == (100, False)
    assert parse(bytes.fromhex("BB B0 71 84 03 02 7F"), 7) == (None, True)
    status = bytes.fromhex("07 10 02 74 0E 74 0E 02 15 00 00 00 02")
    for length in (13, 16, 19):
        assert parse(status.ljust(length, b"\0"), length) == (21, True)
    assert parse(status[:8], 13) is None
    assert parse(status[:8] + b"\x65" + status[9:], 13) is None
    assert parse(b"\x01" + status[1:], 13) is None
