# -*- coding: utf-8 -*-
"""probe.py —— 设备探测工具（CLI）。

对标浏览器端 "CoderTools WebHID 查看器" 的能力，但完全本地、零依赖：

==================  ==========================================================
命令                作用
==================  ==========================================================
``list``            枚举所有 HID 接口：VID/PID、mi/col、usage page、报告长度
``report``          读某个集合的 Feature Report（默认全扫一遍）
``desc``            读报告描述符（Hex / C 数组）
``watch``           监控原始 Input Report（可按 VID/PID 过滤）
``rog``             ROG OMNI 接收器：配对列表 + 键盘/鼠标电量协议试读
``logi``            罗技 HID++ 2.0：接收器槽位、短名、特性、电量、有线直连
``wired``           只看有线直连设备（罗技 HID++ 0xFF / ROG 厂商页）
``all``             跑一遍所有后端（等同 ``main.py --once`` 的数据源）
``json``            把以上结果导出成 JSON（给网页查看器/面板对照）
==================  ==========================================================

用法示例::

    python probe.py list
    python probe.py list --vid 0B05
    python probe.py desc --vid 0B05 --pid 1ACE --mi 2 --col col02
    python probe.py report --vid 046D --pid C539 --mi 2 --col col01 --rid 0x00
    python probe.py watch --vid 0B05 --seconds 5
    python probe.py rog
    python probe.py logi
    python probe.py all --debug
    python probe.py json --out probe.json

安全约束：**只读**。只有 ``rog``/``logi``/``all`` 会发**查询**报文
（电量/配对表/特性索引），不写任何设备设置、固件、灯光或配对表。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Iterable, Optional

# 允许直接 `python probe.py`（脚本目录加进 sys.path）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from devices import generic, hidiface, logitech, reader, rog  # noqa: E402


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------
def _hex(data: bytes) -> str:
    return data.hex(" ")


def _filter(interfaces: Iterable[hidiface.HidInterfaceInfo],
            vid: Optional[int], pid: Optional[int],
            mi: Optional[int], col: Optional[str],
            usage_page: Optional[int]) -> list[hidiface.HidInterfaceInfo]:
    """按命令行条件筛选接口。"""
    out: list[hidiface.HidInterfaceInfo] = []
    for item in interfaces:
        if vid is not None and item.vid != vid:
            continue
        if pid is not None and item.pid != pid:
            continue
        if mi is not None and item.mi != mi:
            continue
        if col is not None and (item.col or "").lower() != col.lower():
            continue
        if usage_page is not None and item.usage_page != usage_page:
            continue
        out.append(item)
    return out


def _parse_int(text: str) -> int:
    """支持 ``0x1ACE`` / ``1ACE`` / ``6894`` 三种写法。"""
    text = text.strip()
    try:
        return int(text, 16) if any(ch in "abcdefABCDEFx" for ch in text) else int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"不是合法整数：{text}")


def _fmt_len(value: int) -> str:
    return f"{value:3d}" if value else "  -"


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    """枚举 HID 接口。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    selected = _filter(interfaces, args.vid, args.pid, args.mi, args.col,
                       args.usage_page)
    if not selected:
        print("没有匹配的 HID 接口。")
        return 0
    print(f"共 {len(interfaces)} 个 HID 接口，匹配 {len(selected)} 个：\n")
    header = (f"{'VID:PID':9s} {'mi':>3s} {'col':5s} {'usage page:usage':18s} "
              f"{'in':>3s} {'out':>3s} {'feat':>4s}  产品名")
    print(header)
    print("-" * len(header))
    for item in selected:
        usage = f"0x{item.usage_page:04X}:0x{item.usage:04X}"
        print(f"{item.vid_pid:9s} {item.mi if item.mi is not None else '-':>3} "
              f"{item.col or '-':5s} {usage:18s} "
              f"{_fmt_len(item.input_len)} {_fmt_len(item.output_len)} "
              f"{_fmt_len(item.feature_len):>4s}  {item.display_name}")
        print(f"          {item.path}")
        if item.error:
            print(f"          [!] {item.error}")
    return 0


