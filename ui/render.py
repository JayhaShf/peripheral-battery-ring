# -*- coding: utf-8 -*-
"""ui/render.py —— GDI+ 绘制层（样式 B：无底板、透明背景、单圆环）。

对外接口（CONTRACT §5.1）：

* :class:`RingSurface` —— ``32bpp DIB + 内存 DC`` 的 GDI+ 绘制目标。绘制直接写进
  内部 DIB，``premultiply`` 由本类负责（UpdateLayeredWindow 要求预乘 alpha）。
* :func:`build_tray_ico` —— 生成 16/20/24/32 多尺寸 ICO 字节流（纯标准库光栅化）。

另外给 ``ui/overlay.py`` 用的布局辅助函数：``ring_metrics`` / ``widget_size``。

实现要点：
* GDI+ 扁平 API（``Gdip*``）由 ``ui.win32`` 声明；绘制目标是
  ``PixelFormat32bppARGB`` 的 GDI+ Bitmap，**直接包住 DIB 的像素内存**（零拷贝），
  画完调用 ``GdipFlush`` 再统一预乘 alpha。
* 所有 GDI+ 对象（Pen/Brush/Font/FontFamily/StringFormat）按参数缓存复用，
  避免 30fps 下反复创建/销毁。
* 样式细节见 CONTRACT §5.1 + 需求变更 02:57：**任务栏只画 1 个圆环**、
  环径 = ``ring_size`` 逻辑 px、线宽 = 环径×0.11、环内百分比（≈ 估算 / — 未知，
  字号 ≈ 环径×0.36）、轨道半透明、充电白色亮弧扫动 + 闪电徽标、深/浅色主题描边。
"""

from __future__ import annotations

import ctypes
import math
import struct
from typing import Any, Optional, Sequence

try:                                    # 作为包导入（main.py: from ui import render）
    from . import win32
except ImportError:                     # 直接运行 ui/_demo_overlay.py 时的兜底
    import win32                      # type: ignore

try:                                    # 数据契约（CONTRACT §3）
    from models import Reading
except Exception:                       # pragma: no cover - 仅在 models.py 缺失时
    Reading = Any                       # type: ignore

# ---------------------------------------------------------------------------
# 配色（CONTRACT §5.1）
# ---------------------------------------------------------------------------
COLOR_GREEN = (93, 220, 138)
COLOR_AMBER = (245, 196, 81)
COLOR_RED = (255, 107, 107)
COLOR_GRAY = (150, 156, 166)

# 最终需求：状态栏显示所有勾选设备，每台一个圆环；防御上限 8
MAX_RINGS = 8

# 字体族（按顺序 fallback）
FONT_SEMIBOLD = ("Segoe UI Semibold", "Segoe UI", "Arial")
FONT_UI = ("Segoe UI", "Arial")
FONT_CJK = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")

_gp = win32.gdiplus
_FAMILY_CACHE: dict[tuple, int] = {}
_FONT_CACHE: dict[tuple, int] = {}
_FORMAT_CACHE: dict[tuple, int] = {}
_PEN_CACHE: dict[tuple, int] = {}
_BRUSH_CACHE: dict[int, int] = {}


# ---------------------------------------------------------------------------
# GDI+ 对象缓存
# ---------------------------------------------------------------------------
def _family(names: Sequence[str]) -> int:
    key = tuple(names)
    handle = _FAMILY_CACHE.get(key)
    if handle:
        return handle
    handle = win32.gp_family(key)
    if handle:
        _FAMILY_CACHE[key] = handle
    return handle


def _font(names: Sequence[str], px: float, style: int = 0) -> int:
    px = max(1.0, float(px))
    key = (tuple(names), round(px, 2), style)
    handle = _FONT_CACHE.get(key)
    if handle:
        return handle
    family = _family(names)
    if not family:
        return 0
    handle = win32.gp_font(family, px, style)
    if handle:
        _FONT_CACHE[key] = handle
    return handle


def _format(align: int, line_align: int) -> int:
    key = (align, line_align)
    handle = _FORMAT_CACHE.get(key)
    if handle:
        return handle
    handle = win32.gp_string_format(align, line_align,
                                    win32.GP_STRING_FLAG_NO_WRAP)
    if handle:
        _FORMAT_CACHE[key] = handle
    return handle


