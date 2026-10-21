# 开发契约（CONTRACT）—— 外设电量圆环工具

> 所有并行开发分支都按本文件对接。**先读本文，再动代码。**
> 工作目录：`D:\2.4P`　Python：`S:\AnConda\python.exe`（3.12.7，`pythonw.exe` 同目录）
> 参考实现：`_ref/repo/`（纯 ctypes 的 HID/托盘参考）、`_ref/probe_rog.py`（ROG 真机验证脚本）、
> `_ref/probe_logitech.py`（罗技真机验证脚本）、`_ref/rogtray/`（Rust 协议事实参考，只抄事实不抄代码）。
> 方案：`方案.md`　设计图：`docs/`

## 0. 铁律

1. **零第三方依赖**：只用 Python 标准库 + `ctypes` / `tkinter`；不要 import numpy/PIL/requests。
2. **只读设备**：只发查询命令，绝不写设备设置/固件/灯光/接收器寄存器/配对表。
3. **不联网、不写日志文件**（只有 `--debug` 时控制台输出）。
4. **Win64 句柄安全**：所有返回指针/句柄的 Win32 API 必须声明 `restype = ctypes.c_void_p`，参数用 `c_void_p`；否则句柄被截断（已验证会崩）。
5. 中文注释/文案用 UTF-8；代码风格 4 空格、类型注解、无外部格式化工具要求。
6. 每个模块必须能 `python -c "import xxx"` 通过；改完自测再报告。
7. 写作用域之外的文件不要动；需要改契约先找 Lead。

## 1. 目录与所有权

| 路径 | 负责人 | 说明 |
|---|---|---|
| `models.py`、`config.py`、`main.py` | Lead | 数据契约 / 配置 / 入口与消息循环 |
| `ui/tray.py` | Lead | 托盘图标 + 菜单 + 隐藏/恢复 |
| `devices/`、`probe.py` | core-dev | 设备发现与电量后端 |
| `ui/render.py`、`ui/overlay.py`、`ui/hover.py`、`ui/win32.py` | ui-dev | 任务栏圆环叠加窗口、GDI+ 渲染、悬停卡片 |
| `ui/panel.py`、`scripts/`、`README.md` | panel-dev | 设置面板、安装/卸载/自启脚本、使用文档 |
| `docs/`、`方案.md` | Lead | 文档 |

被 import 的模块名：`models`、`config`、`devices.reader`、`devices`、`ui.render`、`ui.overlay`、`ui.hover`、`ui.tray`、`ui.panel`。
（`ui/__init__.py`、`devices/__init__.py` 由各自负责人创建，空文件即可。）

## 2. 运行模式（main.py 命令行）

```
pythonw main.py                # 常驻：任务栏圆环 + 托盘（开机自启用这个）
python  main.py --debug        # 前台带控制台输出
python  main.py --once         # 打印一次所有设备读数后退出（验收用）
python  main.py --demo         # 假数据驱动 UI（无设备时联调用）
python  main.py --quit         # 让正在运行的实例退出
python  main.py --status       # 打印配置/进程状态
python  main.py --panel        # 打开设置面板（等价于启动 ui/panel.py）
```

主程序窗口类名：`PeripheralBatteryMainWnd`；单实例 Mutex：`PeripheralBattery_SingleInstance_Mutex`。
消息（`ctypes.windll.user32.RegisterWindowMessageW`）：
- `PeripheralBattery_Reload` —— 面板保存配置后 `PostMessage(HWND_BROADCAST, msg)`，主程序收到后 `config.load()` 并让各 UI 重载。
- `PeripheralBattery_Quit` —— 请求退出。
- `TaskbarCreated` —— Explorer 重启后重建托盘图标 / 重新挂接圆环。

## 3. 数据契约（models.py，已写好，不要改字段语义）

> 注意：文件名是 `models.py` 而不是 `types.py`——避免遮蔽 Python 标准库 `types` 模块（dataclasses 等会 import 它）。

```python
@dataclass
class Reading:
    device_id: str; name: str
    level: Optional[int]          # 0-100 / None
    status: str                   # discharging|charging|full|unknown|unplugged|error
    voltage_mv: Optional[int]
    source: str; updated_at: float; detail: str
    estimated: bool               # 估算百分比
    charging: Optional[bool]      # 协议给没给
    inferred: bool                 # 推断来的
```

## 4. 设备层接口（core-dev 提供）

