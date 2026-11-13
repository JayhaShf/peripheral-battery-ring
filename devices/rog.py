# -*- coding: utf-8 -*-
"""devices/rog.py —— ASUS ROG 无线外设电量后端（OMNI 接收器 + 自有接收器）。

协议事实（真机验证，见 docs/CONTRACT.md §4.1 与 _ref/probe_rog.py）
---------------------------------------------------------------
* OMNI 接收器 ``0B05:1ACE``，控制接口是 ``mi_02``：
  - 配对列表：``col01``，report id ``0x01``，请求 ``01 A0 00``；回复 ``[5:]``
    每 4 字节一个小端 PID，遇 0 结束；
  - 键盘电量：``col02``，report id ``0x02``，请求 ``02 12 01``；
    回复 ``[6]`` = 电量%，``[9] == 1`` 表示充电中；
  - 鼠标电量：``col03``，report id ``0x03``，请求 ``03 12 07``；
    回复 ``[5]`` = 电量（0–4 档的型号 ×25），``[10] != 0`` 表示充电中；
  - 无应答回复 ``[1]=0xFF [2]=0xAA``，表示设备关机/休眠。
* 自有 2.4G 接收器 / 有线型号：控制通道是 ``mi_01``（report id 0，65 字节）
  或 ``0xFF00`` 厂商页，命令字相同。
* **有线直连**（型号表里没有的 ``0B05`` PID，例如某些有线 ROG 键盘）：尽力探测——
  产品名像 ROG 外设时，在厂商页集合上试键盘 ``12 01`` / 鼠标 ``12 07``，
  只有能解析出电量才认，失败静默跳过（不误报、不拖慢发现）。

本模块**只发查询命令**，不写任何设备设置。解析函数都是纯函数，
可以脱离硬件用固定字节串做单元测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from devices import hidiface
from devices._contract import device_info_class, reading_class

Reading = reading_class()
DeviceInfo = device_info_class()

ASUS_VID = 0x0B05
OMNI_PID = 0x1ACE

CMD_GET = 0x12
SUB_PROBE = 0x00
SUB_KEYBOARD_BATTERY = 0x01
SUB_MOUSE_BATTERY = 0x07

# 读取超时：契约给的是 500–700ms
REPLY_TIMEOUT_MS = 650
# 配对列表查询超时（接收器本地应答，通常几毫秒）
PAIRED_TIMEOUT_MS = 500
# 未适配型号的显示名
UNKNOWN_MODEL_NAME = "ROG 设备"

# 有线直连（非 OMNI 接收器、型号表里没有的 PID）**尽力**探测参数：
# 只有产品名像 ROG 外设才试，短超时 + 总预算，失败静默跳过（不误报、不拖慢发现）。
DIRECT_PROBE_TIMEOUT_MS = 250
DIRECT_PROBE_BUDGET_MS = 600.0
DIRECT_MAX_DEVICES = 3
# 产品名/厂商名里出现这些词才认为是 ROG 外设（AURA LED Controller 之类不会被误试）
DIRECT_NAME_HINTS: tuple[str, ...] = (
    "rog", "keyboard", "mouse", "gaming", "azoth", "falchion", "claymore",
    "strix", "tuf", "harpe", "keris", "gladius", "chakram", "spatha", "pugio",
    "scope", "carry", "impact",
)


# --------------------------------------------------------------------------
# 通道与协议描述
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Channel:
    """一个控制通道：在哪个 HID 集合上、用哪个 report id、报文多长。"""

    mi: Optional[int]                 # USB 接口号（路径里的 mi_XX）
    col: Optional[str]                # HID 集合名（路径里的 colXX）
    report_id: int                    # 报文第 0 字节
    report_len: int                   # 报文总长（含 report id）
    usage_page: Optional[int] = None  # 需要按 usage page 定位时用（自有接收器）


@dataclass(frozen=True)
class Protocol:
    """电量怎么问、怎么从回复里取。"""

    kind: str          # "keyboard" / "mouse"
    battery_offset: int
    scale: int = 1
    estimated: bool = False


KEYBOARD = Protocol(kind="keyboard", battery_offset=6)
# Falchion 系列：[6] 是 0–10 档，百分比在 [11]
FALCHION = Protocol(kind="keyboard", battery_offset=11)
MOUSE = Protocol(kind="mouse", battery_offset=5)
MOUSE_GAUGE = Protocol(kind="mouse", battery_offset=5, scale=25, estimated=True)
STRIX_CARRY = Protocol(kind="mouse", battery_offset=7, scale=25, estimated=True)


# OMNI 接收器的三个通道
OMNI_PAIRED_CHANNEL = Channel(mi=2, col="col01", report_id=0x01, report_len=64)
OMNI_KEYBOARD_CHANNEL = Channel(mi=2, col="col02", report_id=0x02, report_len=64)
OMNI_MOUSE_CHANNEL = Channel(mi=2, col="col03", report_id=0x03, report_len=64)
# 自有 2.4G 接收器 / 有线的候选通道
KEYBOARD_MI1 = Channel(mi=1, col=None, report_id=0x00, report_len=65)
KEYBOARD_VENDOR = Channel(mi=None, col=None, report_id=0x00, report_len=65,
                          usage_page=0xFF00)
MOUSE_MI0 = Channel(mi=0, col=None, report_id=0x00, report_len=65)
MOUSE_MI0_64 = Channel(mi=0, col=None, report_id=0x00, report_len=64)
MOUSE_MI1 = Channel(mi=1, col=None, report_id=0x00, report_len=65)
MOUSE_MI2 = Channel(mi=2, col=None, report_id=0x00, report_len=65)


@dataclass(frozen=True)
class Model:
    """一个已知型号。pid 在 OMNI 接收器上表示"配对列表里的 PID"。"""

    pid: int
    name: str
    connection: str                   # omni_receiver / receiver / wired
    channel: Channel
    protocol: Protocol
    name_zh: str = ""


# 型号表（协议事实来自 G-Helper / peripheral-battery-tray 的公开说明，
# 只有 OMNI 接收器 + ROG 夜魔X 在本机真机验证过，其余为"未验证"）。
MODELS: tuple[Model, ...] = (
    # ---- 键盘 ----
    # 本机 OMNI 接收器的配对列表实测就是 0x1C25（ROG 夜魔X），走 col02 键盘通道
    Model(0x1C25, "ROG Azoth X", "omni_receiver", OMNI_KEYBOARD_CHANNEL, KEYBOARD,
          name_zh="ROG 夜魔X"),
    Model(0x1A85, "ROG Azoth", "omni_receiver", OMNI_KEYBOARD_CHANNEL, KEYBOARD,
          name_zh="ROG 夜魔"),
    Model(0x1A85, "ROG Azoth", "receiver", KEYBOARD_MI1, KEYBOARD, name_zh="ROG 夜魔"),
    Model(0x1A83, "ROG Azoth", "wired", KEYBOARD_MI1, KEYBOARD, name_zh="ROG 夜魔"),
    Model(0x1B42, "ROG Azoth Extreme", "omni_receiver", OMNI_KEYBOARD_CHANNEL, KEYBOARD,
          name_zh="ROG 夜魔 Extreme"),
    Model(0x1B3F, "ROG Azoth Extreme", "wired", KEYBOARD_MI1, KEYBOARD,
          name_zh="ROG 夜魔 Extreme"),
    Model(0x1CF1, "ROG Azoth Extreme SE", "omni_receiver", OMNI_KEYBOARD_CHANNEL, KEYBOARD),
    Model(0x1CEF, "ROG Azoth Extreme SE", "wired", KEYBOARD_MI1, KEYBOARD),
    Model(0x1C25, "ROG Azoth X", "receiver", KEYBOARD_MI1, KEYBOARD,
          name_zh="ROG 夜魔X"),
    Model(0x1C24, "ROG Azoth X", "wired", KEYBOARD_MI1, KEYBOARD, name_zh="ROG 夜魔X"),
    Model(0x196B, "ROG Claymore II", "wired", KEYBOARD_VENDOR, KEYBOARD),
    Model(0x193E, "ROG Falchion", "receiver", KEYBOARD_MI1, FALCHION),
    Model(0x193C, "ROG Falchion", "wired", KEYBOARD_MI1, FALCHION),
    Model(0x1B04, "ROG Falchion RX", "wired", KEYBOARD_VENDOR, FALCHION),
    Model(0x1B06, "ROG Falchion RX Low Profile", "omni_receiver",
          OMNI_KEYBOARD_CHANNEL, FALCHION),
    Model(0x1AB0, "ROG Strix Scope II 96", "omni_receiver", OMNI_KEYBOARD_CHANNEL,
          KEYBOARD),
    Model(0x1AAE, "ROG Strix Scope II 96", "wired", KEYBOARD_MI1, KEYBOARD),
    Model(0x1B7A, "ROG Strix Scope II 96 RX", "omni_receiver", OMNI_KEYBOARD_CHANNEL,
          KEYBOARD),
    Model(0x1B78, "ROG Strix Scope II 96 RX", "wired", KEYBOARD_MI1, KEYBOARD),
    Model(0x1A07, "ROG Strix Scope RX TKL", "receiver", KEYBOARD_VENDOR, KEYBOARD),
    # ---- 鼠标 ----
    Model(0x18E5, "ROG Chakram", "receiver", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x18E3, "ROG Chakram", "wired", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1A1A, "ROG Chakram X", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1A18, "ROG Chakram X", "wired", MOUSE_MI0, MOUSE),
    Model(0x18A0, "ROG Gladius II Wireless", "receiver", MOUSE_MI2, MOUSE_GAUGE),
    Model(0x197F, "ROG Gladius III Wireless", "receiver", MOUSE_MI0, MOUSE),
    Model(0x197D, "ROG Gladius III Wireless", "wired", MOUSE_MI0, MOUSE),
    Model(0x1A72, "ROG Gladius III Aimpoint", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1A72, "ROG Gladius III Aimpoint", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1A70, "ROG Gladius III Aimpoint", "wired", MOUSE_MI0, MOUSE),
    Model(0x1B0C, "ROG Gladius III Aimpoint EVA-02", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1B0A, "ROG Gladius III Aimpoint EVA-02", "wired", MOUSE_MI0, MOUSE),
    Model(0x1A94, "ROG Harpe Ace Aim Lab Edition", "omni_receiver",
          OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1A94, "ROG Harpe Ace Aim Lab Edition", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1A92, "ROG Harpe Ace Aim Lab Edition", "wired", MOUSE_MI0, MOUSE),
    Model(0x1B68, "ROG Harpe Ace Extreme", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1B69, "ROG Harpe Ace Extreme", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1B67, "ROG Harpe Ace Extreme", "wired", MOUSE_MI0, MOUSE),
    Model(0x1B65, "ROG Harpe Ace Mini", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1B63, "ROG Harpe Ace Mini", "wired", MOUSE_MI0_64, MOUSE),
    Model(0x1AD0, "ROG Harpe II Ace", "receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1C69, "ROG Harpe II Ace", "wired", MOUSE_MI0_64, MOUSE),
    Model(0x1960, "ROG Keris Wireless", "receiver", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x195E, "ROG Keris Wireless", "wired", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1A59, "ROG Keris Wireless EVA Edition", "receiver", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1A57, "ROG Keris Wireless EVA Edition", "wired", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1A68, "ROG Keris Wireless Aimpoint", "omni_receiver",
          OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1A6A, "ROG Keris Wireless Aimpoint", "omni_receiver",
          OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1A68, "ROG Keris Wireless Aimpoint", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1A66, "ROG Keris Wireless Aimpoint", "wired", MOUSE_MI0, MOUSE),
    Model(0x1B1A, "ROG Keris II Ace", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1B18, "ROG Keris II Ace", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1B16, "ROG Keris II Ace", "wired", MOUSE_MI0, MOUSE),
    Model(0x1C0E, "ROG Keris II Origin", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1C0C, "ROG Keris II Origin", "wired", MOUSE_MI0_64, MOUSE),
    Model(0x1D4E, "ROG Keris II Origin KJP", "omni_receiver", OMNI_MOUSE_CHANNEL, MOUSE),
    Model(0x1D4C, "ROG Keris II Origin KJP", "wired", MOUSE_MI0_64, MOUSE),
    Model(0x1908, "ROG Pugio II", "receiver", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1906, "ROG Pugio II", "wired", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1979, "ROG Spatha X", "receiver", MOUSE_MI0, MOUSE),
    Model(0x1977, "ROG Spatha X", "wired", MOUSE_MI0, MOUSE),
    Model(0x18B4, "ROG Strix Carry", "receiver", MOUSE_MI1, STRIX_CARRY),
    Model(0x1949, "ROG Strix Impact II Wireless", "receiver", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1947, "ROG Strix Impact II Wireless", "wired", MOUSE_MI0, MOUSE_GAUGE),
    Model(0x1AD7, "ROG Strix Impact III Wireless", "omni_receiver",
          OMNI_MOUSE_CHANNEL, MOUSE),
)


def model_name(model: Optional[Model], pid: int) -> str:
    """显示名：优先中文别名，其次英文型号名，最后兜底。"""
    if model is None:
        return f"{UNKNOWN_MODEL_NAME} 0x{pid:04X}"
    return model.name_zh or model.name


def find_model(pid: int, connection: Optional[str] = None) -> Optional[Model]:
    """按 PID（和连接方式）查型号表。"""
    for item in MODELS:
        if item.pid != pid:
            continue
        if connection is not None and item.connection != connection:
            continue
        return item
    return None


# --------------------------------------------------------------------------
# 纯解析函数（单元测试直接喂固定字节串）
# --------------------------------------------------------------------------
def is_no_answer(reply: bytes) -> bool:
    """接收器回复 ``?? FF AA``：设备关机 / 休眠 / 超出范围。"""
    return len(reply) >= 3 and reply[1] == 0xFF and reply[2] == 0xAA


def parse_paired_pids(reply: bytes) -> list[int]:
    """解析 OMNI 配对列表回复：``[5:]`` 每 4 字节一个小端 PID，遇 0 结束。"""
    if len(reply) < 9 or is_no_answer(reply):
        return []
    pids: list[int] = []
    payload = reply[5:]
    for index in range(0, len(payload) - 3, 4):
        pid = payload[index] | (payload[index + 1] << 8)
        if pid == 0:
            break
        pids.append(pid)
    return pids


def parse_keyboard_reply(reply: bytes,
                         battery_offset: int = 6) -> Optional[tuple[int, bool]]:
    """解析键盘电量回复，返回 (电量%, 是否充电中)；无效回复返回 None。"""
    if len(reply) <= max(battery_offset, 9) or is_no_answer(reply):
        return None
    # 回复必须是 12 01 开头（[1]=命令字 0x12，[2]=子命令 0x01）
    if reply[1] != CMD_GET or reply[2] != SUB_KEYBOARD_BATTERY:
        return None
    level = int(reply[battery_offset])
    if level > 100:
        return None
    return level, reply[9] == 1


def parse_mouse_reply(reply: bytes, battery_offset: int = 5,
                      scale: int = 1) -> Optional[tuple[int, bool]]:
    """解析鼠标电量回复，返回 (电量%, 是否充电中)；无效回复返回 None。"""
    if len(reply) <= max(battery_offset, 10) or is_no_answer(reply):
        return None
    if reply[1] != CMD_GET or reply[2] != SUB_MOUSE_BATTERY:
        return None
    level = min(100, int(reply[battery_offset]) * scale)
    charging = reply[10] != 0
    if level == 0 and not charging:
        return None
    return level, charging


def build_request(report_id: int, cmd: int, sub: int, length: int) -> bytes:
    """构造一条查询报文（64/65 字节，第 0 字节是 report id）。"""
    packet = bytearray(max(length, 3))
    packet[0] = report_id & 0xFF
    packet[1] = cmd & 0xFF
    packet[2] = sub & 0xFF
    return bytes(packet)


# --------------------------------------------------------------------------
# 设备对象
# --------------------------------------------------------------------------
@dataclass
class RogDevice:
    """一台已发现的 ROG 设备（读数时才打开 HID）。"""

    device_id: str
    name: str
    model: Optional[Model]
    path: str
    channel: Channel
    protocol: Protocol
    connection: str
    pid: int = 0                      # OMNI 配对列表里的 PID / USB PID
    vid_pid: str = f"{ASUS_VID:04X}:{OMNI_PID:04X}"
    note: str = ""
    _debug: bool = field(default=False, repr=False)

    # -- 展示 ----------------------------------------------------------
    def info(self) -> "DeviceInfo":
        return DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind="rog",
            connection=self.connection,
            vid_pid=self.vid_pid,
            supported=self.model is not None,
            note=self.note,
        )

    # -- 读取 ----------------------------------------------------------
    def read(self) -> "Reading":
        """读一次电量；永不抛异常（失败写进 Reading.detail）。"""
        if self.model is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="rog",
                detail=f"未适配的 ROG 型号（配对 PID 0x{self.pid:04X}），"
                       f"可用 probe.py rog 查看原始回复",
            )
        try:
            reply = query_channel(self.path, self.channel, self.protocol,
                                  timeout_ms=REPLY_TIMEOUT_MS, debug=self._debug)
        except Exception as exc:                       # 打开/读写失败
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="error", source="rog",
                detail=f"读取失败：{exc}",
            )
        if reply is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unplugged", source="rog",
                detail=f"设备没有应答（可能已关机/休眠，{self._wake_hint}）",
            )
        if is_no_answer(reply):
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unplugged", source="rog",
                detail=f"接收器回复 FF AA：设备已关机/休眠（{self._wake_hint}）",
            )
        if self.protocol.kind == "keyboard":
            parsed = parse_keyboard_reply(reply, self.protocol.battery_offset)
        else:
            parsed = parse_mouse_reply(reply, self.protocol.battery_offset,
                                       self.protocol.scale)
        if parsed is None:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="rog",
                detail=f"回复无法解析：{reply[:16].hex(' ')}",
            )
        level, charging = parsed
        status = "charging" if charging else "discharging"
        if charging and level >= 100:
            status = "full"
        if self.protocol.kind == "keyboard":
            sub = SUB_KEYBOARD_BATTERY
            charge_offset = 9
        else:
            sub = SUB_MOUSE_BATTERY
            charge_offset = 10
        return Reading(
            device_id=self.device_id,
            name=self.name,
            level=level,
            status=status,
            voltage_mv=None,
            source="rog_omni" if self.connection == "omni_receiver" else "rog_hid",
            charging=charging,
            estimated=self.protocol.estimated,
            detail=(
                f"{self.connection} · report 0x{self.channel.report_id:02X} "
                f"命令 12 {sub:02X} · 电量[{self.protocol.battery_offset}]="
                f"0x{reply[self.protocol.battery_offset]:02X}"
                + (f" ×{self.protocol.scale}" if self.protocol.scale != 1 else "")
                + f" · 充电标志[{charge_offset}]="
                f"0x{reply[charge_offset]:02X}"
            ),
        )

    @property
    def model_pid(self) -> int:
        return self.model.pid if self.model else self.pid

    @property
    def _wake_hint(self) -> str:
        """按设备类型给一句唤醒提示（键盘敲一下、鼠标动一下）。"""
        kind = self.protocol.kind if self.protocol else ""
        return "敲一下键盘唤醒" if kind == "keyboard" else "动一下鼠标唤醒"


# --------------------------------------------------------------------------
# 底层查询
# --------------------------------------------------------------------------
def query_channel(path: str, channel: Channel, protocol: Protocol,
                  timeout_ms: int = REPLY_TIMEOUT_MS,
                  debug: bool = False) -> Optional[bytes]:
    """在指定集合上发一条查询并等回复。

    返回 None 表示超时；返回 ``?? FF AA`` 表示设备休眠。异常由调用方处理。
    """
    sub = (SUB_KEYBOARD_BATTERY if protocol.kind == "keyboard"
           else SUB_MOUSE_BATTERY)
    with hidiface.open_device(path, write=True,
                              read_length=channel.report_len) as device:
        # 报文长度以驱动声明为准（report id 0 的集合可能略有出入）
        try:
            output_len = int(device.caps.OutputReportByteLength) or channel.report_len
        except hidiface.HidError:
            output_len = channel.report_len
        packet = build_request(channel.report_id, CMD_GET, sub, output_len)
        device.drain(rounds=8)
        device.write_output(packet)
        deadline = time.monotonic() + max(timeout_ms, 50) / 1000.0
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            reply = device.read_input(timeout_ms=max(1, int(remain * 1000)))
            if not reply:
                continue
            if len(reply) < 3:
                continue
            if reply[1:3] == bytes((CMD_GET, sub)) or is_no_answer(reply):
                if debug:
                    print(f"    [rog] {channel.mi}/{channel.col} <- {reply[:20].hex(' ')}")
                return reply


def read_paired_pids(path: str, timeout_ms: int = PAIRED_TIMEOUT_MS,
                     debug: bool = False) -> list[int]:
    """读 OMNI 接收器的配对列表（col01，report 0x01，命令 A0 00）。"""
    request = build_request(0x01, 0xA0, 0x00, OMNI_PAIRED_CHANNEL.report_len)
    try:
        with hidiface.open_device(path, write=True,
                                  read_length=OMNI_PAIRED_CHANNEL.report_len) as device:
            try:
                output_len = int(device.caps.OutputReportByteLength) or len(request)
            except hidiface.HidError:
                output_len = len(request)
            device.drain(rounds=8)
            device.write_output(build_request(0x01, 0xA0, 0x00, output_len))
            deadline = time.monotonic() + max(timeout_ms, 50) / 1000.0
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return []
                reply = device.read_input(timeout_ms=max(1, int(remain * 1000)))
                if not reply or len(reply) < 9:
                    continue
                if reply[1:3] == bytes((0xA0, 0x00)):
                    if debug:
                        print(f"    [rog] 配对列表 <- {reply[:24].hex(' ')}")
                    return parse_paired_pids(reply)
    except Exception as exc:                            # 接收器被拔/权限不足
        if debug:
            print(f"    [rog] 读配对列表失败：{exc}")
        return []


# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------
def _match_channel(interfaces: list[hidiface.HidInterfaceInfo], channel: Channel,
                   instance: Optional[str]) -> Optional[hidiface.HidInterfaceInfo]:
    """在同一物理实例里按通道描述找 HID 集合。"""
    for item in interfaces:
        if instance is not None and hidiface.instance_key(item.path) != instance:
            continue
        if channel.mi is not None and item.mi != channel.mi:
            continue
        if channel.col is not None and (item.col or "").lower() != channel.col:
            continue
        if channel.usage_page is not None and item.usage_page != channel.usage_page:
            continue
        if channel.mi is None and channel.usage_page is None:
            continue
        if item.output_len and item.output_len != channel.report_len:
            continue
        return item
    return None


def _omni_devices(interfaces: list[hidiface.HidInterfaceInfo],
                  omni_iface: hidiface.HidInterfaceInfo,
                  debug: bool) -> list[RogDevice]:
    """从一个 OMNI 接收器上按配对列表发现设备。"""
    instance = hidiface.instance_key(omni_iface.path)
    pids = read_paired_pids(omni_iface.path, debug=debug)
    devices: list[RogDevice] = []
    seen: dict[int, int] = {}
    for pid in pids:
        model = find_model(pid, "omni_receiver")
        # 同一接收器上配对了两台同型号设备时加序号，避免 device_id 冲突
        seen[pid] = seen.get(pid, 0) + 1
        suffix = "" if seen[pid] == 1 else f"#{seen[pid]}"
        if model is None:
            devices.append(RogDevice(
                device_id=f"rog:0x{pid:04X}{suffix}",
                name=f"{UNKNOWN_MODEL_NAME} 0x{pid:04X}",
                model=None,
                path=omni_iface.path,
                channel=OMNI_KEYBOARD_CHANNEL,
                protocol=KEYBOARD,
                connection="omni_receiver",
                pid=pid,
                note="接收器配对列表里有这台设备，但型号未适配",
            ))
            continue
        iface = _match_channel(interfaces, model.channel, instance)
        if iface is None:
            if debug:
                print(f"    [rog] 0x{pid:04X} 找不到 {model.channel} 集合，跳过")
            continue
        devices.append(RogDevice(
            device_id=f"rog:0x{pid:04X}{suffix}",
            name=model_name(model, pid),
            model=model,
            path=iface.path,
            channel=model.channel,
            protocol=model.protocol,
            connection="omni_receiver",
            pid=pid,
            note="" if pid == 0x1C25 else "型号未在真机验证",
        ))
    return devices


def _direct_devices(interfaces: list[hidiface.HidInterfaceInfo],
                    debug: bool) -> list[RogDevice]:
    """发现自有 2.4G 接收器 / 有线直连的 ROG 设备。"""
    devices: list[RogDevice] = []
    # 按 (VID, PID, 实例键) 分组：同一物理设备的多个集合归到一起，
    # 插两个相同接收器时也不会互相串（实例键不同）
    groups: dict[tuple[int, int, str], list[hidiface.HidInterfaceInfo]] = {}
    for item in interfaces:
        if item.vid != ASUS_VID or item.pid == OMNI_PID:
            continue
        key = (item.vid, item.pid, hidiface.instance_key(item.path))
        groups.setdefault(key, []).append(item)
    for (_, pid, instance), items in groups.items():
        model = find_model(pid, "receiver") or find_model(pid, "wired")
        if model is None:
            continue
        iface = _match_channel(items, model.channel, instance)
        if iface is None:
            continue
        # 探针：键盘通道回 "无应答" 或 [5] != 0xFA 才算对；鼠标通道任何回复都算对
        try:
            probe = query_channel(iface.path, model.channel,
                                  Protocol(kind=model.protocol.kind,
                                           battery_offset=model.protocol.battery_offset),
                                  timeout_ms=300, debug=debug)
        except Exception as exc:
            if debug:
                print(f"    [rog] 探针失败 {pid:04X}：{exc}")
            continue
        if probe is None:
            continue
        if model.protocol.kind == "keyboard" and len(probe) > 5 and probe[5] == 0xFA:
            continue
        devices.append(RogDevice(
            device_id=f"rog:0x{pid:04X}#{instance[-6:]}",
            name=model_name(model, pid),
            model=model,
            path=iface.path,
            channel=model.channel,
            protocol=model.protocol,
            connection=model.connection,
            pid=pid,
            vid_pid=f"{ASUS_VID:04X}:{pid:04X}",
            note="型号未在真机验证",
        ))
    return devices


def _looks_like_rog_peripheral(items: list[hidiface.HidInterfaceInfo]) -> bool:
    """产品名/厂商名像不像 ROG 外设（用来挡掉 AURA LED 控制器这类非外设）。"""
    text = " ".join(f"{item.product} {item.manufacturer}" for item in items).lower()
    return any(hint in text for hint in DIRECT_NAME_HINTS)


def _direct_candidates(items: list[hidiface.HidInterfaceInfo]
                       ) -> list[hidiface.HidInterfaceInfo]:
    """从一台设备的接口里挑出能试 OMNI 命令的厂商页集合（按可信度排序）。"""
    candidates = [
        item for item in items
        if item.usage_page >= 0xFF00          # 厂商页
        and item.output_len > 0               # 能发 Output Report
        and item.input_len > 0                # 能收回复
        and not item.reserved                 # 不是被系统独占的键盘/鼠标顶层集合
    ]
    candidates.sort(key=lambda item: (
        item.output_len not in (64, 65),      # 优先 64/65 字节报文（OMNI 协议惯例）
        item.usage_page != 0xFF00,            # 优先 0xFF00 厂商页
        item.path,
    ))
    return candidates


def probe_direct_device(path: str, item: hidiface.HidInterfaceInfo,
                        timeout_ms: int = DIRECT_PROBE_TIMEOUT_MS,
                        debug: bool = False) -> Optional[tuple[Protocol, Channel]]:
    """在一个厂商页集合上试 OMNI 电量命令，返回 ``(协议, 通道)`` 或 None。

    先试键盘 ``12 01``，再试鼠标 ``12 07``；只有**能解析出电量**才算成功
    （回复必须回显 12 01/12 07 且电量合法），任何异常/超时都返回 None。
    """
    channel = Channel(mi=item.mi, col=item.col, report_id=0x00,
                      report_len=max(int(item.input_len), int(item.output_len)) or 65,
                      usage_page=item.usage_page)
    for protocol in (KEYBOARD, MOUSE):
        try:
            reply = query_channel(path, channel, protocol,
                                  timeout_ms=timeout_ms, debug=debug)
        except Exception as exc:                  # 打不开/没有写权限：这台跳过
            if debug:
                print(f"    [rog] 直连探测 {item.vid_pid} 打开失败：{exc}")
            return None
        if not reply or is_no_answer(reply):
            continue
        if protocol.kind == "keyboard" and len(reply) > 5 and reply[5] == 0xFA:
            continue                              # 0xFA = 该通道不支持这条命令
        if protocol.kind == "keyboard":
            parsed = parse_keyboard_reply(reply, protocol.battery_offset)
        else:
            parsed = parse_mouse_reply(reply, protocol.battery_offset,
                                       protocol.scale)
        if parsed is None:
            continue
        if debug:
            print(f"    [rog] 直连探测 {item.vid_pid} mi={item.mi} "
                  f"{item.col or '-'} 命中 {protocol.kind}：{reply[:16].hex(' ')}")
        return protocol, channel
    return None


def discover_direct(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None,
                    debug: bool = False,
                    skip_paths: Optional[Iterable[str]] = None,
                    budget_ms: float = DIRECT_PROBE_BUDGET_MS) -> list[RogDevice]:
    """**尽力**发现有线直连的 ROG 设备（型号表里没有的 PID）。

    做法：把 ``0B05`` 非 OMNI 设备按物理实例分组 → 只挑产品名像 ROG 外设的 →
    在厂商页集合上试 OMNI 协议命令（键盘 ``12 01`` / 鼠标 ``12 07``）。
    只有能解析出电量才认，任何失败/超时都静默跳过；总耗时受 ``budget_ms`` 限制，
    所以不会误报，也不会明显拖慢发现。

    已知型号（:data:`MODELS` 里的 wired/receiver 条目）由 :func:`discover` 的
    ``_direct_devices`` 处理，这里不重复探测（``skip_paths`` 还能跳过已认领设备）。
    """
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    skip_instances = {hidiface.instance_key(path) for path in (skip_paths or ())}
    groups: dict[tuple[int, str], list[hidiface.HidInterfaceInfo]] = {}
    for item in items:
        if item.vid != ASUS_VID or item.pid == OMNI_PID:
            continue
        instance = hidiface.instance_key(item.path)
        if instance in skip_instances:
            continue
        if find_model(item.pid, "receiver") or find_model(item.pid, "wired"):
            continue                              # 型号表里有，交给 _direct_devices
        groups.setdefault((item.pid, instance), []).append(item)

    devices: list[RogDevice] = []
    used_ids: dict[str, int] = {}
    deadline = time.monotonic() + max(budget_ms, 0.0) / 1000.0
    probes = 0
    for (pid, instance), group in sorted(groups.items()):
        if probes >= DIRECT_MAX_DEVICES or time.monotonic() >= deadline:
            break
        if not _looks_like_rog_peripheral(group):
            continue
        candidates = _direct_candidates(group)
        if not candidates:
            continue
        probes += 1
        found = probe_direct_device(candidates[0].path, candidates[0], debug=debug)
        if found is None:
            continue
        protocol, channel = found
        product = next((item.product for item in group if item.product), "")
        name = product or f"{UNKNOWN_MODEL_NAME} 0x{pid:04X}"
        model = Model(pid=pid, name=name, connection="wired",
                      channel=channel, protocol=protocol)
        base = f"rog:0x{pid:04X}#wired"
        used_ids[base] = used_ids.get(base, 0) + 1
        device_id = (base if used_ids[base] == 1
                     else f"{base}#{instance[-6:]}")
        devices.append(RogDevice(
            device_id=device_id,
            name=name,
            model=model,
            path=candidates[0].path,
            channel=channel,
            protocol=protocol,
            connection="wired",
            pid=pid,
            vid_pid=f"{ASUS_VID:04X}:{pid:04X}",
            note="有线直连（未收录型号，OMNI 命令探测成功，未在真机验证）",
            _debug=debug,
        ))
    devices.sort(key=lambda dev: (dev.name, dev.device_id))
    return devices


def discover(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None,
             debug: bool = False) -> list[RogDevice]:
    """发现所有 ROG 设备（OMNI 接收器按配对列表，其余按型号表）。"""
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    asus = [item for item in items if item.vid == ASUS_VID]
    if not asus:
        return []
    devices: list[RogDevice] = []
    omni_instances: set[str] = set()
    for item in asus:
        if item.pid != OMNI_PID or item.mi != 2 or (item.col or "").lower() != "col01":
            continue
        instance = hidiface.instance_key(item.path)
        if instance in omni_instances:
            continue
        omni_instances.add(instance)
        devices.extend(_omni_devices(items, item, debug))
    devices.extend(_direct_devices(asus, debug))
    devices.sort(key=lambda dev: (dev.name, dev.device_id))
    return devices


__all__ = [
    "ASUS_VID",
    "OMNI_PID",
    "Model",
    "Protocol",
    "Channel",
    "MODELS",
    "RogDevice",
    "discover",
    "discover_direct",
    "probe_direct_device",
    "find_model",
    "model_name",
    "is_no_answer",
    "parse_paired_pids",
    "parse_keyboard_reply",
    "parse_mouse_reply",
    "build_request",
    "query_channel",
    "read_paired_pids",
]
