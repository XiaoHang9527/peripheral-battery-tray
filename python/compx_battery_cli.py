"""命令行读取 Compx 2.4G 接收器 (3554:FA09) 背后的键盘电量。"""
import os
import sys
import time

from hid_transport import close_all, enum_hid, get_link, hid_path
from battery_protocols import (CMD_BAT, REPORT_ID, REPORT_LEN, STATE_CABLE,
                     checksum, is_reply, parse_compx_battery)

VID, PID = 0x3554, 0xFA09


def find_battery_node():
    """找 VID/PID 匹配、usage page 0xFF02、20 字节 in/out 的集合。"""
    return [d["inst"] for d in enum_hid()
            if (d["vid"], d["pid"]) == (VID, PID) and d["page"] == 0xFF02
            and d["inl"] == REPORT_LEN and d["outl"] == REPORT_LEN]


def read_battery(inst, known=b""):
    debug = os.environ.get("KB_DEBUG")
    if debug:
        with open(debug, "a", encoding="utf-8") as f:
            f.write("read_battery " + inst + "\n")
    request = bytearray(REPORT_LEN)
    request[0], request[1] = REPORT_ID, CMD_BAT
    request[-1] = checksum(request)
    link = get_link(hid_path(inst), REPORT_LEN)
    received = 0
    for _ in range(3):
        reply, frames = link.exchange(request, lambda frame: is_reply(frame, CMD_BAT), 0.7)
        if reply is not None:
            return reply, None
        received += len(frames)
        time.sleep(0.2)
    return None, "键盘无应答（可能已休眠/关机），收到 %d 帧" % received


def main():
    try:
        nodes = find_battery_node()
        if not nodes:
            print("未找到 3554:FA09 的电量接口")
            return 1
        for inst in nodes:
            reply, error = read_battery(inst)
            print("接口:", inst)
            if error:
                print("结果:", error)
                continue
            print("原始:", reply.hex(" "))
            try:
                level, state = parse_compx_battery(reply)
            except ValueError as exc:
                print("结果:", exc)
                continue
            if state == STATE_CABLE:
                print("状态: 已接线")
                print("电量: 未知（接收器固定上报 100%）")
            else:
                print("状态: 电池供电")
                print("电量: %d%%" % level)
        return 0
    finally:
        close_all()


if __name__ == "__main__":
    sys.exit(main())
