"""2.4G 厂商电量查询。只匹配已知产品与报告长度。"""

from functools import reduce
from operator import xor
from typing import NamedTuple

from hid_transport import feature_exchange, hid_path


class Model(NamedTuple):
    name: str
    feature_length: int
    transaction: int = 0
    interface: int | None = None
    usage: tuple[int, int] | None = None


# 型号与查询格式来自 OpenRazer、OpenMouse 和 Wireless Battery 的协议资料。
MODELS = {
    (0x1532, 0x007B): Model("Razer Viper Ultimate", 91, 0x3F, 2),
    (0x1532, 0x0072): Model("Razer Mamba Wireless", 91, 0x3F, 2),
    (0x1532, 0x0088): Model("Razer Basilisk Ultimate", 91, 0x3F, 2),
    (0x1532, 0x00A6): Model("Razer Viper V2 Pro", 91, 0x1F, 0),
    (0x1532, 0x00B6): Model("Razer DeathAdder V3 Pro", 91, 0x1F, 0),
    (0x258A, 0x2022): Model("Glorious Model O Wireless", 65),
    (0x258A, 0x2023): Model("Glorious Model D Wireless", 65),
    (0x258A, 0x2024): Model("Glorious Model O- Wireless", 65),
    (0x258A, 0x2025): Model("Glorious Model D- Wireless", 65),
    (0x36A7, 0xA874): Model("WLMouse YING", 65, usage=(0xFFFF, 0)),
}


def receivers(interfaces):
    candidates = {}
    for item in interfaces:
        model = MODELS.get((item["vid"], item["pid"]))
        if model is None or item.get("featl") != model.feature_length:
            continue
        if model.usage is not None and (item["page"], item["usage"]) != model.usage:
            continue
        instance = item["inst"].upper()
        expected = model.interface
        if expected is not None and f"&MI_{expected:02X}" not in instance.partition("\\")[2].partition("\\")[0]:
            continue
        # 同一物理 HID 接口可能暴露多个集合。存在歧义时跳过，避免发到其他集合。
        hardware, _, location = instance.rpartition("\\")
        if "&COL" in hardware:
            hardware = hardware.split("&COL")[0]
            location = location.rsplit("&", 1)[0]
        interface_key = hardware, location
        candidates.setdefault(interface_key, []).append((hid_path(item["inst"]), model))
    return dict(matches[0] for matches in candidates.values() if len(matches) == 1)


def _razer_value(path, transaction, command):
    request = bytearray(91)  # Windows 在 90 字节厂商报文前加零号报告字节。
    request[2] = transaction
    request[6:9] = bytes((2, 7, command))
    request[89] = reduce(xor, request[3:89])
    reply = feature_exchange(path, bytes(request), 0.02)
    if (reply and len(reply) == 91 and reply[0:3] == bytes((0, 2, transaction))
            and reply[3:9] == request[3:9] and reply[89] == reduce(xor, reply[3:89])):
        return reply[10]
    return None


def read_battery(path, model):
    if model.transaction:
        raw = _razer_value(path, model.transaction, 0x80)
        if raw is None:
            return None
        charging = _razer_value(path, model.transaction, 0x84) == 1
        return (raw * 100 + 127) // 255, charging

    request = bytes.fromhex("00 00 00 02 02 00 83").ljust(65, b"\x00")
    delay = 0.10 if model.usage is not None else 0.05
    reply = feature_exchange(path, request, delay)
    if (reply and len(reply) == 65 and reply[0:2] == b"\x00\xa1"
            and reply[4] == 2 and reply[6] == 0x83 and reply[8] <= 100):
        return reply[8], reply[7] == 1
    return None
