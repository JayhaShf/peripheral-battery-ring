# -*- coding: utf-8 -*-
"""devices/legacy.py —— 兼容旧仓库（_ref/repo）的两条厂商协议。

对应 docs/CONTRACT.md §4.3：

``373B:1216``（ATK / VXE / VGN 无线鼠标）
----------------------------------------
* 协议 2：UsagePage ``0xFF05``，64 字节 Output Report，report id ``0x08``；
  请求 ``08 7D 72 02 00 01 07 01``；回复电量在第 7 字节，
  回复 ``[1]==0x72``、``[5]==0x07`` 才算有效。
* 协议 1：UsagePage ``0xFF02``，17 字节 Output Report，report id ``0x08``；
  请求 ``08 04 00×15 49``；电量在回复第 6 字节，回复 ``[7]!=0`` 表示有线连接。
* VID ``0x3554``（VXE / VGN）与 0x373B 的协议 1 相同。

``3654:6901``（DARKRIM SHADOW 耳机，杰理方案）
---------------------------------------------
* UsagePage ``0xFF02`` / Usage ``0x0002``，65 字节报文集（无 report id 语义，
  第 0 字节固定 0x00）；
* 帧格式 ``00 FE <seq> <ch> 00 <sub> <frag> <more> <len_lo> <len_hi> <payload>
  <crc16_hi> <crc16_lo>``，CRC16-CCITT-FALSE 覆盖 ``帧[1:10+len]``；
* 载荷是 TLV：``type(1) + len(1) + value(len)``，``0x01`` = 电量。

**本机没有这两类设备**，代码按参考实现移植（未做真机验证），
解析函数是纯函数，单测用固定字节串覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from devices import hidiface
from devices._contract import device_info_class, reading_class

Reading = reading_class()
DeviceInfo = device_info_class()

# --------------------------------------------------------------------------
# ATK / VXE
# --------------------------------------------------------------------------
ATK_VID = 0x373B
VXE_VID = 0x3554

CMD_ATK_QUERY = 0x72
CMD_VXE_QUERY = 0x04


@dataclass(frozen=True)
class AtkProtocol:
    """ATK/VXE 一条协议的全部事实。"""

    name: str
    vid: int
    usage_page: int
    report_id: int
    report_len: int
    payload: bytes
    level_offset: int
    wait_ms: int = 120
    scale: int = 1
    level_max: Optional[int] = 100
    expect: tuple[tuple[int, int], ...] = ()
    retries: int = 2
    wired_offset: Optional[int] = None
    notes: str = ""


ATK_PROTOCOLS: tuple[AtkProtocol, ...] = (
    AtkProtocol(
        name="ATK / VXE 无线鼠标（协议 2）",
        vid=ATK_VID, usage_page=0xFF05, report_id=0x08, report_len=64,
        payload=bytes((0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01)),
        level_offset=7, wait_ms=120, expect=((1, 0x72), (5, 0x07)),
        retries=3, notes="电量在回复第 7 字节；[1]=0x72、[5]=0x07 为有效标志",
    ),
    AtkProtocol(
        name="ATK / VXE / VGN 无线鼠标（协议 1）",
        vid=ATK_VID, usage_page=0xFF02, report_id=0x08, report_len=17,
        payload=bytes((0x04,)) + bytes(15) + bytes((0x49,)),
        level_offset=6, wait_ms=120, wired_offset=7,
        retries=3, notes="电量在回复第 6 字节，第 7 字节非 0 表示有线连接",
    ),
    AtkProtocol(
        name="VXE / VGN 无线鼠标（协议 1）",
        vid=VXE_VID, usage_page=0xFF02, report_id=0x08, report_len=17,
        payload=bytes((0x04,)) + bytes(15) + bytes((0x49,)),
        level_offset=6, wait_ms=120, wired_offset=7,
        retries=3, notes="VXE R1 Pro Max / R1 SE+ / VGN F1 Pro",
    ),
)


def build_atk_request(protocol: AtkProtocol) -> bytes:
    """构造 ATK 请求报文（report id + 载荷 + 补 0 到集合长度）。"""
    body = bytes((protocol.report_id,)) + protocol.payload
    return (body + bytes(protocol.report_len))[: protocol.report_len]


def parse_atk_reply(protocol: AtkProtocol,
                    reply: bytes) -> Optional[tuple[int, bool]]:
    """解析 ATK 回复，返回 (电量%, 是否为有线连接)；无效返回 None。"""
    if len(reply) <= protocol.level_offset:
        return None
    for offset, expected in protocol.expect:
        if len(reply) <= offset or reply[offset] != expected:
            return None
    raw = int(reply[protocol.level_offset])
    if protocol.level_max is not None and raw > protocol.level_max:
        return None
    level = max(0, min(100, raw * protocol.scale))
    wired = bool(protocol.wired_offset is not None
                 and len(reply) > protocol.wired_offset
                 and reply[protocol.wired_offset])
    return level, wired


# --------------------------------------------------------------------------
# DARKRIM（杰理方案）
# --------------------------------------------------------------------------
DARKRIM_VID = 0x3654
DARKRIM_USAGE_PAGE = 0xFF02
DARKRIM_USAGE = 0x0002
DARKRIM_REPORT_LEN = 65

CH_LOCAL = 0x10
CH_HEADSET = 0x11
TLV_BATTERY = 0x01
TLV_NAME = 0x02
TLV_FW_DONGLE = 0x04
TLV_FW_HEADSET = 0x05

TLV_NAMES = {
    0x01: "状态/电量", 0x02: "设备名", 0x03: "EQ 数据",
    0x04: "dongle 固件版本", 0x05: "耳机固件版本", 0x06: "状态码",
    0x09: "基础信息", 0x0B: "地址/ID", 0xFF: "校验信息",
}


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT-FALSE：poly 0x1021，init 0xFFFF，不反转。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_darkrim_frame(seq: int, payload: bytes, ch: int = CH_LOCAL,
                        sub: int = 0x02, frag: int = 0,
                        more: int = 0) -> bytes:
    """构造一帧 DARKRIM 报文（65 字节）。"""
    frame = bytearray(DARKRIM_REPORT_LEN)
    frame[0] = 0x00
    frame[1] = 0xFE
    frame[2] = seq & 0xFF
    frame[3] = ch
    frame[4] = 0x00
    frame[5] = sub
    frame[6] = frag
    frame[7] = more
    frame[8] = len(payload) & 0xFF
    frame[9] = (len(payload) >> 8) & 0xFF
    frame[10:10 + len(payload)] = payload
    crc = crc16_ccitt(bytes(frame[1:10 + len(payload)]))
    frame[10 + len(payload)] = (crc >> 8) & 0xFF
    frame[11 + len(payload)] = crc & 0xFF
    return bytes(frame)


@dataclass
class DarkrimFrame:
    """一帧解析后的 DARKRIM 报文。"""

    seq: int
    ch: int
    sub: int
    frag: int
    more: int
    payload: bytes
    crc_ok: bool
    raw: bytes

    def tlvs(self) -> list[tuple[int, bytes]]:
        """把载荷拆成 (type, value) 列表。"""
        out: list[tuple[int, bytes]] = []
        index = 0
        while index + 2 <= len(self.payload):
            tlv_type = self.payload[index]
            length = self.payload[index + 1]
            out.append((tlv_type, self.payload[index + 2:index + 2 + length]))
            index += 2 + length
        return out


def parse_darkrim_frame(report: bytes) -> Optional[DarkrimFrame]:
    """解析一帧 DARKRIM 报文；不是合法帧返回 None。"""
    if len(report) < 12 or report[1] != 0xFE:
        return None
    length = report[8] | (report[9] << 8)
    if 10 + length + 2 > len(report):
        return None
    payload = report[10:10 + length]
    crc = (report[10 + length] << 8) | report[11 + length]
    return DarkrimFrame(
        seq=report[2], ch=report[3], sub=report[5], frag=report[6],
        more=report[7], payload=payload,
        crc_ok=crc == crc16_ccitt(report[1:10 + length]), raw=report,
    )


def extract_darkrim_battery(frames: list[DarkrimFrame]) -> tuple[Optional[int], str]:
    """从多帧里提取电量与设备名，返回 (电量%, 设备名)。"""
    level: Optional[int] = None
    name = ""
    for frame in frames:
        if not frame.crc_ok:
            continue
        for tlv_type, value in frame.tlvs():
            if tlv_type == TLV_BATTERY and value:
                candidate = int(value[0])
                level = candidate if candidate <= 100 else None
            elif tlv_type == TLV_NAME and value:
                name = "".join(chr(byte) for byte in value if 32 <= byte < 127).strip()
    return level, name


def _ascii_from_tlv(value: bytes) -> str:
    return "".join(chr(byte) for byte in value if 32 <= byte < 127).strip()


# --------------------------------------------------------------------------
# 设备对象
# --------------------------------------------------------------------------
@dataclass
class AtkDevice:
    """一台 ATK / VXE 无线鼠标。"""

    device_id: str
    name: str
    protocol: AtkProtocol
    path: str
    vid_pid: str
    note: str = ""
    _debug: bool = field(default=False, repr=False)

    def info(self) -> "DeviceInfo":
        return DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind="atk",
            connection="receiver",
            vid_pid=self.vid_pid,
            supported=True,
            note=self.note,
        )

    def read(self) -> "Reading":
        """读一次电量；永不抛异常。"""
        packet = build_atk_request(self.protocol)
        reply: Optional[bytes] = None
        try:
            with hidiface.open_device(self.path, write=True,
                                      read_length=self.protocol.report_len) as device:
                device.drain(rounds=8)
                for _ in range(max(1, self.protocol.retries)):
                    device.write_output(packet)
                    deadline = time.monotonic() + self.protocol.wait_ms / 1000.0
                    while time.monotonic() < deadline:
                        candidate = device.read_input(timeout_ms=80)
                        if candidate:
                            reply = candidate
                            if not self.protocol.expect or all(
                                    len(candidate) > offset
                                    and candidate[offset] == expected
                                    for offset, expected in self.protocol.expect):
                                break
                    if reply is not None:
                        break
        except Exception as exc:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="error", source="atk", detail=f"读取失败：{exc}",
            )
        if reply is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unplugged", source="atk",
                detail="接收器没有应答（设备可能已关机/休眠）",
            )
        parsed = parse_atk_reply(self.protocol, reply)
        if parsed is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="atk",
                detail=f"回复无法解析：{reply[:16].hex(' ')}",
            )
        level, wired = parsed
        return Reading(
            device_id=self.device_id, name=self.name, level=level,
            status="discharging", source="atk",
            charging=False,
            detail=f"{self.protocol.name} · 电量[{self.protocol.level_offset}]"
                   f"=0x{reply[self.protocol.level_offset]:02X}"
                   + (" · 有线连接" if wired else ""),
        )


@dataclass
class DarkrimDevice:
    """一台 DARKRIM（杰理方案）耳机 dongle。"""

    device_id: str
    name: str
    path: str
    vid_pid: str
    note: str = ""
    _debug: bool = field(default=False, repr=False)

    def info(self) -> "DeviceInfo":
        return DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind="darkrim",
            connection="receiver",
            vid_pid=self.vid_pid,
            supported=True,
            note=self.note,
        )

    def read(self) -> "Reading":
        """读一次电量（TLV 0x01）；永不抛异常。"""
        frames: list[DarkrimFrame] = []
        try:
            with hidiface.open_device(self.path, write=True,
                                      read_length=DARKRIM_REPORT_LEN) as device:
                device.drain(rounds=8)
                device.write_output(build_darkrim_frame(0, bytes((TLV_BATTERY, 0x00))))
                deadline = time.monotonic() + 0.6
                while time.monotonic() < deadline:
                    report = device.read_input(timeout_ms=120)
                    if not report:
                        continue
                    frame = parse_darkrim_frame(report)
                    if frame is not None and frame.seq == 0:
                        frames.append(frame)
        except Exception as exc:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="error", source="darkrim", detail=f"读取失败：{exc}",
            )
        level, name = extract_darkrim_battery(frames)
        if level is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unplugged", source="darkrim",
                detail="耳机没有上报电量（确认耳机已开机并连上 dongle）",
            )
        return Reading(
            device_id=self.device_id,
            name=name or self.name,
            level=level,
            status="discharging",
            source="darkrim",
            charging=None,
            detail=f"DARKRIM TLV 协议 · TLV 0x01 电量={level}%",
        )


# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------
def discover(interfaces: list[hidiface.HidInterfaceInfo],
             debug: bool = False) -> list[object]:
    """发现 ATK/VXE 与 DARKRIM 设备（本机通常没有，返回空列表）。"""
    devices: list[object] = []
    for protocol in ATK_PROTOCOLS:
        matched = [
            item for item in interfaces
            if item.vid == protocol.vid and item.usage_page == protocol.usage_page
            and item.output_len >= protocol.report_len
        ]
        if not matched:
            continue
        item = matched[0]
        instance = hidiface.instance_key(item.path)
        device_id = f"atk:{protocol.vid:04X}:{instance[-6:]}"
        devices.append(AtkDevice(
            device_id=device_id,
            name="ATK/VXE 无线鼠标",
            protocol=protocol,
            path=item.path,
            vid_pid=item.vid_pid,
            note="旧仓库协议（未真机验证）",
            _debug=debug,
        ))
    darkrim = [
        item for item in interfaces
        if item.vid == DARKRIM_VID and item.usage_page == DARKRIM_USAGE_PAGE
        and item.output_len == DARKRIM_REPORT_LEN
    ]
    if darkrim:
        item = darkrim[0]
        instance = hidiface.instance_key(item.path)
        devices.append(DarkrimDevice(
            device_id=f"darkrim:{instance[-6:]}",
            name="DARKRIM 耳机",
            path=item.path,
            vid_pid=item.vid_pid,
            note="旧仓库协议（未真机验证）",
            _debug=debug,
        ))
    return devices


__all__ = [
    "ATK_VID",
    "VXE_VID",
    "AtkProtocol",
    "ATK_PROTOCOLS",
    "build_atk_request",
    "parse_atk_reply",
    "AtkDevice",
    "DARKRIM_VID",
    "DARKRIM_USAGE_PAGE",
    "DARKRIM_REPORT_LEN",
    "TLV_NAMES",
    "crc16_ccitt",
    "build_darkrim_frame",
    "DarkrimFrame",
    "parse_darkrim_frame",
    "extract_darkrim_battery",
    "DarkrimDevice",
    "discover",
]