# --------------------------------------------------------------------------
# desc（报告描述符）
# --------------------------------------------------------------------------
def cmd_desc(args: argparse.Namespace) -> int:
    """读报告描述符。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    selected = _filter(interfaces, args.vid, args.pid, args.mi, args.col,
                       args.usage_page)
    if not selected:
        print("没有匹配的 HID 接口。")
        return 1
    for item in selected:
        print(f"\n== {item.vid_pid} mi={item.mi} {item.col} "
              f"0x{item.usage_page:04X}:0x{item.usage:04X} ==")
        print(f"   {item.path}")
        try:
            with hidiface.open_device(item.path) as device:
                descriptor = device.report_descriptor()
        except hidiface.HidError as exc:
            print(f"   打开失败：{exc}")
            continue
        if not descriptor:
            print("   该集合不支持读报告描述符（厂商集合常见，不影响电量读取）")
            continue
        print(f"   长度 {len(descriptor)} 字节")
        print(f"   Hex: {_hex(descriptor)}")
        if args.c_array:
            body = ", ".join(f"0x{byte:02X}" for byte in descriptor)
            print(f"   C:   const unsigned char desc[] = {{ {body} }};")
    return 0


# --------------------------------------------------------------------------
# report（Feature Report）
# --------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    """读 Feature Report。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    selected = _filter(interfaces, args.vid, args.pid, args.mi, args.col,
                       args.usage_page)
    if not selected:
        print("没有匹配的 HID 接口。")
        return 1
    for item in selected:
        if not item.feature_len:
            continue
        print(f"\n== {item.vid_pid} mi={item.mi} {item.col} "
              f"feature_len={item.feature_len} ==")
        print(f"   {item.path}")
        try:
            with hidiface.open_device(item.path) as device:
                report_ids = ([args.rid] if args.rid is not None
                              else sorted({int(caps.ReportID)
                                           for caps in device.value_caps(hidiface.HidP_Feature)}
                                          or set(range(0, 0x10))))
                for report_id in report_ids:
                    try:
                        data = device.get_feature_report(int(report_id))
                    except hidiface.HidError as exc:
                        print(f"   rid=0x{report_id:02X}  读取失败：{exc}")
                        continue
                    print(f"   rid=0x{report_id:02X}  {_hex(data)}")
        except hidiface.HidError as exc:
            print(f"   打开失败：{exc}")
    return 0


# --------------------------------------------------------------------------
# watch（原始输入监控）
# --------------------------------------------------------------------------
def cmd_watch(args: argparse.Namespace) -> int:
    """监控原始 Input Report。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    selected = [item for item in _filter(interfaces, args.vid, args.pid, args.mi,
                                         args.col, args.usage_page)
                if item.input_len]
    if not selected:
        print("没有匹配的可读 HID 接口。")
        return 1
    print(f"监控 {len(selected)} 个接口 {args.seconds:g} 秒（Ctrl+C 提前结束）…\n")
    devices: list[hidiface.HidDevice] = []
    opened: list[hidiface.HidInterfaceInfo] = []
    for item in selected:
        try:
            device = hidiface.open_device(item.path)
        except hidiface.HidError as exc:
            print(f"  {item.vid_pid} {item.col or '-'} 打开失败：{exc}")
            continue
        if not device.readable:
            print(f"  {item.vid_pid} mi={item.mi} {item.col or '-'} "
                  f"读句柄打不开（系统独占的顶层集合，跳过）")
            device.close()
            continue
        devices.append(device)
        opened.append(item)
    if not devices:
        print("没有可读的接口（键盘/鼠标顶层集合被系统独占是正常现象）。")
        return 1
    counts: dict[int, int] = {}
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            for index, device in enumerate(devices):
                report = device.read_input(timeout_ms=5)
                if not report:
                    continue
                counts[index] = counts.get(index, 0) + 1
                item = opened[index]
                print(f"[{time.strftime('%H:%M:%S')}] {item.vid_pid} "
                      f"mi={item.mi} {item.col or '-':5s} len={len(report):3d}  "
                      f"{_hex(report[:48])}")
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        for device in devices:
            device.close()
    print("\n统计：")
    for index, count in sorted(counts.items()):
        item = opened[index]
        print(f"  {item.vid_pid} mi={item.mi} {item.col or '-':5s} {count} 条")
    if not counts:
        print("  （这段时间没有报文；键盘/鼠标只在操作时上报）")
    return 0


# --------------------------------------------------------------------------
# rog / logi / all
# --------------------------------------------------------------------------
def cmd_logi(args: argparse.Namespace) -> int:
    """罗技 HID++ 试读（接收器槽位 + 有线直连）。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    receivers = logitech.find_receivers(interfaces)
    print(f"罗技接收器：{len(receivers)} 个")
    for receiver in receivers:
        print(f"  {receiver.pid:04X} {receiver.connection}")
        print(f"      短集合 {receiver.short_path}")
        print(f"      长集合 {receiver.long_path}")
        try:
            with logitech.Channel(receiver.short_path, receiver.long_path) as channel:
                for slot in logitech.SLOTS:
                    name = logitech.read_slot_name(channel, slot, receiver.connection)
                    if not name:
                        continue
                    print(f"      槽位 {slot}: 短名={name!r}")
        except Exception as exc:
            print(f"      打开失败：{exc}")
    links = logitech.find_direct_devices(interfaces)
    print(f"\n罗技有线直连候选：{len(links)} 个（短/长集合按物理实例配对）")
    for link in links:
        print(f"  {link.pid:04X} {link.product!r}")
        print(f"      短集合 {link.short_path}")
        print(f"      长集合 {link.long_path}")
    devices = (logitech.discover(interfaces, debug=True)
               + logitech.discover_direct(interfaces, debug=True))
    print(f"\n发现的罗技设备：{len(devices)}")
    for device in devices:
        reading = device.read()
        print(f"  {device.device_id}  {device.name}  [{device.connection}]")
        print(f"      level={reading.level} status={reading.status} "
              f"voltage={reading.voltage_mv}mV charging={reading.charging} "
              f"estimated={reading.estimated}")
        print(f"      {reading.detail}")
    return 0


