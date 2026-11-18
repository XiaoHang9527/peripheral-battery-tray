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
