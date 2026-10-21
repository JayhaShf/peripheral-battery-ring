# -*- coding: utf-8 -*-
"""ui 包：任务栏圆环视觉层（样式 B）。

对外模块：
* ``ui.render``  —— GDI+ 绘制（RingSurface / build_tray_ico）
* ``ui.overlay`` —— 任务栏叠加窗口（TaskbarOverlay，单环）
* ``ui.win32``   —— 私有底层封装（Win32/GDI/GDI+），不属于对外契约
* ``ui.tray``    —— 托盘图标（Lead 负责）

所有模块只依赖 Python 标准库 + ctypes，不 import 任何第三方库。
"""
