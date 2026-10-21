# -*- coding: utf-8 -*-
"""config.py —— 配置读写（JSON，位于程序目录 config.json）。

原则：
* 文件不存在 / 解析失败 → 使用默认值，绝不崩；
* 保存用"临时文件 + 替换"，避免写一半断电留下坏文件；
* 所有键都有默认值，未知键保留（向前兼容）。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

APP_NAME = "PeripheralBattery"
APP_DISPLAY_NAME = "外设电量圆环"

# 消息名（panel 改完配置后广播，主程序收到后重载）
MSG_RELOAD = "PeripheralBattery_Reload"
MSG_QUIT = "PeripheralBattery_Quit"

DEFAULTS: Dict[str, Any] = {
    "style": "B",                 # A=双环胶囊 B=无底板双环 C=单环 D=环+名称 E=极简
    "ring_size": 30,              # 逻辑像素（圆环外径），可 24/28/30/32
    "refresh_seconds": 5,         # 电量轮询间隔（秒），1–300
    "position_mode": "taskbar",   # taskbar 悬浮圆环位置（实验功能，默认关闭）
    "taskbar_ring_enabled": False,  # 是否在任务栏显示悬浮圆环（默认只用通知区域图标）
    "offset_x": 0,                # 相对自动位置的偏移（拖动后保存）
    "offset_y": 0,
    "device_hidden": [],          # 不在状态栏显示的 device_id（勾选"在状态栏显示"的取反；空=全部显示）
    "device_order": [],           # device_id 列表，决定显示顺序
    "device_aliases": {},         # device_id -> 自定义名称
    "show_tray_icon": True,       # 是否在通知区域显示托盘图标（"隐藏任务栏图标"开关）
    "theme": "dark",              # dark | light | system
    "low_battery_warn": 20,       # 兼容旧字段（颜色以 color_levels 为准）
    "color_levels": [             # 电量颜色分级（按 min 从高到低匹配）
        {"min": 60, "color": "#22c55e"},
        {"min": 20, "color": "#f59e0b"},
        {"min": 0,  "color": "#ef4444"},
    ],
    "charging_color": "#3b82f6",  # 充电状态圆环颜色（蓝色）
    "animate": True,              # 主窗口圆环动画（充电扫光/平滑过渡）
    "autostart": False,           # 只反映"启动"文件夹真实状态，由托盘/面板写入
    "show_names": False,          # 圆环下方是否显示设备名（默认关）
    "window_width": 560,          # 主窗口初始尺寸（可自由缩放）
    "window_height": 800,
    "window_x": None,
    "window_y": None,
}


ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "config.json"


def config_path() -> str:
    """配置文件放在程序目录（与 main.py 同级）。"""
    return os.path.join(ROOT, CONFIG_NAME)


def legacy_config_path() -> str:
    """旧版本位置（%APPDATA%\\PeripheralBattery\\config.json），仅用于一次性迁移。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "config.json")


def _merge(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for k, v in (incoming or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config root is not an object")
    return _merge(DEFAULTS, data)


def load() -> dict:
    path = config_path()
    try:
        return _read(path)
    except FileNotFoundError:
        pass
    except Exception:
        return dict(DEFAULTS)
    # 首次运行：尝试从旧位置迁移一次
    legacy = legacy_config_path()
    try:
        if os.path.isfile(legacy):
            cfg = _read(legacy)
            save(cfg)
            return cfg
    except Exception:
        pass
    return dict(DEFAULTS)


def save(cfg: dict) -> bool:
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cfg-", suffix=".json", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def path_label() -> str:
    """给面板显示用的路径。"""
    return config_path()