```python
# devices/reader.py
def read_all(debug: bool = False) -> list[Reading]:
    """同步读一遍所有已发现设备；永不抛异常（失败写入 Reading.detail）。
    内部做发现缓存 + 热插拔重扫；耗时目标 < 3s（含休眠设备超时）。"""

def read_once() -> list[Reading]:      # 同 read_all，供 --once
def demo_readings() -> list[Reading]:  # --demo 用：键盘 62%+鼠标 ~77%，带充电动画样例

# 可选（panel 用）
def discover(force: bool = False) -> list[DeviceInfo]
```

**已知协议事实（已真机验证，直接照做，但要在真机上复测）**

1. **ROG OMNI 接收器 `0B05:1ACE`**
   - 打开 MI_02 的 HID 集合（路径含 `mi_02&col01/02/03`），输出报文 64 字节（含 report id 在 `[0]`），**只写一次、读回复**；回复可能来自同集合。
   - 配对列表：col01，report id `0x01`，报文 `01 A0 00` + 0；回复 `[5:]` 每 4 字节一个 LE16 PID（`0x1C25` = ROG Azoth X 夜魔X），遇 0 结束。
   - 键盘电量：col02，report id `0x02`，报文 `02 12 01`；有效回复 `[1]=0x12 [2]=0x01`；电量 `[6]`、充电 `[9]==1`；无应答 = `[1]=0xFF [2]=0xAA`。
   - 鼠标电量：col03，report id `0x03`，报文 `03 12 07`；电量 `[5]`（0–4 档的型号 ×25；0–100 的型号 ×1），充电 `[10]!=0`。
   - 读取超时 500–700ms；设备在休眠/关闭时回 `FF AA`，标 `unplugged/休眠`。
2. **罗技 Lightspeed `046D:C539`（也支持 C53A/C53D/C53F/C541/C545/C547/C54D Unifying C52B/C532 Bolt C548）**
   - HID++ 接口：路径含 `mi_02`，usage page `0xFF00`；**短集合 usage 0x0001（out 7 字节）发请求，长集合 usage 0x0002（20 字节）收回复**；两个集合要同时打开。
   - 请求（7 字节）：`[0]=0x10, [1]=设备索引, [2]=feature 索引, [3]=function<<4 | 0x0C(软件ID), [4..]=参数`。
   - 接收器槽位 1..6；读配对短名：`[0]=0x10,[1]=0xFF,[2]=0x83,[3]=0xB5,[4]=0x40+slot-1,[5]=0x00`，回复在长集合，`[4]` 回显子寄存器、`[5]`=名字长度、`[6:]`=名字。
   - 错误：`[2]==0x8F`=HID++1.0 错误；`[2]==0xFF`=HID++2.0 错误。
   - 找特性：请求 `[idx,0x00,0x0C,id>>8,id&0xFF,0,0]`；回复 `[4]`=特性索引（0=没有）。
   - 电量特性按顺序试：`0x1004`（func 1：`[0]`=soc%，`[2]`=状态 1–4 充电）、`0x1000`（func 0：`[0]`=电量%，`[2]`=状态）、`0x1001`（func 0：`[0..1]`=大端 mV，`[2]` bit7=充电）、`0x1F20`（func 0：同电压，`[2]` bit1=充电）。
   - 设备名：特性 `0x0005`（func 0 取长度，func 1 分段取字符串）。
   - 本机 G PRO Wireless：只有 `0x1001`，实测 3967 mV → 按锂电池曲线估算 ≈77%，显示时 `estimated=True`。
   - 超时 2s（首次唤醒慢）；超时/无应答 = 休眠。
3. 兼容旧仓库协议：`373B:1216`（ATK/VXE）与 `3654:6901`（DARKRIM）——代码在 `_ref/repo/battery.py`、`darkrim_proto.py`，移植成后端即可，**本机没设备不需要真机验证**。
4. 兜底：标准 HID 电量用法（`0x0006/0x0020`、`0x0001/0x0006`）与蓝牙 PnP 电池属性（`{104EA319-6EE2-4701-BD47-8DDBF425BBE5},2`，参考 `_ref/repo/bt_battery.py`）。
5. 充电推断（A 方案）：协议给就用；否则"插线重枚举/电量持续上升"推断，`inferred=True`，UI 文案加"（推断）"。

## 5. UI 层接口

### 5.1 `ui/render.py`（ui-dev）

