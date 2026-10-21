# -*- coding: utf-8 -*-
"""devices/__init__.py —— 设备读取层。

对外只暴露 :mod:`devices.reader` 里的接口（见 docs/CONTRACT.md §4）::

    from devices import reader
    reader.read_all()        # 读一遍所有已发现设备 -> list[Reading]
    reader.discover()        # 发现阶段 -> list[DeviceInfo]

导入本包时会自动做两件兼容处理：

* 按文件路径加载项目数据契约（``models.py``，旧分支里可能还叫 ``types.py``），
  见 :mod:`devices._contract`；
* 如果契约文件仍叫 ``types.py``，顺手把标准库 ``types`` 合并替换掉，
  让队友的 ``from types import Reading`` 在 ``import devices`` 之后可用。

设备层只用 Python 标准库 + ctypes，零第三方依赖。
"""

from __future__ import annotations

from devices import _contract

# 安装 types 兼容垫片（契约已改名为 models.py 时是空操作）
_contract.install_types_shim()

__all__ = ["reader", "_contract"]
__version__ = "1.0.0"
