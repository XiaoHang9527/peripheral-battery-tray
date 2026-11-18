"""导入程序前隔离测试配置、电量快照和诊断日志。"""
import os
import tempfile

test_profile = tempfile.TemporaryDirectory(prefix="battery-tray-test-")
os.environ["APPDATA"] = os.path.join(test_profile.name, "roaming")
os.environ["LOCALAPPDATA"] = os.path.join(test_profile.name, "local")