```python
class RingSurface:
    """GDI+ 绘制目标：32bpp DIB + 内存 DC，尺寸可重建。"""
    def __init__(self, width: int, height: int)
    def resize(self, width: int, height: int) -> None
    def render_rings(self, readings: list[Reading], cfg: dict, dpi: float,
                     anim_t: float) -> None      # 直接画到内部 DIB（预乘 alpha 由本类处理）
    def render_hover(self, readings: list[Reading], cfg: dict, dpi: float,
                     width: int, height: int) -> None
    @property
    def hdc(self) -> int
    @property
    def hbitmap(self) -> int
    @property
    def width(self) -> int
    @property
    def height(self) -> int

def build_tray_ico(readings: list[Reading], cfg: dict) -> bytes:
    """生成多尺寸 ICO 文件内容（16/20/24/32）。"""
```

样式 **B（已选定）**：**无底板**，圆环直接浮在任务栏上；
- 环径 `cfg["ring_size"]`（逻辑 px，默认 30），线宽 ≈ 环径×0.11；两环间距 8 逻辑 px；
- 环内居中百分比（Segoe UI Semibold，字号≈环径×0.32；`estimated` 前缀 `≈`；`level is None` 显示 `—`）；
- 环底轨道半透明白（深色任务栏）/半透明黑（浅色任务栏）；
- 颜色：≥60 绿 `(93,220,138)`、30–59 琥珀 `(245,196,81)`、<20 红 `(255,107,107)`、未知灰 `(150,156,166)`；
- 充电中：白色亮弧沿环扫动（30fps，`anim_t` 秒）+ 右下角小闪电徽标；
- 深色主题下给环与文字加 1px 深色描边提高浅色壁纸上的可读性；浅色主题反之；
- 背景完全透明（alpha=0），**合成前必须预乘 alpha**（`UpdateLayeredWindow` 要求）。
- 悬停卡片：圆角深色面板，两行设备（环 20px + 名称 + 百分比 + 状态 + 更新时间），底部一句提示。

### 5.2 `ui/overlay.py`（ui-dev）

> **2026-10-21 最终需求**：状态栏按"软件里勾选要显示的设备"显示，**数量不限**（1 台=单环，N 台=N 环并排）；
> **不要**悬停卡片、HUD 悬浮、全屏隐藏、鼠标穿透。

```python
class TaskbarOverlay:
    def __init__(self, cfg: dict, callbacks: dict)
    # callbacks：on_left_click() / on_double_click() / on_right_click() / on_position_changed(dx,dy)
    def start(self) -> bool
    def set_readings(self, readings: list[Reading]) -> None   # 内部按 device_hidden 过滤 + device_order 排序
    def reload_config(self, cfg: dict) -> None
    def reposition(self) -> None
    def destroy(self) -> None
    @property
    def hwnd(self) -> int
    @property
    def rect(self)
```

- 窗口 `WS_POPUP` + `WS_EX_LAYERED|WS_EX_TOPMOST|WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE`；`UpdateLayeredWindow` + 预乘 alpha。
- 设备筛选：`cfg["device_hidden"]`（device_id 列表）取反 + `cfg["device_order"]` 优先排序；上限 `MAX_RINGS=8`，并按屏幕宽度截断（超限优先保留低电量）。
- 无可见设备时画 1 个灰色虚线环 + "—"（窗口仍在）。
- 位置：`Shell_TrayWnd` 子窗口 `#32770 "TrafficMonitorTaskbarWindow"` 左侧 6px（没有则 `TrayNotifyWnd` 左侧 6px），垂直居中；`offset_x/y` 叠加；任务栏不可见时隐藏；`TaskbarCreated` 重建。
- 动画：充电亮弧 30fps、数值过渡；静止 0 定时器。
- 字体：环内数字 Segoe UI Semibold、字号 ≈环径×0.36（最小 9px），按文字宽度自动收缩到环内直径的 0.82 倍以内；GDI+ ClearType/GridFit。
- 左键刷新 / 双击打开主窗口 / 右键菜单；Alt+拖动保存 offset。

### 5.3 `ui/hover.py` —— **已取消**

需求删除悬停卡片，主程序不再 import；文件保留但不得被引用。

### 5.4 `ui/tray.py`（Lead）