def _pen(color: int, width: float, cap: int = 0, dash: int = -1,
         dash_cap: int = 0) -> int:
    width = max(1.0, float(width))
    key = (int(color), round(width, 2), int(cap), int(dash), int(dash_cap))
    handle = _PEN_CACHE.get(key)
    if handle:
        return handle
    handle = win32.gp_pen(color, width, cap, dash)
    if handle and dash_cap:
        _gp.GdipSetPenLineCap197819(handle, cap, cap, dash_cap)
    if handle:
        _PEN_CACHE[key] = handle
    return handle


def _brush(color: int) -> int:
    key = int(color)
    handle = _BRUSH_CACHE.get(key)
    if handle:
        return handle
    handle = win32.gp_brush(color)
    if handle:
        _BRUSH_CACHE[key] = handle
    return handle


def _wlen(text: str) -> int:
    """GDI+ 的字符数按 UTF-16 code unit 计。"""
    return len(text.encode("utf-16-le")) // 2


def _measure(g: int, text: str, font: int, fmt: int) -> tuple[float, float]:
    """用 GDI+ 测量一行文字（像素）。"""
    if not g or not font:
        return (0.0, 0.0)
    rect = win32.RectF(0.0, 0.0, 4096.0, 1024.0)
    box = win32.RectF()
    status = _gp.GdipMeasureString(g, text, _wlen(text), font,
                                   ctypes.byref(rect), fmt,
                                   ctypes.byref(box), None, None)
    if status != win32.GP_OK:
        return (0.0, 0.0)
    return (float(box.Width), float(box.Height))


def _draw_text(g: int, text: str, font: int, fmt: int, x: float, y: float,
               width: float, height: float, color: int,
               outline: int = 0, outline_px: float = 0.0) -> None:
    """绘制一行文字；``outline`` 非 0 时先画一圈描边（提升浅色壁纸可读性）。"""
    if not g or not font:
        return
    n = _wlen(text)
    if outline and outline_px > 0:
        brush = _brush(outline)
        o = float(outline_px)
        for dx, dy in ((-o, 0.0), (o, 0.0), (0.0, -o), (0.0, o)):
            rect = win32.RectF(x + dx, y + dy, width, height)
            _gp.GdipDrawString(g, text, n, font, ctypes.byref(rect), fmt, brush)
    brush = _brush(color)
    rect = win32.RectF(x, y, width, height)
    _gp.GdipDrawString(g, text, n, font, ctypes.byref(rect), fmt, brush)


def _fill_polygon(g: int, points: Sequence[tuple[float, float]], color: int) -> None:
    if not points:
        return
    arr = (win32.PointF * len(points))()
    for i, (px, py) in enumerate(points):
        arr[i].X = float(px)
        arr[i].Y = float(py)
    brush = _brush(color)
    if brush:
        _gp.GdipFillPolygon(g, brush, arr, len(points),
                            win32.GP_FILLMODE_ALTERNATE)


def _round_rect(g: int, x: float, y: float, w: float, h: float, r: float,
                fill: int = 0, stroke: int = 0, stroke_w: float = 1.0) -> None:
    """圆角矩形（GDI+ GraphicsPath）。"""
    r = max(0.0, min(r, min(w, h) / 2.0))
    path = ctypes.c_void_p()
    if _gp.GdipCreatePath(win32.GP_FILLMODE_ALTERNATE,
                          ctypes.byref(path)) != win32.GP_OK or not path:
        return
    d = r * 2.0
    _gp.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
    _gp.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
    _gp.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
    _gp.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
    _gp.GdipClosePathFigure(path)
    if fill:
        brush = _brush(fill)
        if brush:
            _gp.GdipFillPath(g, brush, path)
    if stroke:
        pen = _pen(stroke, stroke_w)
        if pen:
            _gp.GdipDrawPath(g, pen, path)
    _gp.GdipDeletePath(path)


# ---------------------------------------------------------------------------
# 主题 / 颜色 / 布局
# ---------------------------------------------------------------------------
def resolve_theme(cfg: dict) -> str:
    """把 ``cfg['theme']``（dark/light/system）解析成 dark / light。"""
    theme = str((cfg or {}).get("theme") or "dark").lower()
    if theme == "system":
        return "light" if win32.taskbar_is_light() else "dark"
    return "light" if theme == "light" else "dark"


def hex_to_rgb(text: str, fallback: tuple[int, int, int] = (34, 197, 94)
               ) -> tuple[int, int, int]:
    """``#RRGGBB`` / ``#RGB`` -> (r,g,b)；解析失败返回 fallback。"""
    try:
        s = str(text or "").strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return fallback
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return fallback


