# -*- coding: utf-8 -*-
"""devices/generic.py —— 兜底后端：标准 HID 电量字段 + 蓝牙 PnP 电池属性。

当设备不是 ROG / 罗技（或者厂商协议没适配）时，按下面两条通用路径找电量：

1. **标准 HID 电量用法**（HID Usage Tables 里定义的）
   - ``0x0006:0x0020`` Battery Strength（通用设备控制页）
   - ``0x000D:0x003B`` Battery Strength（数字化仪页）
   - ``0x0085`` 电池系统页的几个 State of Charge
   优先读 Feature Report（``HidD_GetFeature``）；没有 Feature 字段时退一步
   监听几秒 Input Report，看设备有没有主动上报。

2. **蓝牙 PnP 电池属性**（Windows 未公开属性）
   ``PROPERTYKEY {104EA319-6EE2-4701-BD47-8DDBF425BBE5}, 2``（``DEVPROP_TYPE_BYTE``）
   挂在蓝牙设备的节点上（耳机常见于 Hands-Free AG 节点）。参考
   ``_ref/repo/bt_battery.py``。

本模块**只读**：不写任何属性、不设 Feature Report。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Optional

from devices import hidiface
from devices._contract import device_info_class, reading_class

Reading = reading_class()
DeviceInfo = device_info_class()

# 标准电量 usage 表（与 hidiface.BATTERY_USAGES 保持一致）
BATTERY_USAGE_MAP: dict[tuple[int, int], str] = {
    (0x0006, 0x0020): "Battery Strength",
    (0x000D, 0x003B): "Battery Strength（数字化仪）",
    (0x0085, 0x0001): "Absolute State of Charge",
    (0x0085, 0x0066): "Relative State of Charge",
    (0x0085, 0x0065): "Battery Level",
}

# 监听 Input Report 的默认时长（秒）——只在 Feature 路径失败时用
LISTEN_SECONDS = 1.5


# --------------------------------------------------------------------------
# 数值换算
# --------------------------------------------------------------------------
def scale_level(value: int, logical_max: int) -> int:
    """把原始值按 LogicalMax 换算成 0–100。"""
    if value is None:
        return 0
    if logical_max and logical_max > 0 and logical_max != 100:
        if logical_max == 0xFF and value > 100:
            return max(0, min(100, round(value * 100 / 255)))
        return max(0, min(100, round(value * 100 / logical_max)))
    if value > 100:
        return max(0, min(100, round(value * 100 / 255)))
    return max(0, value)


def describe_battery_usage(usage_page: int, usage: int) -> Optional[str]:
    """返回该 usage 的电量字段说明；不是电量字段返回 None。"""
    return BATTERY_USAGE_MAP.get((usage_page, usage))


# --------------------------------------------------------------------------
# 标准 HID 电量
# --------------------------------------------------------------------------
@dataclass
class StandardBattery:
    """一次标准 HID 电量读取的结果。"""

    ok: bool = False
    level: Optional[int] = None
    raw: int = 0
    logical_max: int = 0
    usage_page: int = 0
    usage: int = 0
    report_id: int = 0
    from_feature: bool = True
    detail: str = ""
    error: str = ""


def read_standard_battery(info: hidiface.HidInterfaceInfo,
                          listen_seconds: float = LISTEN_SECONDS) -> StandardBattery:
    """读一个 HID 集合上的标准电量字段；读不到返回 ok=False。"""
    result = StandardBattery()
    try:
        device = hidiface.open_device(info.path, write=False)
    except hidiface.HidError as exc:
        result.error = f"打开失败：{exc}"
        return result
    try:
        # 1) Feature Report 上的电量字段
        for caps in device.value_caps(hidiface.HidP_Feature):
            usage = int(caps.u.NotRange.Usage)
            name = describe_battery_usage(int(caps.UsagePage), usage)
            if name is None:
                continue
            try:
                report = device.get_feature_report(int(caps.ReportID))
            except hidiface.HidError as exc:
                result.error = f"设备声明了{name}，但读 Feature Report 失败：{exc}"
                continue
            value = device.usage_value(hidiface.HidP_Feature, report, caps)
            if value is None:
                continue
            result.ok = True
            result.raw = int(value)
            result.logical_max = int(caps.LogicalMax)
            result.level = scale_level(int(value), int(caps.LogicalMax))
            result.usage_page = int(caps.UsagePage)
            result.usage = usage
            result.report_id = int(caps.ReportID)
            result.from_feature = True
            result.detail = (f"{name} · UsagePage=0x{result.usage_page:04X} "
                             f"Usage=0x{usage:04X} ReportID=0x{result.report_id:02X} "
                             f"原始值={result.raw} 上限={result.logical_max}")
            return result

        # 2) 退一步：监听 Input Report（设备可能只在报文里上报电量）
        input_caps = [caps for caps in device.value_caps(hidiface.HidP_Input)
                      if describe_battery_usage(int(caps.UsagePage),
                                                int(caps.u.NotRange.Usage))]
        if input_caps and listen_seconds > 0:
            deadline = time.monotonic() + listen_seconds
            while time.monotonic() < deadline:
                report = device.read_input(timeout_ms=200)
                if not report:
                    continue
                for caps in input_caps:
                    value = device.usage_value(hidiface.HidP_Input, report, caps)
                    if value is None:
                        continue
                    usage = int(caps.u.NotRange.Usage)
                    name = describe_battery_usage(int(caps.UsagePage), usage) or "电量"
                    result.ok = True
                    result.raw = int(value)
                    result.logical_max = int(caps.LogicalMax)
                    result.level = scale_level(int(value), int(caps.LogicalMax))
                    result.usage_page = int(caps.UsagePage)
                    result.usage = usage
                    result.report_id = int(caps.ReportID)
                    result.from_feature = False
                    result.detail = (f"{name} · 来自 Input Report · "
                                     f"UsagePage=0x{result.usage_page:04X} "
                                     f"Usage=0x{usage:04X} 原始值={result.raw} "
                                     f"上限={result.logical_max}")
                    return result
        if not result.error:
            result.error = "设备没有声明标准电量字段"
    except hidiface.HidError as exc:
        result.error = f"读取失败：{exc}"
    finally:
        device.close()
    return result


# --------------------------------------------------------------------------
# 蓝牙 PnP 电池属性
# --------------------------------------------------------------------------
# {104EA319-6EE2-4701-BD47-8DDBF425BBE5}, 2  —— 蓝牙 HFP 电量上报
FMTID_BLUETOOTH = hidiface.GUID(
    0x104EA319, 0x6EE2, 0x4701,
    (ctypes.c_ubyte * 8)(0xBD, 0x47, 0x8D, 0xDB, 0xF4, 0x25, 0xBB, 0xE5),
)
PID_BATTERY_LEVEL = 2

DIGCF_ALLCLASSES = 0x04
SPDRP_FRIENDLYNAME = 0x0C
SPDRP_DEVICEDESC = 0x00
ERROR_NOT_FOUND = 1168


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", hidiface.GUID), ("pid", wintypes.DWORD)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", hidiface.GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


setupapi = hidiface.setupapi
setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInfo.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA),
]
setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
setupapi.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
setupapi.SetupDiGetDevicePropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(PROPERTYKEY),
    ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
]
setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]


@dataclass
class BluetoothBattery:
    """一个蓝牙节点的电量。"""

    name: str
    instance_id: str
    level: Optional[int] = None
    has_property: bool = False
    reason: str = ""

    @property
    def is_handsfree(self) -> bool:
        lowered = self.name.lower()
        return ("hands-free" in lowered or "handsfree" in lowered
                or "免提" in self.name)


def _device_string(device_set: int, info: SP_DEVINFO_DATA, prop: int) -> str:
    buf = ctypes.create_string_buffer(1024)
    need = wintypes.DWORD(0)
    if not setupapi.SetupDiGetDeviceRegistryPropertyW(
            device_set, ctypes.byref(info), prop, None, buf, 1024, ctypes.byref(need)):
        return ""
    return buf.raw.decode("utf-16-le", errors="ignore").split("\x00")[0]


def _instance_id(device_set: int, info: SP_DEVINFO_DATA) -> str:
    buf = ctypes.create_unicode_buffer(512)
    need = wintypes.DWORD(0)
    if not setupapi.SetupDiGetDeviceInstanceIdW(
            device_set, ctypes.byref(info), buf, 512, ctypes.byref(need)):
        return ""
    return buf.value


def _read_battery_property(device_set: int,
                           info: SP_DEVINFO_DATA) -> tuple[Optional[int], bool, str]:
    """返回 (电量, 属性是否存在, 说明)。"""
    key = PROPERTYKEY(FMTID_BLUETOOTH, PID_BATTERY_LEVEL)
    prop_type = wintypes.ULONG(0)
    value = ctypes.c_ubyte(0)
    need = wintypes.DWORD(0)
    ok = setupapi.SetupDiGetDevicePropertyW(
        device_set, ctypes.byref(info), ctypes.byref(key), ctypes.byref(prop_type),
        ctypes.byref(value), 1, ctypes.byref(need), 0,
    )
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return None, False, "该设备没有电池属性（可能未连接或不上报电量）"
        return None, False, f"读取电池属性失败 err={err}"
    level = int(value.value)
    if level == 0:
        return 0, True, "属性存在但值为 0（通常表示未连接或未上报）"
    return min(100, level), True, "来自蓝牙 HFP 电量上报"


def list_bluetooth_batteries() -> list[BluetoothBattery]:
    """枚举所有 BTHENUM 节点及其电量（没有电量的也会返回，便于 probe 展示）。"""
    found: dict[str, BluetoothBattery] = {}
    attempts = (
        (None, DIGCF_ALLCLASSES),
        ("BTHENUM", 0),
    )
    for enumerator, flags in attempts:
        device_set = setupapi.SetupDiGetClassDevsW(
            None, enumerator, None, flags | hidiface.DIGCF_PRESENT,
        )
        if not device_set or device_set == hidiface.INVALID_HANDLE_VALUE:
            continue
        try:
            index = 0
            while True:
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
                if not setupapi.SetupDiEnumDeviceInfo(device_set, index, ctypes.byref(info)):
                    break
                index += 1
                instance = _instance_id(device_set, info)
                if not instance.upper().startswith("BTHENUM") or instance in found:
                    continue
                name = (_device_string(device_set, info, SPDRP_FRIENDLYNAME)
                        or _device_string(device_set, info, SPDRP_DEVICEDESC))
                level, has_property, reason = _read_battery_property(device_set, info)
                found[instance] = BluetoothBattery(
                    name=name or "(无名称)",
                    instance_id=instance,
                    level=level,
                    has_property=has_property,
                    reason=reason,
                )
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(device_set)
    return list(found.values())


def discover_bluetooth() -> list[BluetoothBattery]:
    """只返回真正带电量值的蓝牙设备（优先 Hands-Free 节点）。"""
    devices = [item for item in list_bluetooth_batteries()
               if item.has_property and item.level is not None]
    devices.sort(key=lambda item: (0 if item.is_handsfree else 1, -(item.level or 0)))
    return devices


# --------------------------------------------------------------------------
# 统一设备对象
# --------------------------------------------------------------------------
@dataclass
class GenericDevice:
    """一个兜底设备：标准 HID 电量集合，或一个蓝牙节点。"""

    device_id: str
    name: str
    kind: str                     # standard_hid / bluetooth
    connection: str = "unknown"
    vid_pid: str = ""
    path: str = ""                # standard_hid 用
    instance_id: str = ""         # bluetooth 用
    note: str = ""
    _debug: bool = field(default=False, repr=False)

    def info(self) -> "DeviceInfo":
        return DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind=self.kind,
            connection=self.connection,
            vid_pid=self.vid_pid,
            supported=True,
            note=self.note,
        )

    def read(self) -> "Reading":
        """读一次电量；永不抛异常。"""
        if self.kind == "bluetooth":
            return self._read_bluetooth()
        return self._read_standard_hid()

    def _read_standard_hid(self) -> "Reading":
        try:
            info = hidiface.probe_interface(self.path)
        except Exception:
            info = hidiface.HidInterfaceInfo(path=self.path)
        result = read_standard_battery(info)
        if not result.ok:
            return Reading(
                device_id=self.device_id, name=self.name, level=None,
                status="unknown", source="standard_hid",
                detail=result.error or "没有读到标准电量字段",
            )
        return Reading(
            device_id=self.device_id,
            name=self.name,
            level=result.level,
            status="unknown",
            source="standard_hid",
            charging=None,
            estimated=bool(result.logical_max and result.logical_max != 100),
            detail=result.detail,
        )

    def _read_bluetooth(self) -> "Reading":
        for item in list_bluetooth_batteries():
            if item.instance_id != self.instance_id:
                continue
            if not item.has_property or item.level is None:
                return Reading(
                    device_id=self.device_id, name=self.name, level=None,
                    status="unplugged", source="bluetooth",
                    detail=item.reason or "蓝牙设备没有上报电量",
                )
            return Reading(
                device_id=self.device_id,
                name=item.name or self.name,
                level=item.level,
                status="unknown",
                source="bluetooth",
                detail=f"蓝牙 PnP 电池属性 · {item.reason}",
            )
        return Reading(
            device_id=self.device_id, name=self.name, level=None,
            status="unplugged", source="bluetooth",
            detail="蓝牙设备已断开",
        )


def declared_battery_usages(info: hidiface.HidInterfaceInfo
                            ) -> list[tuple[int, int, int, bool]]:
    """探测一个集合声明了哪些标准电量字段。

    返回 ``[(usage_page, usage, report_id, 是否来自 Feature), ...]``；
    空列表表示这个集合没有标准电量字段（不代表读不到，只是不走兜底）。
    """
    found: list[tuple[int, int, int, bool]] = []
    try:
        device = hidiface.open_device(info.path, write=False)
    except hidiface.HidError:
        return found
    try:
        for report_type, from_feature in ((hidiface.HidP_Feature, True),
                                          (hidiface.HidP_Input, False)):
            for caps in device.value_caps(report_type):
                usage_page = int(caps.UsagePage)
                usage = int(caps.u.NotRange.Usage)
                if describe_battery_usage(usage_page, usage) is None:
                    continue
                found.append((usage_page, usage, int(caps.ReportID), from_feature))
    except hidiface.HidError:
        pass
    finally:
        device.close()
    return found


# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------
def discover(interfaces: Optional[list[hidiface.HidInterfaceInfo]] = None,
             include_bluetooth: bool = True,
             debug: bool = False) -> list[GenericDevice]:
    """发现兜底设备：声明了标准电量字段的 HID 集合 + 有电量的蓝牙节点。

    注意：ROG / 罗技的厂商集合也会声明一些 usage，但它们的电量走私有协议，
    由 :mod:`devices.reader` 先去重（同一个 HID 路径已被厂商后端认领就跳过）。
    """
    items = interfaces if interfaces is not None else hidiface.enumerate_interfaces()
    devices: list[GenericDevice] = []
    for info in items:
        # 只碰有 Feature/Input 报告长度的集合（纯键鼠集合读不到电量，省时间）
        if not (info.feature_len or info.input_len):
            continue
        declared = declared_battery_usages(info)
        if not declared:
            continue
        usage_page, usage, report_id, from_feature = declared[0]
        name = describe_battery_usage(usage_page, usage) or "电量"
        devices.append(GenericDevice(
            device_id=f"hid:{info.vid:04X}:{info.pid:04X}:"
                      f"{hidiface.instance_key(info.path)[-6:]}",
            name=info.display_name,
            kind="standard_hid",
            connection="unknown",
            vid_pid=info.vid_pid,
            path=info.path,
            note=(f"标准 HID 电量字段（{name} · UsagePage=0x{usage_page:04X} "
                  f"Usage=0x{usage:04X} ReportID=0x{report_id:02X} "
                  f"{'Feature' if from_feature else 'Input'}）"),
            _debug=debug,
        ))
    if include_bluetooth:
        for item in discover_bluetooth():
            devices.append(GenericDevice(
                device_id=f"bt:{item.instance_id[-12:]}",
                name=item.name,
                kind="bluetooth",
                connection="bluetooth",
                instance_id=item.instance_id,
                note=item.reason,
                _debug=debug,
            ))
    devices.sort(key=lambda device: (device.kind, device.name))
    return devices


__all__ = [
    "BATTERY_USAGE_MAP",
    "StandardBattery",
    "read_standard_battery",
    "scale_level",
    "describe_battery_usage",
    "declared_battery_usages",
    "BluetoothBattery",
    "list_bluetooth_batteries",
    "discover_bluetooth",
    "GenericDevice",
    "discover",
]
