# -*- coding: utf-8 -*-
"""devices/hidiface.py —— 纯 ctypes 的 Windows HID 访问层（零第三方依赖）。

职责
----
* 枚举系统里的 HID 接口（VID/PID、UsagePage/Usage、报告长度、产品名、mi_XX/colXX）；
* 打开接口、发 Output Report、读 Input Report、读 Feature Report、读报告描述符；
* 解析 HID 值能力表（HidP_GetValueCaps），给"标准电量字段"兜底用。

两条必须遵守的硬性约束（见 docs/CONTRACT.md §0）
------------------------------------------------
1. **Win64 句柄安全**：所有返回句柄/指针的 API 一律 ``restype = ctypes.c_void_p``，
   否则 64 位句柄被截断成 32 位，后面 CloseHandle 直接崩进程。
2. **读要用独立的 overlapped 读句柄**：在同一个句柄上"阻塞 ReadFile + WriteFile"
   会让 HID 驱动层把进程打崩。这里写句柄和读句柄分开，读句柄带
   ``FILE_FLAG_OVERLAPPED``，超时后用 ``CancelIoEx`` 取消并等它真正完成。

另外两点工程经验
----------------
* 系统独占的顶层集合（键盘/鼠标/触控板）只能用 ``dwDesiredAccess = 0`` 打开，
  此时仍可读属性、能力表和 Feature Report，但不能发 Output Report；
* ``HidD_GetReportDescriptor`` 这个导出在部分系统上不存在，报告描述符改用
  ``DeviceIoControl(IOCTL_HID_GET_REPORT_DESCRIPTOR)`` 读取，失败就返回空（不致命）。
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

if sys.platform != "win32":  # pragma: no cover - 本项目只跑 Windows
    raise RuntimeError("devices/hidiface.py 仅支持 Windows")

# --------------------------------------------------------------------------
# DLL 与常量
# --------------------------------------------------------------------------
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

HidP_Input = 0
HidP_Output = 1
HidP_Feature = 2
HIDP_STATUS_SUCCESS = 0x00110000

IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0000

# 报告描述符里"电量"相关的标准 usage：(UsagePage, Usage, 说明)
BATTERY_USAGES: tuple[tuple[int, int, str], ...] = (
    (0x0006, 0x0020, "Battery Strength（通用设备控制页）"),
    (0x000D, 0x003B, "Battery Strength（数字化仪页）"),
    (0x0085, 0x0001, "Absolute State of Charge（电池系统页）"),
    (0x0085, 0x0066, "Relative State of Charge（电池系统页）"),
    (0x0085, 0x0065, "Battery Level（电池系统页）"),
)

# 被 Windows 独占（Exclusive）打开的顶层集合：只能以 0 访问权限打开
SYSTEM_RESERVED_USAGES: frozenset[tuple[int, int]] = frozenset({
    (0x0001, 0x0001),  # Pointer
    (0x0001, 0x0002),  # Mouse
    (0x0001, 0x0006),  # Keyboard
    (0x0001, 0x0007),  # Keypad
    (0x000D, 0x0001),  # Pen
    (0x000D, 0x0002),  # Pen
    (0x000D, 0x0004),  # Touchscreen
    (0x000D, 0x0005),  # Precision Touchpad
})


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class OVERLAPPED(ctypes.Structure):
    """异步（overlapping）I/O 用；ctypes.wintypes 里没有。"""

    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class _RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", wintypes.USHORT),
        ("UsageMax", wintypes.USHORT),
    ]


class _NOT_RANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("Reserved1", wintypes.USHORT),
        ("StringIndex", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT),
        ("DesignatorIndex", wintypes.USHORT),
        ("Reserved3", wintypes.USHORT),
        ("DataIndex", wintypes.USHORT),
        ("Reserved4", wintypes.USHORT),
    ]


class _USAGE_UNION(ctypes.Union):
    _fields_ = [
        ("Range", _RANGE),
        ("NotRange", _NOT_RANGE),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("u", _USAGE_UNION),
    ]


# --------------------------------------------------------------------------
# 函数原型（句柄/指针一律 c_void_p，见 CONTRACT §0.4）
# --------------------------------------------------------------------------
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
    wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
]
kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]
kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

hid.HidD_GetAttributes.restype = wintypes.BOOL
hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.restype = wintypes.BOOL
hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.restype = wintypes.BOOL
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetValueCaps.restype = ctypes.c_long
hid.HidP_GetValueCaps.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.USHORT), ctypes.c_void_p,
]
hid.HidP_GetUsageValue.restype = ctypes.c_long
hid.HidP_GetUsageValue.argtypes = [
    ctypes.c_int, wintypes.USHORT, wintypes.USHORT, wintypes.USHORT,
    ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG,
]
hid.HidD_GetFeature.restype = wintypes.BOOL
hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_SetOutputReport.restype = wintypes.BOOL
hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
for _fn in ("HidD_GetProductString", "HidD_GetManufacturerString",
            "HidD_GetSerialNumberString"):
    _f = getattr(hid, _fn)
    _f.restype = wintypes.BOOL
    _f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]

# HID 设备接口类 GUID {4D1E55B2-F16F-11CF-88CB-001111000030}
HID_GUID = GUID(
    0x4D1E55B2, 0xF16F, 0x11CF,
    (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)

_RE_MI = re.compile(r"&mi_(\d+)", re.I)
_RE_COL = re.compile(r"&col(\d+)", re.I)
_RE_VID = re.compile(r"vid_([0-9a-f]{4})", re.I)
_RE_PID = re.compile(r"pid_([0-9a-f]{4})", re.I)


# --------------------------------------------------------------------------
# 接口描述
# --------------------------------------------------------------------------
@dataclass
class HidInterfaceInfo:
    """一个 HID 接口（= 一个顶层集合）的静态信息。"""

    path: str
    vid: int = 0
    pid: int = 0
    version: int = 0
    usage_page: int = 0
    usage: int = 0
    input_len: int = 0
    output_len: int = 0
    feature_len: int = 0
    product: str = ""
    manufacturer: str = ""
    serial: str = ""
    mi: Optional[int] = None
    col: Optional[str] = None
    error: str = ""

    @property
    def vid_pid(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def is_vendor_page(self) -> bool:
        return self.usage_page >= 0xFF00

    @property
    def reserved(self) -> bool:
        """是否被系统独占（只能用 0 访问权限打开）。"""
        return (self.usage_page, self.usage) in SYSTEM_RESERVED_USAGES

    @property
    def group(self) -> str:
        """同一物理设备的所有集合共用的分组键（路径第 2 段去掉 mi_/col_）。"""
        return physical_group_key(self.path)

    @property
    def display_name(self) -> str:
        if self.product:
            return self.product
        if self.manufacturer:
            return f"{self.manufacturer} HID 设备"
        return "HID 设备"

    def describe(self) -> str:
        return (f"{self.vid_pid} mi={self.mi} {self.col or '-'} "
                f"up=0x{self.usage_page:04X} u=0x{self.usage:04X} "
                f"in={self.input_len} out={self.output_len} feat={self.feature_len}")


def parse_path_info(path: str) -> tuple[int, int, Optional[int], Optional[str]]:
    """从设备接口路径里解析 (vid, pid, mi, col)。"""
    vid = pid = 0
    m = _RE_VID.search(path)
    if m:
        vid = int(m.group(1), 16)
    m = _RE_PID.search(path)
    if m:
        pid = int(m.group(1), 16)
    m = _RE_MI.search(path)
    mi = int(m.group(1)) if m else None
    m = _RE_COL.search(path)
    col = f"col{int(m.group(1)):02d}" if m else None
    return vid, pid, mi, col


def physical_group_key(path: str) -> str:
    """同一物理设备的多个集合归到一组（路径第 2 段去掉 mi_/col_）。"""
    parts = path.split("#")
    if len(parts) < 2:
        return path
    keep: list[str] = []
    for item in parts[1].split("&"):
        low = item.lower()
        if low.startswith("col") or low.startswith("mi_"):
            continue
        keep.append(item)
    return "&".join(keep) or parts[1]


def instance_key(path: str) -> str:
    """同一个 USB 接口的所有集合共用的实例键。

    路径形如 ``\\\\?\\hid#vid_0b05&pid_1ace&mi_02&col02#a&7aa98ac&0&0001#{guid}``，
    第 3 段 ``a&7aa98ac&0&0001`` 里最后一段是集合序号，去掉它即可把
    mi_02 的 col01/col02/col03 归到同一个实例（区分插了两个相同接收器的情况）。
    """
    parts = path.split("#")
    if len(parts) < 3:
        return physical_group_key(path)
    return parts[2].rsplit("&", 1)[0].lower()


def _wide_string(func, handle, size: int = 512) -> str:
    """调用 HidD_GetXxxString 读宽字符串。"""
    buf = ctypes.create_string_buffer(size)
    try:
        if not func(handle, buf, size):
            return ""
    except Exception:  # pragma: no cover - 个别驱动会直接异常
        return ""
    return buf.raw.decode("utf-16-le", errors="ignore").split("\x00")[0].strip()


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
def list_paths() -> list[str]:
    """只枚举设备接口路径（不打开设备，约 0.5ms，用于热插拔检测）。"""
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(HID_GUID), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        return []
    out: list[str] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(HID_GUID), index, ctypes.byref(iface)
            ):
                break
            index += 1
            need = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(iface), None, 0, ctypes.byref(need), None
            )
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize：Win64 下固定为 8
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(iface), buf, need.value, None, None
            ):
                continue
            path = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.c_wchar_p).value or ""
            if path:
                out.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def enumerate_interfaces(detail: bool = True) -> list[HidInterfaceInfo]:
    """枚举所有 HID 接口。

    detail=False 时只解析路径（快），detail=True 时逐个打开读属性与报告长度
    （本机 26 个接口约 300ms，只在发现阶段做）。
    """
    result: list[HidInterfaceInfo] = []
    for path in list_paths():
        if detail:
            result.append(probe_interface(path))
        else:
            vid, pid, mi, col = parse_path_info(path)
            result.append(HidInterfaceInfo(path=path, vid=vid, pid=pid, mi=mi, col=col))
    return result


def probe_interface(path: str) -> HidInterfaceInfo:
    """打开一个接口读属性/能力表，然后关掉。"""
    vid, pid, mi, col = parse_path_info(path)
    info = HidInterfaceInfo(path=path, vid=vid, pid=pid, mi=mi, col=col)
    handle = kernel32.CreateFileW(
        path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        info.error = f"无法打开（err={ctypes.get_last_error()}）"
        return info
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            info.vid = attrs.VendorID
            info.pid = attrs.ProductID
            info.version = attrs.VersionNumber
        info.product = _wide_string(hid.HidD_GetProductString, handle)
        info.manufacturer = _wide_string(hid.HidD_GetManufacturerString, handle)
        info.serial = _wide_string(hid.HidD_GetSerialNumberString, handle)
        preparsed = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)) and preparsed:
            try:
                caps = HIDP_CAPS()
                if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS:
                    info.usage_page = caps.UsagePage
                    info.usage = caps.Usage
                    info.input_len = caps.InputReportByteLength
                    info.output_len = caps.OutputReportByteLength
                    info.feature_len = caps.FeatureReportByteLength
            finally:
                hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)
    return info


# --------------------------------------------------------------------------
# 句柄封装
# --------------------------------------------------------------------------
class HidError(OSError):
    """HID 打开/读写失败。"""

    def __str__(self) -> str:  # noqa: D105 - 让 detail 文案干净（不带 [Errno N] 前缀）
        return str(self.args[0]) if self.args else "HID 操作失败"


class HidDevice:
    """一个已打开的 HID 接口。

    参数
    ----
    write: 是否需要发 Output Report（True 时用 GENERIC_READ|GENERIC_WRITE 打开，
           失败会自动退回 0 访问权限，此时 write_access=False）；
    read_length: 输入报告长度，默认用能力表里的 InputReportByteLength。

    读和写用**两个不同的句柄**：写句柄普通打开，读句柄带 FILE_FLAG_OVERLAPPED
    并配一个事件，超时用 CancelIoEx 取消（见模块开头说明）。
    """

    def __init__(self, path: str, write: bool = False,
                 read_length: Optional[int] = None) -> None:
        self.path = path
        self.write_access = False
        self.handle: Optional[int] = None
        self._read_handle: Optional[int] = None
        self._read_event: Optional[int] = None
        self._preparsed = ctypes.c_void_p()
        self._caps: Optional[HIDP_CAPS] = None
        self._read_length = read_length or 0

        handle = None
        if write:
            # 先按读写权限打开（厂商集合一般是共享的，能开）
            handle = kernel32.CreateFileW(
                path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                None, OPEN_EXISTING, 0, None,
            )
            if handle and handle != INVALID_HANDLE_VALUE:
                self.write_access = True
            else:
                handle = None
        if not handle:
            # 退一步：0 访问权限（可读能力表/Feature，但不能发 Output Report）
            handle = kernel32.CreateFileW(
                path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                OPEN_EXISTING, 0, None,
            )
            self.write_access = False
        if not handle or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise HidError(f"CreateFileW 失败（err={err}）：{path}", err)
        self.handle = handle
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(self._preparsed)):
            self._preparsed = ctypes.c_void_p()

    # -- 生命周期 ------------------------------------------------------
    def close(self) -> None:
        """关闭所有句柄（幂等）。"""
        if self._read_handle:
            kernel32.CancelIoEx(self._read_handle, None)
            kernel32.CloseHandle(self._read_handle)
            self._read_handle = None
        if self._read_event:
            kernel32.CloseHandle(self._read_event)
            self._read_event = None
        if self._preparsed:
            hid.HidD_FreePreparsedData(self._preparsed)
            self._preparsed = ctypes.c_void_p()
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "HidDevice":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 兜底
        try:
            self.close()
        except Exception:
            pass

    # -- 能力表 --------------------------------------------------------
    @property
    def caps(self) -> HIDP_CAPS:
        if self._caps is None:
            caps = HIDP_CAPS()
            if not self._preparsed:
                raise HidError("没有 preparsed data，读不到能力表")
            if hid.HidP_GetCaps(self._preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                raise HidError("HidP_GetCaps 失败")
            self._caps = caps
        return self._caps

    @property
    def input_length(self) -> int:
        if self._read_length:
            return self._read_length
        try:
            return int(self.caps.InputReportByteLength) or 0
        except HidError:
            return 0

    def value_caps(self, report_type: int = HidP_Feature) -> list[HIDP_VALUE_CAPS]:
        """返回指定报告类型的值能力表。"""
        if not self._preparsed:
            return []
        length = wintypes.USHORT(0)
        hid.HidP_GetValueCaps(report_type, None, ctypes.byref(length), self._preparsed)
        if not length.value:
            return []
        arr = (HIDP_VALUE_CAPS * length.value)()
        status = hid.HidP_GetValueCaps(
            report_type, ctypes.byref(arr), ctypes.byref(length), self._preparsed
        )
        if status != HIDP_STATUS_SUCCESS:
            return []
        return [arr[i] for i in range(length.value)]

    # -- 报文读写 ------------------------------------------------------
    def get_feature_report(self, report_id: int, length: Optional[int] = None) -> bytes:
        """读一条 Feature Report；返回的 bytes[0] 是 report id。"""
        if length is None:
            try:
                length = int(self.caps.FeatureReportByteLength) or 64
            except HidError:
                length = 64
        buf = ctypes.create_string_buffer(length)
        buf[0] = bytes([report_id & 0xFF])
        if not hid.HidD_GetFeature(self.handle, buf, length):
            raise HidError(
                f"读 Feature Report 0x{report_id:02X} 失败"
                f"（err={ctypes.get_last_error()}）"
            )
        return buf.raw

    def set_feature_report(self, data: bytes) -> None:
        """写 Feature Report（仅用于厂商协议的**查询**请求，绝不写设置）。"""
        if not self.write_access:
            raise HidError("当前句柄没有写权限，不能发 Feature Report")
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        if not hid.HidD_SetFeature(self.handle, buf, len(data)):
            raise HidError(f"写 Feature Report 失败（err={ctypes.get_last_error()}）")

    def write_output(self, data: bytes) -> None:
        """发一条 Output Report（data[0] 必须是 report id）。"""
        if not self.write_access:
            raise HidError("当前句柄没有写权限，不能发 Output Report")
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        # HidD_SetOutputReport 对带 report id 的集合更稳，失败再退回 WriteFile
        if hid.HidD_SetOutputReport(self.handle, buf, len(data)):
            return
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(
            self.handle, buf, len(data), ctypes.byref(written), None
        ):
            raise HidError(f"WriteFile 失败（err={ctypes.get_last_error()}）")

    def read_input(self, timeout_ms: int = 200) -> Optional[bytes]:
        """读一条 Input Report，超时返回 None。"""
        length = self.input_length
        if not length or not self._ensure_reader():
            return None
        buf = ctypes.create_string_buffer(length)
        overlapped = OVERLAPPED()
        overlapped.hEvent = self._read_event
        read = wintypes.DWORD(0)
        kernel32.ResetEvent(self._read_event)
        ok = kernel32.ReadFile(
            self._read_handle, buf, length, ctypes.byref(read), ctypes.byref(overlapped)
        )
        if ok:
            return buf.raw[: read.value]
        if ctypes.get_last_error() != ERROR_IO_PENDING:
            return None
        wait = kernel32.WaitForSingleObject(self._read_event, max(int(timeout_ms), 1))
        if wait == WAIT_OBJECT_0:
            done = wintypes.DWORD(0)
            if kernel32.GetOverlappedResult(
                self._read_handle, ctypes.byref(overlapped), ctypes.byref(done), False
            ):
                return buf.raw[: done.value]
            return None
        # 超时：取消挂起的读，并**等它真正结束**（否则缓冲区不能复用）
        kernel32.CancelIoEx(self._read_handle, ctypes.byref(overlapped))
        done = wintypes.DWORD(0)
        kernel32.GetOverlappedResult(
            self._read_handle, ctypes.byref(overlapped), ctypes.byref(done), True
        )
        return None

    def drain(self, rounds: int = 64) -> None:
        """丢弃驱动里堆积的旧报文（发请求前调用）。"""
        if not self.input_length:
            return
        for _ in range(max(1, rounds)):
            if self.read_input(timeout_ms=1) is None:
                return

    def _ensure_reader(self) -> bool:
        """懒创建独立的异步读句柄。"""
        if self._read_handle:
            return True
        handle = kernel32.CreateFileW(
            self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            return False
        event = kernel32.CreateEventW(None, True, False, None)
        if not event:
            kernel32.CloseHandle(handle)
            return False
        self._read_handle = handle
        self._read_event = event
        return True

    @property
    def readable(self) -> bool:
        """当前进程能否读这个集合的 Input Report。

        键盘/鼠标这类被系统独占的顶层集合通常打不开读句柄（返回 False），
        但厂商集合一般可以。
        """
        return self._ensure_reader()

    # -- 报告描述符与 usage 取值 --------------------------------------
    def report_descriptor(self, max_length: int = 4096) -> bytes:
        """读报告描述符（部分厂商集合不支持，失败返回空 bytes）。"""
        buf = ctypes.create_string_buffer(max_length)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            self.handle, IOCTL_HID_GET_REPORT_DESCRIPTOR,
            None, 0, buf, max_length, ctypes.byref(returned), None,
        )
        if not ok or not returned.value:
            return b""
        return buf.raw[: returned.value]

    def usage_value(self, report_type: int, report: bytes,
                    caps: HIDP_VALUE_CAPS) -> Optional[int]:
        """按能力表定义，从原始报文里取出某个 usage 的数值。"""
        if not self._preparsed:
            return None
        value = wintypes.ULONG(0)
        usage = caps.u.NotRange.Usage
        status = hid.HidP_GetUsageValue(
            report_type, caps.UsagePage, caps.LinkCollection, usage,
            ctypes.byref(value), self._preparsed,
            ctypes.create_string_buffer(bytes(report), len(report)), len(report),
        )
        if status != HIDP_STATUS_SUCCESS:
            return None
        return int(value.value)


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------
def find_interfaces(vid: Optional[int] = None, pid: Optional[int] = None,
                    usage_page: Optional[int] = None,
                    usage: Optional[int] = None,
                    mi: Optional[int] = None,
                    col: Optional[str] = None,
                    interfaces: Optional[list[HidInterfaceInfo]] = None,
                    ) -> list[HidInterfaceInfo]:
    """按条件筛选接口；interfaces 传进来就复用（避免重复枚举）。"""
    items = interfaces if interfaces is not None else enumerate_interfaces()
    out: list[HidInterfaceInfo] = []
    for item in items:
        if vid is not None and item.vid != vid:
            continue
        if pid is not None and item.pid != pid:
            continue
        if usage_page is not None and item.usage_page != usage_page:
            continue
        if usage is not None and item.usage != usage:
            continue
        if mi is not None and item.mi != mi:
            continue
        if col is not None and (item.col or "").lower() != col.lower():
            continue
        out.append(item)
    return out


def open_device(path: str, write: bool = False,
                read_length: Optional[int] = None) -> HidDevice:
    """打开一个 HID 接口（语法糖，失败抛 HidError）。"""
    return HidDevice(path, write=write, read_length=read_length)


__all__ = [
    "HID_GUID",
    "HidInterfaceInfo",
    "HidDevice",
    "HidError",
    "HIDP_CAPS",
    "HIDP_VALUE_CAPS",
    "HidP_Input",
    "HidP_Output",
    "HidP_Feature",
    "BATTERY_USAGES",
    "SYSTEM_RESERVED_USAGES",
    "enumerate_interfaces",
    "list_paths",
    "probe_interface",
    "find_interfaces",
    "open_device",
    "parse_path_info",
    "physical_group_key",
]
