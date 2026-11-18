"""任务栏锚点跟随逻辑单测：开机消抖、滤单拍毛刺、真移动要跟上、横跳不许抖。"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peripheral_battery_tray", ROOT / "peripheral_battery_tray.py")
kb = importlib.util.module_from_spec(spec)
sys.modules["peripheral_battery_tray"] = kb
spec.loader.exec_module(kb)


def make(monkeypatch):
    """构造一个 Anchor，并替换掉取锚点函数。"""
    anchor = kb.Anchor()
    seq = []

    def fake():
        return seq.pop(0)

    monkeypatch.setattr(kb, "taskbar_anchor", fake)
    return anchor, seq


def prime(anchor, x=3336, cy=2130, times=None):
    """喂足够的稳定采样，让锚点完成首次确定。"""
    for _ in range(times or kb.Anchor.FIRST_STABLE):
        anchor.samples.append((x, cy))
        anchor.update_value = None
    # 直接用 update 走一遍才符合真实路径
    anchor2 = anchor
    return anchor2


def feed(values, monkeypatch, cy=2130):
    anchor, seq = make(monkeypatch)
    seq.extend(("bar", v, cy) for v in values)
    return [anchor.update() for _ in values]


def warm(anchor, monkeypatch, values, cy=2130):
    """先喂 values（通常 3336 若干次）完成首定，再按顺序给后续采样。"""
    seq = []
    monkeypatch.setattr(kb, "taskbar_anchor", lambda: seq.pop(0))
    out = []
    for v in values:
        seq.append(("bar", v, cy))
        out.append(anchor.update())
    return out


# ------------------------------------------------------------ 开机消抖

def test_first_adoption_waits_for_stable(monkeypatch):
    """第一拍不采信：开机登录时托盘还在逐个出现。"""
    out = feed([3336, 3336, 3336], monkeypatch)
    assert out[0] is None
    assert out[1] is None
    assert out[2] == ("bar", 3336, 2130)


def test_unstable_startup_uses_later_value(monkeypatch):
    """复现开机现场：先是 3444/3408/3372，稳定后是 3336。

    早期值一个都不能被采纳（否则圆环会叠在托盘图标上）。
    """
    out = feed([3444, 3408, 3372, 3336, 3336, 3336], monkeypatch)
    adopted = [v for v in out if v is not None]
    assert adopted, "最终必须定出位置"
    assert adopted[0][1] == 3336
    assert all(v[1] == 3336 for v in adopted)


def test_single_sample_never_adopts(monkeypatch):
    assert feed([3336], monkeypatch)[0] is None


# ------------------------------------------------------------ 稳定后跟随

def test_steady_value_kept(monkeypatch):
    anchor, _ = make(monkeypatch)
    out = warm(anchor, monkeypatch, [3336] * 6)
    assert all(v == ("bar", 3336, 2130) for v in out[2:])


def test_single_spike_ignored(monkeypatch):
    anchor, _ = make(monkeypatch)
    seq = [3336, 3336, 3336] + [3336] * 3 + [3372] + [3336] * 3
    out = warm(anchor, monkeypatch, seq)
    assert all(v[1] == 3336 for v in out if v is not None)


def test_permanent_move_adopted(monkeypatch):
    anchor, _ = make(monkeypatch)
    seq = [3336, 3336, 3336] + [3336] * 2 + [3372] * 6
    out = warm(anchor, monkeypatch, seq)
    assert out[-1] == ("bar", 3372, 2130)
    # 新值确认 2 拍后就该跟上（中位数大概在第 3 拍翻过来）
    first_moved = next(i for i, v in enumerate(out)
                       if v and v[1] == 3372)
    assert first_moved - 7 <= 3


def test_alternating_does_not_jitter(monkeypatch):
    anchor, _ = make(monkeypatch)
    seq = [3336, 3336, 3336] + [3336, 3372] * 8
    out = warm(anchor, monkeypatch, seq)
    assert {v[1] for v in out if v is not None} == {3336}


def test_corner_mode_keeps_last_bar_value(monkeypatch):
    anchor, seq = make(monkeypatch)
    seq.extend([("bar", 3336, 2130)] * 3)
    for _ in range(3):
        anchor.update()
    seq.append(("corner", 3800, 2100))
    assert anchor.update() == ("bar", 3336, 2130)


def test_vertical_center_follows_latest(monkeypatch):
    anchor, seq = make(monkeypatch)
    seq.extend([("bar", 3336, 2130)] * 3)
    for _ in range(3):
        anchor.update()
    seq.append(("bar", 3336, 2100))
    assert anchor.update() == ("bar", 3336, 2100)


def test_corner_mode_before_any_bar(monkeypatch):
    """任务栏还没出现时先给个坐标兜底，不至于返回 None 卡住界面。"""
    anchor, seq = make(monkeypatch)
    seq.append(("corner", 3832, 2142))
    assert anchor.update() == ("corner", 3832, 2142)
