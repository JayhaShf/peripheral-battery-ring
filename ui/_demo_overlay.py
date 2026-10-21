# -*- coding: utf-8 -*-
"""ui/_demo_overlay.py —— 圆环视觉层独立自测脚本（假数据，不依赖设备层 / main.py）。

最终需求版：状态栏按"勾选的设备"显示，**每台勾选设备一个圆环并排**（1 台=单环，
2 台=双环，防御上限 8）；无悬停卡片、无 HUD、无鼠标穿透/全屏隐藏。

它只验证 ui 三件套：GDI+ 渲染 → 分层窗口 → 任务栏定位 + 动画，不读写设备、
不联网、不写日志文件。

用法（在仓库根目录 D:\\2.4P 执行）::

    S:\\AnConda\\python.exe ui/_demo_overlay.py
    S:\\AnConda\\python.exe ui/_demo_overlay.py --seconds 10 --shot docs/selfcheck-overlay.png
    S:\\AnConda\\python.exe ui/_demo_overlay.py --theme light --verbose

参数：
    --seconds  演示时长（默认 6 秒）
    --theme    dark | light | system
    --shot     截屏保存 PNG（GDI+ 编码器，不依赖第三方库）
    --offset-x / --offset-y  叠加偏移（物理像素）
    --verbose  打印更多状态

输出：
    * 可见设备列表 / 圆环数 / 窗口矩形 / DPI / 环径 / 任务栏状态；
    * 用 PostMessage 模拟左键/右键/双击，验证 WndProc → callbacks 接线；
    * device_hidden 隐藏设备（2 环 → 1 环）、device_order 顺序、全部隐藏（占位灰环）；
    * **充电扫光期间 CPU** 与 **切到静态读数后 CPU**（应≈0，且 0 定时器）；
    * 可选截图。

注意：本脚本自己跑一个 PeekMessage 循环（main.py 里是 Lead 的 GetMessage 循环），
用完会自动退出，不会留下常驻进程。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from ui import overlay as overlay_mod      # noqa: E402
from ui import render, win32               # noqa: E402

try:                                        # 用真实数据契约（models.Reading）
    from models import Reading
except Exception:                           # pragma: no cover - 独立演示兜底
    from dataclasses import dataclass, field

    @dataclass
    class Reading:                          # type: ignore[no-redef]
        device_id: str
        name: str
        level: int | None = None
        status: str = "unknown"
        voltage_mv: int | None = None
        source: str = ""
        updated_at: float = field(default_factory=time.time)
        detail: str = ""
        estimated: bool = False
        charging: bool | None = None
        inferred: bool = False


def _demo_readings(level_logi: int = 77) -> list["Reading"]:
    return [
        Reading(device_id="demo:rog", name="ROG 夜魔X", level=62,
                status="discharging", source="demo"),
        Reading(device_id="demo:logi", name="G PRO Wireless",
                level=level_logi, status="charging", charging=True,
                estimated=True, voltage_mv=3963, source="demo"),
    ]


def _pump_messages() -> None:
    """处理本线程窗口消息（等价于 main.py 的主循环体）。"""
    from ctypes import wintypes
    msg = wintypes.MSG()
    while win32.PeekMessageW(msg, None, 0, 0, win32.PM_REMOVE):
        win32.TranslateMessage(msg)
        win32.DispatchMessageW(msg)


def _load_cfg(args: argparse.Namespace) -> dict:
    try:
        import config as appcfg
        cfg = appcfg.load()
    except Exception:
        cfg = {}
    cfg["position_mode"] = "taskbar"
    cfg["theme"] = args.theme
    cfg["offset_x"] = int(args.offset_x)
    cfg["offset_y"] = int(args.offset_y)
    cfg["device_hidden"] = []
    cfg["device_order"] = []
    cfg.setdefault("ring_size", 30)
    cfg.setdefault("show_names", False)
    return cfg


def _take_shot(path: str, overlay_rect: tuple[int, int, int, int] | None,
               tray_rect: tuple[int, int, int, int]) -> str:
    """截取"任务栏 + 圆环 + 邻近上下文"区域并保存 PNG。"""
    if not overlay_rect:
        overlay_rect = (tray_rect[2] - 200, tray_rect[1] - 40,
                        tray_rect[2] - 120, tray_rect[1])
    scale = win32.get_dpi_scale(win32.find_taskbar())
    left = int(overlay_rect[0] - 260 * scale)
    right = int(overlay_rect[2] + 120 * scale)
    top = int(tray_rect[1] - 110 * scale)
    bottom = int(tray_rect[3])
    mon = win32.monitor_rect() or (0, 0, right, bottom)
    left = max(mon[0], left)
    top = max(mon[1], top)
    right = min(mon[2], right)
    bottom = min(mon[3], bottom)
    if right - left < 10 or bottom - top < 10:
        return ""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ok = win32.capture_screen_png(left, top, right - left, bottom - top, path)
    return path if ok else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务栏圆环视觉层自测")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--theme", default="dark",
                        choices=("dark", "light", "system"))
    parser.add_argument("--shot", default="")
    parser.add_argument("--offset-x", type=int, default=0)
    parser.add_argument("--offset-y", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = _load_cfg(args)
    win32.set_dpi_awareness()

    def log(msg: str) -> None:
        print(msg, flush=True)

    callbacks = {
        "on_left_click": lambda: log("[回调] 左键"),
        "on_double_click": lambda: log("[回调] 双击"),
        "on_right_click": lambda: log("[回调] 右键"),
        "on_position_changed": lambda dx, dy: log(f"[回调] 拖动偏移 ({dx},{dy})"),
    }

    ov = overlay_mod.TaskbarOverlay(cfg, callbacks)
    if not ov.start():
        print("FAIL: TaskbarOverlay.start() 返回 False")
        print("底层错误:", win32.errors())
        return 2

    state = win32.taskbar_state() or {}
    tray_rect = state.get("rect") or (0, 0, 0, 0)
    readings = _demo_readings()
    ov.set_readings(readings)
    log(f"窗口 hwnd=0x{ov.hwnd:X}  dpi={ov._dpi:.4f}  "
        f"ring_px/lw/gap/pad={render.ring_metrics(cfg, ov._dpi)}")
    log(f"taskbar_state={state}")
    log(f"任务栏浅色主题={win32.taskbar_is_light()}")
    log(f"可见设备={ov.visible_device_ids} 圆环数={len(ov.visible_device_ids)} "
        f"rect={ov.rect}")

    total = max(1.5, float(args.seconds))
    schedule = [
        (0.07 * total, "simulate"),
        (0.12 * total, "anim_start"),
        (0.28 * total, "value_change"),
        (0.34 * total, "anim_end"),
        (0.40 * total, "shot"),
        (0.50 * total, "order_swap"),
        (0.56 * total, "hide_charging"),
        (0.62 * total, "idle_probe"),
        (0.84 * total, "all_hidden"),
        (0.90 * total, "finish"),
    ]
    start = time.perf_counter()
    anim_cpu_start = 0.0
    anim_cpu_end = 0.0
    anim_seconds = 0.0
    shot_saved = ""

    index = 0
    while True:
        elapsed = time.perf_counter() - start
        _pump_messages()
        while index < len(schedule) and elapsed >= schedule[index][0]:
            name = schedule[index][1]
            index += 1
            if name == "simulate":
                for msg in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP,
                            win32.WM_LBUTTONDBLCLK):
                    win32.PostMessageW(ov.hwnd, msg, 0, 0)
            elif name == "anim_start":
                anim_cpu_start = win32.process_cpu_seconds()
                if args.verbose:
                    log(f"充电扫光开始：timers={ov._timer_anim} "
                        f"visible={ov._visible} rect={ov.rect}")
            elif name == "value_change":
                ov.set_readings(_demo_readings(level_logi=80))
                if args.verbose:
                    log(f"数值变化 77→80：timers={ov._timer_anim} "
                        f"ids={ov.visible_device_ids}")
            elif name == "anim_end":
                anim_cpu_end = win32.process_cpu_seconds()
                anim_seconds = max(0.2, elapsed - 0.12 * total)
            elif name == "shot":
                if args.shot:
                    shot_saved = _take_shot(args.shot, ov.rect, tray_rect)
                    if shot_saved:
                        log(f"截图已保存: {shot_saved}")
            elif name == "order_swap":
                cfg2 = dict(cfg)
                cfg2["device_order"] = ["demo:logi", "demo:rog"]
                ov.reload_config(cfg2)
                log(f"device_order 反转后：ids={ov.visible_device_ids} "
                    f"rect={ov.rect}")
            elif name == "hide_charging":
                # 隐藏充电中的鼠标 → 只剩键盘一个环，动画定时器应停止
                cfg3 = dict(cfg)
                cfg3["device_hidden"] = ["demo:logi"]
                ov.reload_config(cfg3)
                log(f"device_hidden=['demo:logi'] → ids={ov.visible_device_ids} "
                    f"rect={ov.rect} timers={ov._timer_anim}")
            elif name == "idle_probe":
                # 静止探针：窗口内不泵消息（此时不应有定时器），测真实静态开销
                idle_start = win32.process_cpu_seconds()
                idle_seconds = max(0.4, total * 0.22)
                probe_end = time.perf_counter() + idle_seconds
                while time.perf_counter() < probe_end:
                    time.sleep(0.02)
                idle_cpu = win32.process_cpu_seconds() - idle_start
                log(f"静止 CPU ≈ {idle_cpu / idle_seconds * 100.0:.2f}%"
                    f"（{idle_cpu:.4f}s/{idle_seconds:.2f}s，"
                    f"timers={ov._timer_anim}，此窗口内不泵消息）")
            elif name == "all_hidden":
                # 全部隐藏 → 显示 1 个灰色虚线占位环，窗口仍在
                cfg4 = dict(cfg)
                cfg4["device_hidden"] = ["demo:rog", "demo:logi"]
                ov.reload_config(cfg4)
                log(f"全部隐藏 → ids={ov.visible_device_ids} "
                    f"rect={ov.rect} visible={ov._visible}（占位灰环）")
            elif name == "finish":
                break
        if elapsed > total + 1.0:
            break
        time.sleep(0.005)

    if anim_cpu_end > 0:
        anim_cpu = anim_cpu_end - anim_cpu_start
        log(f"充电动画期间 CPU ≈ {anim_cpu / anim_seconds * 100.0:.2f}%"
            f"（单核，{anim_cpu:.4f}s/{anim_seconds:.2f}s，约 30fps 重绘）")

    ov.destroy()
    errors = win32.errors()
    if errors:
        print("底层错误记录：")
        for item in errors[-8:]:
            print("  -", item)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
