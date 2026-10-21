# -*- coding: utf-8 -*-
"""types.py —— 全项目共享的数据契约（设备读数）。

所有设备后端（devices/*.py）只负责把各自的私有协议翻译成 Reading；
UI 层（ui/overlay.py、ui/hover.py、ui/tray.py、ui/panel.py）只认 Reading。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# 状态 -> 中文文案
STATUS_TEXT = {
    "discharging": "放电中",
    "charging": "充电中",
    "full": "已充满",
    "unknown": "未知",
    "unplugged": "未连接",
    "error": "读取异常",
    "idle": "未在充电",
}


@dataclass
class Reading:
    """一台设备的一次读数。"""

    device_id: str                     # 稳定 ID：如 "rog:0x1C25" / "logitech:C539#1"
    name: str                          # 显示名："ROG 夜魔X" / "G PRO Wireless"
    level: Optional[int] = None        # 0-100；None=读不到
    status: str = "unknown"            # discharging|charging|full|unknown|unplugged|error
    voltage_mv: Optional[int] = None   # 电压（罗技老鼠标只有电压）
    source: str = ""                   # 数据来源（协议/后端名，调试与卡片展示用）
    updated_at: float = field(default_factory=time.time)
    detail: str = ""                   # 失败原因/提示（"动一下鼠标" 等）
    estimated: bool = False            # 百分比是否为估算（电压换算/粗档位）
    charging: Optional[bool] = None    # True/False/None(协议未提供)
    inferred: bool = False             # 充电状态是否为推断（非协议直读）

    # -- 展示辅助 ------------------------------------------------------
    @property
    def status_text(self) -> str:
        txt = STATUS_TEXT.get(self.status, self.status or "未知")
        return txt + ("（推断）" if self.inferred else "")

    @property
    def level_text(self) -> str:
        if self.level is None:
            return "—"
        return ("≈" if self.estimated else "") + f"{self.level}%"

    @property
    def age_text(self) -> str:
        age = max(0.0, time.time() - self.updated_at)
        if age < 2:
            return "刚刚"
        if age < 60:
            return f"{int(age)} 秒前"
        return f"{int(age // 60)} 分钟前"


@dataclass
class DeviceInfo:
    """发现阶段的设备摘要（panel/probe 用，不含实时读数）。"""

    device_id: str
    name: str
    kind: str                 # rog_logitech_hidpp / standard_hid / bluetooth / atk / darkrim ...
    connection: str = ""      # omni_receiver / lightspeed / wired / bluetooth / unknown
    vid_pid: str = ""
    supported: bool = True    # False=发现但未适配
    note: str = ""
