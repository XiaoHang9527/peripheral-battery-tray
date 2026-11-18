"""日志滚动与过期清理单测。"""
import importlib.util
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


def test_rotate_moves_current_to_1(tmp_path):
    log = tmp_path / "tray.log"
    log.write_text("new", encoding="utf-8")
    assert kb.rotate_log(str(log), keep=3) is True
    assert not log.exists()
    assert (tmp_path / "tray.log.1").read_text(encoding="utf-8") == "new"


def test_rotate_keeps_only_n_files(tmp_path):
    log = tmp_path / "tray.log"
    for i in range(6):
        log.write_text("gen%d" % i, encoding="utf-8")
        kb.rotate_log(str(log), keep=3)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["tray.log.1", "tray.log.2", "tray.log.3"]
    # 最新的一代在 .1
    assert (tmp_path / "tray.log.1").read_text(encoding="utf-8") == "gen5"


def test_rotate_missing_file_is_noop(tmp_path):
    assert kb.rotate_log(str(tmp_path / "nope.log")) is False


def test_prune_removes_expired_slices(tmp_path):
    (tmp_path / "tray.log").write_text("cur", encoding="utf-8")
    old = tmp_path / "tray.log.1"
    old.write_text("old", encoding="utf-8")
    past = time.time() - 30 * 86400
    os.utime(old, (past, past))
    fresh = tmp_path / "tray.log.2"
    fresh.write_text("fresh", encoding="utf-8")

    removed = kb.prune_logs(str(tmp_path), days=14)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert (tmp_path / "tray.log").exists()      # 当前日志不动


def test_prune_ignores_unrelated_files(tmp_path):
    other = tmp_path / "last.json"
    other.write_text("{}", encoding="utf-8")
    past = time.time() - 30 * 86400
    os.utime(other, (past, past))
    assert kb.prune_logs(str(tmp_path), days=14) == 0
    assert other.exists()


def test_log_rotates_when_too_big(tmp_path, monkeypatch):
    log = tmp_path / "tray.log"
    monkeypatch.setattr(kb, "LOG", str(log))
    monkeypatch.setitem(kb.CFG, "log_max_mb", 0.1)     # 0.1 MB = 102 KB
    log.write_text("x" * 200_000, encoding="utf-8")
    kb.log("hello")
    assert (tmp_path / "tray.log.1").read_text(encoding="utf-8") == "x" * 200_000
    assert log.exists()
    assert "hello" in log.read_text(encoding="utf-8")
