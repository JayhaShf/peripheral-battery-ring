# -*- coding: utf-8 -*-
"""devices/logitech.py —— 罗技无线外设电量后端（HID++ 2.0）。

协议事实（真机验证，见 docs/CONTRACT.md §4.2 与 _ref/probe_logitech.py）
-------------------------------------------------------------------
* 接收器 VID ``046D``，本机 ``C539``（Lightspeed）。HID++ 在 ``mi_02`` 上：
  - **短集合** usage page ``0xFF00`` / usage ``0x0001``，输出 7 字节，用来**发请求**；
  - **长集合** usage page ``0xFF00`` / usage ``0x0002``，输入 20 字节，用来**收回复**；
    两个集合必须同时打开。
* 请求（7 字节）：``[0]=0x10, [1]=设备索引, [2]=feature 索引,
  [3]=function<<4 | 0x0C（软件 ID）, [4..]=参数``。
* 接收器槽位 1..6；读配对短名：``10 FF 83 B5 <0x40+slot-1> 00``，
  回复在长集合：``[4]`` 回显子寄存器、``[5]`` = 名字长度、``[6:]`` = 名字。
  空槽位回 ``10 FF 8F 83 B5 03 00``（HID++1.0 错误 0x8F，错误码 0x03）。
* 错误：``[2]==0x8F`` = HID++1.0 错误（接收器代答），``[2]==0xFF`` = HID++2.0 错误。
* 找特性：请求 ``[idx, 0x00, 0x0C, id>>8, id&0xFF, 0, 0]``；回复 ``[4]`` = 特性索引（0=没有）。
* 电量特性按顺序试：
  - ``0x1004`` func 1：``[0]``=soc%，``[2]``=状态（1–4 充电）；
  - ``0x1000`` func 0：``[0]``=电量%，``[2]``=状态；
  - ``0x1001`` func 0：``[0..1]``=大端 mV，``[2]`` bit7=充电中；
  - ``0x1F20`` func 0：``[0..1]``=大端 mV，``[2]`` bit1=充电中（bit0=有效）。
* 设备名：特性 ``0x0005``（func 0 取长度，func 1 分段取字符串）。
* 本机 G PRO Wireless 只有 ``0x1001``，实测 3962–3967 mV → 按锂电池曲线估算 ≈76–77%，
  返回时 ``estimated=True``。
* **有线直连**（本机 046D:C088，G PRO Wireless 插线，非接收器 PID）：同一个
  ``mi_02`` 上同时有 ``0xFF00:0x0001``（短集合 7/7，发请求）与
  ``0xFF00:0x0002``（长集合 20/20，收回复）；device index 用 ``0xFF``
  （HID++ 2.0 直连设备的标准索引）。因此把 PID 不在 :data:`RECEIVERS` 里的
  046D 设备按物理实例配成"直连设备"即可复用同一套特性探测/读取逻辑。

本模块**只发查询命令**，不写接收器寄存器/配对表。解析函数都是纯函数，
可以脱离硬件用固定字节串做单元测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from devices import hidiface
from devices._contract import device_info_class, reading_class

Reading = reading_class()
DeviceInfo = device_info_class()

LOGITECH_VID = 0x046D
HIDPP_INTERFACE = 2
VENDOR_PAGE = 0xFF00
SHORT_USAGE = 0x0001
LONG_USAGE = 0x0002

SHORT = 0x10          # 短报文 report id
SHORT_LEN = 7
LONG_LEN = 20
SOFTWARE_ID = 0x0C    # 标记本程序的请求（G HUB 用 0x0D，Solaar 用 0x0B）

RECEIVER_INDEX = 0xFF
SLOTS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
# 有线直连设备的 HID++ 设备索引（HID++ 2.0 规范：0xFF = 本机直连设备）
DIRECT_DEVICE_INDEX = 0xFF
# 产品名里带这些词的 046D 设备是接收器（即使 PID 没收录），不能当有线设备
RECEIVER_PRODUCT_HINTS: tuple[str, ...] = ("receiver", "接收器")

# HID++1.0 长寄存器读 + 接收器信息寄存器
READ_LONG_REGISTER = 0x83
RECEIVER_INFO = 0xB5
PAIRED_NAME = 0x40        # 子寄存器 0x40 + slot - 1（Unifying / Lightspeed）
BOLT_PAIRED_NAME = 0x60   # Bolt：子寄存器 0x60 + slot，再跟 0x01

HIDPP10_ERROR = 0x8F
HIDPP20_ERROR = 0xFF
ERR_INVALID_SUB_ID = 0x01

ROOT = 0x00
ROOT_GET_FEATURE = 0

FEATURE_DEVICE_NAME = 0x0005
FEATURE_BATTERY_STATUS = 0x1000
FEATURE_BATTERY_VOLTAGE = 0x1001
FEATURE_UNIFIED_BATTERY = 0x1004
FEATURE_ADC_MEASUREMENT = 0x1F20

# 超时（毫秒）：
# * 接收器本地应答通常 2–16ms，给 300ms 余量（冷启动偶尔慢）；
# * 设备应答首次唤醒可达 1s 以上，契约给的是 2s，这里取 1.5/2s；
# * 设备休眠时接收器会立刻回 HID++1.0 错误，所以不会被超时拖慢。
REGISTER_TIMEOUT_MS = 300
FEATURE_TIMEOUT_MS = 1500
BATTERY_TIMEOUT_MS = 2000
# 发现阶段的特性探测超时：只用来给 panel/probe 显示"支持哪种电量特性"，
# 短超时避免慢唤醒设备拖慢首轮 read_all（读取阶段会用完整超时）
DISCOVERY_PROBE_TIMEOUT_MS = 400

# 支持的接收器型号 -> 连接方式
RECEIVERS: dict[int, str] = {
    0xC539: "lightspeed", 0xC53A: "lightspeed", 0xC53D: "lightspeed",
    0xC53F: "lightspeed", 0xC541: "lightspeed", 0xC545: "lightspeed",
    0xC547: "lightspeed", 0xC54D: "lightspeed",
    0xC52B: "unifying", 0xC532: "unifying",
    0xC548: "bolt",
}

CONNECTION_TEXT = {
    "lightspeed": "Lightspeed 接收器",
    "unifying": "Unifying 接收器",
    "bolt": "Bolt 接收器",
    "wired": "有线 USB",
}

# 电量特性按顺序尝试：(feature id, 名称, 读取用 function)
BATTERY_FEATURES: tuple[tuple[int, str, int], ...] = (
    (FEATURE_UNIFIED_BATTERY, "0x1004 统一电量", 1),
    (FEATURE_BATTERY_STATUS, "0x1000 电量状态", 0),
    (FEATURE_BATTERY_VOLTAGE, "0x1001 电池电压", 0),
    (FEATURE_ADC_MEASUREMENT, "0x1F20 ADC 测量", 0),
)

# 锂电池放电曲线：(mV, 电量%)，从高到低。用于把电压换算成百分比（estimated）。
VOLTAGE_CURVE: tuple[tuple[int, int], ...] = (
    (4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60),
    (3811, 50), (3778, 40), (3751, 30), (3717, 20), (3671, 10),
    (3646, 5), (3579, 2), (3500, 0),
)


# --------------------------------------------------------------------------
# 纯解析函数（单元测试直接喂固定字节串）
# --------------------------------------------------------------------------
def voltage_percent(millivolts: int) -> int:
    """把锂电池电压换算成 0–100 的百分比（线性插值 + 四舍五入）。"""
    if millivolts >= VOLTAGE_CURVE[0][0]:
        return 100
    for index in range(len(VOLTAGE_CURVE) - 1):
        high_mv, high_pct = VOLTAGE_CURVE[index]
        low_mv, low_pct = VOLTAGE_CURVE[index + 1]
        if millivolts >= low_mv:
            span = (high_pct - low_pct) * (millivolts - low_mv)
            return int(low_pct + round(span / (high_mv - low_mv)))
    return 0


def parse_error(reply: bytes, header: bytes) -> Optional[tuple[str, int]]:
    """识别错误回复。

    返回 ``("hidpp10"|"hidpp20", 错误码)``；不是错误回复时返回 None。
    header 是请求的 ``[设备索引, 命令/特性, 子命令/功能]`` 三字节。
    """
    if len(reply) < 6 or len(header) < 3:
        return None
    if reply[1] != header[0]:
        return None
    if reply[2] == HIDPP10_ERROR and reply[3:5] == header[1:3]:
        return "hidpp10", int(reply[5])
    if reply[2] == HIDPP20_ERROR and reply[3:5] == header[1:3]:
        return "hidpp20", int(reply[5])
    return None


def parse_paired_name(data: bytes, bolt: bool = False) -> Optional[str]:
    """解析配对短名回复（data = 回复 ``[4:]``，即去掉 report id/索引/命令/寄存器）。

    Lightspeed/Unifying：``data[0]`` = 子寄存器回显，``data[1]`` = 长度，``data[2:]`` = 名字；
    Bolt：``data[0..1]`` = 子寄存器回显（2 字节），``data[2]`` = 长度，``data[3:]`` = 名字。
    """
    offset = 2 if bolt else 1
    if len(data) <= offset:
        return None
    length = min(int(data[offset]), len(data) - offset - 1)
    if length <= 0:
        return None
    raw = bytes(data[offset + 1:offset + 1 + length])
    name = raw.decode("utf-8", "replace").strip("\x00 ").strip()
    return name or None


def parse_feature_index(data: bytes) -> Optional[int]:
    """解析"找特性"回复：``data[0]`` = 特性索引，0 表示设备没有该特性。"""
    if not data:
        return None
    return int(data[0]) or None


def decode_battery(feature_id: int, data: bytes) -> Optional[dict]:
    """把电量特性的回复解码成 dict。

    返回 ``{"level", "charging", "voltage_mv", "estimated", "detail"}``；
    数据无效（全 0 / 长度不足）返回 None。
    """
    if feature_id == FEATURE_UNIFIED_BATTERY:
        if len(data) < 3:
            return None
        soc, level_bits, status = int(data[0]), int(data[1]), int(data[2])
        charging = status in (1, 2, 3, 4)
        if soc:
            return {
                "level": min(100, soc),
                "charging": charging,
                "voltage_mv": None,
                "estimated": False,
                "detail": f"0x1004 soc={soc}% status={status}",
            }
        # soc 为 0 时退回粗档位（bit3=100%, bit2=50%, bit1=20%, bit0=5%）
        for bit, percent in ((8, 100), (4, 50), (2, 20), (1, 5)):
            if level_bits & bit:
                return {
                    "level": percent,
                    "charging": charging,
                    "voltage_mv": None,
                    "estimated": True,
                    "detail": f"0x1004 粗档位 bits=0x{level_bits:02X} status={status}",
                }
        return None
    if feature_id == FEATURE_BATTERY_STATUS:
        if len(data) < 3 or int(data[0]) == 0:
            return None
        level, status = int(data[0]), int(data[2])
        return {
            "level": min(100, level),
            "charging": status in (1, 2, 3, 4),
            "voltage_mv": None,
            "estimated": False,
            "detail": f"0x1000 level={level}% status={status}",
        }
    if feature_id == FEATURE_BATTERY_VOLTAGE:
        if len(data) < 3:
            return None
        millivolts = (int(data[0]) << 8) | int(data[1])
        if not 2000 <= millivolts <= 5000:
            return None
        return {
            "level": voltage_percent(millivolts),
            "charging": bool(int(data[2]) & 0x80),
            "voltage_mv": millivolts,
            "estimated": True,
            "detail": f"0x1001 电压 {millivolts} mV flags=0x{int(data[2]):02X}",
        }
    if feature_id == FEATURE_ADC_MEASUREMENT:
        if len(data) < 3 or not (int(data[2]) & 0x01):
            return None
        millivolts = (int(data[0]) << 8) | int(data[1])
        if not 2000 <= millivolts <= 5000:
            return None
        return {
            "level": voltage_percent(millivolts),
            "charging": bool(int(data[2]) & 0x02),
            "voltage_mv": millivolts,
            "estimated": True,
            "detail": f"0x1F20 电压 {millivolts} mV flags=0x{int(data[2]):02X}",
        }
    return None


def status_from_charging(charging: bool, level: Optional[int]) -> str:
    """把充电布尔值翻译成契约里的 status。"""
    if charging:
        return "full" if (level is not None and level >= 100) else "charging"
    return "discharging" if level is not None else "unknown"


def build_short_request(header: bytes, params: bytes = b"") -> bytes:
    """构造 7 字节短请求。header = [设备索引, 特性/命令, 功能<<4|软件ID]。"""
    packet = bytearray(SHORT_LEN)
    packet[0] = SHORT
    packet[1:4] = bytes(header)[:3]
    if params:
        packet[4:4 + len(params)] = bytes(params)[:SHORT_LEN - 4]
    return bytes(packet)


def short_name_to_display(name: str) -> str:
    """把设备全名压成适合卡片显示的名字，并加品牌前缀。

    ``G Pro Wireless Gaming Mouse`` -> ``罗技 G Pro Wireless``
    （只去掉 "Gaming Mouse/Keyboard/Headset" 这类泛称，保留 "Wireless"）。
    """
    text = (name or "").strip()
    # 从最短的泛称开始剥，保留 "Wireless" 这类有信息量的词
    for suffix in (" Gaming Mouse", " Gaming Keyboard", " Gaming Headset",
                   " Wireless Mouse", " Wireless Keyboard",
                   " Wireless Gaming Mouse", " Wireless Gaming Keyboard",
                   " Mouse", " Keyboard"):
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].strip()
            break
    if not text:
        return "罗技设备"
    # 官方品牌写法是 "G PRO"（大写 PRO），设备上报的字符串是 "G Pro"
    if text.lower().startswith("g pro"):
        text = "G PRO" + text[len("g pro"):]
    lowered = text.lower()
    if lowered.startswith(("logitech", "罗技")):
        return text
    return f"罗技 {text}"


# --------------------------------------------------------------------------
# 底层通道
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Answer:
    """一次 HID++ 请求的结果。"""

    kind: str                 # reply / unreachable / failed
    data: bytes = b""
    code: int = 0             # failed 时的错误码
    error_kind: str = ""      # hidpp10 / hidpp20


UNREACHABLE = Answer(kind="unreachable")


class Channel:
    """一台接收器的 HID++ 短/长集合（同时打开，用完 close）。"""

    def __init__(self, short_path: str, long_path: str) -> None:
        self.short_path = short_path
        self.long_path = long_path
        self.short = hidiface.open_device(short_path, write=True, read_length=SHORT_LEN)
        try:
            self.long = hidiface.open_device(long_path, write=False, read_length=LONG_LEN)
        except Exception:
            self.short.close()
            raise

    def close(self) -> None:
        for device in (getattr(self, "long", None), getattr(self, "short", None)):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass

    def __enter__(self) -> "Channel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 请求 ----------------------------------------------------------
    def request(self, header: bytes, params: bytes = b"", echo: int = 0,
                timeout_ms: int = REGISTER_TIMEOUT_MS) -> Answer:
        """发一条短请求并等回复。

        echo：回复里必须回显的参数字节数（读寄存器时用来区分不同子寄存器）。
        """
        packet = build_short_request(header, params)
        try:
            self.short.drain(rounds=4)
            self.short.write_output(packet)
        except hidiface.HidError as exc:
            return Answer(kind="failed", code=-1, error_kind=str(exc))
        deadline = time.monotonic() + max(timeout_ms, 10) / 1000.0
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return UNREACHABLE
            # 设备和长寄存器在长集合回复；接收器报"槽位不可达"在短集合回复
            for device, wait in ((self.long, 4), (self.short, 3)):
                reply = device.read_input(timeout_ms=min(wait, max(1, int(remain * 1000))))
                if not reply or len(reply) < SHORT_LEN:
                    continue
                if reply[1] != header[0]:
                    continue
                error = parse_error(reply, header)
                if error is not None:
                    error_kind, code = error
                    if error_kind == "hidpp10" and code == ERR_INVALID_SUB_ID:
                        return Answer(kind="failed", code=code, error_kind=error_kind)
                    if error_kind == "hidpp10":
                        return UNREACHABLE
                    return Answer(kind="failed", code=code, error_kind=error_kind)
                if reply[2:4] == header[1:3] and (
                        echo == 0 or reply[4:4 + echo] == params[:echo]):
                    return Answer(kind="reply", data=bytes(reply[4:]))

    def call(self, index: int, feature: int, function: int,
             params: bytes = b"", timeout_ms: int = FEATURE_TIMEOUT_MS) -> Answer:
        """调特性函数：``[index, feature, function<<4|SWID]``。"""
        return self.request(bytes((index & 0xFF, feature & 0xFF,
                                   ((function & 0x0F) << 4) | SOFTWARE_ID)),
                            params, echo=0, timeout_ms=timeout_ms)

    def read_register(self, params: bytes, bolt: bool = False,
                      timeout_ms: int = REGISTER_TIMEOUT_MS) -> Answer:
        """读接收器信息长寄存器（``FF 83 B5 <子寄存器> ...``）。"""
        return self.request(bytes((RECEIVER_INDEX, READ_LONG_REGISTER, RECEIVER_INFO)),
                            params, echo=1 if not bolt else 2, timeout_ms=timeout_ms)

    def alive(self, index: int = DIRECT_DEVICE_INDEX,
              timeout_ms: int = DISCOVERY_PROBE_TIMEOUT_MS) -> bool:
        """设备索引上有没有 HID++ 设备应答（ROOT getFeature(0x0000) 探针）。

        ``reply`` / ``failed`` 都说明设备在（错误回复也是 HID++ 报文）；
        只有完全没回应（``unreachable``）才算不在。用于有线直连发现时快速
        排除"有 0xFF00 集合但不讲 HID++"的设备，避免白等 4 次特性探测超时。
        """
        answer = self.call(index, ROOT, ROOT_GET_FEATURE, bytes((0x00, 0x00)),
                           timeout_ms=timeout_ms)
        return answer.kind != "unreachable"


# --------------------------------------------------------------------------
# 设备对象
# --------------------------------------------------------------------------
@dataclass
class LogitechDevice:
    """一台已发现的罗技设备（接收器槽位 / 有线直连共用同一结构）。"""

    device_id: str
    slot: int
    short_path: str
    long_path: str
    connection: str
    receiver_pid: int
    paired_name: str = ""
    full_name: str = ""
    battery_feature: Optional[tuple[int, str, int]] = None   # (feature id, 名称, function)
    feature_index: Optional[int] = None                      # 特性在设备上的索引
    note: str = ""
    _debug: bool = field(default=False, repr=False)

    # -- 展示 ----------------------------------------------------------
    @property
    def is_wired(self) -> bool:
        """是否是有线直连设备（没有接收器槽位）。"""
        return self.connection == "wired"

    @property
    def name(self) -> str:
        """显示名：优先设备全名，退回接收器里存的短名 / 有线接口产品名。"""
        if self.full_name:
            return short_name_to_display(self.full_name)
        if self.paired_name:
            return short_name_to_display(self.paired_name)
        if self.is_wired:
            return f"罗技设备（有线 {self.receiver_pid:04X}）"
        return f"罗技设备（槽位 {self.slot}）"

    @property
    def connection_text(self) -> str:
        return CONNECTION_TEXT.get(self.connection, self.connection)

    @property
    def slot_text(self) -> str:
        """detail 里的位置描述：有线直连没有槽位。"""
        return "" if self.is_wired else f"槽位 {self.slot} · "

    @property
    def _no_answer_detail(self) -> str:
        """设备不应答时的提示文案（有线/无线分开，避免误导用户找接收器）。"""
        if self.is_wired:
            return "有线设备没有应答（可能已被拔出，或 HID++ 通道被系统占用）"
        return "设备没有应答（可能已关机/休眠，动一下鼠标唤醒）"

    def info(self) -> "DeviceInfo":
        return DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind="logitech_hidpp",
            connection=self.connection,
            vid_pid=f"{LOGITECH_VID:04X}:{self.receiver_pid:04X}",
            supported=True,
            note=self.note,
        )

    # -- 读取 ----------------------------------------------------------
    def read(self) -> "Reading":
        """读一次电量；永不抛异常（失败写进 Reading.detail）。"""
        try:
            with Channel(self.short_path, self.long_path) as channel:
                return self._read_with(channel)
        except Exception as exc:                        # 打开失败 / 接收器被拔
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="error", source="logitech_hidpp",
                detail=f"读取失败：{exc}",
            )

    def _read_with(self, channel: Channel) -> "Reading":
        # 1) 找到（或复用缓存的）电量特性
        if self.battery_feature is None or self.feature_index is None:
            found = self._find_battery_feature(channel)
            if found is None:
                return Reading(
                    device_id=self.device_id, name=self.name, level=None,
                    status="unplugged", source="logitech_hidpp",
                    detail=self._no_answer_detail,
                )
        battery = self.battery_feature
        feature_index = self.feature_index
        if battery is None or feature_index is None:      # 防御：理论上不会发生
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="logitech_hidpp",
                detail="没有可用的电量特性",
            )
        feature_id, feature_name, function = battery

        # 2) 读电量
        answer = channel.call(self.slot, feature_index, function,
                              timeout_ms=BATTERY_TIMEOUT_MS)
        if answer.kind != "reply":
            if answer.kind == "failed":
                # 特性索引属于当时那版固件，失效了，下次重新找
                self.battery_feature = None
                self.feature_index = None
                return Reading(
                    device_id=self.device_id, name=self.name, level=None,
                    status="unknown", source="logitech_hidpp",
                    detail=f"设备拒绝读取电量（{answer.error_kind} 错误 {answer.code}）",
                )
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unplugged", source="logitech_hidpp",
                detail=self._no_answer_detail,
            )

        decoded = decode_battery(feature_id, answer.data)
        if decoded is None:
            self.battery_feature = None
            self.feature_index = None
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="logitech_hidpp",
                detail=f"{feature_name} 回复无效：{answer.data[:8].hex(' ')}",
            )
        level = decoded["level"]
        charging = decoded["charging"]
        # 3) 首次成功读取后补一次全名（只在发现阶段失败过才做，成功就不再问）
        if not self.full_name:
            self.full_name = self._read_full_name(channel) or ""
        return Reading(
            device_id=self.device_id,
            name=self.name,
            level=level,
            status=status_from_charging(charging, level),
            voltage_mv=decoded["voltage_mv"],
            source="logitech_hidpp",
            charging=charging,
            estimated=decoded["estimated"],
            detail=f"{self.connection_text} · {self.slot_text}{decoded['detail']}",
        )

    # -- 内部 ----------------------------------------------------------
    def _find_battery_feature(self, channel: Channel,
                              timeout_ms: int = FEATURE_TIMEOUT_MS
                              ) -> Optional[tuple[int, str, int]]:
        """按顺序找电量特性；设备不可达时返回 None。"""
        for feature_id, feature_name, function in BATTERY_FEATURES:
            answer = channel.call(self.slot, ROOT, ROOT_GET_FEATURE,
                                  bytes((feature_id >> 8, feature_id & 0xFF)),
                                  timeout_ms=timeout_ms)
            if answer.kind == "unreachable":
                return None
            if answer.kind == "failed":
                return None
            index = parse_feature_index(answer.data)
            if index is None:
                continue
            self.battery_feature = (feature_id, feature_name, function)
            self.feature_index = index
            if not self.full_name:
                self.full_name = self._read_full_name(channel) or ""
            return self.battery_feature
        return None

    def _read_full_name(self, channel: Channel) -> Optional[str]:
        """读特性 0x0005 里的设备全名。"""
        answer = channel.call(self.slot, ROOT, ROOT_GET_FEATURE,
                              bytes((FEATURE_DEVICE_NAME >> 8,
                                     FEATURE_DEVICE_NAME & 0xFF)))
        if answer.kind != "reply":
            return None
        index = parse_feature_index(answer.data)
        if index is None:
            return None
        count = channel.call(self.slot, index, 0)
        if count.kind != "reply" or not count.data:
            return None
        length = int(count.data[0])
        if not 0 < length <= 64:
            return None
        chunks = bytearray()
        while len(chunks) < length:
            chunk = channel.call(self.slot, index, 1, bytes((len(chunks),)))
            if chunk.kind != "reply" or not chunk.data:
                return None
            take = min(length - len(chunks), len(chunk.data))
            if take <= 0:
                return None
            chunks.extend(chunk.data[:take])
        name = bytes(chunks).decode("utf-8", "replace").strip("\x00 ").strip()
        return name or None


# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Receiver:
    """一个已识别的接收器。"""

    pid: int
    connection: str
    short_path: str
    long_path: str
    instance: str


def find_receivers(interfaces: list[hidiface.HidInterfaceInfo]) -> list[Receiver]:
    """找出所有受支持的接收器（短/长集合按物理实例配对）。"""
    logitech = [item for item in interfaces if item.vid == LOGITECH_VID]
    shorts = [item for item in logitech
              if item.mi == HIDPP_INTERFACE and item.usage_page == VENDOR_PAGE
              and item.usage == SHORT_USAGE]
    longs = [item for item in logitech
             if item.mi == HIDPP_INTERFACE and item.usage_page == VENDOR_PAGE
             and item.usage == LONG_USAGE]
    receivers: list[Receiver] = []
    for short in shorts:
        connection = RECEIVERS.get(short.pid)
        if connection is None:
            continue
        instance = hidiface.instance_key(short.path)
        long_item = next((item for item in longs
                          if item.pid == short.pid
                          and hidiface.instance_key(item.path) == instance), None)
        if long_item is None:
            continue
        receivers.append(Receiver(pid=short.pid, connection=connection,
                                  short_path=short.path, long_path=long_item.path,
                                  instance=instance))
    receivers.sort(key=lambda item: (item.pid, item.instance))
    return receivers


@dataclass(frozen=True)
class DirectLink:
    """一台有线直连的罗技设备：同一物理实例上的 HID++ 短/长集合。"""

    pid: int
    short_path: str                 # 0xFF00:0x0001，7 字节，发请求
    long_path: str                  # 0xFF00:0x0002，20 字节，收回复
    instance: str                   # hidiface.instance_key（同型号多台时区分）
    product: str = ""               # HID 接口产品字符串（初始显示名）


def _pick_collection(items: list[hidiface.HidInterfaceInfo],
                     wanted_len: int) -> hidiface.HidInterfaceInfo:
    """同一实例里有多个候选集合时，优先报告长度符合 HID++ 规范的那个。"""
    return min(items, key=lambda item: (
        item.input_len != wanted_len, item.output_len != wanted_len, item.path))


def find_direct_devices(interfaces: list[hidiface.HidInterfaceInfo]) -> list[DirectLink]:
    """找**有线直连**的罗技 HID++ 设备（PID 不在 :data:`RECEIVERS` 表里的）。

    判据与接收器不同：不要求 ``mi_XX``（不同型号放 HID++ 集合的接口号不一样），
    只要求同一个**物理实例**上同时存在 ``0xFF00:0x0001``（短集合，发请求）和
    ``0xFF00:0x0002``（长集合，收回复）。插两个同型号设备时实例键不同，不会串。

    例：本机有线 G PRO Wireless = ``046D:C088`` 的 ``mi_02``，短 7/7 + 长 20/20。
    """
    logitech = [item for item in interfaces
                if item.vid == LOGITECH_VID and item.pid not in RECEIVERS]
    shorts = [item for item in logitech
              if item.usage_page == VENDOR_PAGE and item.usage == SHORT_USAGE]
    longs = [item for item in logitech
             if item.usage_page == VENDOR_PAGE and item.usage == LONG_USAGE]
    # 按 (PID, 物理实例) 分组：同一 USB 接口下的多个 HID 集合归到一起
    groups: dict[tuple[int, str], dict[str, list[hidiface.HidInterfaceInfo]]] = {}
    for kind, items in (("short", shorts), ("long", longs)):
        for item in items:
            key = (item.pid, hidiface.instance_key(item.path))
            groups.setdefault(key, {"short": [], "long": []})[kind].append(item)
    links: list[DirectLink] = []
    for (pid, instance), bucket in groups.items():
        if not bucket["short"] or not bucket["long"]:
            continue
        short = _pick_collection(bucket["short"], SHORT_LEN)
        long_item = _pick_collection(bucket["long"], LONG_LEN)
        product = next((item.product for item in bucket["short"] + bucket["long"]
                        if item.product), "")
        # 没收录的接收器型号（产品名带 Receiver）也不当有线设备
        if any(hint in product.lower() for hint in RECEIVER_PRODUCT_HINTS):
            continue
        links.append(DirectLink(pid=pid, short_path=short.path,
                                long_path=long_item.path, instance=instance,
                                product=product))
    links.sort(key=lambda item: (item.pid, item.instance))
    return links


def read_slot_name(channel: Channel, slot: int, connection: str,
                   timeout_ms: int = REGISTER_TIMEOUT_MS) -> Optional[str]:
    """读某个槽位上配对设备的短名；空槽位返回 None。"""
    bolt = connection == "bolt"
    params = (bytes((BOLT_PAIRED_NAME + slot, 0x01)) if bolt
              else bytes((PAIRED_NAME + slot - 1, 0x00)))
    answer = channel.read_register(params, bolt=bolt, timeout_ms=timeout_ms)
    if answer.kind != "reply":
        return None
    return parse_paired_name(answer.data, bolt=bolt)


def discover_receiver(receiver: Receiver, debug: bool = False) -> list[LogitechDevice]:
    """从一个接收器上发现所有已配对设备（含关机的，靠接收器里的短名）。"""
    devices: list[LogitechDevice] = []
    try:
        channel = Channel(receiver.short_path, receiver.long_path)
    except Exception as exc:
        if debug:
            print(f"    [logi] 打开接收器 {receiver.pid:04X} 失败：{exc}")
        return devices
    try:
        for slot in SLOTS:
            name = read_slot_name(channel, slot, receiver.connection)
            if not name:
                continue
            device = LogitechDevice(
                device_id=f"logitech:{receiver.pid:04X}#{slot}",
                slot=slot,
                short_path=receiver.short_path,
                long_path=receiver.long_path,
                connection=receiver.connection,
                receiver_pid=receiver.pid,
                paired_name=name,
                note="",
                _debug=debug,
            )
            # 发现阶段只做**一次**特性探测给调试用；用较短超时，避免设备慢唤醒时
            # 拖慢首轮 read_all。真正读数时（读取阶段）会用完整超时重试。
            try:
                found = device._find_battery_feature(
                    channel, timeout_ms=DISCOVERY_PROBE_TIMEOUT_MS)
            except Exception:
                found = None
            if found is None:
                device.note = "已配对但未探到电量特性（可能已关机/休眠，读取时会重试）"
            if debug:
                print(f"    [logi] {receiver.pid:04X} 槽位 {slot}: "
                      f"{name!r} 全名={device.full_name!r} "
                      f"特性={device.battery_feature[1] if device.battery_feature else '无'}")
            devices.append(device)
    finally:
        channel.close()
    return devices


def discover(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None,
             debug: bool = False) -> list[LogitechDevice]:
    """发现所有罗技设备（逐个接收器读槽位 1..6 的短名）。"""
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    devices: list[LogitechDevice] = []
    for receiver in find_receivers(items):
        devices.extend(discover_receiver(receiver, debug=debug))
    devices.sort(key=lambda item: (item.name, item.receiver_pid, item.slot))
    return devices


def discover_direct(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None,
                    debug: bool = False) -> list[LogitechDevice]:
    """发现**有线直连**的罗技设备（device index = 0xFF，连接方式 = wired）。

    与 :func:`discover` 的区别：不读接收器槽位，而是把同一物理实例上的 HID++
    短/长集合直接当一台设备；构造出的 :class:`LogitechDevice` 与接收器设备
    完全同构，电量特性探测/读取/设备名逻辑全部复用。

    只有探到**电量特性**才返回：有线设备不存在"休眠所以暂时问不到"的情况，
    探测不到通常说明它压根没有电池（例如有线键盘），不该多出一张空卡片。
    任何一台打不开/不应答都只影响它自己（静默跳过），不会抛异常。
    """
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    devices: list[LogitechDevice] = []
    used_ids: dict[str, int] = {}
    for link in find_direct_devices(items):
        base = f"logitech:{link.pid:04X}#wired"
        device = LogitechDevice(
            device_id=base,                    # 成功后按重名情况再补实例后缀
            slot=DIRECT_DEVICE_INDEX,
            short_path=link.short_path,
            long_path=link.long_path,
            connection="wired",
            receiver_pid=link.pid,
            paired_name=link.product,
            note="",
            _debug=debug,
        )
        try:
            with Channel(link.short_path, link.long_path) as channel:
                # 先探一下这个索引上有没有 HID++ 设备（有 0xFF00 集合的罗技耳机等
                # 不一定讲 HID++）；不应答就跳过，避免 4 次特性探测白白等超时。
                if not channel.alive():
                    if debug:
                        print(f"    [logi] 有线 {link.pid:04X} 无 HID++ 应答，跳过")
                    continue
                # 发现阶段只做一次短超时探测给调试用；真正读数时用完整超时重试
                found = device._find_battery_feature(
                    channel, timeout_ms=DISCOVERY_PROBE_TIMEOUT_MS)
        except Exception as exc:                    # 被拔掉/被系统独占：跳过这一台
            if debug:
                print(f"    [logi] 有线 {link.pid:04X} 打开/探测失败：{exc}")
            continue
        if found is None:
            # 有线设备不会"休眠"：探不到电量特性通常就是没有电池（如纯有线键盘），
            # 不生成空卡片。
            if debug:
                print(f"    [logi] 有线 {link.pid:04X} 未探到电量特性，跳过")
            continue
        # 插了两个同型号设备时用实例键区分，保证 device_id 稳定且唯一
        used_ids[base] = used_ids.get(base, 0) + 1
        device.device_id = (base if used_ids[base] == 1
                            else f"{base}#{link.instance[-8:]}")
        if debug:
            print(f"    [logi] 有线 {link.pid:04X}: 产品={link.product!r} "
                  f"全名={device.full_name!r} "
                  f"特性={device.battery_feature[1] if device.battery_feature else '无'}")
        devices.append(device)
    devices.sort(key=lambda item: (item.name, item.receiver_pid))
    return devices


def pairings(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None
             ) -> Optional[list[tuple[str, int]]]:
    """当前所有接收器的配对表 ``[(short_path, slot), ...]``。

    用于热插拔/重新配对检测：只有接收器本地应答，不会唤醒设备。
    任一接收器读失败时返回 None（表示"这次读不准，别据此删设备"）。
    """
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    result: list[tuple[str, int]] = []
    for receiver in find_receivers(items):
        try:
            with Channel(receiver.short_path, receiver.long_path) as channel:
                for slot in SLOTS:
                    if read_slot_name(channel, slot, receiver.connection):
                        result.append((receiver.short_path, slot))
        except Exception:
            return None
    result.sort()
    return result


__all__ = [
    "LOGITECH_VID",
    "RECEIVERS",
    "BATTERY_FEATURES",
    "VOLTAGE_CURVE",
    "DIRECT_DEVICE_INDEX",
    "RECEIVER_PRODUCT_HINTS",
    "Answer",
    "Channel",
    "Receiver",
    "DirectLink",
    "LogitechDevice",
    "discover",
    "discover_direct",
    "discover_receiver",
    "find_direct_devices",
    "find_receivers",
    "pairings",
    "read_slot_name",
    "parse_error",
    "parse_paired_name",
    "parse_feature_index",
    "decode_battery",
    "voltage_percent",
    "status_from_charging",
    "build_short_request",
    "short_name_to_display",
]
