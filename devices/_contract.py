# -*- coding: utf-8 -*-
"""devices/_contract.py —— 加载全项目共享的数据契约（``models.py``）。

背景（历史坑，已由 Lead 按方案 A 解决）
---------------------------------------
契约文件原来叫根目录的 ``types.py``，但 ``types`` 是 Python **标准库**模块名，
解释器启动时就已经把它放进 ``sys.modules`` 了，因此 ``import types`` 拿到的
永远是标准库模块，实测:

    cd D:\\2.4P
    S:\\AnConda\\python.exe -c "from types import Reading"
    ImportError: cannot import name 'Reading' from 'types' (S:\\AnConda\\Lib\\types.py)

Lead 已把契约改名为 ``models.py``，名字冲突消失。本模块仍然保留一层兼容：

1. ``load()``：依次尝试 ``models.py`` / ``contract.py`` / ``types.py``；
   前两者走**正常 import**，保证 ``from models import Reading`` 与设备层拿到
   同一个类对象（isinstance 检查靠这个）；只有旧的 ``types.py`` 才按文件路径加载；
2. ``install_types_shim()``：万一回退到 ``types.py``，把契约模块合并进
   ``sys.modules["types"]``（标准库同名属性全部保留），让
   ``from types import Reading`` 在 ``import devices`` 之后也能用；
   契约已叫 ``models.py`` 时它是空操作。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types as _stdlib_types
from types import ModuleType
from typing import Any, Optional

# 项目根目录（devices/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 契约文件的候选名（按优先级）。Lead 已把 types.py 改名为 models.py，
# 旧的 types.py 名字保留在候选里只是为了兼容改名前的分支。
_CANDIDATES = ("models.py", "contract.py", "types.py")

# 加载到 sys.modules 时用的内部名字（避免与标准库 types 抢名字）
_MODULE_NAME = "peripheral_contract"

# 缓存加载结果
_loaded: Optional[ModuleType] = None


def _load_contract() -> ModuleType:
    """加载契约模块，并缓存到 sys.modules[_MODULE_NAME]。

    契约文件是 ``models.py`` / ``contract.py`` 时**走正常的 import**（先把项目根
    目录放进 sys.path），这样 ``from models import Reading`` 与设备层拿到的是
    **同一个类对象**（isinstance 检查、dataclass 语义都靠这个）。
    只有旧的 ``types.py`` 名字才需要按文件路径加载（因为和标准库重名）。
    """
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    for filename in _CANDIDATES:
        path = os.path.join(PROJECT_ROOT, filename)
        if not os.path.isfile(path):
            continue
        module_name = os.path.splitext(filename)[0]
        if module_name != "types":
            # 正常 import：与项目其它模块共享同一个模块对象
            if PROJECT_ROOT not in sys.path:
                sys.path.insert(0, PROJECT_ROOT)
            module = importlib.import_module(module_name)
            sys.modules[_MODULE_NAME] = module
            return module
        # types.py：只能按路径加载，避免命中标准库 types
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
        if spec is None or spec.loader is None:  # pragma: no cover - 理论上不会发生
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_MODULE_NAME, None)
            raise
        return module
    raise ImportError(
        "找不到项目数据契约文件：在 %s 下尝试了 %s"
        % (PROJECT_ROOT, " / ".join(_CANDIDATES))
    )


def load() -> ModuleType:
    """返回契约模块（幂等）。"""
    global _loaded
    if _loaded is None:
        _loaded = _load_contract()
    return _loaded


# 契约里的两个数据类（延迟到第一次访问时加载，import 本模块本身不触发文件 IO）
def __getattr__(name: str) -> Any:
    if name in ("Reading", "DeviceInfo", "STATUS_TEXT"):
        return getattr(load(), name)
    raise AttributeError(name)


def reading_class() -> type:
    """返回契约里的 Reading 类（显式函数版，便于 IDE 与测试使用）。"""
    return load().Reading


def device_info_class() -> type:
    """返回契约里的 DeviceInfo 类。"""
    return load().DeviceInfo


def install_types_shim() -> bool:
    """把项目契约模块合并进 ``sys.modules["types"]``。

    只在契约文件确实叫 ``types.py`` 时安装（此时才存在名字冲突）；
    标准库 types 的全部属性都会复制过去，因此 tkinter / json / unittest /
    dataclasses / enum 等照常工作。

    返回 True 表示已安装（或本来就是我们装的），False 表示无需安装。
    """
    contract = load()
    contract_file = os.path.abspath(getattr(contract, "__file__", "") or "")
    if os.path.basename(contract_file).lower() != "types.py":
        return False  # 契约已改名，不存在冲突
    current = sys.modules.get("types")
    if current is contract:
        return True
    # 标准库 types 的全部公开属性补齐到契约模块上（不覆盖契约自己的名字）
    for name in dir(_stdlib_types):
        if not hasattr(contract, name):
            try:
                setattr(contract, name, getattr(_stdlib_types, name))
            except Exception:  # pragma: no cover - 只读属性时忽略
                pass
    sys.modules["types"] = contract
    return True


__all__ = [
    "PROJECT_ROOT",
    "load",
    "reading_class",
    "device_info_class",
    "install_types_shim",
]