def color_levels(cfg: dict) -> list[tuple[int, tuple[int, int, int]]]:
    """配置里的 [(min_level, rgb), ...]，按 min 从高到低。"""
    raw = (cfg or {}).get("color_levels")
    out: list[tuple[int, tuple[int, int, int]]] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                out.append((int(item.get("min", 0)),
                            hex_to_rgb(item.get("color", ""), COLOR_GRAY)))
            except Exception:
                continue
    if not out:
        out = [(60, COLOR_GREEN), (20, COLOR_AMBER), (0, COLOR_RED)]
    out.sort(key=lambda pair: -pair[0])
    return out


def charging_rgb(cfg: dict) -> tuple[int, int, int]:
    """充电状态颜色（默认蓝色）。"""
    return hex_to_rgb((cfg or {}).get("charging_color") or "#3b82f6",
                      (59, 130, 246))


def level_color(level: Optional[int], low_warn: int = 20,
                cfg: Optional[dict] = None) -> tuple[int, int, int]:
    """电量颜色分级：优先用 cfg['color_levels']，否则按 60/30 分档。"""
    if level is None:
        return COLOR_GRAY
    if cfg:
        for minimum, rgb in color_levels(cfg):
            if level >= minimum:
                return rgb
        return COLOR_GRAY
    if level >= 60:
        return COLOR_GREEN
    if level >= 30:
        return COLOR_AMBER
    return COLOR_RED


def _theme_colors(cfg: dict) -> dict:
    """按深/浅色主题给出轨道、描边、文字颜色（均为 GDI+ ARGB）。"""
    if resolve_theme(cfg) == "light":
        return {
            # 轨道/描边降亮度，让深色数字更清楚（用户要求）
            "track": win32.argb(44, 0, 0, 0),
            "outline": win32.argb(110, 255, 255, 255),
            "text": win32.argb(255, 18, 20, 26),
            "text_outline": win32.argb(150, 255, 255, 255),
            "sub": win32.argb(220, 60, 64, 72),
        }
    return {
        "track": win32.argb(34, 255, 255, 255),
        "outline": win32.argb(120, 6, 8, 12),
        "text": win32.argb(255, 245, 248, 252),
        "text_outline": win32.argb(150, 6, 8, 12),
        "sub": win32.argb(220, 178, 186, 198),
    }


def ring_metrics(cfg: dict, dpi: float) -> tuple[int, int, int, int]:
    """返回 ``(环外径, 线宽, 环间距, 内边距)``，单位：物理像素。"""
    size = float((cfg or {}).get("ring_size") or 30)
    size = min(64.0, max(16.0, size))
    scale = float(dpi or 1.0)
    ring_px = max(12, int(round(size * scale)))
    lw = max(2, int(round(size * 0.11 * scale)))
    gap = max(3, int(round(8 * scale)))
    pad = max(2, int(round(2 * scale)))
    return ring_px, lw, gap, pad


def widget_size(cfg: dict, dpi: float, show_names: bool = False,
                count: int = 1) -> tuple[int, int]:
    """多环叠加窗口的像素尺寸（count = 要并排显示的圆环数，至少 1）。"""
    ring_px, _lw, gap, pad = ring_metrics(cfg, dpi)
    n = max(1, min(int(count), MAX_RINGS))
    width = n * ring_px + (n - 1) * gap + 2 * pad
    height = ring_px + 2 * pad
    if show_names:
        height += int(round(12 * float(dpi or 1.0)))
    return width, height


