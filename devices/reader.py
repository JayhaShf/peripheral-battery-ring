# -*- coding: utf-8 -*-
"""devices/reader.py —— 设备读取层的统一入口（docs/CONTRACT.md §4）。

对外接口
--------
::

    read_all(debug=False)  -> list[Reading]   # 读一遍所有已发现设备，永不抛异常
    read_once()            -> list[Reading]   # 同 read_all，供 --once
    demo_readings()        -> list[Reading]   # --demo 用的假数据（含充电中样例）
    discover(force=False)  -> list[DeviceInfo]

设计要点
--------
* **自由识别**：启动时扫描所有 HID，ROG OMNI 接收器按配对列表发现设备、
  罗技接收器读槽位 1–6 的短名，**有线直连**设备按"同一物理实例的 HID++ 短/长
  集合"（罗技，device index 0xFF）或 OMNI 命令尽力探测（ROG）识别，
  剩下的交给标准 HID / 蓝牙兜底；
* **发现缓存 + 热插拔重扫**：发现结果缓存起来，之后每轮只做一次轻量路径枚举
  （约 0.5ms）比对路径集合，变了才重新发现；另外每隔
  :data:`FULL_RESCAN_SECONDS` 秒强制全量重扫一次（防止漏掉只改配对表的变化）；
* **耗时目标 < 3s**：首次发现约 0.3–0.6s（罗技 6 个空槽各 ~15ms），
  之后每轮读数约 20–80ms；
* **永不抛异常**：任何失败都写进 ``Reading.detail``，UI 不会因为设备抽风崩掉。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from devices import generic, hidiface, legacy, logitech, rog
from devices._contract import device_info_class, reading_class

Reading = reading_class()
DeviceInfo = device_info_class()

# 全量重扫间隔（秒）：配对表变化（例如给接收器新配了鼠标）不会改 HID 路径，
# 所以除了路径比对，还要定期全量重扫一次。
FULL_RESCAN_SECONDS = 60.0
# 路径枚举的最小间隔（秒）：连续两次 read_all 之间不重复枚举
PATH_CHECK_MIN_INTERVAL = 1.0
# 发现阶段的总时间预算（毫秒）：超预算就跳过最不重要的兜底扫描，
# 保证首轮 read_all 不突破契约的 3 秒目标（休眠设备只有超时才慢）。
DISCOVERY_BUDGET_MS = 2500.0

# 充电推断（CONTRACT §4.5 A 方案）参数：
# 协议没给 charging 标志时，用"电量持续上升 / 下降"来推断，并打 inferred=True。
INFER_WINDOW_SECONDS = 1800.0   # 只看最近 30 分钟的历史
INFER_MIN_RISE = 1              # 至少涨 1 个百分点才算在充电（避免读数抖动误判）


@dataclass
class _Entry:
    """缓存里的一台设备。"""

    device_id: str
    backend: str                 # rog / logitech / generic
    obj: object                  # RogDevice / LogitechDevice / GenericDevice
    info: DeviceInfo


def _normalize_name(name: str) -> str:
    """设备名归一化：小写 + 只留字母数字（"ROG 夜魔X" → "rog夜魔x"）。"""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _logitech_same_device(short_name: str, wired: "_Entry") -> bool:
    """接收器里的短名与有线直连设备是否指向同一台物理设备。

    接收器存的是截断短名（"G Pro"），有线接口产品名是全名
    （"G Pro Wireless Gaming Mouse"），所以按"短名是全名前缀"判断；
    短名太短（<4 字符）时不做前缀匹配，避免误删。
    """
    obj = wired.obj
    for name in (getattr(obj, "full_name", ""),
                 getattr(obj, "paired_name", "")):
        full = _normalize_name(name)
        if not full:
            continue
        if full == short_name or (len(short_name) >= 4 and full.startswith(short_name)):
            return True
    return False


def _dedupe_wired(entries: list["_Entry"]) -> list["_Entry"]:
    """有线直连设备与接收器槽位设备去重（同一物理设备只保留有线那条）。

    有线模式下接收器里的配对记录仍然存在，会多出一张"未连接"卡片。
    只在有线设备**已探到电量特性**（说明它确实在用）时去重，名称对不上就
    保持原样——宁可多一张，也不误删真设备。
    """
    wired_logitech = [
        entry for entry in entries
        if entry.backend == "logitech"
        and getattr(entry.obj, "is_wired", False)
        and getattr(entry.obj, "battery_feature", None) is not None
    ]
    wired_rog_names = {
        _normalize_name(entry.info.name) for entry in entries
        if entry.backend == "rog" and entry.info.connection == "wired"
    }
    if not wired_logitech and not wired_rog_names:
        return entries
    kept: list[_Entry] = []
    for entry in entries:
        if entry.backend == "logitech" and not getattr(entry.obj, "is_wired", True):
            short = _normalize_name(getattr(entry.obj, "paired_name", ""))
            if short and any(_logitech_same_device(short, wired)
                             for wired in wired_logitech):
                continue                  # 接收器里的配对记录：设备其实插着线
        if entry.backend == "rog" and entry.info.connection != "wired":
            if _normalize_name(entry.info.name) in wired_rog_names:
                continue                  # OMNI 配对记录：同型号键盘其实插着线
        kept.append(entry)
    return kept


class DeviceReader:
    """设备发现 + 读取的缓存与调度。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: list[_Entry] = []
        self._discovered_at: float = 0.0
        self._last_path_check: float = 0.0
        self._paths: frozenset[str] = frozenset()
        self._discovered = False
        self.last_discover_ms: float = 0.0
        self.last_read_ms: float = 0.0
        # 上一次读数缓存：设备休眠时 UI 可以继续显示上次的值（由调用方决定）
        self.last_readings: dict[str, Reading] = {}
        # 电量历史：device_id -> [(时间, 电量), ...]，用于充电推断
        self._history: dict[str, list[tuple[float, int]]] = {}

    # -- 对外 ----------------------------------------------------------
    def read_all(self, debug: bool = False) -> list[Reading]:
        """读一遍所有已发现设备；永不抛异常。"""
        started = time.perf_counter()
        try:
            entries = self._ensure_devices(debug=debug)
        except Exception as exc:                        # 发现阶段兜底
            if debug:
                print(f"[reader] 发现设备失败：{exc}")
            entries = list(self._entries)
        readings: list[Reading] = []
        for entry in entries:
            try:
                reading = self._read_entry(entry, debug=debug)   # type: ignore[arg-type]
            except Exception as exc:                    # 单个设备失败不影响其它设备
                reading = Reading(
                    device_id=entry.device_id, name=entry.info.name, level=None,
                    status="error", source=entry.backend,
                    detail=f"读取异常：{exc}",
                )
            reading = self._apply_inference(reading, debug=debug)
            readings.append(reading)
            if reading.level is not None:
                self.last_readings[reading.device_id] = reading
        self.last_read_ms = (time.perf_counter() - started) * 1000.0
        if debug:
            print(f"[reader] 读取 {len(readings)} 台设备，耗时 "
                  f"{self.last_read_ms:.0f}ms（发现 {self.last_discover_ms:.0f}ms）")
        return readings

    # -- 内部：充电推断（CONTRACT §4.5 A 方案） ------------------------
    def _apply_inference(self, reading: Reading, debug: bool = False) -> Reading:
        """协议没给充电标志时，用"电量持续上升/下降"推断，并标记 inferred。

        只对 ``charging is None`` 的读数生效（ROG / 罗技协议直读的不动）。
        """
        if reading.level is None:
            return reading
        now = time.time()
        history = self._history.setdefault(reading.device_id, [])
        # 只保留窗口内的历史，避免长时间运行后无限增长
        cutoff = now - INFER_WINDOW_SECONDS
        while history and history[0][0] < cutoff:
            history.pop(0)
        previous = history[-1] if history else None
        history.append((now, int(reading.level)))
        if len(history) > 64:
            del history[:-64]

        if reading.charging is not None:
            return reading              # 协议直读，无需推断
        if previous is None:
            return reading
        previous_level = previous[1]
        delta = int(reading.level) - previous_level
        if delta >= INFER_MIN_RISE:
            reading.charging = True
            reading.status = "charging"
            reading.inferred = True
            if debug:
                print(f"[reader] {reading.device_id} 电量 {previous_level}→"
                      f"{reading.level}%，推断为充电中")
        elif delta <= -INFER_MIN_RISE:
            reading.charging = False
            reading.status = "discharging"
            reading.inferred = True
            if debug:
                print(f"[reader] {reading.device_id} 电量 {previous_level}→"
                      f"{reading.level}%，推断为未充电")
        return reading

    def discover(self, force: bool = False, debug: bool = False) -> list[DeviceInfo]:
        """返回发现到的设备摘要（panel 用）。"""
        self._ensure_devices(force=force, debug=debug)
        return [entry.info for entry in self._entries]

    def invalidate(self) -> None:
        """丢弃缓存，下一次 read_all 重新发现（热插拔事件可以主动调用）。"""
        with self._lock:
            self._discovered = False
            self._paths = frozenset()
            self._last_path_check = 0.0

    # -- 内部：发现 ----------------------------------------------------
    def _ensure_devices(self, force: bool = False,
                        debug: bool = False) -> list[_Entry]:
        """必要时重新发现设备，返回缓存条目。"""
        with self._lock:
            now = time.monotonic()
            if not self._discovered or force:
                self._rediscover(debug=debug)
                return list(self._entries)
            # 定期全量重扫（配对表可能变了）
            if now - self._discovered_at >= FULL_RESCAN_SECONDS:
                self._rediscover(debug=debug)
                return list(self._entries)
            # 轻量：比对 HID 路径集合，变了才重扫
            if now - self._last_path_check >= PATH_CHECK_MIN_INTERVAL:
                self._last_path_check = now
                paths = frozenset(hidiface.list_paths())
                if paths != self._paths:
                    if debug:
                        added = paths - self._paths
                        removed = self._paths - paths
                        print(f"[reader] HID 路径变化（+{len(added)} -{len(removed)}），重新发现")
                    self._rediscover(debug=debug)
            return list(self._entries)

    def _rediscover(self, debug: bool = False) -> None:
        """全量重新发现所有设备。"""
        started = time.perf_counter()
        entries: list[_Entry] = []
        try:
            interfaces = hidiface.enumerate_interfaces(detail=True)
        except Exception as exc:
            if debug:
                print(f"[reader] 枚举 HID 失败：{exc}")
            interfaces = []
        self._paths = frozenset(item.path for item in interfaces)
        claimed: set[str] = set()          # 已被厂商后端认领的 HID 路径

        # 1) ROG（OMNI 接收器按配对列表 / 自有接收器按型号表）
        try:
            for device in rog.discover(interfaces, debug=debug):
                claimed.add(device.path)
                entries.append(_Entry(device.device_id, "rog", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] ROG 发现失败：{exc}")

        # 2) 罗技（接收器槽位 1..6 / 有线直连 HID++ 0xFF）
        try:
            for receiver in logitech.find_receivers(interfaces):
                claimed.add(receiver.short_path)
                claimed.add(receiver.long_path)
            for device in logitech.discover(interfaces, debug=debug):
                entries.append(_Entry(device.device_id, "logitech", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] 罗技发现失败：{exc}")

        # 2b) 有线直连：PID 不在接收器表里的罗技设备（同一物理实例短/长集合配对，
        #     device index 0xFF）。与接收器设备按 device_id 去重（见文末 unique）。
        try:
            for link in logitech.find_direct_devices(interfaces):
                claimed.add(link.short_path)
                claimed.add(link.long_path)
            for device in logitech.discover_direct(interfaces, debug=debug):
                entries.append(_Entry(device.device_id, "logitech", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] 罗技有线直连发现失败：{exc}")

        # 2c) 有线直连：未收录型号的 ROG 设备（尽力探测 OMNI 命令，失败静默跳过）
        #     已由 rog.discover 认领的路径/实例不再重复探测。
        try:
            for device in rog.discover_direct(interfaces, debug=debug,
                                              skip_paths=claimed):
                claimed.add(device.path)
                entries.append(_Entry(device.device_id, "rog", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] ROG 有线直连发现失败：{exc}")

        # 3) 兼容旧仓库协议（ATK/VXE 373B、DARKRIM 3654；本机通常没有）
        try:
            for device in legacy.discover(interfaces, debug=debug):
                claimed.add(device.path)
                entries.append(_Entry(device.device_id, "legacy", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] 旧仓库协议发现失败：{exc}")

        # 4) 兜底：标准 HID 电量 + 蓝牙 PnP 属性
        #    时间预算不够时跳过（兜底最不重要，优先保证首轮 read_all 不超时）
        try:
            if (time.perf_counter() - started) * 1000.0 > DISCOVERY_BUDGET_MS:
                if debug:
                    print("[reader] 发现耗时超出预算，跳过兜底扫描")
            else:
                # 跳过厂商后端已认领的路径；ASUS/罗技的**厂商页**集合也不再当标准
                # HID 扫描（它们的电量走私有协议），但同一台设备的标准 usage 集合仍可兜底
                vendor_vids = (rog.ASUS_VID, logitech.LOGITECH_VID)
                remaining = [
                    item for item in interfaces
                    if item.path not in claimed
                    and not (item.vid in vendor_vids and item.usage_page >= 0xFF00)
                ]
                for device in generic.discover(remaining, include_bluetooth=True,
                                               debug=debug):
                    entries.append(_Entry(device.device_id, "generic", device, device.info()))
        except Exception as exc:
            if debug:
                print(f"[reader] 兜底发现失败：{exc}")

        # 同一物理设备同时被"有线直连"和"接收器槽位"发现时，只保留有线那条
        entries = _dedupe_wired(entries)
        # 同一 device_id 只保留一个（防止重复枚举）
        unique: dict[str, _Entry] = {}
        for entry in entries:
            unique.setdefault(entry.device_id, entry)
        self._entries = list(unique.values())
        self._discovered = True
        self._discovered_at = time.monotonic()
        self.last_discover_ms = (time.perf_counter() - started) * 1000.0
        if debug:
            print(f"[reader] 发现 {len(self._entries)} 台设备，耗时 "
                  f"{self.last_discover_ms:.0f}ms")
            for entry in self._entries:
                print(f"         - {entry.device_id}  {entry.info.name}  "
                      f"({entry.backend}/{entry.info.connection})")

    # -- 内部：读取 ----------------------------------------------------
    @staticmethod
    def _read_entry(entry: _Entry, debug: bool = False) -> Reading:
        """读一台设备（各后端的 read() 都不抛异常，这里再兜一层）。"""
        obj = entry.obj
        read = getattr(obj, "read", None)
        if read is None:
            return Reading(device_id=entry.device_id, name=entry.info.name,
                           level=None, status="error", source=entry.backend,
                           detail="该设备没有读取实现")
        return read()


# 进程级单例（main.py / panel 进程各自持有一个，互不影响）
_reader = DeviceReader()


# --------------------------------------------------------------------------
# 模块级接口（契约 §4）
# --------------------------------------------------------------------------
def read_all(debug: bool = False) -> list[Reading]:
    """同步读一遍所有已发现设备；永不抛异常。

    内部做发现缓存 + 热插拔重扫（HID 路径比对 + 每 60 秒全量重扫），
    首次调用含发现耗时（约 0.3–0.6 秒），之后每轮约 20–80ms。
    """
    return _reader.read_all(debug=debug)


def read_once(debug: bool = False) -> list[Reading]:
    """同 :func:`read_all`，供 ``main.py --once`` 使用。"""
    return _reader.read_all(debug=debug)


def discover(force: bool = False, debug: bool = False) -> list[DeviceInfo]:
    """发现阶段摘要（panel 用）。force=True 时强制重新扫描。"""
    return _reader.discover(force=force, debug=debug)


def invalidate() -> None:
    """丢弃发现缓存（热插拔/重新配对后主动调用）。"""
    _reader.invalidate()


def reset_history() -> None:
    """清空电量历史（重新连接后避免拿旧数据做充电推断）。"""
    _reader._history.clear()
    _reader.last_readings.clear()


def last_discover_ms() -> float:
    """上一次全量发现耗时（毫秒），便于排查"启动慢"。"""
    return _reader.last_discover_ms


def last_read_ms() -> float:
    """上一次 read_all 耗时（毫秒）。"""
    return _reader.last_read_ms


def demo_readings() -> list[Reading]:
    """``main.py --demo`` 用的假数据（**固定两台**，对齐 UI 的样式 B 双环）。

    * 键盘 62%，**充电中**（``status="charging"`` / ``charging=True`` /
      ``inferred=False``）——供 UI 联调"亮弧扫动 + 闪电徽标"；
    * 鼠标 ≈77%（只有电压，``estimated=True``），放电中——供 UI 联调 ``≈`` 前缀。

    ``inferred`` 全部为 False（这些是协议直读的模拟值），
    推断态由 UI 自己按 ``inferred`` 字段显示"（推断）"。
    """
    now = time.time()
    return [
        Reading(
            device_id="demo:rog:0x1C25", name="ROG 夜魔X",
            level=62, status="charging", source="demo",
            updated_at=now, charging=True, estimated=False, inferred=False,
            detail="demo 数据 · OMNI 接收器 · 12 01 → [6]=0x3E，充电标志 [9]=1",
        ),
        Reading(
            device_id="demo:logitech:C539#1", name="罗技 G PRO Wireless",
            level=77, status="discharging", voltage_mv=3967, source="demo",
            updated_at=now, charging=False, estimated=True, inferred=False,
            detail="demo 数据 · HID++ 0x1001 电压 3967 mV → 按曲线估算 ≈77%",
        ),
    ]


__all__ = [
    "DeviceReader",
    "read_all",
    "read_once",
    "discover",
    "invalidate",
    "reset_history",
    "demo_readings",
    "last_discover_ms",
    "last_read_ms",
    "FULL_RESCAN_SECONDS",
    "INFER_WINDOW_SECONDS",
]
