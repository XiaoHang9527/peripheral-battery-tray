"""导入程序前隔离测试配置、电量快照和诊断日志。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

test_profile = tempfile.TemporaryDirectory(prefix="battery-tray-test-")
os.environ["APPDATA"] = os.path.join(test_profile.name, "roaming")
os.environ["LOCALAPPDATA"] = os.path.join(test_profile.name, "local")