# ---------------------------------------------------------------------------
# 圆环绘制
# ---------------------------------------------------------------------------
def _draw_ring(g: int, cx: float, cy: float, ring_px: int, lw: int,
               level: Optional[int], charging: bool, anim_t: float,
               cfg: dict, colors: dict, low_warn: int) -> None:
    """画一个圆环（含轨道 / 进度 / 充电动画 / 闪电徽标）。"""
    r = max(1.0, (ring_px - lw) / 2.0)          # 描边中线半径
    left = cx - r
    top = cy - r
    d = r * 2.0
    outline = colors["outline"]
    outline_px = max(1.0, ring_px * 0.018)

    if level is None:
        # 未知：灰色虚线环（CONTRACT/设计图：灰色虚环）
        track_pen = _pen(win32.argb(150, 150, 156, 166), lw,
                         dash=win32.GP_DASH_STYLE_DOT,
                         dash_cap=win32.GP_DASH_CAP_ROUND)
        if track_pen:
            _gp.GdipDrawEllipse(g, track_pen, left, top, d, d)
        return

    frac = max(0.0, min(1.0, float(level) / 100.0))
    if charging:
        red, green, blue = charging_rgb(cfg)
    else:
        red, green, blue = level_color(level, low_warn, cfg)
    color = win32.argb(255, red, green, blue)
    sweep = 360.0 * frac

    # 轨道（半透明）
    track_pen = _pen(colors["track"], lw)
    if track_pen:
        _gp.GdipDrawEllipse(g, track_pen, left, top, d, d)

    # 1px 描边：低电量时额外加一层淡淡的红色光晕
    if level <= low_warn and frac > 0:
        glow = _pen(win32.argb(52, red, green, blue), lw + max(2, ring_px * 0.14))
        if glow:
            _gp.GdipDrawArc(g, glow, left, top, d, d, -90.0, sweep)
    if outline:
        outline_pen = _pen(outline, lw + max(2.0, outline_px * 2.0))
        if outline_pen:
            _gp.GdipDrawArc(g, outline_pen, left, top, d, d, -90.0, sweep)
    if sweep > 0.05:
        arc_pen = _pen(color, lw, cap=win32.GP_LINE_CAP_ROUND)
        if arc_pen:
            _gp.GdipDrawArc(g, arc_pen, left, top, d, d, -90.0, sweep)

    # 充电中：白色亮弧沿环扫动（30fps，anim_t 秒）
    if charging:
        _draw_charge_comet(g, cx, cy, r, d, lw, anim_t)
        _draw_bolt(g, cx, cy, r)


def _draw_charge_comet(g: int, cx: float, cy: float, r: float, d: float,
                       lw: int, anim_t: float) -> None:
    """白色亮弧 + 渐隐拖尾，绕环匀速扫动。"""
    period = 1.9
    phase = (float(anim_t) / period) % 1.0
    head = -90.0 + 360.0 * phase
    left = cx - r
    top = cy - r
    # 拖尾由远及近：越靠后越淡越细
    for delta, sweep, alpha, width_f in ((62.0, 26.0, 55, 0.72),
                                         (30.0, 40.0, 120, 0.88)):
        start = head - delta - sweep
        pen = _pen(win32.argb(alpha, 255, 255, 255),
                   max(1.0, lw * width_f), cap=win32.GP_LINE_CAP_ROUND)
        if pen:
            _gp.GdipDrawArc(g, pen, left, top, d, d, start, sweep)
    head_pen = _pen(win32.argb(235, 255, 255, 255),
                    max(1.0, lw * 0.92), cap=win32.GP_LINE_CAP_ROUND)
    if head_pen:
        _gp.GdipDrawArc(g, head_pen, left, top, d, d, head, 46.0)


def _draw_bolt(g: int, cx: float, cy: float, r: float) -> None:
    """右下角小闪电徽标（深色描边 + 琥珀色亮芯）。"""
    k = max(0.35, r / 16.2)
    bx = cx + r * 0.72
    by = cy + r * 0.72
    dark_pts = [
        (bx + 1.2 * k, by - 6.2 * k), (bx - 3.6 * k, by + 1.2 * k),
        (bx - 0.8 * k, by + 1.2 * k), (bx - 2.6 * k, by + 6.4 * k),
        (bx + 3.8 * k, by - 1.2 * k), (bx + 0.8 * k, by - 1.2 * k),
    ]
    amber_pts = [
        (bx + 0.6 * k, by - 5.2 * k), (bx - 2.8 * k, by + 0.9 * k),
        (bx - 0.5 * k, by + 0.9 * k), (bx - 2.0 * k, by + 5.4 * k),
        (bx + 3.0 * k, by - 0.9 * k), (bx + 0.6 * k, by - 0.9 * k),
    ]
    _fill_polygon(g, dark_pts, win32.argb(235, 18, 20, 24))
    _fill_polygon(g, amber_pts, win32.argb(255, 214, 235, 255))


