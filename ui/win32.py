# -*- coding: utf-8 -*-
"""ui/win32.py —— Win32 / GDI / GDI+ 底层封装（ui-dev 私有模块，不属于对外契约）。

只做底层，不含任何界面业务逻辑：

* DLL、常量、结构体、函数原型（**Win64 句柄安全**：返回句柄/指针的 API 一律
  ``restype = c_void_p``，参数用 ``c_void_p``，见 CONTRACT §0.4）；
* GDI+ 扁平 API（ctypes 直接调 gdiplus.dll），惰性 ``GdiplusStartup``；
* 32bpp 自上而下 DIB + 内存 DC（UpdateLayeredWindow 的像素来源）+ alpha 预乘；
* 任务栏 / TrafficMonitor 任务栏窗口 / TrayNotifyWnd 探测，DPI、主题、全屏、
  任务栏自动隐藏判定；
* 分层窗口（WS_EX_LAYERED）包装与 UpdateLayeredWindow；
* 屏幕条带取样（判断圆环目标位置是否被任务栏应用按钮占用）。

约定：import 本模块**不做任何窗口 / GDI+ 调用**（只加载 DLL），可以安全地被
``main.py --once`` 这类不启动 UI 的模式导入。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# DLL
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdiplus = ctypes.WinDLL("gdiplus")

LPCWSTR = wintypes.LPCWSTR
LPARAM = wintypes.LPARAM
LRESULT = ctypes.c_ssize_t
BOOL = wintypes.BOOL
UINT = wintypes.UINT
DWORD = wintypes.DWORD
HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HANDLE = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 窗口风格
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
CS_DBLCLKS = 0x0008
GWL_EXSTYLE = -20
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
IDC_ARROW = 32512

# 消息
WM_DESTROY = 0x0002
WM_MOVE = 0x0003
WM_SIZE = 0x0005
WM_TIMER = 0x0113
WM_SETTINGCHANGE = 0x001A
WM_MOUSEACTIVATE = 0x0021
WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MOUSELEAVE = 0x02A3
MA_NOACTIVATE = 3
TME_LEAVE = 0x00000002
VK_MENU = 0x12
PM_REMOVE = 0x0001
WM_QUIT = 0x0012

# 任务栏 / 虚拟屏
ABM_GETSTATE = 0x00000004
ABS_AUTOHIDE = 0x0001
MONITOR_DEFAULTTONULL = 0x00000000
MONITOR_DEFAULTTOPRIMARY = 0x00000001
MONITOR_DEFAULTTONEAREST = 0x00000002

# GDI
BI_RGB = 0
DIB_RGB_COLORS = 0
LOGPIXELSX = 88
SRCCOPY = 0x00CC0020
WHITE_BRUSH = 0

# DPI 感知上下文
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
PROCESS_PER_MONITOR_DPI_AWARE = 2

# GDI+ 状态码 / 枚举
GP_OK = 0
GP_UNIT_PIXEL = 2
GP_FONT_STYLE_REGULAR = 0
GP_SMOOTHING_ANTIALIAS = 4
GP_TEXT_RENDERING_ANTIALIAS = 4
GP_TEXT_RENDERING_ANTIALIAS_GRIDFIT = 3
GP_TEXT_CONTRAST_DEFAULT = 6
GP_TEXT_RENDERING_CLEARTYPE_GRIDFIT = 5
GP_INTERPOLATION_HQ_BICUBIC = 7
GP_PIXEL_OFFSET_HQ = 2
GP_COMPOSITING_QUALITY_HQ = 2
GP_FLUSH_SYNC = 1
GP_FILLMODE_ALTERNATE = 0
GP_DASH_STYLE_SOLID = 0
GP_DASH_STYLE_DOT = 2
GP_LINE_CAP_ROUND = 2
GP_LINE_CAP_FLAT = 0
GP_DASH_CAP_ROUND = 2
GP_STRING_ALIGN_NEAR = 0
GP_STRING_ALIGN_CENTER = 1
GP_STRING_ALIGN_FAR = 2
GP_STRING_FLAG_NO_WRAP = 0x00001000
PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
PIXEL_FORMAT_32BPP_PARGB = 0x000E200B
DIFFUSE_ATTRIBUTE = 0


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, ctypes.c_size_t, LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HANDLE),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HANDLE),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", DWORD),
    ]


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("hWnd", HWND),
        ("uCallbackMessage", UINT),
        ("uEdge", UINT),
        ("rc", wintypes.RECT),
        ("lParam", LPARAM),
    ]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("dwFlags", DWORD),
        ("hwndTrack", HWND),
        ("dwHoverTime", DWORD),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", DWORD),
        ("dwHighDateTime", DWORD),
    ]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", UINT),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", BOOL),
        ("SuppressExternalCodecs", BOOL),
    ]


class PointF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]


class RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


# ---------------------------------------------------------------------------
# 原型声明（注意：句柄类一律 c_void_p，避免 Win64 截断）
# ---------------------------------------------------------------------------
def _declare(dll: ctypes.WinDLL, name: str, restype, argtypes) -> Optional[object]:
    """声明函数原型；当前系统没有该导出时返回 None（用于 Win7/旧系统降级）。"""
    fn = getattr(dll, name, None)
    if fn is None:
        return None
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# --- user32 ---------------------------------------------------------------
FindWindowW = _declare(user32, "FindWindowW", HWND, [LPCWSTR, LPCWSTR])
FindWindowExW = _declare(user32, "FindWindowExW", HWND,
                         [HWND, HWND, LPCWSTR, LPCWSTR])
CreateWindowExW = _declare(user32, "CreateWindowExW", HWND,
                           [DWORD, LPCWSTR, LPCWSTR, DWORD,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            HWND, HANDLE, HINSTANCE, HANDLE])
DestroyWindow = _declare(user32, "DestroyWindow", BOOL, [HWND])
ShowWindow = _declare(user32, "ShowWindow", BOOL, [HWND, ctypes.c_int])
IsWindowVisible = _declare(user32, "IsWindowVisible", BOOL, [HWND])
IsIconic = _declare(user32, "IsIconic", BOOL, [HWND])
GetWindowRect = _declare(user32, "GetWindowRect", BOOL,
                         [HWND, ctypes.POINTER(wintypes.RECT)])
GetClassNameW = _declare(user32, "GetClassNameW", ctypes.c_int,
                         [HWND, wintypes.LPWSTR, ctypes.c_int])
GetWindowTextW = _declare(user32, "GetWindowTextW", ctypes.c_int,
                          [HWND, wintypes.LPWSTR, ctypes.c_int])
EnumWindows = _declare(user32, "EnumWindows", BOOL, [ENUMPROC, LPARAM])
EnumChildWindows = _declare(user32, "EnumChildWindows", BOOL,
                            [HWND, ENUMPROC, LPARAM])
GetForegroundWindow = _declare(user32, "GetForegroundWindow", HWND, [])
GetDC = _declare(user32, "GetDC", HDC, [HWND])
ReleaseDC = _declare(user32, "ReleaseDC", ctypes.c_int, [HWND, HDC])
GetCursorPos = _declare(user32, "GetCursorPos", BOOL,
                        [ctypes.POINTER(wintypes.POINT)])
SetCapture = _declare(user32, "SetCapture", HWND, [HWND])
ReleaseCapture = _declare(user32, "ReleaseCapture", BOOL, [])
GetKeyState = _declare(user32, "GetKeyState", ctypes.c_short, [ctypes.c_int])
GetDoubleClickTime = _declare(user32, "GetDoubleClickTime", UINT, [])
SetTimer = _declare(user32, "SetTimer", ctypes.c_size_t,
                    [HWND, ctypes.c_size_t, UINT, ctypes.c_void_p])
KillTimer = _declare(user32, "KillTimer", BOOL, [HWND, ctypes.c_size_t])
TrackMouseEvent = _declare(user32, "TrackMouseEvent", BOOL,
                           [ctypes.POINTER(TRACKMOUSEEVENT)])
RegisterWindowMessageW = _declare(user32, "RegisterWindowMessageW", UINT,
                                  [LPCWSTR])
DefWindowProcW = _declare(user32, "DefWindowProcW", LRESULT,
                          [HWND, UINT, ctypes.c_size_t, LPARAM])
RegisterClassExW = _declare(user32, "RegisterClassExW", wintypes.WORD,
                            [ctypes.POINTER(WNDCLASSEXW)])
UpdateLayeredWindow = _declare(user32, "UpdateLayeredWindow", BOOL,
                               [HWND, HDC, ctypes.POINTER(wintypes.POINT),
                                ctypes.POINTER(wintypes.SIZE), HDC,
                                ctypes.POINTER(wintypes.POINT), DWORD,
                                ctypes.POINTER(BLENDFUNCTION), DWORD])
SetWindowPos = _declare(user32, "SetWindowPos", BOOL,
                        [HWND, HWND, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, UINT])
GetSystemMetrics = _declare(user32, "GetSystemMetrics", ctypes.c_int,
                            [ctypes.c_int])
MonitorFromWindow = _declare(user32, "MonitorFromWindow", HANDLE,
                             [HWND, DWORD])
MonitorFromPoint = _declare(user32, "MonitorFromPoint", HANDLE,
                            [wintypes.POINT, DWORD])
GetMonitorInfoW = _declare(user32, "GetMonitorInfoW", BOOL,
                           [HANDLE, ctypes.POINTER(MONITORINFO)])
LoadCursorW = _declare(user32, "LoadCursorW", HANDLE, [HINSTANCE, LPCWSTR])
SetProcessDPIAware = _declare(user32, "SetProcessDPIAware", BOOL, [])
SetProcessDpiAwarenessContext = _declare(
    user32, "SetProcessDpiAwarenessContext", BOOL, [ctypes.c_void_p])
GetDpiForWindow = _declare(user32, "GetDpiForWindow", UINT, [HWND])
GetDpiForSystem = _declare(user32, "GetDpiForSystem", UINT, [])
PeekMessageW = _declare(user32, "PeekMessageW", BOOL,
                        [ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT, UINT])
TranslateMessage = _declare(user32, "TranslateMessage", BOOL,
                            [ctypes.POINTER(wintypes.MSG)])
DispatchMessageW = _declare(user32, "DispatchMessageW", LRESULT,
                            [ctypes.POINTER(wintypes.MSG)])
GetMessageW = _declare(user32, "GetMessageW", ctypes.c_int,
                       [ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT])
PostQuitMessage = _declare(user32, "PostQuitMessage", None, [ctypes.c_int])
PostMessageW = _declare(user32, "PostMessageW", BOOL,
                        [HWND, UINT, ctypes.c_size_t, LPARAM])

# GetWindowLongPtrW / SetWindowLongPtrW：64 位系统导出，32 位系统没有
_GetWindowLongPtrW = getattr(user32, "GetWindowLongPtrW", None) or getattr(
    user32, "GetWindowLongW", None)
_SetWindowLongPtrW = getattr(user32, "SetWindowLongPtrW", None) or getattr(
    user32, "SetWindowLongW", None)
if _GetWindowLongPtrW is not None:
    _GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
if _SetWindowLongPtrW is not None:
    _SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]

# --- gdi32 ----------------------------------------------------------------
CreateCompatibleDC = _declare(gdi32, "CreateCompatibleDC", HDC, [HDC])
DeleteDC = _declare(gdi32, "DeleteDC", BOOL, [HDC])
CreateDIBSection = _declare(gdi32, "CreateDIBSection", HBITMAP,
                            [HDC, ctypes.POINTER(BITMAPINFO), UINT,
                             ctypes.POINTER(ctypes.c_void_p), HANDLE, DWORD])
SelectObject = _declare(gdi32, "SelectObject", HGDIOBJ, [HDC, HGDIOBJ])
DeleteObject = _declare(gdi32, "DeleteObject", BOOL, [HGDIOBJ])
GetDeviceCaps = _declare(gdi32, "GetDeviceCaps", ctypes.c_int,
                         [HDC, ctypes.c_int])
BitBlt = _declare(gdi32, "BitBlt", BOOL,
                  [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   HDC, ctypes.c_int, ctypes.c_int, DWORD])

# --- kernel32 -------------------------------------------------------------
GetModuleHandleW = _declare(kernel32, "GetModuleHandleW", HINSTANCE, [LPCWSTR])
GetCurrentProcess = _declare(kernel32, "GetCurrentProcess", HANDLE, [])
GetProcessTimes = _declare(kernel32, "GetProcessTimes", BOOL,
                           [HANDLE, ctypes.POINTER(FILETIME),
                            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                            ctypes.POINTER(FILETIME)])

# --- shell32 --------------------------------------------------------------
SHAppBarMessage = _declare(shell32, "SHAppBarMessage", ctypes.c_size_t,
                           [DWORD, ctypes.POINTER(APPBARDATA)])

# --- gdiplus --------------------------------------------------------------
GdiplusStartup = _declare(gdiplus, "GdiplusStartup", ctypes.c_int,
                          [ctypes.POINTER(ctypes.c_size_t),
                           ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p])
GdiplusShutdown = _declare(gdiplus, "GdiplusShutdown", None,
                           [ctypes.c_size_t])
GdipCreateBitmapFromScan0 = _declare(
    gdiplus, "GdipCreateBitmapFromScan0", ctypes.c_int,
    [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
GdipDisposeImage = _declare(gdiplus, "GdipDisposeImage", ctypes.c_int,
                            [ctypes.c_void_p])
GdipGetImageGraphicsContext = _declare(
    gdiplus, "GdipGetImageGraphicsContext", ctypes.c_int,
    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
GdipDeleteGraphics = _declare(gdiplus, "GdipDeleteGraphics", ctypes.c_int,
                              [ctypes.c_void_p])
GdipGraphicsClear = _declare(gdiplus, "GdipGraphicsClear", ctypes.c_int,
                             [ctypes.c_void_p, ctypes.c_uint])
GdipFlush = _declare(gdiplus, "GdipFlush", ctypes.c_int,
                     [ctypes.c_void_p, ctypes.c_int])
GdipSetSmoothingMode = _declare(gdiplus, "GdipSetSmoothingMode", ctypes.c_int,
                                [ctypes.c_void_p, ctypes.c_int])
GdipSetTextRenderingHint = _declare(
    gdiplus, "GdipSetTextRenderingHint", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int])
GdipSetInterpolationMode = _declare(
    gdiplus, "GdipSetInterpolationMode", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int])
GdipSetTextContrast = _declare(gdiplus, "GdipSetTextContrast",
                               ctypes.c_int,
                               [ctypes.c_void_p, ctypes.c_uint])
GdipSetPixelOffsetMode = _declare(gdiplus, "GdipSetPixelOffsetMode",
                                  ctypes.c_int, [ctypes.c_void_p, ctypes.c_int])
GdipSetCompositingQuality = _declare(
    gdiplus, "GdipSetCompositingQuality", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int])
GdipCreatePen1 = _declare(gdiplus, "GdipCreatePen1", ctypes.c_int,
                          [ctypes.c_uint, ctypes.c_float, ctypes.c_int,
                           ctypes.POINTER(ctypes.c_void_p)])
GdipDeletePen = _declare(gdiplus, "GdipDeletePen", ctypes.c_int,
                         [ctypes.c_void_p])
GdipSetPenLineCap197819 = _declare(
    gdiplus, "GdipSetPenLineCap197819", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int])
GdipSetPenDashStyle = _declare(gdiplus, "GdipSetPenDashStyle", ctypes.c_int,
                               [ctypes.c_void_p, ctypes.c_int])
GdipSetPenLineJoin = _declare(gdiplus, "GdipSetPenLineJoin", ctypes.c_int,
                              [ctypes.c_void_p, ctypes.c_int])
GdipCreateSolidFill = _declare(gdiplus, "GdipCreateSolidFill", ctypes.c_int,
                               [ctypes.c_uint,
                                ctypes.POINTER(ctypes.c_void_p)])
GdipDeleteBrush = _declare(gdiplus, "GdipDeleteBrush", ctypes.c_int,
                           [ctypes.c_void_p])
GdipDrawArc = _declare(gdiplus, "GdipDrawArc", ctypes.c_int,
                       [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float,
                        ctypes.c_float, ctypes.c_float, ctypes.c_float,
                        ctypes.c_float, ctypes.c_float])
GdipDrawEllipse = _declare(gdiplus, "GdipDrawEllipse", ctypes.c_int,
                           [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float,
                            ctypes.c_float, ctypes.c_float, ctypes.c_float])
GdipDrawLine = _declare(gdiplus, "GdipDrawLine", ctypes.c_int,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float,
                         ctypes.c_float, ctypes.c_float, ctypes.c_float])
GdipFillEllipse = _declare(gdiplus, "GdipFillEllipse", ctypes.c_int,
                           [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float,
                            ctypes.c_float, ctypes.c_float, ctypes.c_float])
GdipFillPolygon = _declare(gdiplus, "GdipFillPolygon", ctypes.c_int,
                           [ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(PointF), ctypes.c_int, ctypes.c_int])
GdipCreatePath = _declare(gdiplus, "GdipCreatePath", ctypes.c_int,
                          [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])
GdipDeletePath = _declare(gdiplus, "GdipDeletePath", ctypes.c_int,
                          [ctypes.c_void_p])
GdipAddPathArc = _declare(gdiplus, "GdipAddPathArc", ctypes.c_int,
                          [ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
                           ctypes.c_float, ctypes.c_float, ctypes.c_float,
                           ctypes.c_float])
GdipClosePathFigure = _declare(gdiplus, "GdipClosePathFigure", ctypes.c_int,
                               [ctypes.c_void_p])
GdipFillPath = _declare(gdiplus, "GdipFillPath", ctypes.c_int,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
GdipDrawPath = _declare(gdiplus, "GdipDrawPath", ctypes.c_int,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
GdipCreateStringFormat = _declare(
    gdiplus, "GdipCreateStringFormat", ctypes.c_int,
    [ctypes.c_int, wintypes.WORD, ctypes.POINTER(ctypes.c_void_p)])
GdipDeleteStringFormat = _declare(gdiplus, "GdipDeleteStringFormat",
                                  ctypes.c_int, [ctypes.c_void_p])
GdipSetStringFormatAlign = _declare(gdiplus, "GdipSetStringFormatAlign",
                                    ctypes.c_int,
                                    [ctypes.c_void_p, ctypes.c_int])
GdipSetStringFormatLineAlign = _declare(
    gdiplus, "GdipSetStringFormatLineAlign", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int])
GdipSetStringFormatTrimming = _declare(
    gdiplus, "GdipSetStringFormatTrimming", ctypes.c_int,
    [ctypes.c_void_p, ctypes.c_int])
GdipCreateFontFamilyFromName = _declare(
    gdiplus, "GdipCreateFontFamilyFromName", ctypes.c_int,
    [LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
GdipDeleteFontFamily = _declare(gdiplus, "GdipDeleteFontFamily", ctypes.c_int,
                                [ctypes.c_void_p])
GdipCreateFont = _declare(gdiplus, "GdipCreateFont", ctypes.c_int,
                          [ctypes.c_void_p, ctypes.c_float, ctypes.c_int,
                           ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])
GdipDeleteFont = _declare(gdiplus, "GdipDeleteFont", ctypes.c_int,
                          [ctypes.c_void_p])
GdipDrawString = _declare(gdiplus, "GdipDrawString", ctypes.c_int,
                          [ctypes.c_void_p, LPCWSTR, ctypes.c_int,
                           ctypes.c_void_p, ctypes.POINTER(RectF),
                           ctypes.c_void_p, ctypes.c_void_p])
GdipMeasureString = _declare(
    gdiplus, "GdipMeasureString", ctypes.c_int,
    [ctypes.c_void_p, LPCWSTR, ctypes.c_int, ctypes.c_void_p,
     ctypes.POINTER(RectF), ctypes.c_void_p, ctypes.POINTER(RectF),
     ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)])


# ---------------------------------------------------------------------------
# 错误记录（渲染/窗口操作失败不应该把主程序弄崩，只记下来）
# ---------------------------------------------------------------------------
_errors: list[str] = []


def note_error(msg: str) -> None:
    """记一条底层错误（最多保留最近 8 条），供 --debug/排障读取。"""
    _errors.append(str(msg))
    del _errors[:-8]


def last_error() -> str:
    return _errors[-1] if _errors else ""


def errors() -> list[str]:
    return list(_errors)


class Win32Error(RuntimeError):
    pass


def argb(a: int, r: int, g: int, b: int) -> int:
    """GDI+ 的 ARGB 颜色（DWORD）。"""
    return ((int(a) & 0xFF) << 24) | ((int(r) & 0xFF) << 16) | \
        ((int(g) & 0xFF) << 8) | (int(b) & 0xFF)


def process_cpu_seconds() -> float:
    """当前进程累计 CPU 时间（秒，100ns 精度；自测脚本测 CPU 占用用）。"""
    if GetProcessTimes is None or GetCurrentProcess is None:
        return time.process_time()
    creation = FILETIME()
    exit_time = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    try:
        ok = GetProcessTimes(GetCurrentProcess(), ctypes.byref(creation),
                             ctypes.byref(exit_time), ctypes.byref(kernel),
                             ctypes.byref(user))
        if not ok:
            return time.process_time()
        total = ((kernel.dwHighDateTime << 32) | kernel.dwLowDateTime) \
            + ((user.dwHighDateTime << 32) | user.dwLowDateTime)
        return total / 1e7
    except Exception:
        return time.process_time()


def now() -> float:
    """单调时钟（秒），动画时间轴用。"""
    return time.monotonic()


# ---------------------------------------------------------------------------
# GDI+ 会话（惰性启动）
# ---------------------------------------------------------------------------
class _GdiplusSession:
    token: int = 0
    started: bool = False


def ensure_gdiplus() -> bool:
    """保证 GDI+ 已启动；import 本模块时不会调用。"""
    if _GdiplusSession.started:
        return bool(_GdiplusSession.token)
    inp = GdiplusStartupInput()
    inp.GdiplusVersion = 1
    token = ctypes.c_size_t()
    st = GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None)
    if st != GP_OK:
        note_error(f"GdiplusStartup 失败 status={st}")
        return False
    _GdiplusSession.token = int(token.value)
    _GdiplusSession.started = True
    return True


def _gp_check(st: int, what: str) -> bool:
    if st != GP_OK:
        note_error(f"GDI+ {what} 失败 status={st}")
        return False
    return True


def gp_pen(color: int, width: float, cap: int = 0, dash: int = -1) -> int:
    """创建 GDI+ 画笔（调用方负责 GdipDeletePen）。"""
    handle = ctypes.c_void_p()
    if not _gp_check(GdipCreatePen1(color & 0xFFFFFFFF, ctypes.c_float(width),
                                     GP_UNIT_PIXEL, ctypes.byref(handle)),
                     "GdipCreatePen1"):
        return 0
    if cap:
        GdipSetPenLineCap197819(handle, cap, cap, GP_DASH_CAP_ROUND)
    if dash >= 0:
        GdipSetPenDashStyle(handle, dash)
    return int(handle.value or 0) if handle.value else 0


def gp_brush(color: int) -> int:
    handle = ctypes.c_void_p()
    if not _gp_check(GdipCreateSolidFill(color & 0xFFFFFFFF,
                                         ctypes.byref(handle)),
                     "GdipCreateSolidFill"):
        return 0
    return int(handle.value or 0) if handle.value else 0


def gp_family(names: tuple[str, ...] | list[str]) -> int:
    """按顺序尝试字体族名，返回第一个可用的 GpFontFamily*（0=全失败）。"""
    for name in names:
        handle = ctypes.c_void_p()
        st = GdipCreateFontFamilyFromName(name, None, ctypes.byref(handle))
        if st == GP_OK and handle.value:
            return int(handle.value)
    note_error(f"GdipCreateFontFamilyFromName 失败: {names!r}")
    return 0


def gp_font(family: int, pixel_size: float, style: int = GP_FONT_STYLE_REGULAR) -> int:
    handle = ctypes.c_void_p()
    if not _gp_check(GdipCreateFont(family, ctypes.c_float(max(1.0, pixel_size)),
                                     style, GP_UNIT_PIXEL,
                                     ctypes.byref(handle)),
                     "GdipCreateFont"):
        return 0
    return int(handle.value or 0) if handle.value else 0


def gp_string_format(align: int = GP_STRING_ALIGN_NEAR,
                     line_align: int = GP_STRING_ALIGN_NEAR,
                     flags: int = GP_STRING_FLAG_NO_WRAP) -> int:
    handle = ctypes.c_void_p()
    if not _gp_check(GdipCreateStringFormat(flags, 0, ctypes.byref(handle)),
                     "GdipCreateStringFormat"):
        return 0
    GdipSetStringFormatAlign(handle, align)
    GdipSetStringFormatLineAlign(handle, line_align)
    GdipSetStringFormatTrimming(handle, 0)
    return int(handle.value or 0) if handle.value else 0


# ---------------------------------------------------------------------------
# DIB 画布（32bpp 自上而下，BGRA；UpdateLayeredWindow 的像素来源）
# ---------------------------------------------------------------------------
_screen_dc: int = 0


def get_screen_dc() -> int:
    """缓存的屏幕 DC（只读用途：CreateCompatibleDC / BitBlt / UpdateLayeredWindow）。"""
    global _screen_dc
    if not _screen_dc:
        _screen_dc = int(GetDC(None) or 0)
    return _screen_dc


_mul_table: Optional[bytes] = None


def _get_mul_table() -> bytes:
    """预乘查表：table[(a << 8) | c] = c * a / 255。"""
    global _mul_table
    if _mul_table is None:
        table = bytearray(65536)
        for a in range(256):
            base = a << 8
            for c in range(256):
                table[base | c] = (c * a) // 255
        _mul_table = bytes(table)
    return _mul_table


def premultiply_bgra(bits_addr: int, width: int, height: int) -> None:
    """就地预乘 alpha。

    UpdateLayeredWindow 要求像素是**预乘**的 BGRA，而 GDI+ 画出来的是直通 alpha。
    不预乘的话所有半透明边缘（圆环抗锯齿边、轨道、文字）会发黑。
    """
    if not bits_addr or width <= 0 or height <= 0:
        return
    n = width * height * 4
    buf = (ctypes.c_ubyte * n).from_address(bits_addr)
    table = _get_mul_table()
    for i in range(0, n, 4):
        a = buf[i + 3]
        if a == 255:
            continue
        if a == 0:
            buf[i] = 0
            buf[i + 1] = 0
            buf[i + 2] = 0
        else:
            base = a << 8
            buf[i] = table[base | buf[i]]
            buf[i + 1] = table[base | buf[i + 1]]
            buf[i + 2] = table[base | buf[i + 2]]


class Dib:
    """32bpp 自上而下 DIB + 内存 DC。

    ``bits`` 是像素首地址（BGRA，行序自上而下，stride = width*4）。
    """

    __slots__ = ("width", "height", "hdc", "hbitmap", "bits", "_old", "_closed")

    def __init__(self, width: int, height: int) -> None:
        if not ensure_gdiplus():
            raise Win32Error("GDI+ 初始化失败")
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._closed = False
        self.hdc = 0
        self.hbitmap = 0
        self.bits = 0
        self._old = None
        screen = get_screen_dc()
        if not screen:
            raise Win32Error("GetDC(None) 失败")
        self.hdc = int(CreateCompatibleDC(screen) or 0)
        if not self.hdc:
            raise Win32Error("CreateCompatibleDC 失败")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.width
        bmi.bmiHeader.biHeight = -self.height      # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = self.width * self.height * 4
        bits = ctypes.c_void_p()
        self.hbitmap = int(CreateDIBSection(self.hdc, ctypes.byref(bmi),
                                            DIB_RGB_COLORS,
                                            ctypes.byref(bits), None, 0) or 0)
        if not self.hbitmap or not bits.value:
            self.close()
            raise Win32Error("CreateDIBSection 失败")
        self.bits = int(bits.value)
        self._old = SelectObject(self.hdc, self.hbitmap)
        ctypes.memset(self.bits, 0, self.width * self.height * 4)

    def premultiply(self) -> None:
        premultiply_bgra(self.bits, self.width, self.height)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.hdc and self._old:
            SelectObject(self.hdc, self._old)
            self._old = None
        if self.hbitmap:
            DeleteObject(self.hbitmap)
            self.hbitmap = 0
        if self.hdc:
            DeleteDC(self.hdc)
            self.hdc = 0
        self.bits = 0

    def __del__(self) -> None:      # pragma: no cover - 兜底释放
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 基础窗口 / 屏幕 / DPI / 主题
# ---------------------------------------------------------------------------
def get_module_handle() -> int:
    return int(GetModuleHandleW(None) or 0)


def get_window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _enum(parent: int) -> list[int]:
    """枚举父窗口的全部后代（EnumChildWindows 本身是递归的）。"""
    out: list[int] = []

    @ENUMPROC
    def _cb(hwnd, _lparam):       # noqa: ANN001 - ctypes 回调签名固定
        out.append(int(hwnd or 0))
        return True

    if parent:
        EnumChildWindows(parent, _cb, 0)
    else:
        EnumWindows(_cb, 0)
    return out


def descendants(hwnd: int) -> list[int]:
    return _enum(hwnd)


def monitor_rect(hwnd: int = 0) -> Optional[tuple[int, int, int, int]]:
    """窗口所在/主显示器矩形（物理像素）。"""
    if hwnd:
        hmon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    else:
        hmon = MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def work_area(hwnd: int = 0) -> Optional[tuple[int, int, int, int]]:
    if hwnd:
        hmon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    else:
        hmon = MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    r = mi.rcWork
    return (r.left, r.top, r.right, r.bottom)


def set_dpi_awareness() -> None:
    """尽力把进程设为 Per-Monitor V2 DPI 感知（必须在建窗口前调用）。

    main.py 可能已经调用过；重复调用失败不影响（后两个是旧系统兜底）。
    """
    try:
        if SetProcessDpiAwarenessContext is not None:
            if SetProcessDpiAwarenessContext(
                    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        fn = shcore.SetProcessDpiAwareness
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_int]
        if fn(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return
    except Exception:
        pass
    try:
        if SetProcessDPIAware is not None:
            SetProcessDPIAware()
    except Exception:
        pass


def get_dpi_scale(hwnd: int = 0) -> float:
    """窗口/系统的 DPI 缩放比（96 DPI = 1.0）。"""
    dpi = 0
    try:
        if hwnd and GetDpiForWindow is not None:
            dpi = int(GetDpiForWindow(hwnd))
        if not dpi and GetDpiForSystem is not None:
            dpi = int(GetDpiForSystem())
    except Exception:
        dpi = 0
    if not dpi:
        try:
            dpi = int(GetDeviceCaps(get_screen_dc(), LOGPIXELSX))
        except Exception:
            dpi = 96
    return (dpi / 96.0) if dpi else 1.0


_theme_cache = {"t": -1.0, "light": False}


def taskbar_is_light() -> bool:
    """任务栏是否为浅色主题（读注册表 SystemUsesLightTheme，2 秒缓存）。"""
    stamp = now()
    if stamp - _theme_cache["t"] < 2.0:
        return bool(_theme_cache["light"])
    light = False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _kind = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            light = bool(int(value))
        finally:
            winreg.CloseKey(key)
    except Exception:
        light = False
    _theme_cache["t"] = stamp
    _theme_cache["light"] = light
    return light


# ---------------------------------------------------------------------------
# 任务栏 / TrafficMonitor 探测
# ---------------------------------------------------------------------------
def find_taskbar() -> int:
    return int(FindWindowW("Shell_TrayWnd", None) or 0)


def find_tray_notify(tray: int) -> int:
    if not tray:
        return 0
    return int(FindWindowExW(tray, None, "TrayNotifyWnd", None) or 0)


def find_trafficmonitor_taskbar_window(tray: int) -> int:
    """找 TrafficMonitor 的任务栏窗口：``#32770`` + 标题 ``TrafficMonitorTaskbarWindow``。

    TrafficMonitor 的窗口是 Shell_TrayWnd 的孙窗口（中间可能隔一层），所以递归枚举。
    """
    if not tray:
        return 0
    children = _enum(tray)
    # 先精确匹配；找不到再放宽到标题前缀，兼容 TrafficMonitor 版本差异
    for hwnd in children:
        if (get_class_name(hwnd) == "#32770"
                and get_window_text(hwnd) == "TrafficMonitorTaskbarWindow"):
            return hwnd
    for hwnd in children:
        if get_window_text(hwnd).startswith("TrafficMonitorTaskbar"):
            return hwnd
    return 0


def taskbar_state() -> Optional[dict]:
    """返回任务栏状态：hwnd/rect/monitor/visible/auto_hide/hidden/orient。"""
    tray = find_taskbar()
    if not tray:
        return None
    rect = get_window_rect(tray)
    if not rect:
        return None
    mon = monitor_rect(tray)
    if not mon:
        return None
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    horizontal = width >= height
    visible = bool(IsWindowVisible(tray))
    auto_hide = False
    try:
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        abd.hWnd = wintypes.HWND(tray)
        state = int(SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd)) or 0)
        auto_hide = bool(state & ABS_AUTOHIDE)
    except Exception:
        auto_hide = False
    if horizontal:
        hidden = (not visible) or height <= 8 or top >= mon[3] - 2
    else:
        hidden = (not visible) or width <= 8 or left >= mon[2] - 2
    return {
        "hwnd": tray,
        "rect": rect,
        "monitor": mon,
        "visible": visible,
        "auto_hide": auto_hide,
        "hidden": hidden,
        "horizontal": horizontal,
    }


# ---------------------------------------------------------------------------
# 鼠标 / 键盘小工具
# ---------------------------------------------------------------------------
def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not GetCursorPos(ctypes.byref(pt)):
        return (0, 0)
    return (int(pt.x), int(pt.y))


def key_down(vk: int) -> bool:
    try:
        return int(GetKeyState(int(vk))) < 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 分层窗口（WS_EX_LAYERED + UpdateLayeredWindow）
# ---------------------------------------------------------------------------
_BLEND = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
_windows: dict[int, "LayeredWindow"] = {}
_window_classes: dict[str, bool] = {}


def _dispatch_wndproc(hwnd, msg, wparam, lparam):        # noqa: ANN001
    """模块级统一窗口过程：按 HWND 分发给对应的 LayeredWindow 实例。

    这样窗口类只需要注册一次，销毁/重建窗口时也不会把回调指针弄丢。
    """
    win = _windows.get(int(hwnd or 0))
    if win is not None:
        try:
            result = win._handle_message(msg, wparam, lparam)
            if result is not None:
                return result
        except Exception as exc:        # 窗口过程绝不能把异常抛给系统
            note_error(f"WndProc 异常: {exc!r}")
    return DefWindowProcW(hwnd, msg, wparam, lparam)


_DISPATCH_PROC = WNDPROC(_dispatch_wndproc)


def _ensure_window_class(class_name: str) -> bool:
    if _window_classes.get(class_name):
        return True
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = CS_DBLCLKS                       # 双击消息 WM_LBUTTONDBLCLK
    wc.lpfnWndProc = _DISPATCH_PROC
    wc.hInstance = get_module_handle()
    wc.hCursor = LoadCursorW(None, wintypes.LPCWSTR(IDC_ARROW))
    wc.lpszClassName = class_name
    atom = RegisterClassExW(ctypes.byref(wc))
    if not atom:
        err = ctypes.get_last_error()
        if err != 1410:                          # 1410 = 类已存在
            note_error(f"RegisterClassExW 失败 class={class_name} err={err}")
            return False
    _window_classes[class_name] = True
    return True


class LayeredWindow:
    """一个 per-pixel alpha 的分层窗口（不显示任务栏按钮、不抢焦点）。"""

    def __init__(self, class_name: str, title: str = "") -> None:
        self._class_name = class_name
        self._title = title
        self._hwnd = 0
        self._visible = False
        self.message_handler: Optional[Callable[[int, int, int], Optional[int]]] = None

    # -- 生命周期 ----------------------------------------------------------
    def create(self, x: int = 0, y: int = 0, width: int = 1,
               height: int = 1) -> bool:
        if self._hwnd:
            return True
        if not _ensure_window_class(self._class_name):
            return False
        ex_style = (WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                    | WS_EX_NOACTIVATE)
        hwnd = CreateWindowExW(
            ex_style, self._class_name, self._title, WS_POPUP,
            int(x), int(y), max(1, int(width)), max(1, int(height)),
            None, None, get_module_handle(), None)
        if not hwnd:
            note_error(f"CreateWindowExW 失败 class={self._class_name} "
                       f"err={ctypes.get_last_error()}")
            return False
        self._hwnd = int(hwnd)
        _windows[self._hwnd] = self
        return True

    def destroy(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        self._hwnd = 0
        self._visible = False
        _windows.pop(hwnd, None)
        DestroyWindow(hwnd)

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def visible(self) -> bool:
        return bool(self._hwnd) and bool(IsWindowVisible(self._hwnd))

    # -- 消息 --------------------------------------------------------------
    def _handle_message(self, msg: int, wparam: int, lparam: int) -> Optional[int]:
        if msg == WM_DESTROY:
            _windows.pop(self._hwnd, None)
            self._hwnd = 0
            self._visible = False
            return 0
        if self.message_handler is not None:
            return self.message_handler(msg, wparam, lparam)
        return None

    # -- 显示 / 位置 -------------------------------------------------------
    def show(self) -> None:
        if not self._hwnd:
            return
        ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._visible = True

    def hide(self) -> None:
        if not self._hwnd:
            return
        ShowWindow(self._hwnd, SW_HIDE)
        self._visible = False

    def move(self, x: int, y: int) -> None:
        if not self._hwnd:
            return
        SetWindowPos(self._hwnd, HWND_TOPMOST, int(x), int(y), 0, 0,
                     SWP_NOSIZE | SWP_NOACTIVATE)

    def present(self, surface, x: int, y: int) -> bool:
        """把 RingSurface 的像素用 UpdateLayeredWindow 贴到 (x, y)。"""
        if not self._hwnd or surface is None:
            return False
        screen = get_screen_dc()
        if not screen:
            return False
        pt_dst = wintypes.POINT(int(x), int(y))
        size = wintypes.SIZE(max(1, surface.width), max(1, surface.height))
        pt_src = wintypes.POINT(0, 0)
        ok = UpdateLayeredWindow(
            self._hwnd, screen, ctypes.byref(pt_dst), ctypes.byref(size),
            surface.hdc, ctypes.byref(pt_src), 0, ctypes.byref(_BLEND),
            ULW_ALPHA)
        if not ok:
            note_error(f"UpdateLayeredWindow 失败 err={ctypes.get_last_error()}")
            return False
        if not self._visible:
            self.show()
        return True

    def get_rect(self) -> Optional[tuple[int, int, int, int]]:
        return get_window_rect(self._hwnd)


# ---------------------------------------------------------------------------
# 屏幕截图 → PNG（仅自测 / 调试用；GDI+ PNG 编码器，不依赖第三方库）
# ---------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_CLSID_PNG = GUID(0x557CF406, 0x1A04, 0x11D3,
                  (0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))

GdipSaveImageToFile = _declare(gdiplus, "GdipSaveImageToFile", ctypes.c_int,
                               [ctypes.c_void_p, LPCWSTR,
                                ctypes.POINTER(GUID), ctypes.c_void_p])


def save_bgra_png(bits_addr: int, width: int, height: int, path: str) -> bool:
    """把自上而下 BGRA 像素（alpha 会被强制置为 255）写成 PNG 文件。"""
    if not bits_addr or width <= 0 or height <= 0:
        return False
    if not ensure_gdiplus() or GdipSaveImageToFile is None:
        return False
    total = width * height * 4
    buf = (ctypes.c_ubyte * total).from_address(bits_addr)
    for i in range(3, total, 4):
        buf[i] = 255
    bitmap = ctypes.c_void_p()
    status = gdiplus.GdipCreateBitmapFromScan0(
        int(width), int(height), int(width) * 4, PIXEL_FORMAT_32BPP_ARGB,
        ctypes.c_void_p(bits_addr), ctypes.byref(bitmap))
    if status != GP_OK or not bitmap.value:
        note_error(f"截图位图创建失败 status={status}")
        return False
    try:
        status = GdipSaveImageToFile(bitmap, str(path),
                                     ctypes.byref(_CLSID_PNG), None)
        if status != GP_OK:
            note_error(f"GdipSaveImageToFile 失败 status={status}")
            return False
        return True
    finally:
        gdiplus.GdipDisposeImage(bitmap)


def capture_screen_png(left: int, top: int, width: int, height: int,
                       path: str) -> bool:
    """截取屏幕上的一块矩形并保存为 PNG（自测脚本用）。"""
    width = max(1, int(width))
    height = max(1, int(height))
    try:
        dib = Dib(width, height)
    except Win32Error as exc:
        note_error(f"截图失败: {exc}")
        return False
    try:
        if not BitBlt(dib.hdc, 0, 0, width, height, get_screen_dc(),
                      int(left), int(top), SRCCOPY):
            note_error("截图 BitBlt 失败")
            return False
        return save_bgra_png(dib.bits, width, height, path)
    finally:
        dib.close()
