"""无线外设电量协议解析（纯函数，无 Win32 依赖）。

单独拆出来的目的：这层最容易出错、也最值得单测，且不依赖任何系统调用。
"""

# ---- Compx 2.4G 接收器（AULA/Womier/Redragon 等共用）
REPORT_ID = 0x13
CMD_BAT = 0x4A
CMD_MODEL = 0x05
REPORT_LEN = 20
MODEL_AT = 10
MODEL_NAMES = {0xCD: "AULA F75", 0xA4: "AULA F99"}
STATE_BATTERY, STATE_CABLE = 0x01, 0x10

# ---- Logitech HID++ 2.0 电量状态（0..6）
BATTERY_STATUS = {0: "放电中", 1: "充电中", 2: "即将充满", 3: "已充满",
                  4: "慢速充电", 5: "电池异常", 6: "温度异常"}


def checksum(b):
    """Compx 20 字节报文的校验字：前 19 字节之和。"""
    return sum(b[:REPORT_LEN - 1]) & 0xFF


def is_reply(r, cmd):
    """是否是 cmd 的应答：报文号/命令/校验和都对得上。"""
    return (len(r) >= REPORT_LEN and r[0] == REPORT_ID
            and (r[1] & 0x7F) == cmd
            and checksum(r) == r[REPORT_LEN - 1])


def parse_compx_battery(frame):
    """解析键盘电量帧：byte5=电量、byte6=供电状态。非法帧抛 ValueError。"""
    if not is_reply(frame, CMD_BAT):
        raise ValueError("不是电量应答帧")
    level, state = frame[5], frame[6]
    if state not in (STATE_BATTERY, STATE_CABLE):
        raise ValueError("未知供电状态 %#04x" % state)
    if state == STATE_CABLE:
        return 100, state          # 有线模式下接收器固定报 100
    if level == 0:
        raise ValueError("设备未上报电量")
    if level > 100:
        raise ValueError("电量越界: %d" % level)
    return level, state


def parse_compx_model(frame):
    """解析型号帧：第 10 字节是型号 ID。"""
    if not is_reply(frame, CMD_MODEL):
        raise ValueError("不是型号应答帧")
    return frame[MODEL_AT]


def parse_hidpp_battery(frame, dev=None):
    """解析 HID++ 0x1000 fn0：byte4=电量、byte6=状态。"""
    if len(frame) < 7 or frame[0] != 0x11:
        raise ValueError("不是 HID++ 长报文")
    if dev is not None and frame[1] != dev:
        raise ValueError("设备索引不符: %#04x" % frame[1])
    level, status = frame[4], frame[6]
    if level == 0:
        raise ValueError("设备未上报电量")
    if level > 100:
        raise ValueError("电量越界: %d" % level)
    return level, status


def parse_hidpp_unified_battery(frame):
    """HID++ 0x1004 fn1 的百分比及充电状态，调用方先确认设备提供百分比。"""
    if len(frame) < 8 or frame[0] != 0x11 or frame[4] > 100:
        raise ValueError("无效的 HID++ 统一电量应答")
    # 统一电量的 2、4 分别是慢速充电、充电错误。
    return frame[4], (0, 1, 4, 3, 5)[min(frame[6], 4)]


def parse_rapoo_vt_battery(frame, report_length):
    """雷柏 VT 接收器主动上报的百分比和充电状态。"""
    if len(frame) != report_length:
        return None
    if report_length == 7 and frame[:2] == b"\xbb\xb0":
        state, level = frame[5:7]
        if state == 2 and level == 0x7F:
            return None, True
    elif report_length in (13, 16, 19) and frame[0] == 0x07 and frame[1] & 0x0F == 0:
        state, level = frame[7:9]
    else:
        return None
    if state in (1, 2) and level <= 100:
        return level, state == 2
    return None