def cmd_rog(args: argparse.Namespace) -> int:
    """ROG 协议试读（OMNI 接收器 + 已知型号 + 有线直连尽力探测）。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    omni = [item for item in interfaces
            if item.vid == rog.ASUS_VID and item.pid == rog.OMNI_PID]
    print(f"ROG OMNI 接收器接口：{len(omni)} 个")
    for item in omni:
        print(f"  mi={item.mi} {item.col or '-':5s} "
              f"0x{item.usage_page:04X}:0x{item.usage:04X} "
              f"in={item.input_len} out={item.output_len}  {item.path}")
    paired_iface = next((item for item in omni
                         if item.mi == 2 and (item.col or "").lower() == "col01"), None)
    if paired_iface is not None:
        pids = rog.read_paired_pids(paired_iface.path, debug=True)
        print(f"\n配对列表 PID：{[f'0x{pid:04X}' for pid in pids]}")
        for pid in pids:
            model = rog.find_model(pid, "omni_receiver")
            print(f"  0x{pid:04X} -> {rog.model_name(model, pid)}"
                  + ("" if model else "（型号未适配）"))
    else:
        print("\n没有找到 mi_02/col01 的配对列表集合。")
    known = rog.discover(interfaces, debug=True)
    claimed = {device.path for device in known}
    direct = rog.discover_direct(interfaces, debug=True, skip_paths=claimed)
    print(f"\nROG 有线直连（尽力探测）：{len(direct)} 台")
    devices = known + direct
    print(f"\n发现的 ROG 设备：{len(devices)}")
    for device in devices:
        reading = device.read()
        print(f"  {device.device_id}  {device.name}  [{device.connection}]")
        print(f"      level={reading.level} status={reading.status} "
              f"charging={reading.charging} estimated={reading.estimated}")
        print(f"      {reading.detail}")
    return 0


def cmd_wired(args: argparse.Namespace) -> int:
    """只看有线直连设备（罗技 HID++ 0xFF / ROG 厂商页尽力探测）。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    print("== 罗技有线直连（HID++ 短/长集合配对，device index 0xFF）==")
    links = logitech.find_direct_devices(interfaces)
    print(f"候选 {len(links)} 个：")
    for link in links:
        print(f"  {link.pid:04X} {link.product!r}")
        print(f"      短集合 {link.short_path}")
        print(f"      长集合 {link.long_path}")
    for device in logitech.discover_direct(interfaces, debug=args.debug):
        reading = device.read()
        print(f"  {device.device_id}  {device.name}  [{device.connection}]")
        print(f"      level={reading.level} status={reading.status} "
              f"voltage={reading.voltage_mv}mV estimated={reading.estimated}")
        print(f"      {reading.detail}")
    print("\n== ROG 有线直连（0B05 厂商页 + OMNI 命令尽力探测）==")
    known_wired = [device for device in rog.discover(interfaces, debug=args.debug)
                   if device.connection == "wired"]
    direct = rog.discover_direct(interfaces, debug=args.debug)
    rog_devices = known_wired + direct
    print(f"发现 {len(rog_devices)} 台：")
    for device in rog_devices:
        reading = device.read()
        print(f"  {device.device_id}  {device.name}  [{device.connection}]")
        print(f"      level={reading.level} status={reading.status} "
              f"charging={reading.charging}")
        print(f"      {reading.detail}")
    if not links and not rog_devices:
        print("\n（没有发现有线直连设备）")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """跑一遍所有后端（与 main.py --once 的数据源一致）。"""
    readings = reader.read_all(debug=args.debug)
    print(f"\n共 {len(readings)} 台设备：")
    for reading in readings:
        level = reading.level_text
        print(f"  {reading.name:24s} {level:>6s}  {reading.status_text:8s} "
              f"{reading.device_id}")
        if reading.voltage_mv is not None:
            print(f"      电压 {reading.voltage_mv} mV")
        if reading.detail:
            print(f"      {reading.detail}")
    print(f"\n发现耗时 {reader.last_discover_ms():.0f}ms，"
          f"本次读取耗时 {reader.last_read_ms():.0f}ms")
    return 0