def _draw_ring_text(g: int, cx: float, cy: float, ring_px: int, lw: int,
                    level: Optional[int], estimated: bool, colors: dict) -> None:
    """环内居中读数（最终需求版）。

    * 数字 Segoe UI Semibold，基准字号 ≈ 环径×0.36；``%`` 用小一号字跟在右下；
    * **文字笔迹宽度限制在环内直径的 ~0.82 倍**（Lead 03:12）：超宽时自动缩小
      字号（最低不小于环径×0.24），3 字符（如 "62%"）保持基准字号；
    * ``estimated`` 前缀 ≈；``level is None`` 显示 —。

    GDI+ ``GdipMeasureString`` 的返回值带约 1/3 em 的空白 padding，这里换算成
    "实际笔迹宽度"再做布局，避免小环上数字压到圆环描边。
    """
    fmt_center = _format(win32.GP_STRING_ALIGN_CENTER,
                         win32.GP_STRING_ALIGN_CENTER)
    outline_px = max(0.7, ring_px * 0.018)

    def _painted(text: str, px: float) -> tuple[float, float, float, int]:
        font = _font(FONT_SEMIBOLD, px)
        width, height = _measure(g, text, font, fmt_center)
        return max(0.0, width - px * 0.32), width, height, font

    if level is None:
        text = "—"
        paint, width, height, font = _painted(text, ring_px * 0.36)
        _draw_text(g, text, font, fmt_center, cx - width / 2.0,
                   cy - height / 2.0, width + 2.0, height + 2.0,
                   colors["text"], colors["text_outline"], outline_px)
        return

    text = ("≈" if estimated else "") + str(int(level))
    inner = max(1.0, ring_px - lw * 2.0)
    max_width = inner * 0.82
    min_big = ring_px * 0.24
    min_pct = ring_px * 0.14
    big_px = ring_px * 0.36
    pct_px = ring_px * 0.20
    show_pct = True
    paint_big = width_big = height_big = paint_pct = width_pct = height_pct = 0.0
    font_big = font_pct = 0
    gap = 0.0
    for _attempt in range(8):
        paint_big, width_big, height_big, font_big = _painted(text, big_px)
        if show_pct:
            paint_pct, width_pct, height_pct, font_pct = _painted("%", pct_px)
            gap = max(0.4, big_px * 0.04)
        else:
            paint_pct = width_pct = height_pct = gap = 0.0
            font_pct = 0
        total = paint_big + gap + paint_pct
        if total <= max_width:
            break
        if big_px > min_big + 1e-6:
            scale = 0.92
            big_px = max(min_big, big_px * scale)
            pct_px = max(min_pct, pct_px * scale)
            continue
        if show_pct:                    # 已经缩到最小：去掉 % 保数字
            show_pct = False
            continue
        break

    if not show_pct:
        paint_big, width_big, height_big, font_big = _painted(text, big_px)
        _draw_text(g, text, font_big, fmt_center, cx - width_big / 2.0,
                   cy - height_big / 2.0, width_big + 2.0, height_big + 2.0,
                   colors["text"], colors["text_outline"], outline_px)
        return

    total = paint_big + gap + paint_pct
    group_left = cx - total / 2.0
    big_center = group_left + paint_big / 2.0
    big_y = cy - height_big / 2.0
    _draw_text(g, text, font_big, fmt_center, big_center - width_big / 2.0,
               big_y, width_big + 2.0, height_big + 2.0, colors["text"],
               colors["text_outline"], outline_px)
    pct_center = group_left + paint_big + gap + paint_pct / 2.0
    pct_y = big_y + height_big - height_pct - ring_px * 0.04
    _draw_text(g, "%", font_pct, fmt_center, pct_center - width_pct / 2.0,
               pct_y, width_pct + 2.0, height_pct + 2.0, colors["sub"],
               colors["text_outline"], outline_px * 0.8)


