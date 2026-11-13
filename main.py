# -*- coding: utf-8 -*-
"""main.py —— 外设电量圆环：入口 / 单实例 / 消息循环 / 轮询调度 / 装配。

运行方式：
    pythonw main.py            常驻（任务栏圆环 + 托盘）
    python  main.py --debug    前台调试
    python  main.py --once     打印一次读数
    python  main.py --demo     假数据联调动画
    python  main.py --quit     让常驻实例退出
    python  main.py --status   打印状态
    python  main.py --panel    打开设置面板

线程模型：
* 主线程：Win32 消息循环（托盘回调、定时器、UI 更新都在这里）
* 后台线程：轮询设备（devices.reader.read_all），结果用 PostMessage 回主线程
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config as appcfg                      # noqa: E402
from models import Reading                   # noqa: E402

# ---------------------------------------------------------------------------
# Win32 常量与声明
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WND_CLASS = "PeripheralBatteryMainWnd"
MUTEX_NAME = "PeripheralBattery_SingleInstance_Mutex"

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_SETTINGCHANGE = 0x001A
WM_DISPLAYCHANGE = 0x007E
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
HWND_BROADCAST = 0xFFFF
TIMER_LAYOUT = 1
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xB001
MOD_CONTROL = 0x0002
MOD_ALT = 0x0001

MSG_TRAY_CALLBACK = WM_APP + 1
MSG_READINGS = WM_APP + 2

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


user32.RegisterClassExW.restype = wintypes.WORD
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                ctypes.c_size_t, ctypes.c_ssize_t]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.restype = wintypes.BOOL
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.RegisterHotKey.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]

kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.GetConsoleWindow.restype = ctypes.c_void_p
kernel32.GetConsoleProcessList.restype = wintypes.DWORD
kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                            wintypes.DWORD]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000


def hide_own_console() -> None:
    """双击 .py 启动时会带 python.exe 的控制台窗口 —— 若控制台只属于本进程就藏掉。

    从 cmd / PowerShell 里运行（控制台里还有别的进程）时不隐藏，避免把用户的终端藏了。
    """
    try:
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        buf = (wintypes.DWORD * 4)()
        count = kernel32.GetConsoleProcessList(buf, 4)
        if count <= 1:
            user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # SW_HIDE
    except Exception:
        pass


def pythonw_path() -> str:
    """优先用同目录 pythonw.exe（无黑框）。"""
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    if exe.lower().endswith("pythonw.exe"):
        return exe
    return exe or "pythonw.exe"


# ---------------------------------------------------------------------------
# 开机自启（启动文件夹快捷方式，任务管理器可见）
# ---------------------------------------------------------------------------
STARTUP_LNK_NAME = "外设电量圆环.lnk"


def startup_lnk() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", STARTUP_LNK_NAME)


def is_autostart() -> bool:
    return os.path.exists(startup_lnk())


def set_autostart(enable: bool) -> bool:
    try:
        if enable:
            ps = os.path.join(BASE_DIR, "scripts", "install.ps1")
            if not os.path.exists(ps):
                return False
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", ps, "-NoDesktop", "-NoStartMenu"],
                cwd=BASE_DIR, creationflags=CREATE_NO_WINDOW, timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            if os.path.exists(startup_lnk()):
                os.remove(startup_lnk())
        return is_autostart() == enable
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
class App:
    def __init__(self, debug: bool = False, demo: bool = False):
        self.debug = debug
        self.demo = demo
        self.cfg = appcfg.load()
        self.hwnd = 0
        self._wndproc_ref = None
        self._quit = False
        self._readings: list[Reading] = []
        self.overlay = None
        self.tray = None
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._msg_quit = 0
        self._msg_reload = 0
        self._taskbar_created = 0
        self._last_readings_err = None

    # -- 日志（默认不落盘） -------------------------------------------------
    def log(self, msg: str) -> None:
        if self.debug:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # -- 装配 ---------------------------------------------------------------
    def setup(self) -> bool:
        self._msg_quit = user32.RegisterWindowMessageW(appcfg.MSG_QUIT)
        self._msg_reload = user32.RegisterWindowMessageW(appcfg.MSG_RELOAD)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = WND_CLASS
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):        # 1410 = class already exists
                self.log(f"RegisterClassExW 失败 err={err}")
                return False
        self.hwnd = user32.CreateWindowExW(0, WND_CLASS, "PeripheralBatteryMainWnd",
                                           0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            self.log(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
            return False

        if self.cfg.get("taskbar_ring_enabled", False):
            self._setup_overlay()
        else:
            self.log("任务栏悬浮圆环已关闭（使用通知区域图标）")
        self._setup_tray()
        try:
            user32.RegisterHotKey(ctypes.c_void_p(self.hwnd), HOTKEY_ID,
                                  MOD_CONTROL | MOD_ALT, ord('B'))
        except Exception:
            pass
        self._start_poller()
        user32.SetTimer(self.hwnd, TIMER_LAYOUT, 2000, None)
        self.log(f"启动完成 style={self.cfg.get('style')} demo={self.demo}")
        return True

    def _callbacks(self) -> dict:
        return {
            "on_left_click": self.refresh_now,
            "on_double_click": self.open_panel,
            "on_right_click": self.show_menu,
            "on_position_changed": self.on_position_changed,
        }

    def _setup_overlay(self) -> None:
        try:
            from ui.overlay import TaskbarOverlay
            self.overlay = TaskbarOverlay(self.cfg, self._callbacks())
            if not self.overlay.start():
                self.log("圆环窗口启动失败，降级为仅托盘模式")
                self.overlay = None
        except Exception:
            self.log("圆环模块不可用：\n" + traceback.format_exc())
            self.overlay = None

    def _setup_tray(self) -> None:
        try:
            from ui.tray import TrayIcon
            tray_cb = {
                "open_panel": self.open_panel,
                "refresh": self.refresh_now,
                "toggle_tray": self.toggle_tray,
                "toggle_autostart": self.toggle_autostart,
                "quit": self.request_quit,
                "is_autostart": is_autostart,
            }
            self.tray = TrayIcon(self.hwnd, MSG_TRAY_CALLBACK, self.cfg, tray_cb)
            if self.cfg.get("show_tray_icon", True):
                ok = self.tray.add()
                self.log(f"tray.add -> {ok}")
                self.tray.add()
        except Exception:
            self.log("托盘模块不可用：\n" + traceback.format_exc())
            self.tray = None

    # -- 轮询线程 -----------------------------------------------------------
    def _start_poller(self) -> None:
        self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="battery-poller")
        self._poller.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.demo:
                    try:
                        from devices.reader import demo_readings
                        data = demo_readings()
                    except Exception:
                        data = _builtin_demo_readings()
                else:
                    from devices import reader
                    data = reader.read_all(debug=self.debug)
                if not isinstance(data, (list, tuple)):
                    data = []
                self._readings = list(data)
                self._last_readings_err = None
            except Exception:
                self._last_readings_err = traceback.format_exc()
                self.log("读取设备失败：\n" + self._last_readings_err)
                self._readings = []
            if self.hwnd:
                user32.PostMessageW(self.hwnd, MSG_READINGS, 0, 0)
            interval = max(1, int(self.cfg.get("refresh_seconds", 5) or 5))
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def refresh_now(self) -> None:
        self._wake.set()

    # -- 读数应用 -----------------------------------------------------------
    def apply_readings(self) -> None:
        data = list(self._readings)
        if self.overlay:
            try:
                self.overlay.set_readings(data)
            except Exception:
                self.log("overlay.set_readings 异常：\n" + traceback.format_exc())
        if self.tray:
            try:
                self.tray.update(data)
            except Exception:
                self.log("tray.update 异常：\n" + traceback.format_exc())

    # -- 菜单 / 面板 / 配置 --------------------------------------------------
    def show_menu(self) -> None:
        if self.tray:
            self.tray.show_menu()

    def open_panel(self) -> None:
        panel = os.path.join(BASE_DIR, "ui", "panel.py")
        if not os.path.exists(panel):
            self.log("设置面板还没就绪：ui/panel.py 不存在")
            return
        try:
            subprocess.Popen([pythonw_path(), panel], cwd=BASE_DIR)
            self.log("已打开设置面板")
        except Exception:
            self.log("打开面板失败：\n" + traceback.format_exc())

    def toggle_tray(self) -> None:
        show = not bool(self.cfg.get("show_tray_icon", True))
        self.cfg["show_tray_icon"] = show
        appcfg.save(self.cfg)
        if self.tray:
            self.tray.set_visible(show)
        self.log(f"托盘图标 -> {'显示' if show else '隐藏'}")

    def toggle_autostart(self) -> None:
        target = not is_autostart()
        if set_autostart(target):
            self.cfg["autostart"] = is_autostart()
            appcfg.save(self.cfg)
            self.log(f"开机自启 -> {self.cfg['autostart']}")
        else:
            self.log("切换开机自启失败（检查 scripts/install.ps1 是否存在）")

    def reload_config(self) -> None:
        old = self.cfg
        self.cfg = appcfg.load()
        self.cfg["autostart"] = is_autostart()
        appcfg.save(self.cfg)
        if self.overlay:
            try:
                self.overlay.reload_config(self.cfg)
            except Exception:
                self.log("overlay.reload_config 异常：\n" + traceback.format_exc())
        if self.tray:
            self.tray.set_config(self.cfg)
            self.tray.set_visible(bool(self.cfg.get("show_tray_icon", True)))
        if old.get("refresh_seconds") != self.cfg.get("refresh_seconds"):
            self.refresh_now()
        self.apply_readings()
        self.log("配置已重载")

    def on_position_changed(self, dx: int, dy: int) -> None:
        self.cfg["offset_x"] = int(dx)
        self.cfg["offset_y"] = int(dy)
        appcfg.save(self.cfg)
        self.log(f"圆环位置偏移 -> ({dx},{dy})")

    # -- 退出 ---------------------------------------------------------------
    def request_quit(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, self._msg_quit, 0, 0)

    def cleanup(self) -> None:
        if self._quit:
            return
        self._quit = True
        self._stop.set()
        self._wake.set()
        try:
            if self.tray:
                self.tray.remove()
        except Exception:
            pass
        try:
            if self.overlay:
                self.overlay.destroy()
        except Exception:
            pass
        try:
            if self.hwnd:
                user32.UnregisterHotKey(ctypes.c_void_p(self.hwnd), HOTKEY_ID)
                user32.KillTimer(self.hwnd, TIMER_LAYOUT)
                user32.DestroyWindow(self.hwnd)
        except Exception:
            pass

    # -- 窗口过程 -----------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == MSG_TRAY_CALLBACK:
                event = int(lparam) & 0xFFFF
                if event == WM_RBUTTONUP:
                    self.show_menu()
                elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.open_panel()
                return 0
            if msg == MSG_READINGS:
                self.apply_readings()
                return 0
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                self.open_panel()
                return 0
            if msg == WM_TIMER:
                if wparam == TIMER_LAYOUT and self.overlay:
                    try:
                        self.overlay.reposition()
                    except Exception:
                        pass
                return 0
            if msg == self._msg_reload:
                self.reload_config()
                return 0
            if msg == self._msg_quit:
                self.cleanup()
                return 0
            if msg == self._taskbar_created:
                self.log("任务栏重建，重新挂接")
                if self.tray:
                    try:
                        self.tray.add()
                    except Exception:
                        pass
                if self.overlay:
                    try:
                        self.overlay.reposition()
                    except Exception:
                        pass
                return 0
            if msg in (WM_SETTINGCHANGE, WM_DISPLAYCHANGE):
                if self.overlay:
                    try:
                        self.overlay.reposition()
                    except Exception:
                        pass
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            self.log("窗口过程异常：\n" + traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> int:
        if not self.setup():
            return 1
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.cleanup()
        self.log("消息循环结束")
        return 0


def _builtin_demo_readings() -> list[Reading]:
    """devices.reader 还没写好时的兜底假数据。"""
    now = time.time()
    return [
        Reading(device_id="rog:0x1C25", name="ROG 夜魔X", level=62,
                status="discharging", source="ROG OMNI（demo）", updated_at=now),
        Reading(device_id="logitech:C539#1", name="G PRO Wireless", level=77,
                status="charging", charging=True, voltage_mv=3963, estimated=True,
                source="Logitech HID++（demo）", updated_at=now),
    ]


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def do_once(debug: bool = False) -> int:
    try:
        from devices import reader
        readings = reader.read_once()
    except Exception:
        print("读取失败：", file=sys.stderr)
        traceback.print_exc()
        return 1
    if not readings:
        print("没有发现可读设备。")
        return 1
    for r in readings:
        extra = f"  [{r.source}]" if debug and r.source else ""
        print(f"{r.name}: {r.level if r.level is not None else '—'}% "
              f"{r.status_text}{extra}")
        if r.detail and debug:
            print(f"    detail: {r.detail}")
    return 0


def do_quit() -> int:
    msg = user32.RegisterWindowMessageW(appcfg.MSG_QUIT)
    hwnd = user32.FindWindowW(WND_CLASS, None)
    if not hwnd:
        print("没有正在运行的实例。")
        return 0
    user32.PostMessageW(hwnd, msg, 0, 0)
    print("已通知退出。")
    return 0


def do_status() -> int:
    cfg = appcfg.load()
    hwnd = user32.FindWindowW(WND_CLASS, None)
    print(json.dumps({
        "running": bool(hwnd),
        "config_path": appcfg.config_path(),
        "autostart": is_autostart(),
        "style": cfg.get("style"),
        "refresh_seconds": cfg.get("refresh_seconds"),
        "show_tray_icon": cfg.get("show_tray_icon"),
        "position_mode": cfg.get("position_mode"),
        "taskbar_ring_enabled": cfg.get("taskbar_ring_enabled", False),
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="外设电量圆环")
    parser.add_argument("--debug", action="store_true", help="前台输出调试信息")
    parser.add_argument("--demo", action="store_true", help="假数据驱动 UI")
    parser.add_argument("--once", action="store_true", help="打印一次读数后退出")
    parser.add_argument("--quit", action="store_true", help="让运行中的实例退出")
    parser.add_argument("--status", action="store_true", help="打印状态")
    parser.add_argument("--panel", action="store_true", help="打开设置面板")
    args = parser.parse_args(argv)

    if args.once:
        return do_once(args.debug)
    if args.quit:
        return do_quit()
    if args.status:
        return do_status()

    if args.panel:
        panel = os.path.join(BASE_DIR, "ui", "panel.py")
        if not os.path.exists(panel):
            print("ui/panel.py 不存在")
            return 1
        subprocess.Popen([pythonw_path(), panel], cwd=BASE_DIR)
        return 0

    # 双击 .py 启动时藏掉自带控制台（--debug 保留）
    if not args.debug:
        hide_own_console()

    # 单实例
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if already:
        hwnd = user32.FindWindowW(WND_CLASS, None)
        if hwnd:
            if args.debug:
                print("已经在运行了。")
            return 0
        # 没有窗口但锁还在：上一次异常退出留下的僵尸锁，继续启动
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    app = App(debug=args.debug, demo=args.demo)
    try:
        return app.run()
    except KeyboardInterrupt:
        app.cleanup()
        return 0
    except Exception:
        traceback.print_exc()
        app.cleanup()
        return 1
    finally:
        try:
            if mutex:
                kernel32.CloseHandle(ctypes.c_void_p(mutex))
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