# --------------------------------------------------------------------------
# json
# --------------------------------------------------------------------------
def collect_json(args: argparse.Namespace) -> dict[str, Any]:
    """把探测结果整理成可导出的 dict。"""
    interfaces = hidiface.enumerate_interfaces(detail=True)
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "interfaces": [
            {
                "path": item.path,
                "vid": f"{item.vid:04X}",
                "pid": f"{item.pid:04X}",
                "vid_pid": item.vid_pid,
                "mi": item.mi,
                "col": item.col,
                "usage_page": f"0x{item.usage_page:04X}",
                "usage": f"0x{item.usage:04X}",
                "input_len": item.input_len,
                "output_len": item.output_len,
                "feature_len": item.feature_len,
                "product": item.product,
                "manufacturer": item.manufacturer,
                "serial": item.serial,
                "error": item.error,
            }
            for item in interfaces
        ],
    }
    readings = reader.read_all(debug=args.debug)
    payload["readings"] = [
        {
            "device_id": reading.device_id,
            "name": reading.name,
            "level": reading.level,
            "status": reading.status,
            "voltage_mv": reading.voltage_mv,
            "source": reading.source,
            "detail": reading.detail,
            "estimated": reading.estimated,
            "charging": reading.charging,
            "inferred": reading.inferred,
        }
        for reading in readings
    ]
    payload["discovery"] = [
        {
            "device_id": info.device_id,
            "name": info.name,
            "kind": info.kind,
            "connection": info.connection,
            "vid_pid": info.vid_pid,
            "supported": info.supported,
            "note": info.note,
        }
        for info in reader.discover()
    ]
    payload["bluetooth"] = [
        {
            "name": item.name,
            "instance_id": item.instance_id,
            "level": item.level,
            "has_property": item.has_property,
            "reason": item.reason,
        }
        for item in generic.list_bluetooth_batteries()
    ]
    return payload


def cmd_json(args: argparse.Namespace) -> int:
    """导出 JSON。"""
    payload = collect_json(args)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"已写入 {args.out}（{len(payload['interfaces'])} 个接口，"
              f"{len(payload['readings'])} 条读数）")
    else:
        print(text)
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description="外设电量圆环 · 设备探测工具（只读查询，零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="只发查询命令，绝不写设备设置/固件/配对表。",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    def add_filters(target: argparse.ArgumentParser) -> None:
        target.add_argument("--vid", type=_parse_int, help="按 VID 过滤（十六进制，如 0B05）")
        target.add_argument("--pid", type=_parse_int, help="按 PID 过滤（十六进制，如 1ACE）")
        target.add_argument("--mi", type=int, help="按 USB 接口号过滤（路径里的 mi_XX）")
        target.add_argument("--col", help="按 HID 集合过滤（col01/col02/…）")
        target.add_argument("--usage-page", type=_parse_int, dest="usage_page",
                            help="按 usage page 过滤（如 0xFF00）")

    add_filters(subparsers.add_parser("list", help="枚举 HID 接口"))

    desc = subparsers.add_parser("desc", help="读报告描述符")
    add_filters(desc)
    desc.add_argument("--c-array", action="store_true", dest="c_array",
                      help="同时输出 C 数组形式")

    report = subparsers.add_parser("report", help="读 Feature Report")
    add_filters(report)
    report.add_argument("--rid", type=_parse_int, help="只读指定 report id（默认全扫）")

    watch = subparsers.add_parser("watch", help="监控原始 Input Report")
    add_filters(watch)
    watch.add_argument("--seconds", type=float, default=5.0, help="监控时长（默认 5 秒）")

    subparsers.add_parser("rog", help="ROG OMNI 接收器协议试读")
    subparsers.add_parser("logi", help="罗技 HID++ 协议试读")
    wired_parser = subparsers.add_parser("wired", help="只看有线直连设备")
    wired_parser.add_argument("--debug", action="store_true", help="打印调试信息")

    all_parser = subparsers.add_parser("all", help="跑一遍所有后端")
    all_parser.add_argument("--debug", action="store_true", help="打印调试信息")

    json_parser = subparsers.add_parser("json", help="导出探测结果 JSON")
    json_parser.add_argument("--out", help="输出文件路径（默认打印到标准输出）")
    json_parser.add_argument("--debug", action="store_true", help="打印调试信息")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """命令行入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {
        "list": cmd_list,
        "desc": cmd_desc,
        "report": cmd_report,
        "watch": cmd_watch,
        "rog": cmd_rog,
        "logi": cmd_logi,
        "wired": cmd_wired,
        "all": cmd_all,
        "json": cmd_json,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