```python
class TrayIcon:
    def __init__(self, hwnd: int, callback_msg: int, cfg: dict, callbacks: dict)
    def add(self) / update(readings) / remove(self)
```
- `Shell_NotifyIconW`，图标由 `ui.render.build_tray_ico` 生成（写临时 .ico + `LoadImageW`，缓存）；
- 左键：弹设备卡片/立刻刷新；右键：原生菜单（打开面板 / 立即刷新 / 显示托盘图标✓ / 开机自启✓ / 退出）；
- `cfg["show_tray_icon"]=False` → `NIM_DELETE`；重新显示必须 `NIM_ADD`（不要用 NIM_MODIFY 改回）。

## 6. 面板（panel-dev）

`ui/panel.py` 是**独立进程**的 tkinter 主窗口（同时是"主程序 UI"）。

设计图：`docs/10-主界面-设备页.png`、`docs/11-主界面-设置页.png`。

```bash
pythonw ui/panel.py            # 打开主窗口（设备页）
```

最终需求（2026-10-21）：
- 窗口**可自由缩放**：`resizable(True, True)`，最小 480×560；布局 grid 自适应；内容超高滚动；窗口位置/大小保存到 `config.window_x/y/width/height`。
- 标签页：`设备` / `设置` / `关于`。
- **设备页**：设备卡片（大圆环 + 名称 + VID:PID/连接方式 + 状态/更新时间 + 单卡刷新）；
  每张卡片有复选框「**在状态栏显示**」（默认勾选；不勾选 = device_id 写入 `config.device_hidden`，数量不限）；
  快捷开关只保留「显示托盘图标」「电量刷新间隔」；圆环颜色图例；底部「打开设置」「立即刷新」。
- **设置页**：任务栏圆环（尺寸 小24/中30/大34、停靠位置（自动/任务栏））；通知区域（显示托盘图标）；
  通用（刷新间隔、开机自启、主题）；设备管理（改名、查看 device_id）；「设备探测/测试」入口（启动 probe.py）；
  「恢复默认」「保存并应用」。**不要**全屏隐藏、鼠标穿透、悬停卡片这三个开关。
- **关于页**：版本、隐私（不联网/不写日志）、协议致谢（Solaar/G-Helper）、配置路径。
- 字体：Microsoft YaHei UI 10pt 正文、标题 15pt 加粗、正文最小 9pt；数字 Segoe UI Semibold；做 DPI 感知。
- 数据：`devices.reader.read_all()`，失败优雅降级；保存 = `config.save()` + `PostMessage(HWND_BROADCAST, RegisterWindowMessageW("PeripheralBattery_Reload"))`。
- 不产生任务栏按钮（`WS_EX_TOOLWINDOW`）。

## 7. 安装脚本（panel-dev）

- `scripts/install.ps1`：当前用户级；创建
  1) 启动文件夹 `外设电量圆环.lnk` → `wscript.exe "<root>\scripts\run_hidden.vbs"`（开机自启，任务管理器可见）；
  2) 桌面 / 开始菜单快捷方式 → 同上（默认打开面板：参数 `--panel`）；
  3) 写入 `scripts/python-path.txt`（`pythonw.exe` 绝对路径）供 vbs 复用；
  4) 生成 `scripts/install-manifest.json`；可 `-DryRun`、`-NoStartup`、`-Uninstall`。
- `scripts/run_hidden.vbs`：从 `python-path.txt` 读 `pythonw.exe`，无黑框启动 `main.py`（参数透传）。
- `scripts/uninstall.ps1`：删除上述快捷方式；保留程序文件。
- 入口脚本是 `main.py`（不是 batt.py）；仓库根目录 `D:\2.4P`。

## 8. 自测命令（每人交付前必须跑）

```bash
S:\AnConda\python.exe -c "import models, config; print(config.load()['style'])"
S:\AnConda\python.exe -m py_compile main.py devices\reader.py ui\render.py ui\overlay.py ui\hover.py ui\tray.py ui\panel.py probe.py
S:\AnConda\python.exe main.py --demo --debug        # 假数据：任务栏应出现会动的圆环
S:\AnConda\python.exe main.py --once                # 真机：应打印 键盘/鼠标 两行
```

## 9. 汇报格式（队友 → Lead）

完成后用 `send_message` 发给 `lead`：
1) 改了哪些文件；2) 怎么自测的（命令 + 关键输出）；3) 未完成/风险点；4) 需要 Lead 注意的接口变更。
不要只说"完成了"。
