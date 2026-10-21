# -*- coding: utf-8 -*-
"""ui/overlay.py —— 任务栏电量圆环叠加窗口（CONTRACT §5.2，最终需求版）。

* 窗口：``WS_POPUP`` + ``WS_EX_LAYERED|TOPMOST|TOOLWINDOW|NOACTIVATE``，
  像素来自 :class:`ui.render.RingSurface`，用 ``UpdateLayeredWindow`` 上屏；
* **显示的设备 = 用户在面板里勾选的设备（数量不限）**：每台勾选设备一个圆环并排；
  ``cfg['device_hidden']`` 是不显示的 device_id 列表（空列表 = 全部显示，
  兼容旧面板键 ``device_disabled``）；``cfg['device_order']`` 决定顺序，
  其余按发现顺序；防御上限 ``render.MAX_RINGS``（超出时优先保留低电量设备）；
* 没有可见设备时显示 1 个灰色虚线环 + "—"（窗口仍在，不崩）；
* 位置：TrafficMonitor 任务栏窗口（``#32770`` + ``TrafficMonitorTaskbarWindow``）
  左侧 6 逻辑 px、垂直居中；没有则 ``TrayNotifyWnd`` 左侧 6 px；``offset_x/y`` 叠加
  （Alt+左键拖动后回调 ``on_position_changed(dx, dy)``）；
* 交互：左键 / 双击 / 右键 → ``callbacks`` 对应回调；不抢焦点；
* 动画：仅充电 / 数值变化时开 ``SetTimer(16ms)``（~30fps 真正绘制），静止时 **0 定时器**；
* 任务栏自身不可见（Explorer 未启动 / 自动隐藏）时隐藏；``TaskbarCreated``、
  DPI/显示设置变化后重算并重建。

注意：本类**不跑消息循环**，消息由 main.py 的消息循环分发到本窗口的 WndProc
（``ui.win32.LayeredWindow`` 内部按 HWND 统一分发）。
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Optional

try:                                    # 作为包导入
    from . import render, win32
except ImportError:                     # 直接运行 ui/_demo_overlay.py 时的兜底
    import render, win32              # type: ignore

try:                                    # 数据契约（CONTRACT §3）
    from models import Reading
except Exception:                       # pragma: no cover - 仅在 models.py 缺失时
    Reading = Any                       # type: ignore

WM_TIMER = win32.WM_TIMER
WM_MOUSEMOVE = win32.WM_MOUSEMOVE
WM_LBUTTONDOWN = win32.WM_LBUTTONDOWN
WM_LBUTTONUP = win32.WM_LBUTTONUP
WM_LBUTTONDBLCLK = win32.WM_LBUTTONDBLCLK
WM_RBUTTONUP = win32.WM_RBUTTONUP
WM_MOUSEACTIVATE = win32.WM_MOUSEACTIVATE
WM_DPICHANGED = win32.WM_DPICHANGED
WM_SETTINGCHANGE = win32.WM_SETTINGCHANGE
WM_DISPLAYCHANGE = win32.WM_DISPLAYCHANGE

TIMER_ANIM = 1          # 16ms 定时器：充电扫光 / 数值过渡（真正绘制约 30fps）
ANIM_INTERVAL = 16
INTERP_SECONDS = 0.5    # 数值切换过渡时长


def _device_id(reading: Any) -> str:
    return str(getattr(reading, "device_id", "") or "")


class TaskbarOverlay:
    """任务栏上的多环叠加窗口（样式 B，无底板，每台勾选设备一个环）。"""

    WND_CLASS = "PeripheralBatteryRingOverlay"

    def __init__(self, cfg: dict, callbacks: Optional[dict] = None) -> None:
        self.cfg = dict(cfg or {})
        self.callbacks = dict(callbacks or {})
        self._window: Optional[win32.LayeredWindow] = None
        self._surface: Optional[render.RingSurface] = None
        self._readings: list[Reading] = []
        self._prev_levels: dict[str, Optional[int]] = {}
        self._interp: dict[str, tuple[float, float, float]] = {}

        self._dpi = win32.get_dpi_scale()
        self._width = 0
        self._height = 0
        self._x = 0
        self._y = 0
        self._base_x = 0            # 不含 offset 的自动位置（拖动时算偏移用）
        self._base_y = 0
        self._count = 0             # 当前绘制的圆环数（0 = 占位环）

        self._visible = False
        self._hidden_reason = ""
        self._last_parse: tuple = ()
        self._last_theme: Optional[str] = None

        self._anim_start = win32.now()
        self._anim_until = 0.0
        self._timer_anim = False
        self._last_paint = 0.0

        self._drag: Optional[dict] = None

        try:
            self._taskbar_msg = int(
                win32.RegisterWindowMessageW("TaskbarCreated") or 0)
        except Exception:
            self._taskbar_msg = 0

    # ------------------------------------------------------------------
    # 对外接口（CONTRACT §5.2）
    # ------------------------------------------------------------------
    @property
    def hwnd(self) -> int:
        return self._window.hwnd if self._window is not None else 0

    @property
    def rect(self) -> Optional[tuple[int, int, int, int]]:
        """当前圆环组的屏幕矩形 (l, t, r, b)；隐藏或未定位时为 None。"""
        if not self._visible or not self._width:
            return None
        return (self._x, self._y, self._x + self._width,
                self._y + self._height)

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def placement(self) -> str:
        """当前落点：``taskbar`` / ``hidden``。"""
        return "taskbar" if self._visible else "hidden"

    @property
    def visible_device_ids(self) -> list[str]:
        """当前实际显示的 device_id 列表（占位环时为 []）。"""
        return [_device_id(r) for r in self._capped_readings()]

    def start(self) -> bool:
        """创建窗口并首次定位（不阻塞、不跑消息循环）。"""
        return self._create_window()

    def set_readings(self, readings: list[Reading]) -> None:
        """更新全部设备读数；内部按 device_hidden/device_order 选出要显示的环。"""
        try:
            self._apply_readings(readings)
        except Exception as exc:
            win32.note_error(f"overlay.set_readings 异常: {exc!r}")

    def reload_config(self, cfg: dict) -> None:
        """配置热重载（面板保存后由 main.py 调用）。"""
        try:
            self.cfg = dict(cfg or {})
            self._last_parse = ()
            self._last_theme = None
            self._drop_hidden_interp()
            self._layout_size(force=True)
            self._render()
            if self._visible:
                self._present()
            self.reposition()
        except Exception as exc:
            win32.note_error(f"overlay.reload_config 异常: {exc!r}")

    def reposition(self) -> None:
        """幂等重算位置/可见性（main.py 每 2s 调一次；自己也可以调）。"""
        try:
            self._reposition_once()
        except Exception as exc:
            win32.note_error(f"overlay.reposition 异常: {exc!r}")

    def destroy(self) -> None:
        """销毁窗口、定时器与 GDI+ 资源。"""
        self._kill_timer()
        if self._surface is not None:
            try:
                self._surface.close()
            except Exception:
                pass
            self._surface = None
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        self._window = None
        self._visible = False

    # ------------------------------------------------------------------
    # 窗口创建
    # ------------------------------------------------------------------
    def _create_window(self) -> bool:
        if self._window is not None and self._window.hwnd:
            return True
        win32.set_dpi_awareness()
        tray = win32.find_taskbar()
        self._dpi = win32.get_dpi_scale(tray)
        self._layout_size(force=True)
        window = win32.LayeredWindow(self.WND_CLASS,
                                     "PeripheralBatteryRingOverlay")
        if not window.create(0, 0, max(1, self._width), max(1, self._height)):
            win32.note_error("圆环叠加窗口创建失败")
            self._window = window
            return False
        window.message_handler = self._on_message
        self._window = window
        self._surface = render.RingSurface(self._width, self._height)
        self._last_parse = ()
        self._render()
        self.reposition()
        return True

    def _recreate_window(self) -> None:
        """TaskbarCreated / 显示设置变化后重建窗口（重新抢 TOPMOST 层序）。"""
        self._kill_timer()
        if self._surface is not None:
            try:
                self._surface.close()
            except Exception:
                pass
            self._surface = None
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
        self._window = None
        self._visible = False
        self._last_parse = ()
        self._create_window()

    # ------------------------------------------------------------------
    # 设备筛选 / 排序（需求：按 device_hidden + device_order）
    # ------------------------------------------------------------------
    def _hidden_ids(self) -> set[str]:
        hidden = self.cfg.get("device_hidden")
        if hidden is None:                  # 兼容旧面板写的 device_disabled
            hidden = self.cfg.get("device_disabled")
        try:
            return {str(item) for item in (hidden or [])}
        except TypeError:
            return set()

    def _visible_readings(self) -> list[Reading]:
        """过滤 device_hidden → 按 device_order 优先、其余按发现顺序。"""
        hidden = self._hidden_ids()
        visible: list[Reading] = []
        for reading in self._readings:
            if _device_id(reading) in hidden:
                continue
            visible.append(reading)
        try:
            order = [str(item) for item in (self.cfg.get("device_order") or [])]
        except TypeError:
            order = []
        positions = {device_id: index
                     for index, device_id in enumerate(order)}
        indexed = list(enumerate(visible))
        indexed.sort(key=lambda item: (
            (0, positions[_device_id(item[1])])
            if _device_id(item[1]) in positions else (1, item[0])))
        return [reading for _index, reading in indexed]

    @staticmethod
    def _level_rank(reading: Reading) -> int:
        level = getattr(reading, "level", None)
        return 999 if level is None else int(level)

    def _capped_readings(self) -> list[Reading]:
        """防御上限：最多 MAX_RINGS 个 / 屏幕宽度内；超出时优先低电量设备。"""
        visible = self._visible_readings()
        if not visible:
            return []
        ring_px, _lw, gap, _pad = render.ring_metrics(self.cfg, self._dpi)
        cap = int(render.MAX_RINGS)
        mon = win32.monitor_rect()
        if mon is not None and ring_px + gap > 0:
            max_by_width = max(1, (mon[2] - mon[0] - 16 + gap)
                               // (ring_px + gap))
            cap = min(cap, max_by_width)
        if len(visible) <= cap:
            return visible
        keep_ids = {_device_id(r)
                    for r in sorted(visible, key=self._level_rank)[:cap]}
        return [r for r in visible if _device_id(r) in keep_ids]

    def _display_readings(self) -> list[Reading]:
        """返回要绘制的读数列表（可能带数值过渡中的临时副本）。"""
        readings = self._capped_readings()
        if not readings:
            return []
        now = win32.now()
        out: list[Reading] = []
        for reading in readings:
            device_id = _device_id(reading)
            item = self._interp.get(device_id)
            if item is None:
                out.append(reading)
                continue
            start_level, end_level, t0 = item
            progress = (now - t0) / INTERP_SECONDS
            if progress >= 1.0:
                out.append(reading)
                continue
            eased = progress * progress * (3.0 - 2.0 * progress)
            value = int(round(start_level + (end_level - start_level) * eased))
            try:
                out.append(dataclasses.replace(
                    reading, level=max(0, min(100, value))))
            except Exception:
                out.append(reading)
        # 清掉已结束的过渡
        for device_id, item in list(self._interp.items()):
            if now - item[2] >= INTERP_SECONDS:
                del self._interp[device_id]
        return out

    def _drop_hidden_interp(self) -> None:
        """设备被隐藏/移除后清掉它的过渡动画。"""
        known = {_device_id(r) for r in self._readings}
        for device_id in list(self._interp.keys()):
            if device_id not in known:
                del self._interp[device_id]

    # ------------------------------------------------------------------
    # 尺寸 / 渲染
    # ------------------------------------------------------------------
    def _layout_size(self, force: bool = False) -> None:
        show_names = bool(self.cfg.get("show_names", False))
        count = max(1, len(self._capped_readings()))
        width, height = render.widget_size(self.cfg, self._dpi, show_names,
                                           count)
        self._count = count
        if force or (width, height) != (self._width, self._height):
            self._width, self._height = width, height
            if self._surface is not None:
                self._surface.resize(width, height)

    def _render(self) -> None:
        if self._surface is None:
            return
        try:
            anim_t = win32.now() - self._anim_start
            self._surface.render_rings(self._display_readings(), self.cfg,
                                       self._dpi, anim_t)
        except Exception as exc:
            win32.note_error(f"overlay 渲染异常: {exc!r}")

    def _present(self) -> bool:
        """把当前 surface 贴到窗口（必要时显示窗口）。"""
        if self._window is None or self._surface is None:
            return False
        if not self._window.hwnd:
            return False
        ok = self._window.present(self._surface, self._x, self._y)
        if ok:
            self._visible = True
        return ok

    def _hide(self, reason: str = "") -> None:
        if self._window is not None and self._window.hwnd and self._visible:
            self._window.hide()
        self._visible = False
        self._hidden_reason = reason
        self._sync_timer()

    # ------------------------------------------------------------------
    # 位置 / 可见性
    # ------------------------------------------------------------------
    def _taskbar_widget_rects(self, tray: int) -> tuple[Optional[tuple], Optional[tuple]]:
        """TrafficMonitor 任务栏窗口、TrayNotifyWnd 的屏幕矩形。"""
        tm = win32.find_trafficmonitor_taskbar_window(tray)
        tm_rect = win32.get_window_rect(tm) if tm else None
        notify_rect = None
        if tm_rect is None:
            notify = win32.find_tray_notify(tray)
            notify_rect = win32.get_window_rect(notify) if notify else None
        return tm_rect, notify_rect

    def _compute_position(self, state: dict, tm_rect: Optional[tuple],
                          notify_rect: Optional[tuple]) -> tuple[int, int]:
        """圆环组右缘 = 锚点窗口左侧 − 6 逻辑px，任务栏垂直居中（+ offset）。"""
        scale = float(self._dpi or 1.0)
        left, top, right, bottom = state["rect"]
        mon = state["monitor"]
        gap = int(round(6 * scale))
        horizontal = bool(state.get("horizontal", True))
        if horizontal:
            if tm_rect is not None:
                anchor_left = tm_rect[0]
            elif notify_rect is not None:
                anchor_left = notify_rect[0]
            else:
                anchor_left = mon[2] - int(round(320 * scale))
            x = anchor_left - gap - self._width
            y = top + (bottom - top - self._height) // 2
        else:
            # 垂直任务栏（少见）：放到任务栏内侧靠右，垂直居中
            x = right - self._width - gap
            y = top + (bottom - top - self._height) // 2
        self._base_x, self._base_y = x, y
        try:
            offset_x = int(self.cfg.get("offset_x", 0) or 0)
            offset_y = int(self.cfg.get("offset_y", 0) or 0)
        except (TypeError, ValueError):
            offset_x = offset_y = 0
        x += offset_x
        y += offset_y
        # 夹紧到显示器范围（留 2px 余量）
        x = max(mon[0] + 2, min(x, mon[2] - self._width - 2))
        y = max(mon[1] + 2, min(y, mon[3] - self._height - 2))
        return x, y

    def _reposition_once(self) -> None:
        if self._window is None or not self._window.hwnd:
            return
        state = win32.taskbar_state()
        if state is None:
            self._hide("no_taskbar")
            return
        tray = state["hwnd"]
        new_dpi = win32.get_dpi_scale(tray)
        if abs(new_dpi - self._dpi) > 1e-3:
            self._dpi = new_dpi
            self._layout_size(force=True)
            self._render()
            self._last_theme = None
        if state.get("hidden"):
            # 任务栏本身不可见（自动隐藏 / Explorer 重启中）：隐藏，别浮在空中
            self._hide("taskbar_hidden")
            return

        tm_rect, notify_rect = self._taskbar_widget_rects(tray)
        self._last_parse = (tuple(state["rect"]), tm_rect, notify_rect,
                            bool(state.get("horizontal", True)))

        theme = render.resolve_theme(self.cfg)
        if theme != self._last_theme:
            self._last_theme = theme
            self._render()
            if self._visible:
                self._present()

        x, y = self._compute_position(state, tm_rect, notify_rect)
        moved = (x != self._x) or (y != self._y)
        self._x, self._y = x, y

        if not self._visible:
            self._present()
        elif moved:
            self._window.move(x, y)
        self._sync_timer()

    # ------------------------------------------------------------------
    # 定时器 / 动画
    # ------------------------------------------------------------------
    def _any_charging(self) -> bool:
        for reading in self._capped_readings():
            level = getattr(reading, "level", None)
            if level is None:
                continue
            status = str(getattr(reading, "status", "") or "")
            charging = getattr(reading, "charging", None)
            if status == "charging":
                return True
            if charging is True and status != "full":
                return True
        return False

    def _sync_timer(self) -> None:
        now = win32.now()
        wanted = bool(self._visible) and (
            self._any_charging() or bool(self._interp)
            or now < self._anim_until)
        if wanted == self._timer_anim:
            return
        if self._window is None or not self._window.hwnd:
            return
        if wanted:
            win32.SetTimer(self._window.hwnd, TIMER_ANIM, ANIM_INTERVAL, None)
        else:
            win32.KillTimer(self._window.hwnd, TIMER_ANIM)
        self._timer_anim = wanted

    def _kill_timer(self) -> None:
        if self._window is not None and self._window.hwnd:
            try:
                win32.KillTimer(self._window.hwnd, TIMER_ANIM)
            except Exception:
                pass
        self._timer_anim = False

    def _on_timer_tick(self) -> None:
        if not self._visible or self._window is None or not self._window.hwnd:
            self._sync_timer()
            return
        now = win32.now()
        if now - self._last_paint < 0.030:
            # 16ms 定时器只按 ~30fps 真正绘制（充电扫光 30fps），省 CPU
            return
        self._last_paint = now
        self._render()
        self._present()
        self._sync_timer()

    # ------------------------------------------------------------------
    # 鼠标 / 回调
    # ------------------------------------------------------------------
    def _fire(self, name: str, *args: Any) -> None:
        callback = self.callbacks.get(name)
        if not callable(callback):
            return
        try:
            callback(*args)
        except Exception as exc:
            win32.note_error(f"overlay 回调 {name} 异常: {exc!r}")

    def _on_left_down(self) -> int:
        if win32.key_down(win32.VK_MENU):
            # Alt+左键拖动：更新 offset 并回调 main.py 保存
            self._drag = {
                "cursor": win32.get_cursor_pos(),
                "window": (self._x, self._y),
                "moved": False,
            }
            win32.SetCapture(self.hwnd)
        return 0

    def _on_mouse_move(self) -> None:
        if self._drag is None:
            return
        cursor = win32.get_cursor_pos()
        start_cursor, start_window = self._drag["cursor"], self._drag["window"]
        dx = cursor[0] - start_cursor[0]
        dy = cursor[1] - start_cursor[1]
        self._x = start_window[0] + dx
        self._y = start_window[1] + dy
        if self._window is not None:
            self._window.move(self._x, self._y)
        if abs(dx) + abs(dy) > 2:
            self._drag["moved"] = True

    def _on_left_up(self) -> int:
        if self._drag is not None:
            drag = self._drag
            self._drag = None
            win32.ReleaseCapture()
            if drag.get("moved"):
                offset_x = self._x - self._base_x
                offset_y = self._y - self._base_y
                self.cfg["offset_x"] = int(offset_x)
                self.cfg["offset_y"] = int(offset_y)
                self._fire("on_position_changed", int(offset_x), int(offset_y))
                self.reposition()
            return 0
        self._fire("on_left_click")
        return 0

    def _on_message(self, msg: int, wparam: int, lparam: int) -> Optional[int]:
        if msg == win32.WM_MOUSEMOVE:
            self._on_mouse_move()
            return 0
        if msg == WM_LBUTTONDOWN:
            return self._on_left_down()
        if msg == WM_LBUTTONUP:
            return self._on_left_up()
        if msg == WM_LBUTTONDBLCLK:
            self._fire("on_double_click")
            return 0
        if msg == WM_RBUTTONUP:
            self._fire("on_right_click")
            return 0
        if msg == WM_MOUSEACTIVATE:
            return win32.MA_NOACTIVATE
        if msg == WM_TIMER:
            self._on_timer_tick()
            return 0
        if self._taskbar_msg and msg == self._taskbar_msg:
            self._recreate_window()
            return 0
        if msg in (WM_DPICHANGED, WM_SETTINGCHANGE, WM_DISPLAYCHANGE):
            self.reposition()
            return 0
        return None

    # ------------------------------------------------------------------
    # 读数
    # ------------------------------------------------------------------
    def _apply_readings(self, readings: list[Reading]) -> None:
        self._readings = list(readings or [])
        now = win32.now()
        new_levels: dict[str, Optional[int]] = {}
        new_interp: dict[str, tuple[float, float, float]] = {}
        for reading in self._readings:
            device_id = _device_id(reading)
            level = getattr(reading, "level", None)
            new_levels[device_id] = level
            previous = self._prev_levels.get(device_id)
            if (previous is not None and level is not None
                    and int(previous) != int(level)):
                new_interp[device_id] = (float(previous), float(level), now)
        self._prev_levels = new_levels
        self._interp = new_interp
        self._drop_hidden_interp()
        if new_interp:
            self._anim_until = max(self._anim_until, now + INTERP_SECONDS)
        self._layout_size()
        self._render()
        if self._visible:
            self._present()
        self._sync_timer()