# ---------------------------------------------------------------------------
# RingSurface
# ---------------------------------------------------------------------------
class RingSurface:
    """GDI+ 绘制目标：32bpp DIB + 内存 DC，尺寸可重建。

    用法::

        surface = RingSurface(81, 38)
        surface.render_rings(readings, cfg, dpi, anim_t)
        window.present(surface, x, y)      # 由 ui.win32.LayeredWindow 完成 ULW
    """

    def __init__(self, width: int = 1, height: int = 1) -> None:
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._dib: Optional[win32.Dib] = None
        self._gdi_bitmap = 0
        self._graphics = 0
        # True = GDI+ 直接写预乘 alpha（PARGB），无需再手工预乘
        self._premultiplied = False
        self._build()

    # -- 生命周期 ----------------------------------------------------------
    def _release(self) -> None:
        if self._graphics:
            _gp.GdipDeleteGraphics(self._graphics)
            self._graphics = 0
        if self._gdi_bitmap:
            _gp.GdipDisposeImage(self._gdi_bitmap)
            self._gdi_bitmap = 0
        if self._dib is not None:
            self._dib.close()
            self._dib = None

    def _build(self) -> None:
        if not win32.ensure_gdiplus():
            return
        try:
            self._dib = win32.Dib(self._width, self._height)
        except win32.Win32Error as exc:
            win32.note_error(f"RingSurface 创建 DIB 失败: {exc}")
            self._dib = None
            return
        bitmap = ctypes.c_void_p()
        # 优先用 PARGB（GDI+ 直接输出预乘 alpha，UpdateLayeredWindow 要求）；
        # 个别环境不支持时退化为 ARGB + 手工预乘（_end_frame 里处理）。
        status = _gp.GdipCreateBitmapFromScan0(
            self._width, self._height, self._width * 4,
            win32.PIXEL_FORMAT_32BPP_PARGB,
            ctypes.c_void_p(self._dib.bits), ctypes.byref(bitmap))
        self._premultiplied = (status == win32.GP_OK and bool(bitmap.value))
        if not self._premultiplied:
            bitmap = ctypes.c_void_p()
            status = _gp.GdipCreateBitmapFromScan0(
                self._width, self._height, self._width * 4,
                win32.PIXEL_FORMAT_32BPP_ARGB,
                ctypes.c_void_p(self._dib.bits), ctypes.byref(bitmap))
        if status != win32.GP_OK or not bitmap.value:
            win32.note_error(f"GdipCreateBitmapFromScan0 失败 status={status}")
            self._dib.close()
            self._dib = None
            return
        self._gdi_bitmap = int(bitmap.value)
        graphics = ctypes.c_void_p()
        status = _gp.GdipGetImageGraphicsContext(
            ctypes.c_void_p(self._gdi_bitmap), ctypes.byref(graphics))
        if status != win32.GP_OK or not graphics.value:
            win32.note_error(f"GdipGetImageGraphicsContext 失败 status={status}")
            _gp.GdipDisposeImage(ctypes.c_void_p(self._gdi_bitmap))
            self._gdi_bitmap = 0
            self._dib.close()
            self._dib = None
            return
        self._graphics = int(graphics.value)
        _gp.GdipSetSmoothingMode(self._graphics, win32.GP_SMOOTHING_ANTIALIAS)
        # 透明分层窗口上不能用 ClearType（亚像素会留下红蓝彩边），用灰阶抗锯齿
        # + 提高 TextContrast，效果更干净；字号策略见 _draw_ring_text。
        _gp.GdipSetTextRenderingHint(
            self._graphics, win32.GP_TEXT_RENDERING_ANTIALIAS_GRIDFIT)
        # 提高文字对比度（0-12，默认 4），让 11px 数字在任务栏上更清楚
        _gp.GdipSetTextContrast(self._graphics,
                                win32.GP_TEXT_CONTRAST_DEFAULT)
        _gp.GdipSetInterpolationMode(self._graphics,
                                     win32.GP_INTERPOLATION_HQ_BICUBIC)
        _gp.GdipSetPixelOffsetMode(self._graphics, win32.GP_PIXEL_OFFSET_HQ)
        _gp.GdipSetCompositingQuality(self._graphics,
                                      win32.GP_COMPOSITING_QUALITY_HQ)
        _gp.GdipGraphicsClear(self._graphics, 0)

    def resize(self, width: int, height: int) -> None:
        width = max(1, int(width))
        height = max(1, int(height))
        if width == self._width and height == self._height and self._dib:
            return
        self._release()
        self._width = width
        self._height = height
        self._build()

    def close(self) -> None:
        self._release()

    def __del__(self) -> None:              # pragma: no cover - 兜底释放
        try:
            self._release()
        except Exception:
            pass

    # -- 对外属性 ----------------------------------------------------------
    @property
    def hdc(self) -> int:
        return int(self._dib.hdc) if self._dib is not None else 0

    @property
    def hbitmap(self) -> int:
        return int(self._dib.hbitmap) if self._dib is not None else 0

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def ready(self) -> bool:
        return bool(self._graphics and self._dib is not None)

    # -- 帧管理 ------------------------------------------------------------
    def _begin_frame(self) -> bool:
        if not self._graphics:
            self._build()
        if not self._graphics:
            return False
        _gp.GdipGraphicsClear(self._graphics, 0)
        return True

    def _end_frame(self) -> None:
        if not self._graphics:
            return
        _gp.GdipFlush(self._graphics, win32.GP_FLUSH_SYNC)
        if self._dib is not None and not self._premultiplied:
            self._dib.premultiply()

    # -- 绘制：任务栏圆环 ------------------------------------------------
    def render_rings(self, readings: list[Reading], cfg: dict, dpi: float,
                     anim_t: float) -> None:
        """把 ``readings`` 画到内部 DIB（背景透明）。"""
        try:
            if not self._begin_frame():
                return
            cfg = cfg or {}
            colors = _theme_colors(cfg)
            show_names = bool(cfg.get("show_names", False))
            ring_px, lw, gap, pad = ring_metrics(cfg, dpi)
            data = list(readings or [])[:MAX_RINGS]
            n = max(1, len(data))
            name_h = int(round(12 * float(dpi or 1.0))) if show_names else 0
            content_h = ring_px + name_h
            y0 = max(pad, (self._height - content_h) // 2)
            cy = y0 + ring_px / 2.0
            total_w = n * ring_px + (n - 1) * gap
            x0 = (self._width - total_w) // 2
            fmt_center = _format(win32.GP_STRING_ALIGN_CENTER,
                                 win32.GP_STRING_ALIGN_NEAR)
            try:
                low_warn = int((cfg or {}).get("low_battery_warn", 20) or 20)
            except (TypeError, ValueError):
                low_warn = 20
            for i in range(n):
                reading = data[i] if i < len(data) else None
                cx = x0 + ring_px / 2.0 + i * (ring_px + gap)
                if reading is None:
                    level = None
                    charging = False
                    estimated = False
                else:
                    level = getattr(reading, "level", None)
                    status = str(getattr(reading, "status", "") or "")
                    charging = status in ("charging", "full") and level is not None
                    if status == "full":
                        charging = bool(getattr(reading, "charging", None)) and \
                            int(level or 0) < 100
                    estimated = bool(getattr(reading, "estimated", False))
                _draw_ring(self._graphics, cx, cy, ring_px, lw, level,
                           charging, anim_t, cfg, colors, low_warn)
                _draw_ring_text(self._graphics, cx, cy, ring_px, lw, level,
                                estimated, colors)
                if show_names and reading is not None:
                    name = str(getattr(reading, "name", "") or "")
                    if name:
                        font = _font(FONT_CJK, 8.0 * float(dpi or 1.0))
                        max_w = ring_px + gap * 0.55
                        text = _ellipsize(name, font, fmt_center, max_w)
                        ny = cy + ring_px / 2.0 + pad * 0.2
                        _draw_text(self._graphics, text, font, fmt_center,
                                   cx - max_w / 2.0, ny, max_w,
                                   name_h, colors["sub"],
                                   colors["text_outline"],
                                   max(0.5, float(dpi or 1.0) * 0.8))
            self._end_frame()
        except Exception as exc:            # 绘制失败绝不能影响主程序
            win32.note_error(f"render_rings 异常: {exc!r}")

def _ellipsize(text: str, font: int, fmt: int, max_width: float) -> str:
    """按像素宽度截断文字并加省略号（设备名过长时）。"""
    if not text or not font:
        return text
    g = _scratch_graphics()
    if not g:
        return text
    width, _h = _measure(g, text, font, fmt)
    if width <= max_width:
        return text
    ell = "…"
    lo, hi = 1, len(text)
    best = ell
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid] + ell
        w, _h2 = _measure(g, candidate, font, fmt)
        if w <= max_width:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


