# -*- coding: utf-8 -*-
"""ui/panel.py —— 「外设电量圆环」主界面 / 设置面板（独立进程 tkinter 应用）。

用法：
    pythonw ui/panel.py                  # 打开主窗口（设备页）
    pythonw ui/panel.py --tab 设置        # 直接打开设置页
    pythonw ui/panel.py --tab 关于        # 直接打开关于页
    python  ui/panel.py --debug          # 前台运行，异常打印到控制台
    python  ui/panel.py --demo           # 假数据（无设备时看界面）
    python  ui/panel.py --self-check     # 自测：构建界面后打印摘要并退出

设计（对齐 docs/CONTRACT.md §6 与 docs/10-主界面-设备页.png、11-主界面-设置页.png）：
* 独立进程；深色；中文；标签页 设备 / 设置 / 关于；
* 窗口**可自由缩放**（最小 480×560 逻辑 px），位置/大小保存到
  config 的 window_x/y/width/height；内容超高时用 Canvas + 滚动条；
* 设备页：设备卡片（大圆环 + 名称 + VID:PID/连接 + 状态/时间 + 单卡刷新
  + 「在状态栏显示」勾选）、快捷开关（托盘图标 / 刷新间隔）、圆环颜色图例、
  底部「打开设置」「立即刷新」；
* 设置页：任务栏圆环（尺寸 / 停靠位置）、通知区域（托盘图标）、
  通用（刷新间隔 / 开机自启 / 主题）、设备管理（改名）、设备探测入口，
  底部「恢复默认」「保存并应用」；
* 关于页：版本、隐私说明、致谢、配置路径；
* 保存 = config.save() + PostMessage(HWND_BROADCAST,
  RegisterWindowMessageW("PeripheralBattery_Reload"))；
* 关闭窗口 = 退出面板进程（常驻程序继续在后台跑）；
* 窗口 WS_EX_TOOLWINDOW（不产生任务栏按钮），可最小化；
* 字体：正文 微软雅黑 UI 10pt、标题 15pt 加粗、最小 9pt；
  数字 Segoe UI Semibold；Per-Monitor DPI 感知 + Tk scaling；
* 零第三方依赖；不联网、不写日志；设备层坏掉也要能开窗。
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 路径：把仓库根加入 sys.path（脚本通常在 ui\ 下被直接运行）
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

APP_TITLE = "外设电量圆环"
APP_HEADING = "外设电量"
APP_VERSION = "1.0"
MAIN_WINDOW_CLASS = "PeripheralBatteryMainWnd"
STARTUP_LNK_NAME = "外设电量圆环.lnk"

# ---------------------------------------------------------------------------
# 深色配色（对齐 docs/10、docs/11 设计图）
# ---------------------------------------------------------------------------
C = {
    "bg": "#131417",
    "card": "#1c1d22",
    "card2": "#26272d",
    "card3": "#2c2d34",
    "input": "#23242a",
    "border": "#2b2c33",
    "sep": "#26272c",
    "fg": "#eceef3",
    "fg2": "#c7ccd6",
    "muted": "#8b929e",
    "dim": "#6b7280",
    "green": "#5ddc8a",
    "amber": "#f5c451",
    "red": "#ff6b6b",
    "gray": "#969ca6",
    "track": "#343740",
    "accent_dark": "#12351f",
    "on_green": "#0d1a12",
}

FONT_CN = "Microsoft YaHei UI"
FONT_NUM = "Segoe UI Semibold"
FONT_ICON = "Segoe MDL2 Assets"


# ---------------------------------------------------------------------------
# 安全 import：config / models.Reading / devices.reader
# ---------------------------------------------------------------------------
def _load_config_module():
    """导入项目 config.py；失败时返回只读兜底实现（面板仍能打开）。"""
    try:
        import config as _config  # type: ignore

        if hasattr(_config, "load") and hasattr(_config, "save"):
            return _config
    except Exception:
        pass

    class _FallbackConfig:
        APP_NAME = "PeripheralBattery"
        APP_DISPLAY_NAME = APP_TITLE
        MSG_RELOAD = "PeripheralBattery_Reload"
        MSG_QUIT = "PeripheralBattery_Quit"
        DEFAULTS: Dict[str, Any] = {
            "style": "B",
            "ring_size": 30,
            "refresh_seconds": 5,
            "position_mode": "taskbar",
            "offset_x": 0,
            "offset_y": 0,
            "device_hidden": [],
            "device_order": [],
            "device_aliases": {},
            "show_tray_icon": True,
            "theme": "dark",
            "low_battery_warn": 20,
            "autostart": False,
            "show_names": False,
            "window_width": 560,
            "window_height": 700,
            "window_x": None,
            "window_y": None,
        }

        @staticmethod
        def config_path() -> str:
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "PeripheralBattery", "config.json")

        @classmethod
        def load(cls) -> dict:
            return dict(cls.DEFAULTS)

        @staticmethod
        def save(cfg: dict) -> bool:
            return False

        @staticmethod
        def path_label() -> str:
            return "%APPDATA%\\PeripheralBattery\\config.json"

    return _FallbackConfig


def _load_reading_class():
    """拿到 models.Reading；拿不到就用本地等价实现（UI 只依赖属性）。"""
    for mod_name in ("models", "peripheral_contract"):
        try:
            mod = sys.modules.get(mod_name)
            if mod is not None:
                cls = getattr(mod, "Reading", None)
                if cls is not None:
                    return cls
        except Exception:
            pass
    for mod_name, filename in (("models", "models.py"), ("peripheral_types", "types.py")):
        path = os.path.join(ROOT, filename)
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                cls = getattr(mod, "Reading", None)
                if cls is not None:
                    return cls
        except Exception:
            continue

    @dataclass
    class _Reading:
        device_id: str = ""
        name: str = ""
        level: Optional[int] = None
        status: str = "unknown"
        voltage_mv: Optional[int] = None
        source: str = ""
        updated_at: float = field(default_factory=time.time)
        detail: str = ""
        estimated: bool = False
        charging: Optional[bool] = None
        inferred: bool = False

        @property
        def status_text(self) -> str:
            txt = {
                "discharging": "放电中", "charging": "充电中", "full": "已充满",
                "unknown": "未知", "unplugged": "未连接", "error": "读取异常",
                "idle": "未在充电",
            }.get(self.status, self.status or "未知")
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

    return _Reading


def _load_device_reader():
    """导入 devices.reader；失败返回 (None, 异常)。"""
    try:
        from devices import reader as mod  # type: ignore

        return mod, None
    except Exception as exc:  # noqa: BLE001 - 任何异常都不允许拖垮面板
        return None, exc


config = _load_config_module()
Reading = _load_reading_class()
device_reader, READER_ERROR = _load_device_reader()


# ---------------------------------------------------------------------------
# Win32 小工具（句柄类一律声明 restype，避免 Win64 截断 —— CONTRACT §0.4）
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32 if os.name == "nt" else None
if _user32 is not None:
    _user32.FindWindowW.restype = ctypes.c_void_p
    _user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    _user32.RegisterWindowMessageW.restype = ctypes.c_uint
    _user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    _user32.PostMessageW.restype = ctypes.c_int
    _user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_size_t]
    _user32.GetParent.restype = ctypes.c_void_p
    _user32.GetParent.argtypes = [ctypes.c_void_p]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.ShowWindow.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetForegroundWindow.restype = ctypes.c_int
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

HWND_BROADCAST = 0xFFFF
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_HIDE, SW_SHOWNORMAL, SW_RESTORE = 0, 1, 9
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def _main_hwnd() -> Optional[int]:
    """常驻主程序的窗口句柄（None = 未运行）。"""
    if _user32 is None:
        return None
    try:
        h = _user32.FindWindowW(MAIN_WINDOW_CLASS, None)
        return int(h) if h else None
    except Exception:
        return None


def _find_panel_window(title: str) -> Optional[int]:
    """已打开的面板窗口（单实例激活用）。"""
    if _user32 is None:
        return None
    try:
        h = _user32.FindWindowW(None, title)
        return int(h) if h else None
    except Exception:
        return None


def broadcast_reload() -> int:
    """通知常驻进程重载配置；返回消息号（0 = 失败）。"""
    if _user32 is None:
        return 0
    try:
        msg = int(_user32.RegisterWindowMessageW(config.MSG_RELOAD))
        if not msg:
            return 0
        ok = _user32.PostMessageW(ctypes.c_void_p(HWND_BROADCAST), msg, 0, 0)
        return msg if ok else 0
    except Exception:
        return 0


def _powershell_exe() -> str:
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    p = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.isfile(p) else "powershell.exe"


def _run_hidden(cmd: Sequence[str], timeout: float = 60.0) -> Tuple[int, str]:
    """静默运行子进程（不弹黑框），返回 (returncode, 合并输出)。"""
    try:
        proc = subprocess.run(
            list(cmd), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "找不到命令：" + str(cmd[0] if cmd else "")
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _pythonw_exe() -> str:
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        return exe
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.isfile(cand) else (exe or "pythonw.exe")


def _spawn_detached(cmd: Sequence[str]) -> None:
    """后台起一个独立进程（不占控制台、不等待）。"""
    flags = (CREATE_NO_WINDOW | DETACHED_PROCESS) if os.name == "nt" else 0
    subprocess.Popen(list(cmd), cwd=ROOT, close_fds=True, creationflags=flags)


# ---------------------------------------------------------------------------
# 开机自启：只认「启动」文件夹里的快捷方式真实状态
# ---------------------------------------------------------------------------
def startup_lnk_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", STARTUP_LNK_NAME)


def autostart_enabled() -> bool:
    try:
        return os.path.isfile(startup_lnk_path())
    except Exception:
        return False


def set_autostart(enable: bool) -> Tuple[bool, str]:
    """开/关开机自启：调用 scripts 下的安装/卸载脚本（保证命名一致）。"""
    if enable == autostart_enabled():
        return True, ""
    scripts = os.path.join(ROOT, "scripts")
    if enable:
        script = os.path.join(scripts, "install.ps1")
        if not os.path.isfile(script):
            return False, "找不到 scripts\\install.ps1"
        code, out = _run_hidden([
            _powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", script, "-Root", ROOT, "-NoDesktop", "-NoStartMenu", "-Quiet",
        ])
    else:
        script = os.path.join(scripts, "uninstall.ps1")
        if not os.path.isfile(script):
            return False, "找不到 scripts\\uninstall.ps1"
        code, out = _run_hidden([
            _powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", script, "-Root", ROOT, "-StartupOnly", "-Yes", "-NoPause",
            "-Quiet",
        ])
    if code == 0:
        return True, ""
    tail = " / ".join([ln for ln in out.splitlines() if ln.strip()][-2:])
    return False, tail or f"退出码 {code}"


# ---------------------------------------------------------------------------
# DPI / 尺寸 / 字体
# ---------------------------------------------------------------------------
def setup_dpi() -> float:
    """声明 DPI 感知（优先 Per-Monitor V2）并返回缩放系数（1.0 = 96dpi）。"""
    if os.name != "nt":
        return 1.0
    aware = False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        aware = bool(_user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        aware = False
    if not aware:
        try:
            aware = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
        except Exception:
            aware = False
    if not aware:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        dpi = int(_user32.GetDpiForSystem())
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(None)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))
        except Exception:
            dpi = 96
    if not 72 <= dpi <= 480:
        dpi = 96
    return dpi / 96.0


SCALE = setup_dpi()


def S(px: float) -> int:
    """逻辑像素 → 物理像素（布局用的尺寸）。"""
    return max(1, int(round(px * SCALE)))


def F(pt: float, bold: bool = False, family: str = FONT_CN) -> tuple:
    """字体：**pt**（Tk scaling 会按 DPI 换算），正文最小 9pt。

    注意：Tk 的字号必须是 int（或负数字号），这里统一 int()，避免
    ``expected integer but got "10.0"`` 这类 TclError。
    """
    return (family, max(9, int(round(float(pt)))), "bold" if bold else "normal")


def F_NUM(pt: float) -> tuple:
    """数字：Segoe UI Semibold（Tk scaling 自动适配 DPI）。"""
    return (FONT_NUM, max(9, int(round(float(pt)))), "normal")


def color_levels(cfg: Optional[Dict[str, Any]]) -> list:
    """[(min_level, "#hex"), ...] 按 min 从高到低（缺省回退内置配色）。"""
    raw = (cfg or {}).get("color_levels")
    out: list = []
    if isinstance(raw, list):
        for item in raw:
            try:
                hexv = str(item.get("color") or "").strip()
                if hexv.startswith("#") and len(hexv) in (4, 7):
                    out.append((int(item.get("min", 0)), hexv))
            except Exception:
                continue
    if not out:
        out = [(60, C["green"]), (20, C["amber"]), (0, C["red"])]
    out.sort(key=lambda pair: -pair[0])
    return out


def charging_color(cfg: Optional[Dict[str, Any]]) -> str:
    val = str((cfg or {}).get("charging_color") or "").strip()
    return val if val.startswith("#") and len(val) in (4, 7) else "#3b82f6"


def hex_to_rgb(text: str, fallback: Tuple[int, int, int] = (34, 197, 94)
               ) -> Tuple[int, int, int]:
    """``#RRGGBB`` / ``#RGB`` -> (r, g, b)；解析失败返回 fallback。

    本项目零第三方依赖，不 import ui/render.py（那是托盘渲染层，可能未就绪）。
    """
    try:
        s = str(text or "").strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return fallback
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return fallback


def mix_hex(color: str, other: str, t: float,
            fallback: str = "#3b82f6") -> str:
    """把两种颜色按比例混合（t=0 取 color，t=1 取 other）。"""
    try:
        a = hex_to_rgb(color, hex_to_rgb(fallback))
        b = hex_to_rgb(other, (255, 255, 255))
        t = max(0.0, min(1.0, float(t)))
        return "#%02x%02x%02x" % tuple(
            max(0, min(255, int(round(a[i] + (b[i] - a[i]) * t))))
            for i in range(3))
    except Exception:
        return fallback


def level_color(level: Optional[int], low_warn: int = 20,
                cfg: Optional[Dict[str, Any]] = None) -> str:
    if level is None:
        return C["gray"]
    if cfg:
        for minimum, hexv in color_levels(cfg):
            if level >= minimum:
                return hexv
        return C["gray"]
    if level >= 60:
        return C["green"]
    if level >= int(low_warn or 20):
        return C["amber"]
    return C["red"]


def status_color(status: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    return {
        "charging": charging_color(cfg),
        "full": C["green"],
        "discharging": C["green"],
        "unplugged": C["dim"],
        "error": C["red"],
        "unknown": C["gray"],
        "idle": C["fg2"],
    }.get(status, C["gray"])


CONNECTION_TEXT = {
    "omni_receiver": "ROG OMNI 接收器",
    "lightspeed": "LIGHTSPEED 接收器",
    "unifying": "Unifying 接收器",
    "bolt": "Bolt 接收器",
    "receiver": "2.4G 接收器",
    "wired": "有线",
    "bluetooth": "蓝牙",
    "unknown": "未知连接",
    "": "",
}


# ---------------------------------------------------------------------------
# 通用绘制小工具
# ---------------------------------------------------------------------------
def _round_rect(canv: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                r: float, fill: str) -> None:
    """在 Canvas 上画圆角矩形（纯 tkinter）。"""
    r = max(0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0:
        canv.create_rectangle(x1, y1, x2, y2, fill=fill, outline=fill)
        return
    canv.create_oval(x1, y1, x1 + 2 * r, y1 + 2 * r, fill=fill, outline=fill)
    canv.create_oval(x2 - 2 * r, y1, x2, y1 + 2 * r, fill=fill, outline=fill)
    canv.create_oval(x1, y2 - 2 * r, x1 + 2 * r, y2, fill=fill, outline=fill)
    canv.create_oval(x2 - 2 * r, y2 - 2 * r, x2, y2, fill=fill, outline=fill)
    canv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill)
    canv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=fill)


def draw_ring(canv: tk.Canvas, cx: float, cy: float, radius: float,
              thick: float, level: Optional[int], *,
              color: Optional[str] = None, track: Optional[str] = None,
              charging: bool = False, estimated: bool = False,
              text_pt: float = 12.0, text_color: Optional[str] = None,
              low_warn: int = 20,
              cfg: Optional[Dict[str, Any]] = None,
              phase: float = 0.0, sweep: bool = False) -> None:
    """画一个电量圆环（12 点方向起顺时针，弧长 = 电量）。

    ``charging=True`` 时环色用「充电颜色」（默认蓝），可选 ``sweep`` 画扫光亮弧。
    """
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    lw = max(1, int(round(thick)))
    canv.create_arc(*box, start=0, extent=359.999, style="arc",
                    outline=track or C["track"], width=lw)
    charge_hex = charging_color(cfg)
    if level is not None:
        frac = max(0.0, min(1.0, float(level) / 100.0))
        if frac > 0:
            ring_hex = color or (charge_hex if charging else
                                 level_color(level, low_warn, cfg))
            canv.create_arc(*box, start=90, extent=-359.999 * frac, style="arc",
                            outline=ring_hex, width=lw)
    label = "—" if level is None else (("≈" if estimated else "") + str(int(level)))
    canv.create_text(cx, cy, text=label, fill=text_color or C["fg"],
                     font=F_NUM(text_pt))
    if charging:
        bright = mix_hex(charge_hex, "#ffffff", 0.62)
        glow = mix_hex(charge_hex, "#ffffff", 0.30)
        if sweep:
            # 亮弧沿环顺时针扫动：相位 0→1 走完一圈（周期由调用方控制）
            head = (phase % 1.0) * 360.0
            canv.create_arc(*box, start=90 - head - 58, extent=58, style="arc",
                            outline=glow, width=max(1, lw))
            canv.create_arc(*box, start=90 - head - 26, extent=26, style="arc",
                            outline=bright, width=max(1, lw))
        # 闪电徽标（随圆环半径缩放）
        u = max(0.55, radius / 25.0)
        bx, by = cx + radius * 0.70, cy + radius * 0.70
        pts = [(bx + 0.6 * u, by - 4.6 * u), (bx - 2.6 * u, by + 0.9 * u),
               (bx - 0.5 * u, by + 0.9 * u), (bx - 1.7 * u, by + 4.9 * u),
               (bx + 3.0 * u, by - 0.8 * u), (bx + 0.7 * u, by - 0.8 * u)]
        canv.create_polygon(pts, fill=bright, outline="")


# ---------------------------------------------------------------------------
# 小部件：开关（自绘胶囊）
# ---------------------------------------------------------------------------
class Switch(tk.Canvas):
    W, H = 42, 22

    def __init__(self, master: tk.Misc, variable: tk.BooleanVar,
                 command: Optional[Callable[[], None]] = None,
                 bg: str = C["bg"]) -> None:
        super().__init__(master, width=S(self.W), height=S(self.H), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._var = variable
        self._command = command
        self._trace = variable.trace_add("write", lambda *_: self._redraw())
        self.bind("<Button-1>", self._toggle)
        self._redraw()

    def destroy(self) -> None:
        try:
            self._var.trace_remove("write", self._trace)
        except Exception:
            pass
        super().destroy()

    def _redraw(self) -> None:
        self.delete("all")
        on = bool(self._var.get())
        w, h = S(self.W), S(self.H)
        pad = max(1, S(1))
        _round_rect(self, pad, pad, w - pad, h - pad, (h - 2 * pad) / 2.0,
                    C["green"] if on else C["input"])
        r = (h - 2 * pad) / 2.0 - S(2)
        cy = h / 2.0
        cx = (w - pad - S(2) - r) if on else (pad + S(2) + r)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#ffffff", outline="")

    def _toggle(self, _evt: Any = None) -> None:
        self._var.set(not self._var.get())
        if self._command is not None:
            self._command()


# ---------------------------------------------------------------------------
# 小部件：自绘圆角按钮
# ---------------------------------------------------------------------------
class PillButton(tk.Canvas):
    """圆角按钮：ghost / primary；可选刷新图标；宽度随文字自适应。"""

    STYLES = {
        "ghost": (C["card2"], C["fg"], C["card3"]),
        "primary": (C["green"], C["on_green"], "#79e8a3"),
    }

    def __init__(self, master: tk.Misc, text: str,
                 command: Optional[Callable[[], None]] = None,
                 kind: str = "ghost", icon: Optional[str] = None,
                 bg: Optional[str] = None, height: float = 30.0,
                 pad_x: float = 16.0, font_pt: float = 10.0,
                 bold: bool = True) -> None:
        self._bg = bg or C["bg"]
        self._font = F(font_pt, bold)
        self._text = text
        self._icon = icon
        self._kind = kind
        self._pad_x = pad_x
        self._h = height
        self._command = command
        super().__init__(master, bg=self._bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self._rebuild()

    def _measure_text(self) -> int:
        try:
            f = tkfont.Font(root=self, font=self._font)
            w = f.measure(self._text)
        except Exception:
            w = len(self._text) * S(10)
        if self._icon:
            w += S(16)
        return w

    def _rebuild(self) -> None:
        w = self._measure_text() + S(self._pad_x) * 2
        h = S(self._h)
        self.configure(width=w, height=h)
        fill, fg, hover = self.STYLES.get(self._kind, self.STYLES["ghost"])
        self._hover = hover
        self._fill = fill
        self._fg = fg
        self._paint(fill)
        self.bind("<Enter>", lambda _e: self._paint(self._hover))
        self.bind("<Leave>", lambda _e: self._paint(self._fill))
        self.bind("<Button-1>", lambda _e: self._paint(self._hover))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self, color: str) -> None:
        w = max(int(self.winfo_reqwidth()), 1)
        h = max(int(self.winfo_reqheight()), 1)
        self.delete("all")
        _round_rect(self, 0, 0, w, h, S(8), color)
        x = S(self._pad_x)
        if self._icon == "refresh":
            self._draw_refresh_icon(x, h / 2.0, self._fg)
            x += S(16)
        self.create_text(x, h / 2.0, text=self._text, fill=self._fg,
                         font=self._font, anchor="w")

    def _draw_refresh_icon(self, x: float, cy: float, color: str) -> None:
        r = S(5)
        self.create_arc(x, cy - r, x + 2 * r, cy + r, start=35, extent=290,
                        style="arc", outline=color, width=max(1, S(1.3)))
        ax, ay = x + 2 * r, cy - r * 0.55
        self.create_polygon(ax - S(2.2), ay - S(1.2), ax + S(1.4), ay - S(1.6),
                            ax + S(0.2), ay + S(2.2), fill=color, outline="")

    def _on_release(self, evt: Any) -> None:
        self._paint(self._hover)
        try:
            inside = 0 <= evt.x <= self.winfo_width() and \
                0 <= evt.y <= self.winfo_height()
        except Exception:
            inside = True
        if inside and self._command is not None:
            self._command()

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._rebuild()


# ---------------------------------------------------------------------------
# 小部件：标签页按钮
# ---------------------------------------------------------------------------
class TabButton(tk.Frame):
    def __init__(self, master: tk.Misc, text: str,
                 command: Callable[[str], None], bg: str = C["bg"]) -> None:
        super().__init__(master, bg=bg)
        self._text = text
        self._command = command
        self._bg = bg
        self.label = tk.Label(self, text=text, bg=bg, fg=C["muted"],
                              font=F(10, True), cursor="hand2", anchor="w")
        self.label.pack(side="left")
        self.underline = tk.Frame(self, bg=bg, height=S(2))
        self.underline.pack(side="left", fill="x", expand=True)
        for w in (self, self.label, self.underline):
            w.bind("<Button-1>", lambda _e: self._command(self._text))
            w.bind("<Enter>", lambda _e: self._on_enter())
            w.bind("<Leave>", lambda _e: self._on_leave())

    def set_active(self, active: bool) -> None:
        self.label.configure(fg=C["fg"] if active else C["muted"])
        self.underline.configure(bg=C["green"] if active else self._bg)

    def _on_enter(self) -> None:
        if self.underline.cget("bg") == self._bg:
            self.label.configure(fg=C["fg2"])

    def _on_leave(self) -> None:
        if self.underline.cget("bg") == self._bg:
            self.label.configure(fg=C["muted"])


# ---------------------------------------------------------------------------
# 小部件：竖向滚动容器（内容超高时出现滚动条）
# ---------------------------------------------------------------------------
class ScrollArea(tk.Frame):
    def __init__(self, master: tk.Misc, bg: str = C["bg"]) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.canvas.yview,
                                  style="Panel.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_scroll_set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))
        self.body.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self._vbar_visible = False

    def _bind_wheel(self, on: bool) -> None:
        if on:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            try:
                self.canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

    def _on_wheel(self, evt: Any) -> None:
        try:
            if self._vbar_visible:
                self.canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        except Exception:
            pass

    def _on_body_configure(self, _evt: Any = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, evt: Any) -> None:
        self.canvas.itemconfigure(self._win, width=evt.width)

    def _on_scroll_set(self, first: str, last: str) -> None:
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self._vbar_visible:
            self.vbar.pack(side="right", fill="y", padx=(S(2), 0))
            self._vbar_visible = True
        elif not need and self._vbar_visible:
            self.vbar.pack_forget()
            self._vbar_visible = False
        self.vbar.set(first, last)

    def scroll_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 小部件：分组（标题 + 分隔线 + 内容）
# ---------------------------------------------------------------------------
class Section(tk.Frame):
    def __init__(self, master: tk.Misc, title: str) -> None:
        super().__init__(master, bg=C["bg"])
        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x")
        tk.Label(head, text=title, bg=C["bg"], fg=C["fg"], font=F(10, True),
                 anchor="w").pack(side="left")
        tk.Frame(self, bg=C["sep"], height=max(1, S(1))).pack(
            fill="x", pady=(S(8), S(2)))
        self.body = tk.Frame(self, bg=C["bg"])
        self.body.pack(fill="x")

    def row(self, label: str, hint: str = "",
            label_var: Optional[tk.StringVar] = None) -> tk.Frame:
        """新增一行；返回右侧容器（左侧已放标签/提示）。"""
        holder = tk.Frame(self.body, bg=C["bg"])
        holder.pack(fill="x", pady=(S(9), S(9)))
        left = tk.Frame(holder, bg=C["bg"])
        left.pack(side="left", fill="x", expand=True)
        if label_var is not None:
            lbl: tk.Label = tk.Label(left, textvariable=label_var, bg=C["bg"],
                                     fg=C["fg"], font=F(10), anchor="w",
                                     justify="left", wraplength=S(300))
        else:
            lbl = tk.Label(left, text=label, bg=C["bg"], fg=C["fg"], font=F(10),
                           anchor="w", justify="left", wraplength=S(300))
        lbl.pack(anchor="w")
        if hint:
            tk.Label(left, text=hint, bg=C["bg"], fg=C["dim"], font=F(9),
                     anchor="w", justify="left", wraplength=S(320)).pack(
                anchor="w", pady=(S(2), 0))
        right = tk.Frame(holder, bg=C["bg"])
        right.pack(side="right", anchor="e")
        return right


# ---------------------------------------------------------------------------
# 设备卡片（设备页）
# ---------------------------------------------------------------------------
class DeviceCard(tk.Frame):
    """一台设备：大圆环 + 名称/VID:PID/连接 + 状态/时间 + 刷新 + 显示勾选。

    刷新时调用 ``update_reading()`` 原地更新（不重建控件，避免闪烁）；
    圆环支持电量过渡动画与充电扫光（仅动画期间有定时器）。
    """

    RING = 58.0
    ANIM_MS = 350                 # 电量过渡时长（ease-out）
    TICK_MS = 33                  # 30fps
    SWEEP_STEP = 0.018            # 每帧相位增量 → 1.9s 一圈

    def __init__(self, master: tk.Misc, reading: Any, info: Any,
                 on_refresh: Callable[[str], None],
                 on_visible_change: Callable[[str], None], *,
                 name: str = "", visible: bool = True,
                 low_warn: int = 20,
                 cfg: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(master, bg=C["card"], highlightbackground=C["border"],
                         highlightthickness=max(1, S(0.7)))
        self.cfg = dict(cfg or {})
        self.reading = reading
        self.info = info
        self._on_refresh = on_refresh
        self._on_visible_change = on_visible_change
        self._did = str(getattr(reading, "device_id", "") or "")
        self._low_warn = int(low_warn or 20)
        self._job = None
        self._phase = 0.0
        self._anim_level: Optional[float] = None
        self._target_level: Optional[float] = None
        self._anim_from = 0.0
        self._anim_t0 = 0.0
        self._charging = False
        self._tooltip = None
        self._scale = self._scale_for(self.cfg)
        self._ring_px = self._px_for(self._scale)

        self.grid_columnconfigure(0, weight=1)
        top = tk.Frame(self, bg=C["card"])
        top.pack(fill="x", padx=(S(14), S(12)), pady=(S(12), S(6)))

        self.canvas = tk.Canvas(top, width=self._ring_px, height=self._ring_px,
                                bg=C["card"], highlightthickness=0, bd=0)
        self.canvas.pack(side="left", padx=(0, S(14)))
        self.canvas.bind("<Enter>", self._show_tooltip, add="+")
        self.canvas.bind("<Leave>", self._hide_tooltip, add="+")

        mid = tk.Frame(top, bg=C["card"])
        mid.pack(side="left", fill="both", expand=True)
        self.lbl_name = tk.Label(mid, text="", bg=C["card"], fg=C["fg"],
                                 font=F(12, True), anchor="w", justify="left")
        self.lbl_name.pack(anchor="w")
        self.lbl_meta = tk.Label(mid, text="", bg=C["card"], fg=C["muted"],
                                 font=F(9), anchor="w", justify="left")
        self.lbl_meta.pack(anchor="w", pady=(S(3), 0))
        self.lbl_status = tk.Label(mid, text="", bg=C["card"], fg=C["fg2"],
                                   font=F(9), anchor="w", justify="left")
        self.lbl_status.pack(anchor="w", pady=(S(4), 0))
        self.lbl_detail = tk.Label(mid, text="", bg=C["card"], fg=C["dim"],
                                   font=F(9), anchor="w", justify="left",
                                   wraplength=S(260))
        self.lbl_detail.pack(anchor="w", pady=(S(2), 0))
        self.lbl_detail.pack_forget()

        right = tk.Frame(top, bg=C["card"])
        right.pack(side="right", anchor="e")
        self.btn_refresh = PillButton(right, "刷新", kind="ghost", icon="refresh",
                                      bg=C["card"], height=28, pad_x=13,
                                      font_pt=9.5,
                                      command=lambda: self._on_refresh(self._did))
        self.btn_refresh.pack(anchor="e")

        bottom = tk.Frame(self, bg=C["card"])
        bottom.pack(fill="x", padx=(S(14), S(12)), pady=(S(2), S(9)))
        self.var_visible = tk.BooleanVar(value=bool(visible))
        self.chk_visible = tk.Checkbutton(
            bottom, text="在状态栏显示", variable=self.var_visible,
            command=lambda: self._on_visible_change(self._did),
            bg=C["card"], fg=C["fg2"], activebackground=C["card"],
            activeforeground=C["fg"], selectcolor=C["input"],
            highlightthickness=0, bd=0, font=F(9), anchor="w",
            cursor="hand2")
        self.chk_visible.pack(side="left")
        self.chk_visible.configure(selectcolor=C["input"])

        self.update_reading(reading, info, name=name, low_warn=low_warn,
                            animate=False)

    # -- 尺寸 -----------------------------------------------------------
    @classmethod
    def _scale_for(cls, cfg: Optional[Dict[str, Any]]) -> float:
        """ring_size → 缩放系数（0.8~1.25 倍；30 为基准）。"""
        try:
            ring_size = float((cfg or {}).get("ring_size") or 30)
        except Exception:
            ring_size = 30.0
        return max(0.8, min(1.25, ring_size / 30.0))

    @classmethod
    def _px_for(cls, scale: float) -> int:
        return max(int(S(34)), int(round(S(cls.RING * scale))))

    def apply_cfg(self, cfg: Optional[Dict[str, Any]]) -> None:
        """配置变化（圆环尺寸 / 颜色 / 动画开关）→ 就地生效，不重建控件。"""
        self.cfg = dict(cfg or {})
        scale = self._scale_for(self.cfg)
        px = self._px_for(scale)
        if px != self._ring_px or abs(scale - self._scale) > 1e-6:
            self._scale = scale
            self._ring_px = px
            try:
                self.canvas.configure(width=px, height=px)
            except Exception:
                pass
        if not bool(self.cfg.get("animate", True)):
            self._stop_anim()
            self._anim_level = self._target_level
        self._draw()

    # -- 数据更新 -------------------------------------------------------
    def update_reading(self, reading: Any, info: Any, *,
                       name: str = "", low_warn: int = 20,
                       animate: bool = True) -> None:
        self.reading = reading
        self.info = info
        self._did = str(getattr(reading, "device_id", "") or self._did)
        self._low_warn = int(low_warn or 20)
        level = getattr(reading, "level", None)
        charging = bool(getattr(reading, "charging", False)) or \
            str(getattr(reading, "status", "") or "") == "charging"
        self.lbl_name.configure(text=name or self._title_text(reading))
        self.lbl_meta.configure(text=self._meta_text(reading, info))
        self.lbl_status.configure(
            text=self._status_text(reading),
            fg=status_color(str(getattr(reading, "status", "unknown")),
                            self.cfg))
        detail = str(getattr(reading, "detail", "") or "")
        if detail and str(getattr(reading, "status", "")) in ("error", "unplugged"):
            self.lbl_detail.configure(text=detail)
            self.lbl_detail.pack(anchor="w", pady=(S(2), 0))
        else:
            self.lbl_detail.pack_forget()
        self._charging = charging
        new_target = None if level is None else float(level)
        self._target_level = new_target

        animated = animate and bool(self.cfg.get("animate", True))
        # 没有起点（首次 / 之前读不到）→ 直接落到目标值，不做过渡
        if not animated or self._anim_level is None or new_target is None:
            self._anim_level = new_target
            self._draw()
        elif abs(float(self._anim_level) - new_target) > 0.4:
            # 有变化：从当前显示值平滑过渡到新值
            self._anim_from = float(self._anim_level)
            self._anim_t0 = time.time()
        else:
            self._anim_level = new_target
            self._draw()

        # 只有「充电扫光」或「数值过渡」进行中才启动定时器；静止时 0 个。
        if charging or self._animating():
            self._start_anim()
        else:
            self._stop_anim()

    def _animating(self) -> bool:
        if self._anim_level is None or self._target_level is None:
            return False
        return abs(float(self._anim_level) - float(self._target_level)) > 0.4

    # -- 绘制 -----------------------------------------------------------
    def _draw(self) -> None:
        try:
            self.canvas.delete("all")
            cx = cy = self._ring_px / 2.0
            radius = self._ring_px / 2.0 - S(4)
            level = self._anim_level
            shown = None if level is None else int(round(level))
            draw_ring(self.canvas, cx, cy, radius, S(5), shown,
                      charging=self._charging,
                      estimated=bool(getattr(self.reading, "estimated", False)),
                      text_pt=13, low_warn=self._low_warn,
                      cfg=self.cfg, phase=self._phase, sweep=True)
        except Exception:
            pass

    # -- 动画 -----------------------------------------------------------
    def _start_anim(self) -> None:
        if self._job is not None:
            return
        self._job = self.after(33, self._tick)

    def _stop_anim(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self) -> None:
        self._job = None
        moving = False
        if self._anim_level is not None and self._target_level is not None:
            target = float(self._target_level)
            if abs(float(self._anim_level) - target) > 0.4:
                # ease-out：进度 p 用 1-(1-p)^3，350ms 内走完
                elapsed = max(0.0, time.time() - self._anim_t0)
                p = min(1.0, elapsed / (self.ANIM_MS / 1000.0))
                eased = 1.0 - (1.0 - p) ** 3
                self._anim_level = self._anim_from + (target - self._anim_from) * eased
                moving = p < 1.0
            if abs(float(self._anim_level) - target) <= 0.4:
                self._anim_level = target
        if self._charging:
            # 扫光相位按帧推进（33ms × 57.6 帧 ≈ 1.9s 一圈）
            self._phase = (self._phase + self.SWEEP_STEP) % 1.0
            moving = True
        self._draw()
        if moving:
            self._job = self.after(self.TICK_MS, self._tick)
        else:
            self._phase = 0.0

    def destroy(self) -> None:
        self._hide_tooltip()
        self._stop_anim()
        super().destroy()

    # -- 悬停提示 -------------------------------------------------------
    def _show_tooltip(self, _evt: Any = None) -> None:
        try:
            if self._tooltip is not None:
                return
            top = tk.Toplevel(self)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            level = getattr(self.reading, "level", None)
            lvl = "—" if level is None else (
                ("≈" if getattr(self.reading, "estimated", False) else "")
                + f"{level}%")
            text = f"{self._title_text(self.reading)}\n{lvl} · " \
                   f"{getattr(self.reading, 'status_text', '未知')}"
            tk.Label(top, text=text, bg="#111318", fg="#f2f5fa",
                     font=F(9), justify="left", padx=S(10), pady=S(6),
                     highlightbackground=C["border"],
                     highlightthickness=1).pack()
            x = self.winfo_pointerx() + S(14)
            y = self.winfo_pointery() + S(14)
            top.geometry(f"+{x}+{y}")
            self._tooltip = top
        except Exception:
            self._tooltip = None

    def _hide_tooltip(self, _evt: Any = None) -> None:
        top, self._tooltip = self._tooltip, None
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass

    # -- 文本 -----------------------------------------------------------
    @staticmethod
    def _title_text(reading: Any) -> str:
        return str(getattr(reading, "name", "") or
                   getattr(reading, "device_id", "") or "未知设备")

    @staticmethod
    def _meta_text(reading: Any, info: Any) -> str:
        bits: List[str] = []
        vid_pid = ""
        connection = ""
        if info is not None:
            vid_pid = str(getattr(info, "vid_pid", "") or "")
            connection = str(getattr(info, "connection", "") or "")
        if not vid_pid:
            did = str(getattr(reading, "device_id", "") or "")
            tokens = [t for t in did.replace("#", ":").split(":") if t]
            hexes = [t.upper() for t in tokens
                     if len(t) == 4 and all(ch in "0123456789abcdefABCDEF" for ch in t)]
            if len(hexes) >= 2:
                vid_pid = f"{hexes[0]}:{hexes[1]}"
            elif len(hexes) == 1:
                vid_pid = hexes[0]
        conn_text = CONNECTION_TEXT.get(connection, connection)
        if vid_pid:
            bits.append(vid_pid)
        if conn_text:
            bits.append(conn_text)
        if not bits:
            src = str(getattr(reading, "source", "") or "")
            if src:
                bits.append(src)
        return " · ".join(bits) if bits else "未知设备"

    @staticmethod
    def _status_text(reading: Any) -> str:
        status = str(getattr(reading, "status_text", "") or "未知")
        age = str(getattr(reading, "age_text", "") or "")
        return f"{status} · {age}" if age else status

    def mark_busy(self, busy: bool) -> None:
        try:
            self.btn_refresh.set_text("读取中…" if busy else "刷新")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 编辑别名对话框
# ---------------------------------------------------------------------------
class AliasDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, title: str, initial: str) -> None:
        super().__init__(master)
        self.result: Optional[str] = None
        self.title(title)
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(master)
        self.attributes("-topmost", True)
        w, h = S(380), S(160)
        try:
            mx = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
            my = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 3)
        except Exception:
            mx = my = 200
        self.geometry(f"{w}x{h}+{mx}+{my}")

        tk.Label(self, text=title, bg=C["bg"], fg=C["fg"], font=F(11, True),
                 anchor="w").pack(fill="x", padx=S(16), pady=(S(14), S(4)))
        tk.Label(self, text="留空表示使用设备默认名称（例如 ROG 夜魔X）",
                 bg=C["bg"], fg=C["dim"], font=F(9), anchor="w").pack(
            fill="x", padx=S(16))
        self.entry = tk.Entry(self, bg=C["input"], fg=C["fg"],
                              insertbackground=C["fg"], relief="flat",
                              font=F(10), highlightthickness=1,
                              highlightbackground=C["border"],
                              highlightcolor=C["green"])
        self.entry.pack(fill="x", padx=S(16), pady=(S(8), S(12)), ipady=S(4))
        self.entry.insert(0, initial or "")
        self.entry.select_range(0, "end")
        self.entry.focus_set()

        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=S(16))
        PillButton(bar, "确定", kind="primary", bg=C["bg"], height=28, pad_x=18,
                   font_pt=9.5, command=self._ok).pack(side="right")
        PillButton(bar, "取消", kind="ghost", bg=C["bg"], height=28, pad_x=16,
                   font_pt=9.5, command=self._cancel).pack(side="right",
                                                           padx=(0, S(8)))

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        try:
            self.grab_set()
        except Exception:
            pass
        self.wait_window(self)

    def _ok(self) -> None:
        self.result = self.entry.get().strip()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class PanelApp:
    """主界面：设备页 + 设置页 + 关于页（独立进程，可自由缩放）。"""

    TABS = ("设备", "设置", "关于")
    WIN_W, WIN_H = 560, 800          # 默认尺寸（逻辑 px）
    MIN_W, MIN_H = 480, 560          # 最小尺寸（逻辑 px）

    def __init__(self, demo: bool = False, debug: bool = False,
                 tab: str = "设备") -> None:
        self.demo = demo
        self.debug = debug
        self.cfg: Dict[str, Any] = dict(config.load())
        self._loading = True
        self._reader_busy = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._cards: Dict[str, DeviceCard] = {}
        self._readings: List[Any] = []
        self._infos: Dict[str, Any] = {}
        self._read_error: Optional[str] = None
        self._refresh_job: Optional[str] = None
        self._status_job: Optional[str] = None
        self._status_clear_job: Optional[str] = None
        self._geom_job: Optional[str] = None
        self._alias_vars: Dict[str, tk.StringVar] = {}
        self._current_tab = ""
        self._build_lock = False
        self._hwnd: Optional[int] = None

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_style()
        self._build_ui()             # 先建界面，才能按内容算初始高度
        # _loading 必须覆盖 _load_into_ui()：否则每个 var.set() 都会触发
        # _on_discrete_change → config.save + broadcast_reload（启动时连写好几次、
        # 常驻程序被反复叫醒重绘 → 看起来就是"开窗闪一下"）。
        try:
            self._load_into_ui()
        finally:
            self._loading = False
        self._apply_geometry()
        self.select_tab(tab)

        self.root.after(40, self._refresh_devices)
        self._schedule_status_poll()
        self.root.bind("<Control-s>", lambda _e: self._save_explicit())
        self.root.bind("<Escape>", lambda _e: self._on_close())
        self.root.bind("<Configure>", self._on_configure)
        self.root.after(120, self._apply_dark_titlebar)
        self.root.after(500, self._apply_dark_titlebar)

    # ==================================================================
    # 样式 / 字体 / DPI
    # ==================================================================
    def _setup_style(self) -> None:
        # Tk scaling：1 pt = (dpi/72) px；与 Per-Monitor DPI 感知保持一致
        try:
            self.root.tk.call("tk", "scaling", max(1.0, SCALE * 96.0 / 72.0))
        except Exception:
            pass
        # 全局默认字体（tk 普通控件走 option 数据库；ttk 走 style）
        try:
            for opt, val in (
                ("*Font", F(10)),
                ("*TextArea.Font", F(10)),
                ("*Listbox.Font", F(10)),
                ("*Menu.Font", F(10)),
                ("*TCombobox*Listbox.font", F(10)),
            ):
                self.root.option_add(opt, val)
        except Exception:
            pass

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=F(10), background=C["bg"], foreground=C["fg"])
        style.configure("TFrame", background=C["bg"])
        style.configure("TLabel", background=C["bg"], foreground=C["fg"], font=F(10))
        style.configure("TCheckbutton", background=C["bg"], foreground=C["fg"],
                        font=F(10))
        style.configure(
            "TCombobox", fieldbackground=C["input"], background=C["input"],
            foreground=C["fg"], arrowcolor=C["muted"], bordercolor=C["border"],
            lightcolor=C["input"], darkcolor=C["input"],
            selectbackground=C["accent_dark"], selectforeground=C["fg"],
            padding=(S(8), S(4)), font=F(10),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", C["input"])],
            foreground=[("readonly", C["fg"])],
            background=[("readonly", C["input"])],
            selectbackground=[("readonly", C["accent_dark"])],
            selectforeground=[("readonly", C["fg"])],
        )
        style.layout("Panel.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure("Panel.Vertical.TScrollbar", background=C["card2"],
                        troughcolor=C["bg"], bordercolor=C["bg"],
                        arrowcolor=C["bg"], relief="flat", borderwidth=0,
                        width=S(7))
        style.map("Panel.Vertical.TScrollbar",
                  background=[("active", C["muted"]), ("pressed", C["muted"])])
        self.root.option_add("*TCombobox*Listbox.background", C["input"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent_dark"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", C["green"])

    # ==================================================================
    # 几何：可自由缩放 + 记忆位置/大小
    # ==================================================================
    def _apply_geometry(self) -> None:
        """按配置恢复窗口位置/大小；没有记录就居中。"""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        try:
            w = int(self.cfg.get("window_width") or self.WIN_W)
            h = int(self.cfg.get("window_height") or self.WIN_H)
        except Exception:
            w, h = self.WIN_W, self.WIN_H
        w = max(int(self.MIN_W), min(int(w), int(sw * 0.95)))
        h = max(int(self.MIN_H), min(int(h), int(sh * 0.95)))
        x = self.cfg.get("window_x")
        y = self.cfg.get("window_y")
        try:
            x = int(x) if x is not None else None
            y = int(y) if y is not None else None
        except Exception:
            x = y = None
        if x is None or x < 0 or x > sw - S(120):
            x = max(0, (sw - S(w)) // 2)
        if y is None or y < 0 or y > sh - S(120):
            y = max(0, (sh - S(h)) // 2 - S(16))
        self.root.geometry(f"{S(w)}x{S(h)}+{x}+{y}")
        self.root.minsize(S(self.MIN_W), S(self.MIN_H))
        self.root.resizable(True, True)
        self._apply_window_style()
        try:
            self.root.iconphoto(True, self._make_icon())
        except Exception:
            pass

    def _apply_dark_titlebar(self) -> None:
        """窗口映射后把标题栏切成深色（Win11 DWMWA_USE_IMMERSIVE_DARK_MODE）。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            child = int(self.root.winfo_id())
            hwnd = int(user32.GetAncestor(ctypes.c_void_p(child), 2) or 0)  # GA_ROOT=2
            if not hwnd:
                hwnd = child
            for attr in (20, 19):
                try:
                    val = ctypes.c_int(1)
                    rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd), attr, ctypes.byref(val),
                        ctypes.sizeof(val))
                    if rc == 0:
                        break
                except Exception:
                    pass
            # 标题栏主题（仅 DWM 属性在部分 Tk 窗口上不生效）
            try:
                ctypes.windll.uxtheme.SetWindowTheme(ctypes.c_void_p(hwnd),
                                                ctypes.c_wchar_p('DarkMode_Explorer'), None)
            except Exception:
                pass
            # 触发非客户区重绘
            try:
                user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                                    0x0020 | 0x0002 | 0x0001 | 0x0004)
            except Exception:
                pass
        except Exception:
            pass

    def _apply_window_style(self) -> None:
        """WS_EX_TOOLWINDOW（不产生任务栏按钮）+ 深色标题栏。"""
        if _user32 is None:
            return
        try:
            self.root.update_idletasks()
            child = int(self.root.winfo_id())
            hwnd = int(_user32.GetParent(ctypes.c_void_p(child)) or child)
            self._hwnd = hwnd
            ex = int(_user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE))
            ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            _user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)
            try:
                for attr in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE
                    val = ctypes.c_int(1)
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            ctypes.c_void_p(hwnd), attr, ctypes.byref(val),
                            ctypes.sizeof(val)) == 0:
                        break
            except Exception:
                pass
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNORMAL)
        except Exception:
            if self.debug:
                import traceback
                traceback.print_exc()

    def _make_icon(self) -> tk.PhotoImage:
        """32×32 圆环图标（纯 tkinter 点阵）。"""
        size = 32
        img = tk.PhotoImage(width=size, height=size)
        img.put(C["bg"], to=(0, 0, size, size))
        cx = cy = (size - 1) / 2.0
        for yy in range(size):
            for xx in range(size):
                d = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
                if 9.0 <= d <= 13.0:
                    img.put(C["green"], (xx, yy))
                elif d < 9.0:
                    img.put("#0d1a12", (xx, yy))
        return img

    def _on_configure(self, evt: Any) -> None:
        """窗口尺寸/位置变化后防抖保存（只记根窗口自己的事件）。"""
        if evt.widget is not self.root:
            return
        if self._loading:
            return
        if self._geom_job:
            try:
                self.root.after_cancel(self._geom_job)
            except Exception:
                pass
        self._geom_job = self.root.after(600, self._remember_geometry)

    def _remember_geometry(self) -> None:
        self._geom_job = None
        try:
            if self.root.state() != "normal":
                return
            w = int(self.root.winfo_width() / SCALE)
            h = int(self.root.winfo_height() / SCALE)
            x = int(self.root.winfo_x())
            y = int(self.root.winfo_y())
        except Exception:
            return
        cfg = dict(self.cfg)
        cfg["window_width"] = max(int(self.MIN_W), w)
        cfg["window_height"] = max(int(self.MIN_H), h)
        cfg["window_x"] = x
        cfg["window_y"] = y
        self.cfg = cfg
        config.save(cfg)               # 只存几何，不广播（常驻程序不关心）

    # ==================================================================
    # 界面骨架
    # ==================================================================
    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        # ---- 顶栏 ----
        top = tk.Frame(self.root, bg=C["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=(S(18), S(14)),
                 pady=(S(14), S(2)))
        left = tk.Frame(top, bg=C["bg"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=APP_HEADING, bg=C["bg"], fg=C["fg"],
                 font=F(15, True), anchor="w").pack(anchor="w")
        self.lbl_subtitle = tk.Label(left, text="正在读取设备…", bg=C["bg"],
                                     fg=C["muted"], font=F(10), anchor="w")
        self.lbl_subtitle.pack(anchor="w", pady=(S(3), 0))

        self.btn_gear = self._icon_button(top, "gear",
                                          lambda: self.select_tab("设置"), "设置")
        self.btn_gear.pack(side="right", padx=(S(8), 0))
        self.btn_close = self._icon_button(top, "close", self._on_close, "关闭")
        self.btn_close.pack(side="right")

        # ---- 标签栏 ----
        tabs = tk.Frame(self.root, bg=C["bg"])
        tabs.grid(row=1, column=0, sticky="ew", padx=(S(18), S(18)),
                  pady=(S(6), 0))
        self.tab_buttons: Dict[str, TabButton] = {}
        for idx, name in enumerate(self.TABS):
            btn = TabButton(tabs, name, self.select_tab)
            btn.pack(side="left",
                     padx=(0, S(26) if idx < len(self.TABS) - 1 else 0))
            self.tab_buttons[name] = btn
        tk.Frame(self.root, bg=C["sep"], height=max(1, S(1))).grid(
            row=1, column=0, sticky="ews", pady=(S(36), 0))

        # ---- 页面容器（每个 tab：可滚动内容 + 固定底栏）----
        self.pages: Dict[str, tk.Frame] = {}
        self.page_scroll: Dict[str, ScrollArea] = {}
        for name in self.TABS:
            page = tk.Frame(self.root, bg=C["bg"])
            page.grid(row=2, column=0, sticky="nsew")
            page.grid_rowconfigure(0, weight=1)
            page.grid_columnconfigure(0, weight=1)
            scroll = ScrollArea(page, bg=C["bg"])
            scroll.grid(row=0, column=0, sticky="nsew")
            footer = tk.Frame(page, bg=C["bg"])
            footer.grid(row=1, column=0, sticky="ew")
            self.pages[name] = page
            self.page_scroll[name] = scroll
            self._build_page(name, scroll.body, footer)

        # ---- 状态栏 ----
        self.lbl_status = tk.Label(
            self.root, text=f"配置：{config.path_label()}", bg=C["bg"],
            fg=C["dim"], font=F(9), anchor="w", justify="left")
        self.lbl_status.grid(row=3, column=0, sticky="ew",
                             padx=(S(18), S(18)), pady=(S(2), S(8)))

    def _icon_button(self, master: tk.Misc, kind: str,
                     command: Callable[[], None], _tooltip: str) -> tk.Canvas:
        """右上角方形图标按钮（齿轮 / 关闭）。"""
        size = S(32)
        canv = tk.Canvas(master, width=size, height=size, bg=C["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        state = {"hover": False}

        def paint() -> None:
            canv.delete("all")
            _round_rect(canv, 0, 0, size, size, S(8),
                        C["card2"] if state["hover"] else C["card"])
            if kind == "gear":
                if self._has_icon_font():
                    canv.create_text(size / 2, size / 2, text="\ue713",
                                     fill=C["fg2"], font=(FONT_ICON, -S(15)))
                else:
                    cx = cy = size / 2.0
                    r1, r2 = S(7), S(4.4)
                    for k in range(8):
                        ang = k * 3.14159265 / 4.0
                        canv.create_line(cx + r2 * __import__("math").cos(ang),
                                         cy + r2 * __import__("math").sin(ang),
                                         cx + r1 * __import__("math").cos(ang),
                                         cy + r1 * __import__("math").sin(ang),
                                         fill=C["fg2"], width=S(2))
                    canv.create_oval(cx - S(4.4), cy - S(4.4), cx + S(4.4),
                                     cy + S(4.4), outline=C["fg2"], width=S(2))
            else:
                m = S(10)
                canv.create_line(m, m, size - m, size - m, fill=C["fg2"],
                                 width=max(1, S(1.4)))
                canv.create_line(size - m, m, m, size - m, fill=C["fg2"],
                                 width=max(1, S(1.4)))

        canv.bind("<Enter>",
                  lambda _e: (state.update(hover=True), paint()))
        canv.bind("<Leave>",
                  lambda _e: (state.update(hover=False), paint()))
        canv.bind("<Button-1>", lambda _e: command())
        paint()
        return canv

    _ICON_FONT_OK: Optional[bool] = None

    def _has_icon_font(self) -> bool:
        if self._ICON_FONT_OK is None:
            try:
                self._ICON_FONT_OK = FONT_ICON in set(tkfont.families(self.root))
            except Exception:
                self._ICON_FONT_OK = False
        return bool(self._ICON_FONT_OK)

    # ==================================================================
    # 页面分发
    # ==================================================================
    def _build_page(self, name: str, body: tk.Frame, footer: tk.Frame) -> None:
        body.configure(bg=C["bg"])
        if name == "设备":
            self._build_device_page(body, footer)
        elif name == "设置":
            self._build_settings_page(body, footer)
        else:
            self._build_about_page(body, footer)

    def select_tab(self, name: str) -> None:
        name = name if name in self.TABS else "设备"
        if name == self._current_tab:
            return
        self._current_tab = name
        for tab_name, page in self.pages.items():
            if tab_name == name:
                page.grid()
            else:
                page.grid_remove()
            self.tab_buttons[tab_name].set_active(tab_name == name)
        try:
            self.page_scroll[name].scroll_top()
        except Exception:
            pass

    # ==================================================================
    # 设备页
    # ==================================================================
    def _build_device_page(self, body: tk.Frame, footer: tk.Frame) -> None:
        body.grid_columnconfigure(0, weight=1)
        self.dev_holder = tk.Frame(body, bg=C["bg"])
        self.dev_holder.pack(fill="x", padx=(S(18), S(18)), pady=(S(14), 0))
        self.dev_empty = tk.Label(
            self.dev_holder, text="正在读取设备…", bg=C["card"], fg=C["muted"],
            font=F(9.5), anchor="w", justify="left", padx=S(14), pady=S(14),
            wraplength=S(460))
        self.dev_empty.pack(fill="x")

        # ---- 快捷开关（只保留托盘图标 + 刷新间隔）----
        quick = tk.Frame(body, bg=C["bg"])
        quick.pack(fill="x", padx=(S(18), S(18)), pady=(S(16), 0))
        self.sec_quick = Section(quick, "快捷开关")
        self.sec_quick.pack(fill="x")
        self.var_tray = tk.BooleanVar(value=True)
        self.var_refresh = tk.StringVar()
        self.refresh_choices = self._refresh_options()
        self._switch_row(self.sec_quick, "在通知区域显示设备图标（每台一个）", "", self.var_tray)
        row = self.sec_quick.row("电量刷新间隔")
        self.combo_refresh_dev = ttk.Combobox(
            row, textvariable=self.var_refresh, values=list(self.refresh_choices),
            state="readonly", width=8, font=F(10))
        self.combo_refresh_dev.pack(side="right")

        # ---- 圆环图例 ----
        legend_wrap = tk.Frame(body, bg=C["bg"])
        legend_wrap.pack(fill="x", padx=(S(18), S(18)), pady=(S(16), 0))
        self.sec_legend = Section(legend_wrap, "圆环含义")
        self.sec_legend.pack(fill="x")
        legend = tk.Frame(self.sec_legend.body, bg=C["bg"])
        legend.pack(fill="x", pady=(S(10), S(10)))
        self._legend_cells: List[dict] = []
        for _k in range(4):
            cell = tk.Frame(legend, bg=C["bg"])
            cell.pack(side="left", fill="x", expand=True)
            cv = tk.Canvas(cell, width=int(S(34)), height=int(S(34)), bg=C["bg"],
                           highlightthickness=0, bd=0)
            cv.pack(side="left")
            txt = tk.Frame(cell, bg=C["bg"])
            txt.pack(side="left", padx=(S(6), 0))
            lbl_title = tk.Label(txt, text="", bg=C["bg"], fg=C["fg"],
                                 font=F(9.5, True), anchor="w")
            lbl_title.pack(anchor="w")
            lbl_sub = tk.Label(txt, text="", bg=C["bg"], fg=C["muted"],
                               font=F(9), anchor="w")
            lbl_sub.pack(anchor="w")
            self._legend_cells.append({"canvas": cv, "title": lbl_title,
                                       "sub": lbl_sub})
        self._draw_legend()

        # ---- 底栏 ----
        bar = tk.Frame(footer, bg=C["bg"])
        bar.pack(fill="x", padx=(S(18), S(18)), pady=(S(12), S(6)))
        PillButton(bar, "打开设置", kind="ghost", bg=C["bg"], height=38,
                   pad_x=26, font_pt=10.5,
                   command=lambda: self.select_tab("设置")).pack(side="left")
        PillButton(bar, "立即刷新", kind="ghost", bg=C["bg"], height=38,
                   pad_x=26, font_pt=10.5,
                   command=self._refresh_devices).pack(side="left",
                                                       padx=(S(10), 0))
        hint = tk.Frame(bar, bg=C["bg"])
        hint.pack(side="right", anchor="e")
        tk.Label(hint, text="关闭窗口后台继续运行", bg=C["bg"], fg=C["muted"],
                 font=F(9), anchor="e").pack(anchor="e")
        tk.Label(hint, text="点击通知区域图标 / 右键菜单可再次打开", bg=C["bg"],
                 fg=C["dim"], font=F(9), anchor="e").pack(anchor="e",
                                                           pady=(S(2), 0))

        for var in (self.var_tray, self.var_refresh):
            var.trace_add("write", lambda *_: self._on_discrete_change())

    # ==================================================================
    # 设置页
    # ==================================================================
    def _build_settings_page(self, body: tk.Frame, footer: tk.Frame) -> None:
        pad = (S(18), S(18))
        self.var_ring = tk.StringVar()
        self.var_taskbar_ring = tk.BooleanVar(value=False)
        self.var_min_high = tk.IntVar(value=60)
        self.var_min_mid = tk.IntVar(value=20)
        self.var_color_high = tk.StringVar(value="#22c55e")
        self.var_color_mid = tk.StringVar(value="#f59e0b")
        self.var_color_low = tk.StringVar(value="#ef4444")
        self.var_color_charging = tk.StringVar(value="#3b82f6")
        self.var_autostart = tk.BooleanVar(value=autostart_enabled())
        self.var_theme = tk.StringVar()
        self.ring_choices = [
            ("小 · 24 px", 24), ("中 · 28 px", 28), ("中 · 30 px", 30),
            ("大 · 32 px", 32), ("特大 · 36 px", 36),
        ]
        self.theme_choices = [("深色", "dark"), ("浅色", "light"),
                              ("跟随系统", "system")]

        # ---- 任务栏圆环 ----
        sec1 = Section(body, "任务栏圆环")
        sec1.pack(fill="x", padx=pad)
        size_row = sec1.row("圆环尺寸")
        self.size_preview = tk.Canvas(size_row, width=S(44), height=S(44),
                                      bg=C["bg"], highlightthickness=0, bd=0)
        self.size_preview.pack(side="right", padx=(S(12), S(4)))
        combo_ring = ttk.Combobox(
            size_row, textvariable=self.var_ring,
            values=[t for t, _ in self.ring_choices], state="readonly",
            width=12, font=F(10))
        combo_ring.pack(side="right")
        self.combo_ring = combo_ring
        self.var_ring.trace_add("write", lambda *_: self._draw_size_preview())
        tk.Label(sec1.body,
                 text="通知区域图标尺寸由 Windows 决定；此设置影响主界面圆环"
                      "与任务栏悬浮圆环。",
                 bg=C["bg"], fg=C["dim"], font=F(9), anchor="w",
                 justify="left", wraplength=S(460)).pack(
            fill="x", padx=(S(2), S(2)), pady=(0, S(6)))
        self._static_row(sec1, "显示位置", "通知区域 · 每台勾选设备一个图标")
        self._switch_row(sec1, "在任务栏显示悬浮圆环（实验）",
                         "默认关闭；关闭时只在通知区域显示每台设备的圆环图标。",
                         self.var_taskbar_ring)

        # ---- 圆环颜色 ----
        sec_color = Section(body, "圆环颜色")
        sec_color.pack(fill="x", padx=pad, pady=(S(16), 0))
        self._color_row(sec_color, "高电量 ≥", self.var_min_high,
                        self.var_color_high)
        self._color_row(sec_color, "中电量 ≥", self.var_min_mid,
                        self.var_color_mid)
        # 第三档阈值 = 中电量的 min（< 它就算低电量）
        self.var_low_label = tk.StringVar(value="低电量 < 20%")
        self._sync_low_label()
        self.var_min_mid.trace_add("write", lambda *_: self._sync_low_label())
        self._color_row(sec_color, "", None, self.var_color_low,
                        label_var=self.var_low_label)
        self._color_row(sec_color, "充电中", None, self.var_color_charging)
        tk.Label(sec_color.body,
                 text="点颜色块用系统取色器选色；保存后主界面图例与设备圆环"
                      "立即生效，充电时圆环用「充电中」颜色。",
                 bg=C["bg"], fg=C["dim"], font=F(9), anchor="w",
                 justify="left", wraplength=S(460)).pack(
            fill="x", padx=(S(2), S(2)), pady=(0, S(6)))

        # ---- 通知区域 ----
        sec2 = Section(body, "通知区域")
        sec2.pack(fill="x", padx=pad, pady=(S(16), 0))
        self._switch_row(
            sec2, "显示托盘图标",
            "关闭后不占通知区域格子；仍可从圆环右键 / Ctrl+Alt+B 打开本窗口。",
            self.var_tray)

        # ---- 通用 ----
        sec3 = Section(body, "通用")
        sec3.pack(fill="x", padx=pad, pady=(S(16), 0))
        self._combo(sec3.row("电量刷新间隔"), self.var_refresh,
                    list(self.refresh_choices), width=8)
        self._switch_row(sec3, "开机自启（启动文件夹快捷方式）", "",
                         self.var_autostart)
        self._combo(sec3.row("主题"), self.var_theme,
                    [t for t, _ in self.theme_choices], width=14)

        # ---- 设备管理（改名）----
        sec4 = Section(body, "设备管理")
        sec4.pack(fill="x", padx=pad, pady=(S(16), 0))
        self.device_mgr = tk.Frame(sec4.body, bg=C["bg"])
        self.device_mgr.pack(fill="x", pady=(S(2), S(4)))
        self.mgr_placeholder = tk.Label(
            self.device_mgr, text="正在读取设备…", bg=C["bg"], fg=C["muted"],
            font=F(9), anchor="w", justify="left", wraplength=S(460))
        self.mgr_placeholder.pack(anchor="w", pady=(S(6), S(4)))
        tk.Label(sec4.body,
                 text="改的名字会显示在状态栏圆环的提示与本页；"
                      "「在状态栏显示」在设备页卡片上勾选。",
                 bg=C["bg"], fg=C["dim"], font=F(9), anchor="w",
                 justify="left", wraplength=S(460)).pack(fill="x",
                                                         pady=(S(4), S(6)))

        # ---- 设备探测 / 测试 ----
        sec5 = Section(body, "设备探测 / 测试")
        sec5.pack(fill="x", padx=pad, pady=(S(16), S(6)))
        probe_row = sec5.body
        probe_desc = tk.Label(
            probe_row,
            text="枚举本机 HID 接口、读取报告描述符、试发协议命令，"
                 "用于适配新设备。",
            bg=C["bg"], fg=C["dim"], font=F(9), anchor="w", justify="left",
            wraplength=S(460))
        probe_desc.pack(fill="x", pady=(S(8), S(8)))
        probe_bar = tk.Frame(probe_row, bg=C["bg"])
        probe_bar.pack(fill="x", pady=(0, S(8)))
        PillButton(probe_bar, "打开设备探测", kind="ghost", bg=C["bg"],
                   height=32, pad_x=20, font_pt=10,
                   command=self._launch_probe).pack(side="left")
        self.lbl_probe_state = tk.Label(probe_bar, text="", bg=C["bg"],
                                        fg=C["dim"], font=F(9), anchor="w")
        self.lbl_probe_state.pack(side="left", padx=(S(10), 0))

        # ---- 底栏 ----
        bar = tk.Frame(footer, bg=C["bg"])
        bar.pack(fill="x", padx=pad, pady=(S(12), S(6)))
        PillButton(bar, "恢复默认", kind="ghost", bg=C["bg"], height=42,
                   pad_x=28, font_pt=10.5,
                   command=self._restore_defaults).pack(side="left")
        PillButton(bar, "设备探测", kind="ghost", bg=C["bg"], height=42,
                   pad_x=22, font_pt=10.5,
                   command=self._launch_probe).pack(side="left",
                                                       padx=(S(10), 0))
        PillButton(bar, "保存并应用", kind="primary", bg=C["bg"], height=42,
                   pad_x=34, font_pt=10.5,
                   command=self._save_explicit).pack(side="right")

        for var in (self.var_ring, self.var_taskbar_ring,
                    self.var_min_high, self.var_min_mid,
                    self.var_color_high, self.var_color_mid,
                    self.var_color_low, self.var_color_charging,
                    self.var_autostart, self.var_theme):
            var.trace_add("write", lambda *_: self._on_discrete_change())

        try:
            self.root.after(80, self._draw_size_preview)
        except Exception:
            pass
    # ==================================================================
    # 关于页
    # ==================================================================
    def _build_about_page(self, body: tk.Frame, footer: tk.Frame) -> None:
        pad = (S(18), S(18))

        sec = Section(body, "版本")
        sec.pack(fill="x", padx=pad)
        self._kv(sec.body, "程序", f"{APP_TITLE} v{APP_VERSION}")
        self._kv(sec.body, "运行环境",
                 f"Python {platform.python_version()} · Windows {platform.release()}")
        self._kv(sec.body, "依赖",
                 "零第三方依赖（仅 Python 标准库 + ctypes + tkinter）")

        sec2 = Section(body, "隐私")
        sec2.pack(fill="x", padx=pad, pady=(S(16), 0))
        self._kv(sec2.body, "不联网", "程序不发起任何网络请求，也不做更新检查")
        self._kv(sec2.body, "不写日志",
                 "没有日志文件；调试仅在 --debug 时输出到控制台")
        self._kv(sec2.body, "只读设备",
                 "只发送查询命令，绝不写入设备设置 / 固件 / 灯光")

        sec3 = Section(body, "致谢")
        sec3.pack(fill="x", padx=pad, pady=(S(16), 0))
        self._kv(sec3.body, "Solaar",
                 "罗技 HID++ 2.0 协议事实参考（特性索引与回复格式）")
        self._kv(sec3.body, "G-Helper",
                 "华硕 ROG OMNI 接收器电量报文事实参考")

        sec4 = Section(body, "配置")
        sec4.pack(fill="x", padx=pad, pady=(S(16), S(6)))
        self._kv(sec4.body, "配置文件", config.path_label())
        self._kv(sec4.body, "程序目录", ROOT)
        self._kv(sec4.body, "开机自启",
                 "已启用" if autostart_enabled() else "未启用（可在设置页打开）")
        self.lbl_about_state = tk.Label(
            sec4.body, text="", bg=C["bg"], fg=C["dim"], font=F(9), anchor="w",
            justify="left", wraplength=S(460))
        self.lbl_about_state.pack(fill="x", pady=(S(6), S(8)))

        bar = tk.Frame(footer, bg=C["bg"])
        bar.pack(fill="x", padx=pad, pady=(S(12), S(6)))
        PillButton(bar, "打开设置", kind="ghost", bg=C["bg"], height=38,
                   pad_x=24, font_pt=10.5,
                   command=lambda: self.select_tab("设置")).pack(side="left")
        PillButton(bar, "刷新状态", kind="ghost", bg=C["bg"], height=38,
                   pad_x=24, font_pt=10.5,
                   command=self._refresh_about_state).pack(side="left",
                                                           padx=(S(10), 0))

    def _kv(self, parent: tk.Misc, key: str, value: str) -> None:
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(S(7), S(7)))
        tk.Label(row, text=key, bg=C["bg"], fg=C["muted"], font=F(9.5),
                 anchor="nw", width=8).pack(side="left", anchor="n")
        tk.Label(row, text=value, bg=C["bg"], fg=C["fg"], font=F(9.5),
                 anchor="w", justify="left", wraplength=S(340)).pack(
            side="left", padx=(S(8), 0), fill="x", expand=True)

    def _refresh_about_state(self) -> None:
        try:
            running = "运行中" if _main_hwnd() else "未运行"
            self.lbl_about_state.configure(
                text=f"常驻程序：{running} · 开机自启："
                     f"{'已启用' if autostart_enabled() else '未启用'}")
        except Exception:
            pass

    # ==================================================================
    # 控件工厂
    # ==================================================================
    # -- 颜色 / 预览辅助 -------------------------------------------------
    def _preview_level(self, cfg: Optional[Dict[str, Any]] = None) -> int:
        """预览圆环显示的电量：取最高档阈值，颜色即最高档颜色。"""
        levels = color_levels(cfg if cfg is not None else self.cfg)
        try:
            return max(1, min(100, int(levels[0][0])))
        except Exception:
            return 60

    def _draw_size_preview(self) -> None:
        """「圆环尺寸」行右侧的实时预览：直径随下拉选择变化（40~64px）。"""
        try:
            cv = getattr(self, "size_preview", None)
            if cv is None:
                return
            cv.delete("all")
            size = int(self._value_for(self.ring_choices,
                                       self.var_ring.get(), 30))
            scale = max(0.8, min(1.25, size / 30.0))
            # 预览直径 40~64 逻辑 px，与设备卡片同比例
            diameter = int(round(48 * scale))
            diameter = max(S(40), min(S(64), int(S(diameter))))
            cv.configure(width=diameter, height=diameter)
            radius = diameter / 2.0 - S(4)
            thick = max(2, int(S(4.6 * scale)))
            level = self._preview_level()
            draw_ring(cv, diameter / 2.0, diameter / 2.0, radius, thick, level,
                      color=level_color(level, cfg=self.cfg),
                      text_pt=10, cfg=self.cfg)
        except Exception:
            pass

    def _draw_legend(self) -> None:
        """设备页图例：颜色/文案跟随 color_levels 与 charging_color。"""
        cells = getattr(self, "_legend_cells", None)
        if not cells:
            return
        low_warn = int(self.cfg.get("low_battery_warn", 20) or 20)
        levels = color_levels(self.cfg)
        try:
            hi = int(levels[0][0])
            mid = int(levels[1][0]) if len(levels) > 1 else 20
        except Exception:
            hi, mid = 60, 20
        hi = max(1, min(100, hi))
        mid = max(0, min(hi - 1 if hi > 1 else 0, mid))
        items = [
            (max(1, min(100, hi)), f"≥{hi}%", "正常",
             level_color(hi, low_warn, self.cfg), False),
            (max(0, mid), f"{mid}–{hi - 1}%", "偏低",
             level_color(max(0, mid), low_warn, self.cfg), False),
            (max(0, mid - 1), f"<{mid}%", "低电量",
             level_color(max(0, mid - 1), low_warn, self.cfg), False),
            (55, "充电中", "蓝色 + 扫光", charging_color(self.cfg), True),
        ]
        for cell, (lv, title, sub, color, charging) in zip(cells, items):
            try:
                cv = cell["canvas"]
                cv.delete("all")
                draw_ring(cv, S(17), S(17), S(13.5), S(3.4), lv, color=color,
                          charging=charging, text_pt=9, cfg=self.cfg)
                cell["title"].configure(text=title)
                cell["sub"].configure(text=sub)
            except Exception:
                continue

    def _pick_color(self, var: tk.StringVar, button: tk.Widget) -> None:
        try:
            from tkinter import colorchooser
            initial = str(var.get() or "#22c55e")
            _rgb, hexv = colorchooser.askcolor(color=initial,
                                               title="选择颜色")
            if hexv:
                var.set(str(hexv).lower())
                try:
                    button.configure(bg=str(hexv).lower())
                except Exception:
                    pass
        except Exception:
            pass

    def _sync_low_label(self) -> None:
        """「低电量 < N%」的 N 跟随中电量阈值。"""
        try:
            mid = max(1, min(100, int(self.var_min_mid.get())))
        except Exception:
            mid = 20
        try:
            self.var_low_label.set(f"低电量 < {mid}%")
        except Exception:
            pass

    def _color_row(self, section: "Section", label: str,
                   min_var: Optional[tk.IntVar],
                   color_var: tk.StringVar,
                   label_var: Optional[tk.StringVar] = None) -> tk.Frame:
        if label_var is not None:
            row = section.row(label, label_var=label_var)
        else:
            row = section.row(label)
        btn = tk.Button(row, width=3, relief="flat", bd=0,
                        bg=str(color_var.get()), activebackground=str(color_var.get()),
                        cursor="hand2",
                        command=lambda: self._pick_color(color_var, btn))
        btn.pack(side="right", padx=(S(8), 0))
        if min_var is not None:
            spin = tk.Spinbox(row, from_=1, to=100, width=4,
                              textvariable=min_var, font=F(9),
                              bg=C["input"], fg=C["fg"], bd=0,
                              buttonbackground=C["card2"],
                              highlightthickness=0)
            spin.pack(side="right", padx=(S(6), S(2)))
            tk.Label(row, text="%", bg=C["bg"], fg=C["muted"],
                     font=F(9)).pack(side="right")
        color_var.trace_add("write", lambda *_: btn.configure(
            bg=str(color_var.get())))
        return row

    def _combo(self, parent: tk.Misc, var: tk.StringVar,
               values: Sequence[str], width: int = 20) -> ttk.Combobox:
        box = ttk.Combobox(parent, textvariable=var, values=list(values),
                           state="readonly", width=width, font=F(10))
        box.pack(side="right", anchor="e")
        return box

    def _static_row(self, section: Section, label: str, value: str) -> None:
        """只读信息行（没有可编辑控件）。"""
        row = section.row(label)
        tk.Label(row, text=value, bg=C["bg"], fg=C["muted"],
                 font=F(9.5)).pack(side="right")

    def _switch_row(self, section: Section, label: str, hint: str,
                    var: tk.BooleanVar) -> Switch:
        row = section.row(label, hint)
        sw = Switch(row, var, bg=C["bg"])
        sw.pack(side="right")
        return sw

    @staticmethod
    def _refresh_options() -> List[str]:
        return ["1 秒", "2 秒", "3 秒", "5 秒", "10 秒", "15 秒", "30 秒", "60 秒"]

    # ==================================================================
    # 配置 <-> 界面
    # ==================================================================
    def _load_into_ui(self) -> None:
        cfg = self.cfg
        ring = int(cfg.get("ring_size", 30) or 30)
        self.var_ring.set(self._label_for(self.ring_choices, ring, "中 · 30 px"))
        self.var_taskbar_ring.set(bool(cfg.get("taskbar_ring_enabled", False)))
        levels = color_levels(cfg)
        try:
            self.var_min_high.set(int(levels[0][0]))
            self.var_color_high.set(str(levels[0][1]))
            self.var_min_mid.set(int(levels[1][0]) if len(levels) > 1 else 20)
            self.var_color_mid.set(str(levels[1][1]) if len(levels) > 1
                                   else C["amber"])
            self.var_color_low.set(str(levels[2][1]) if len(levels) > 2
                                   else C["red"])
        except Exception:
            pass
        self.var_color_charging.set(charging_color(cfg))
        self._draw_size_preview()
        self.var_tray.set(bool(cfg.get("show_tray_icon", True)))
        secs = int(cfg.get("refresh_seconds", 5) or 5)
        self.var_refresh.set(f"{secs} 秒")
        self.var_autostart.set(autostart_enabled())
        self.var_theme.set(self._label_for(self.theme_choices,
                                          str(cfg.get("theme", "dark")), "深色"))

    @staticmethod
    def _label_for(choices: Sequence[tuple], value: Any, fallback: str) -> str:
        for label, val in choices:
            if val == value:
                return str(label)
        return fallback

    @staticmethod
    def _value_for(choices: Sequence[tuple], label: str, fallback: Any) -> Any:
        for lab, val in choices:
            if str(lab) == str(label):
                return val
        return fallback

    def _collect(self) -> Dict[str, Any]:
        """界面值 → 配置（只写 config.py 里存在的键）。"""
        cfg = dict(self.cfg)
        cfg["style"] = "B"
        cfg["ring_size"] = int(self._value_for(self.ring_choices,
                                              self.var_ring.get(), 30))
        cfg["show_tray_icon"] = bool(self.var_tray.get())
        cfg["taskbar_ring_enabled"] = bool(self.var_taskbar_ring.get())
        try:
            hi = max(1, min(100, int(self.var_min_high.get())))
        except Exception:
            hi = 60
        try:
            mid = max(1, min(100, int(self.var_min_mid.get())))
        except Exception:
            mid = 20
        if mid >= hi:
            mid = max(1, hi - 1)
        cfg["color_levels"] = [
            {"min": hi, "color": str(self.var_color_high.get())},
            {"min": mid, "color": str(self.var_color_mid.get())},
            {"min": 0, "color": str(self.var_color_low.get())},
        ]
        cfg["charging_color"] = str(self.var_color_charging.get())
        try:
            secs = int(str(self.var_refresh.get()).split()[0])
        except Exception:
            secs = 5
        cfg["refresh_seconds"] = max(1, min(300, secs))
        cfg["autostart"] = bool(self.var_autostart.get())
        cfg["theme"] = str(self._value_for(self.theme_choices,
                                           self.var_theme.get(), "dark"))

        # 设备别名：以现有配置为底，避免丢当前不在线的设备
        aliases: Dict[str, str] = dict(cfg.get("device_aliases") or {})
        for did, var in self._alias_vars.items():
            name = (var.get() or "").strip()
            if name:
                aliases[did] = name
            else:
                aliases.pop(did, None)
        cfg["device_aliases"] = aliases

        # 状态栏显示：以现有 device_hidden 为底，按卡片勾选覆盖
        hidden = [str(d) for d in (cfg.get("device_hidden") or [])]
        for did, card in self._cards.items():
            show = bool(card.var_visible.get())
            if show:
                hidden = [d for d in hidden if d != did]
            elif did not in hidden:
                hidden.append(did)
        cfg["device_hidden"] = hidden

        # 清理已废弃的键（Lead 已从 config.py 删除）
        for legacy in ("hide_on_fullscreen", "click_through", "show_hover_card",
                       "device_disabled", "primary_device"):
            cfg.pop(legacy, None)
        return cfg

    # ==================================================================
    # 保存 / 广播
    # ==================================================================
    def _on_discrete_change(self) -> None:
        if self._loading or self._build_lock:
            return
        self._apply(explicit=False)

    def _apply_appearance(self) -> None:
        """颜色 / 圆环尺寸 / 阈值变化后立刻重画（不写盘、不重建控件）。"""
        if self._loading or self._build_lock:
            return
        try:
            self._draw_size_preview()
        except Exception:
            pass
        try:
            self._draw_legend()
        except Exception:
            pass
        for card in list(self._cards.values()):
            try:
                card.apply_cfg(self.cfg)
            except Exception:
                pass

    def _save_explicit(self) -> None:
        self._apply(explicit=True)

    def _apply(self, explicit: bool = False) -> None:
        if self._loading or self._build_lock:
            return
        prev_secs = int(self.cfg.get("refresh_seconds", 5) or 5)
        cfg = self._collect()
        ok = bool(config.save(cfg))
        self.cfg = cfg

        auto_msg = ""
        if bool(cfg["autostart"]) != autostart_enabled():
            good, err = set_autostart(bool(cfg["autostart"]))
            if not good:
                auto_msg = f"；开机自启未生效（{err}）"
                self._loading = True
                try:
                    self.var_autostart.set(autostart_enabled())
                finally:
                    self._loading = False
                cfg["autostart"] = autostart_enabled()
                config.save(cfg)

        msg_id = broadcast_reload()
        self._reschedule_refresh()
        self._update_subtitle()
        # 外观类配置（颜色 / 圆环尺寸 / 阈值）→ 就地生效，不重建卡片
        self._apply_appearance()
        if cfg["refresh_seconds"] != prev_secs:
            self._render_device_cards()

        parts = ["已保存" if ok else "保存失败（配置目录不可写？）",
                 "已通知常驻进程" if msg_id else "常驻进程未响应（可稍后重试）"]
        if explicit:
            parts.append(time.strftime("%H:%M:%S"))
        self._set_status(" · ".join(parts) + auto_msg)

    def _restore_defaults(self) -> None:
        defaults = getattr(config, "DEFAULTS", {}) or {}
        self._build_lock = True
        try:
            self.var_ring.set(self._label_for(self.ring_choices,
                                             int(defaults.get("ring_size", 30)),
                                             "中 · 30 px"))
            self.var_tray.set(bool(defaults.get("show_tray_icon", True)))
            self.var_taskbar_ring.set(bool(defaults.get("taskbar_ring_enabled", False)))
            dlevels = color_levels(defaults)
            try:
                self.var_min_high.set(int(dlevels[0][0]))
                self.var_min_mid.set(int(dlevels[1][0]) if len(dlevels) > 1 else 20)
                self.var_color_high.set(str(dlevels[0][1]))
                self.var_color_mid.set(str(dlevels[1][1]) if len(dlevels) > 1
                                       else "#f59e0b")
                self.var_color_low.set(str(dlevels[2][1]) if len(dlevels) > 2
                                       else "#ef4444")
                self.var_color_charging.set(charging_color(defaults))
            except Exception:
                pass
            self.var_refresh.set(f"{int(defaults.get('refresh_seconds', 5) or 5)} 秒")
            self.var_theme.set(self._label_for(self.theme_choices,
                                              str(defaults.get("theme", "dark")),
                                              "深色"))
            self.var_autostart.set(autostart_enabled())
            for var in self._alias_vars.values():
                var.set("")
            for card in self._cards.values():
                card.var_visible.set(True)
        finally:
            self._build_lock = False
        self._apply(explicit=True)
        self._set_status("已恢复默认设置 · " + time.strftime("%H:%M:%S"))

    # ==================================================================
    # 设备数据
    # ==================================================================
    def _refresh_devices(self, focus_did: str = "") -> None:
        if self._reader_busy:
            return
        self._reader_busy = True
        card = self._cards.get(focus_did or "")
        if card is not None:
            card.mark_busy(True)
        if not self._readings:
            self._set_subtitle_loading()
        threading.Thread(target=self._read_worker, daemon=True).start()
        self.root.after(120, self._poll_reading_queue)

    def _read_worker(self) -> None:
        readings: List[Any] = []
        infos: List[Any] = []
        err: Optional[str] = None
        if self.demo:
            readings = self._demo_readings()
        elif device_reader is None:
            err = f"{type(READER_ERROR).__name__}: {READER_ERROR}"
        else:
            try:
                readings = list(device_reader.read_all() or [])
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            try:
                discover = getattr(device_reader, "discover", None)
                if discover is not None:
                    infos = list(discover() or [])
            except Exception:
                infos = []
        self._queue.put((readings, infos, err))

    def _demo_readings(self) -> List[Any]:
        if device_reader is not None:
            fn = getattr(device_reader, "demo_readings", None)
            if fn is not None:
                try:
                    got = list(fn() or [])
                    if got:
                        return got
                except Exception:
                    pass
        try:
            return [
                Reading(device_id="demo:rog:0x1C25", name="ROG 夜魔X", level=62,
                        status="discharging", source="demo", charging=False),
                Reading(device_id="demo:logitech:C539#1", name="G PRO Wireless",
                        level=77, status="discharging", voltage_mv=3967,
                        source="demo", estimated=True, charging=False),
            ]
        except Exception:
            return []

    def _demo_infos(self) -> List[Any]:
        """演示模式下补上 VID:PID / 连接方式，让卡片信息完整。"""
        out: List[Any] = []
        try:
            info_cls = None
            for mod_name in ("models", "peripheral_contract"):
                mod = sys.modules.get(mod_name)
                if mod is not None and getattr(mod, "DeviceInfo", None) is not None:
                    info_cls = getattr(mod, "DeviceInfo")
                    break

            def make(did: str, name: str, kind: str, conn: str, vid_pid: str):
                if info_cls is None:
                    return None
                try:
                    return info_cls(device_id=did, name=name, kind=kind,
                                    connection=conn, vid_pid=vid_pid)
                except Exception:
                    return None

            for args in (
                ("demo:rog:0x1C25", "ROG 夜魔X", "rog", "omni_receiver",
                 "0B05:1ACE"),
                ("demo:logitech:C539#1", "G PRO Wireless", "logitech",
                 "lightspeed", "046D:C539"),
            ):
                obj = make(*args)
                if obj is not None:
                    out.append(obj)
        except Exception:
            pass
        return out

    def _poll_reading_queue(self) -> None:
        try:
            readings, infos, err = self._queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_reading_queue)
            return
        self._reader_busy = False
        self._readings = [r for r in readings if r is not None]
        self._read_error = err
        self._infos = {}
        if self.demo and not infos:
            infos = self._demo_infos()
        for info in infos:
            did = str(getattr(info, "device_id", "") or "")
            if did:
                self._infos[did] = info
        self._render_device_cards()
        self._render_device_manager()
        self._update_subtitle()
        self._refresh_about_state()
        self._reschedule_refresh()

    # ------------------------------------------------------------------
    def _render_device_cards(self) -> None:
        """刷新设备卡片：集合/顺序不变时原地更新，避免整块重建闪烁。

        * 设备集合与顺序都没变 → 逐张 ``update_reading()``（不建/不销毁控件）；
        * 有增删/换序 → 只对变化的卡片做增删，其余复用，最后按顺序重排。
        """
        aliases = self.cfg.get("device_aliases") or {}
        hidden = set(self.cfg.get("device_hidden") or [])
        low_warn = int(self.cfg.get("low_battery_warn", 20) or 20)
        readings = [r for r in self._readings if r is not None]
        desired = [str(getattr(r, "device_id", "") or "") for r in readings]

        if desired and desired == list(self._cards.keys()):
            # 原地更新：不重建任何控件 → 不闪、不跳、滚动位置不变
            for reading, did in zip(readings, desired):
                card = self._cards.get(did)
                if card is None:
                    continue
                card.update_reading(
                    reading, self._infos.get(did),
                    name=str(aliases.get(did, "") or ""),
                    low_warn=low_warn, animate=True)
                visible = did not in hidden
                if bool(card.var_visible.get()) != visible:
                    card.var_visible.set(visible)
            return

        yview = None
        try:
            yview = self.page_scroll["设备"].canvas.yview()
        except Exception:
            pass

        if not readings:
            for card in self._cards.values():
                try:
                    card.destroy()
                except Exception:
                    pass
            self._cards = {}
            for child in self.dev_holder.winfo_children():
                child.destroy()
            text, color = self._empty_text()
            tk.Label(self.dev_holder, text=text, bg=C["card"], fg=color,
                     font=F(9.5), anchor="w", justify="left", padx=S(14),
                     pady=S(14), wraplength=S(460)).pack(fill="x")
            return

        # 有设备：先清掉空态占位，再按 device_id 复用/新建卡片
        for child in self.dev_holder.winfo_children():
            if not isinstance(child, DeviceCard):
                child.destroy()

        keep = set(desired)
        for did in list(self._cards.keys()):
            if did not in keep:
                try:
                    self._cards[did].destroy()
                except Exception:
                    pass
                self._cards.pop(did, None)

        for reading in readings:
            did = str(getattr(reading, "device_id", "") or "")
            card = self._cards.get(did)
            if card is None:
                card = DeviceCard(
                    self.dev_holder, reading, self._infos.get(did),
                    on_refresh=self._refresh_devices,
                    on_visible_change=self._on_visible_change,
                    name=str(aliases.get(did, "") or ""),
                    visible=did not in hidden,
                    low_warn=low_warn,
                    cfg=self.cfg,
                )
                self._cards[did] = card
            else:
                card.apply_cfg(self.cfg)
                card.update_reading(
                    reading, self._infos.get(did),
                    name=str(aliases.get(did, "") or ""),
                    low_warn=low_warn, animate=False)

        # 按期望顺序重排（pack 顺序 = 插入顺序），并让 _cards 的键序与之一致
        for did in desired:
            card = self._cards.get(did)
            if card is None:
                continue
            try:
                card.pack_forget()
                card.pack(fill="x", pady=(0, S(10)))
            except Exception:
                continue
        self._cards = {did: self._cards[did] for did in desired
                       if did in self._cards}

        if yview:
            try:
                self.page_scroll["设备"].canvas.yview_moveto(yview[0])
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _empty_text(self) -> tuple[str, str]:
        """没有设备时显示的空态文案。"""
        if self._read_error:
            return (f"读取设备失败：{self._read_error}\n\n"
                    "请确认接收器已插好、设备已唤醒；也可以点「设备探测」"
                    "查看 HID 枚举情况。", C["red"])
        if self.demo:
            return ("演示模式：没有读到演示数据。", C["muted"])
        return ("没有发现可读的电量设备。\n\n"
                "无线外设：确认接收器已插好、设备已唤醒；\n"
                "有线设备：换一个 USB 口或重新插拔试试。", C["muted"])

    def _on_visible_change(self, did: str) -> None:
        """「在状态栏显示」勾选变化 → 写 device_hidden + 广播。"""
        if self._build_lock or not did:
            return
        card = self._cards.get(did)
        show = True if card is None else bool(card.var_visible.get())
        hidden = [str(d) for d in (self.cfg.get("device_hidden") or [])]
        if show:
            hidden = [d for d in hidden if d != did]
        elif did not in hidden:
            hidden.append(did)
        self.cfg["device_hidden"] = hidden
        self._apply(explicit=False)

    def _render_device_manager(self, force: bool = False) -> None:
        """设置页「设备管理」：改别名（设备集合不变时跳过重建，避免闪烁）。"""
        ids = [str(getattr(r, "device_id", "") or "") for r in self._readings]
        if not force and ids == getattr(self, "_mgr_ids", None):
            return
        self._mgr_ids = ids
        for child in self.device_mgr.winfo_children():
            child.destroy()
        self._alias_vars = {}
        if not self._readings:
            tk.Label(self.device_mgr, text="没有发现可管理的设备。", bg=C["bg"],
                     fg=C["muted"], font=F(9), anchor="w").pack(
                anchor="w", pady=(S(6), S(4)))
            return
        aliases = self.cfg.get("device_aliases") or {}
        for idx, reading in enumerate(self._readings):
            did = str(getattr(reading, "device_id", "") or "")
            alias_var = tk.StringVar(value=str(aliases.get(did, "") or ""))
            self._alias_vars[did] = alias_var
            if idx:
                tk.Frame(self.device_mgr, bg=C["sep"], height=max(1, S(1))).pack(
                    fill="x", pady=(S(2), S(2)))
            row = tk.Frame(self.device_mgr, bg=C["bg"])
            row.pack(fill="x", pady=(S(6), S(6)))
            label = str(aliases.get(did, "") or "") or DeviceCard._title_text(reading)
            tk.Label(row, text=label, bg=C["bg"], fg=C["fg"], font=F(10),
                     anchor="w", justify="left", wraplength=S(240)).pack(
                side="left", fill="x", expand=True)
            PillButton(row, "改名", kind="ghost", bg=C["bg"], height=26,
                       pad_x=12, font_pt=9,
                       command=lambda d=did, v=alias_var: self._rename_device(d, v)
                       ).pack(side="right")

    def _rename_device(self, did: str, alias_var: tk.StringVar) -> None:
        current = str(alias_var.get() or "").strip()
        dlg = AliasDialog(self.root, "设备改名", current)
        if dlg.result is None:
            return
        alias_var.set(dlg.result)
        self._apply(explicit=True)
        self._render_device_cards()
        self._render_device_manager(force=True)

    def _update_subtitle(self) -> None:
        secs = int(self.cfg.get("refresh_seconds", 5) or 5)
        n = len(self._readings)
        if self._reader_busy:
            self._set_subtitle_loading()
            return
        if self._read_error and not n:
            text = f"读取失败 · 每 {secs} 秒重试"
        elif n:
            hidden = len(self.cfg.get("device_hidden") or [])
            shown = max(0, n - hidden)
            extra = f"（{hidden} 台已隐藏）" if hidden else ""
            text = f"{shown} 台设备 · 每 {secs} 秒自动刷新{extra}"
        else:
            text = f"未发现设备 · 每 {secs} 秒自动刷新"
        try:
            self.lbl_subtitle.configure(text=text, fg=C["muted"])
        except Exception:
            pass

    def _set_subtitle_loading(self) -> None:
        try:
            self.lbl_subtitle.configure(text="正在读取设备…", fg=C["dim"])
        except Exception:
            pass

    def _reschedule_refresh(self) -> None:
        if self._refresh_job:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
        secs = max(2, int(self.cfg.get("refresh_seconds", 5) or 5))
        self._refresh_job = self.root.after(secs * 1000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        self._refresh_job = None
        try:
            visible = self.root.state() == "normal"
        except Exception:
            visible = True
        if visible:
            self._refresh_devices()
        self._reschedule_refresh()

    # ==================================================================
    # 设备探测（probe.py，独立进程）
    # ==================================================================
    def _launch_probe(self) -> None:
        probe = os.path.join(ROOT, "probe.py")
        if not os.path.isfile(probe):
            self._set_probe_state("找不到 probe.py（设备探测工具）", C["red"])
            return
        try:
            _spawn_detached([_pythonw_exe(), probe])
            self._set_probe_state("已启动设备探测窗口", C["muted"])
        except Exception as exc:  # noqa: BLE001
            self._set_probe_state(f"启动失败：{type(exc).__name__}: {exc}", C["red"])

    def _set_probe_state(self, text: str, color: str) -> None:
        try:
            self.lbl_probe_state.configure(text=text, fg=color)
        except Exception:
            pass

    # ==================================================================
    # 状态栏
    # ==================================================================
    def _set_status(self, text: str) -> None:
        try:
            self.lbl_status.configure(text=text, fg=C["muted"])
        except Exception:
            return
        if self._status_clear_job:
            try:
                self.root.after_cancel(self._status_clear_job)
            except Exception:
                pass
        self._status_clear_job = self.root.after(6000, self._set_status_quiet)

    def _set_status_quiet(self) -> None:
        self._status_clear_job = None
        try:
            self.lbl_status.configure(text=f"配置：{config.path_label()}",
                                      fg=C["dim"])
        except Exception:
            pass

    def _schedule_status_poll(self) -> None:
        self._refresh_about_state()
        self._status_job = self.root.after(3000, self._schedule_status_poll)

    # ==================================================================
    # 关闭
    # ==================================================================
    def _on_close(self) -> None:
        """关闭窗口 = 退出本面板进程（常驻圆环继续在后台跑）。"""
        try:
            self._remember_geometry()
        except Exception:
            pass
        for job in (self._refresh_job, self._status_job, self._status_clear_job,
                    self._geom_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ==================================================================
    def run(self) -> int:
        self.root.mainloop()
        return 0


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
def self_check(demo: bool, tab: str = "设备") -> int:
    """无交互自测：构建界面 → 读一次设备 → 打印摘要 → 退出。"""
    app = PanelApp(demo=demo, debug=True, tab=tab)
    deadline = time.time() + 20.0
    while time.time() < deadline and not app._readings and \
            not getattr(app, "_read_error", None):
        app.root.update()
        time.sleep(0.05)
    for _ in range(40):
        app.root.update()
        time.sleep(0.02)

    out: List[str] = []
    out.append(f"[self-check] ROOT = {ROOT}")
    out.append(f"[self-check] config = {config.config_path()}")
    out.append(f"[self-check] DPI scale = {SCALE:.3f}；窗口 "
               f"{app.root.winfo_width()}×{app.root.winfo_height()} px "
               f"（可缩放 = {bool(app.root.resizable())}）")
    out.append(f"[self-check] 设备层 = "
               f"{'可用' if device_reader is not None else '不可用（已降级）'}")
    if READER_ERROR is not None:
        out.append(f"[self-check] 设备层异常 = {type(READER_ERROR).__name__}: "
                   f"{READER_ERROR}")
    out.append(f"[self-check] 读到 {len(app._readings)} 条读数")
    for r in app._readings:
        out.append(f"    - {getattr(r, 'device_id', '?')} | "
                   f"{getattr(r, 'name', '?')} | {getattr(r, 'level_text', '?')} | "
                   f"{getattr(r, 'status_text', '?')}")
    out.append(f"[self-check] 设备卡片 = {len(app._cards)} 张；"
               f"标签页 = {'/'.join(app.TABS)}")
    try:
        child = int(app.root.winfo_id())
        hwnd = int(_user32.GetParent(ctypes.c_void_p(child)) or child)
        ex = int(_user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE))
        out.append(f"[self-check] WS_EX_TOOLWINDOW = {bool(ex & WS_EX_TOOLWINDOW)}")
    except Exception as exc:
        out.append(f"[self-check] 窗口样式检查失败：{exc}")
    cfg = app._collect()
    keep = ("ring_size", "refresh_seconds", "show_tray_icon", "theme", "autostart",
            "device_hidden", "window_width", "window_height", "window_x", "window_y")
    out.append("[self-check] 设置项 = " +
               " ".join(f"{k}={cfg.get(k)}" for k in keep))
    stale = [k for k in ("hide_on_fullscreen", "click_through", "show_hover_card",
                         "device_disabled", "primary_device") if k in cfg]
    out.append(f"[self-check] 已废弃键残留 = {stale or '无'}")
    out.append(f"[self-check] 启动文件夹快捷方式存在 = {autostart_enabled()}")
    out.append(f"[self-check] 常驻主窗口 = {_main_hwnd()}")
    msg = broadcast_reload()
    out.append(f"[self-check] 广播消息号 = {msg}（0 表示失败，主程序未运行时正常）")
    app._on_close()
    text = "\n".join(out)
    print(text)
    try:
        path = os.path.join(ROOT, "_ref", "selfcheck.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    print("[self-check] OK")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
_TAB_ALIASES = {
    "设备": "设备", "dev": "设备", "device": "设备", "devices": "设备",
    "设置": "设置", "set": "设置", "settings": "设置", "setting": "设置",
    "关于": "关于", "about": "关于",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} · 主界面")
    parser.add_argument("--demo", action="store_true",
                        help="使用假数据（无设备时看界面）")
    parser.add_argument("--debug", action="store_true",
                        help="前台运行并打印异常")
    parser.add_argument("--self-check", action="store_true",
                        help="自测：构建界面后退出")
    parser.add_argument("--tab", default="设备",
                        help="启动后显示的标签页：设备 / 设置 / 关于")
    parser.add_argument("--panel", action="store_true",
                        help="兼容参数：等同于打开面板")
    args, _unknown = parser.parse_known_args(
        list(argv) if argv is not None else sys.argv[1:])

    tab = _TAB_ALIASES.get(str(args.tab or "").strip(), "设备")
    if args.self_check:
        return self_check(args.demo, tab)
    if not args.demo and os.environ.get("PB_PANEL_DEMO") == "1":
        args.demo = True

    # 单实例：已经开着就把那个窗口拉到前台
    existing = _find_panel_window(APP_TITLE)
    if existing and _user32 is not None:
        try:
            _user32.ShowWindow(ctypes.c_void_p(existing), SW_RESTORE)
            _user32.SetForegroundWindow(ctypes.c_void_p(existing))
        except Exception:
            pass
        return 0

    app = PanelApp(demo=args.demo, debug=args.debug, tab=tab)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
