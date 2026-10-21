# -*- coding: utf-8 -*-
"""ui/tray.py —— 通知区域图标（每台勾选设备一个）+ 原生右键菜单。

纯 ctypes，零第三方依赖。

* 每个"在状态栏显示"的设备对应一个托盘圆环图标（单环：绿/黄/红/灰，充电有高亮）；
* 图标 tooltip 显示设备名、电量、状态、更新时间；
* 左键：打开主窗口；右键：原生菜单（主界面 / 立即刷新 / 显示图标 / 开机自启 / 固定图标设置 / 退出）；
* ``show_tray_icon=False``：全部 NIM_DELETE；恢复时 NIM_ADD；
* 不写日志、不联网；图标临时文件仅放 %TEMP%\\PeripheralBattery_icons。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from ctypes import wintypes
from typing import Any, Optional

from models import Reading

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_NULL = 0x0000

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80
NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512

CMD_OPEN = 1
CMD_REFRESH = 2
CMD_TOGGLE_TRAY = 3
CMD_TOGGLE_AUTOSTART = 4
CMD_TRAY_SETTINGS = 5
CMD_QUIT = 6

UID_PLACEHOLDER = 1


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t,
                                ctypes.c_ssize_t]


def _device_id(reading: Any) -> str:
    return str(getattr(reading, "device_id", "") or "")


def _tip_for(reading: Optional[Reading]) -> str:
    """托盘图标悬停提示：设备名 + 电量 + 当前状态（不含相对时间，保持稳定）。"""
    if reading is None:
        return "外设电量圆环：没有发现可读设备"
    name = str(getattr(reading, "name", "") or "设备")
    level = getattr(reading, "level", None)
    lvl = "—" if level is None else (
        ("≈" if getattr(reading, "estimated", False) else "") + f"{level}%")
    status = str(getattr(reading, "status_text", "") or "")
    charging = bool(getattr(reading, "charging", False)) or status == "充电中"
    text = f"{name}：{lvl} · {status}"
    if charging:
        text += "（充电中）" if "充电" not in status else ""
    return text[:120]


class TrayIcon:
    """通知区域多图标管理（一台设备一个）。"""

    def __init__(self, hwnd: int, callback_msg: int, cfg: dict, callbacks: dict):
        self.hwnd = hwnd
        self.callback_msg = callback_msg
        self.cfg = dict(cfg)
        self.callbacks = callbacks or {}
        self._ico_dir = os.path.join(tempfile.gettempdir(), "PeripheralBattery_icons")
        self._readings: list[Reading] = []
        self._visible = bool(cfg.get("show_tray_icon", True))
        self._icons: dict[int, dict] = {}
        self._dev_uid: dict[str, int] = {}
        self._next_uid = 10

    # -- 基础 ----------------------------------------------------------------
    def _nid(self, uid: int, flags: int = 0) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = uid
        nid.uFlags = flags
        nid.uCallbackMessage = self.callback_msg
        return nid

    def _ico_path(self, key: tuple) -> str:
        digest = hashlib.md5(repr(key).encode("utf-8", "replace")).hexdigest()[:12]
        return os.path.join(self._ico_dir, f"tray_{digest}.ico")

    def _load_icon(self, reading: Optional[Reading], key: tuple) -> int:
        ico_bytes = b""
        try:
            from ui.render import build_tray_ico
            data = [reading] if reading is not None else []
            ico_bytes = build_tray_ico(data, self.cfg, count=1)
        except Exception:
            ico_bytes = b""
        if ico_bytes:
            try:
                os.makedirs(self._ico_dir, exist_ok=True)
                path = self._ico_path(key)
                with open(path, "wb") as f:
                    f.write(ico_bytes)
                h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
                if h:
                    return int(h)
            except Exception:
                pass
        h = user32.LoadImageW(None, ctypes.c_void_p(IDI_APPLICATION), IMAGE_ICON,
                              0, 0, 0)
        return int(h) if h else 0

    @staticmethod
    def _icon_key(reading: Optional[Reading]) -> tuple:
        if reading is None:
            return ("empty",)
        return (getattr(reading, "level", None),
                str(getattr(reading, "status", "") or ""),
                bool(getattr(reading, "estimated", False)),
                bool(getattr(reading, "charging", None)))

    def _wanted(self, readings: list[Reading]) -> list[Reading]:
        hidden = {str(item) for item in (self.cfg.get("device_hidden") or [])}
        order = [str(x) for x in (self.cfg.get("device_order") or [])]
        pos = {did: i for i, did in enumerate(order)}
        visible = [r for r in (readings or []) if _device_id(r) not in hidden]
        indexed = list(enumerate(visible))
        indexed.sort(key=lambda it: ((0, pos[_device_id(it[1])])
                                     if _device_id(it[1]) in pos else (1, it[0])))
        return [r for _i, r in indexed][:8]

    # -- 增删改 --------------------------------------------------------------
    def _add_icon(self, uid: int, reading: Optional[Reading], dev: str) -> None:
        key = self._icon_key(reading)
        hicon = self._load_icon(reading, key)
        nid = self._nid(uid, NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.hIcon = ctypes.c_void_p(hicon)
        nid.szTip = _tip_for(reading)
        ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if ok:
            pass  # v4 tooltip 实验：暂时不设版本
        old = self._icons.get(uid)
        if old and old.get("hicon") and old["hicon"] != hicon:
            try:
                user32.DestroyIcon(ctypes.c_void_p(old["hicon"]))
            except Exception:
                pass
        self._icons[uid] = {"dev": dev, "added": ok, "hicon": hicon,
                            "key": key, "tip": nid.szTip, "reading": reading}

    def _modify_icon(self, uid: int, reading: Optional[Reading], dev: str) -> None:
        info = self._icons.get(uid)
        if not info:
            self._add_icon(uid, reading, dev)
            return
        key = self._icon_key(reading)
        tip = _tip_for(reading)
        if key == info["key"] and tip == info["tip"] and info.get("added"):
            return
        hicon = info.get("hicon") or 0
        if key != info["key"]:
            hicon = self._load_icon(reading, key)
            old = info.get("hicon")
            if old and old != hicon:
                try:
                    user32.DestroyIcon(ctypes.c_void_p(old))
                except Exception:
                    pass
        nid = self._nid(uid, NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.hIcon = ctypes.c_void_p(hicon)
        nid.szTip = tip
        if not info.get("added"):
            self._add_icon(uid, reading, dev)
            return
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        info.update({"hicon": hicon, "key": key, "tip": tip, "reading": reading,
                     "dev": dev})

    def _delete_icon(self, uid: int) -> None:
        info = self._icons.pop(uid, None)
        if not info:
            return
        try:
            nid = self._nid(uid, 0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        if info.get("hicon"):
            try:
                user32.DestroyIcon(ctypes.c_void_p(info["hicon"]))
            except Exception:
                pass

    def _sync(self, readings: list[Reading]) -> None:
        self._readings = list(readings or [])
        wanted = self._wanted(self._readings) if self._visible else []
        keep_uids: set[int] = set()
        if wanted:
            for reading in wanted:
                dev = _device_id(reading)
                uid = self._dev_uid.get(dev)
                if uid is None:
                    uid = self._next_uid
                    self._next_uid += 1
                    self._dev_uid[dev] = uid
                keep_uids.add(uid)
                if uid in self._icons:
                    self._modify_icon(uid, reading, dev)
                else:
                    self._add_icon(uid, reading, dev)
        elif self._visible:
            # 没有设备：显示一个灰色占位图标，告诉用户程序在跑
            keep_uids.add(UID_PLACEHOLDER)
            if UID_PLACEHOLDER in self._icons:
                self._modify_icon(UID_PLACEHOLDER, None, "")
            else:
                self._add_icon(UID_PLACEHOLDER, None, "")
        for uid in list(self._icons.keys()):
            if uid not in keep_uids:
                self._delete_icon(uid)

    # -- 对外 ----------------------------------------------------------------
    def add(self) -> bool:
        self._visible = bool(self.cfg.get("show_tray_icon", True))
        if not self._visible:
            return False
        self._sync(self._readings)
        return any(info.get("added") for info in self._icons.values())

    def update(self, readings: list[Reading]) -> None:
        self._sync(list(readings or []))

    def sync(self, readings: list[Reading]) -> None:
        self._sync(list(readings or []))

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        if not self._visible:
            for uid in list(self._icons.keys()):
                self._delete_icon(uid)
        else:
            self._sync(self._readings)

    def set_config(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        if not bool(self.cfg.get("show_tray_icon", True)):
            self.set_visible(False)
        else:
            self._visible = True
            self._sync(self._readings)

    def remove(self) -> None:
        for uid in list(self._icons.keys()):
            self._delete_icon(uid)
        self._dev_uid.clear()

    # -- 菜单 ----------------------------------------------------------------
    def show_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, CMD_OPEN, "打开主界面")
            user32.AppendMenuW(hmenu, MF_STRING, CMD_REFRESH, "立即刷新")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            tray_on = bool(self.cfg.get("show_tray_icon", True))
            user32.AppendMenuW(hmenu, MF_STRING | (MF_CHECKED if tray_on else 0),
                               CMD_TOGGLE_TRAY, "显示通知区域图标")
            try:
                auto = bool(self.callbacks.get("is_autostart", lambda: False)())
            except Exception:
                auto = False
            user32.AppendMenuW(hmenu, MF_STRING | (MF_CHECKED if auto else 0),
                               CMD_TOGGLE_AUTOSTART, "开机自启")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_TRAY_SETTINGS,
                               "把图标固定到任务栏…")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_QUIT, "退出")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(ctypes.c_void_p(self.hwnd))
            cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON |
                                        TPM_NONOTIFY, pt.x, pt.y, 0,
                                        ctypes.c_void_p(self.hwnd), None)
            user32.PostMessageW(ctypes.c_void_p(self.hwnd), WM_NULL, 0, 0)
            if cmd:
                self._dispatch(int(cmd))
        finally:
            user32.DestroyMenu(ctypes.c_void_p(hmenu))

    def _dispatch(self, cmd: int) -> None:
        def call(name: str) -> None:
            fn = self.callbacks.get(name)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        if cmd == CMD_OPEN:
            call("open_panel")
        elif cmd == CMD_REFRESH:
            call("refresh")
        elif cmd == CMD_TOGGLE_TRAY:
            call("toggle_tray")
        elif cmd == CMD_TOGGLE_AUTOSTART:
            call("toggle_autostart")
        elif cmd == CMD_TRAY_SETTINGS:
            try:
                os.startfile("ms-settings:taskbar")
            except Exception:
                pass
        elif cmd == CMD_QUIT:
            call("quit")