_SCRATCH = {"surface": None}


def _scratch_graphics() -> int:
    """测量文字用的临时 Graphics（不参与显示）。"""
    surface = _SCRATCH.get("surface")
    if surface is None:
        surface = RingSurface(8, 8)
        _SCRATCH["surface"] = surface
    return surface._graphics


# ---------------------------------------------------------------------------
# 托盘 ICO（纯标准库光栅化，不依赖 GDI+；CONTRACT §5.4 由 tray.py 使用）
# ---------------------------------------------------------------------------
_ICO_SIZES = (16, 20, 24, 32)


def _icon_pixels(size: int, readings: list[Reading], cfg: dict,
                 count: int = 2) -> bytearray:
    """光栅化一个小图标（count=1 单环；count=2 两个小圆环保留旧行为）。

    返回自上而下的 BGRA 直通 alpha 像素（size*size*4）。
    """
    count = 1 if int(count) <= 1 else 2
    data = list(readings or [])[:count]
    cfg = cfg or {}
    try:
        ring_size = float(cfg.get("ring_size") or 30)
    except Exception:
        ring_size = 30.0
    # 圆环尺寸设置：24→细一点，36→占满图标（系统托盘图标尺寸固定，改的是环在图标内的比例）
    scale_ratio = max(0.55, min(0.92, 0.62 + (ring_size - 24.0) * 0.025))
    px = bytearray(size * size * 4)
    ss = 4                                  # 4x4 超采样
    if count == 1:
        ring_d = size * scale_ratio
        lw = max(1.0, ring_d * 0.16)
        centers = [size / 2.0]
    else:
        margin = max(1.0, size * 0.06)
        gap = max(1.0, size * 0.10)
        ring_d = (size - 2 * margin - gap) / 2.0
        lw = max(1.0, ring_d * 0.16)
        centers = [margin + ring_d / 2.0,
                   margin + ring_d + gap + ring_d / 2.0]
    cy = size / 2.0
    colors = []
    for i in range(count):
        reading = data[i] if i < len(data) else None
        level = getattr(reading, "level", None) if reading else None
        status = str(getattr(reading, "status", "") or "") if reading else ""
        charging = (status == "charging" and level is not None) or \
            bool(getattr(reading, "charging", False)) if reading else False
        if charging:
            color = charging_rgb(cfg)
        else:
            color = level_color(level, cfg=cfg)
        colors.append((level, charging, color))

    for y in range(size):
        for x in range(size):
            # 对每个采样点做覆盖率（环 + 空心底）
            hits = [0.0, 0.0, 0.0]          # 轨道/进度/其它（暂不用）
            ring_hits = [[0.0, 0.0] for _ in range(count)]   # [track, progress]
            for sy in range(ss):
                fy = y + (sy + 0.5) / ss
                for sx in range(ss):
                    fx = x + (sx + 0.5) / ss
                    for i, cx in enumerate(centers):
                        dx = fx - cx
                        dy = fy - cy
                        dist = math.hypot(dx, dy)
                        if abs(dist - ring_d / 2.0) > lw / 2.0:
                            continue
                        ring_hits[i][0] += 1.0
                        level, charging, color = colors[i]
                        if level is None:
                            continue
                        frac = max(0.0, min(1.0, level / 100.0))
                        ang = math.degrees(math.atan2(dy, dx)) + 90.0
                        if ang % 360.0 <= 360.0 * frac:
                            ring_hits[i][1] += 1.0
                        elif charging and (ang % 360.0) >= 300.0:
                            ring_hits[i][1] += 1.25     # 充电时右上角高光
            total = float(ss * ss)
            # 合成到该像素
            alpha = 0.0
            rgb = [0.0, 0.0, 0.0]
            for i in range(count):
                track_cov = min(1.0, ring_hits[i][0] / total)
                prog_cov = min(1.0, ring_hits[i][1] / total)
                if track_cov <= 0:
                    continue
                level, _charging, color = colors[i]
                if prog_cov > 0:
                    a = prog_cov
                    src = color
                else:
                    a = track_cov * 0.35
                    src = (150, 156, 166)
                # source-over 合成
                out_a = a + alpha * (1.0 - a)
                if out_a <= 0:
                    continue
                rgb[0] = (src[0] * a + rgb[0] * alpha * (1.0 - a)) / out_a
                rgb[1] = (src[1] * a + rgb[1] * alpha * (1.0 - a)) / out_a
                rgb[2] = (src[2] * a + rgb[2] * alpha * (1.0 - a)) / out_a
                alpha = out_a
            if alpha <= 0.004:
                continue
            idx = (y * size + x) * 4
            px[idx] = min(255, int(rgb[2] + 0.5))
            px[idx + 1] = min(255, int(rgb[1] + 0.5))
            px[idx + 2] = min(255, int(rgb[0] + 0.5))
            px[idx + 3] = min(255, int(alpha * 255 + 0.5))
    return px


def _ico_image(size: int, bgra_topdown: bytes) -> bytes:
    """把自上而下 BGRA 像素包装成 ICO 里的 BMP 条目（DIB + AND mask）。"""
    stride = size * 4
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(bgra_topdown), 0, 0, 0, 0)
    rows = [bgra_topdown[y * stride:(y + 1) * stride] for y in range(size)]
    xor_bitmap = b"".join(reversed(rows))           # BMP 行序自下而上
    mask_stride = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * size)
    return header + xor_bitmap + and_mask


def build_tray_ico(readings: list[Reading], cfg: dict,
                   count: int = 2) -> bytes:
    """生成多尺寸 ICO（16/20/24/32）字节流，供托盘 ``Shell_NotifyIconW`` 使用。

    ``count=1`` 单环（每台设备一个托盘图标）；``count=2`` 双环（旧聚合图标）。
    """
    images = [_ico_image(size, bytes(_icon_pixels(size, readings, cfg or {}, count)))
              for size in _ICO_SIZES]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = []
    offset = 6 + 16 * count
    for size, image in zip(_ICO_SIZES, images):
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32,
                                   len(image), offset))
        offset += len(image)
    return header + b"".join(entries) + b"".join(images)
