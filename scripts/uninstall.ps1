#Requires -Version 5.1
<#
.SYNOPSIS
    卸载「外设电量圆环」的入口：开机自启项、桌面快捷方式、开始菜单项（含残留注册表 Run 项）。
    只删「入口」，不删程序本体（main.py / ui\ / devices\ / scripts\ 全部保留）。可反复执行。

.DESCRIPTION
    清理范围：
      1. %APPDATA%\...\Startup\外设电量圆环.lnk
      2. <桌面>\外设电量圆环.lnk
      3. ...\Programs\外设电量圆环\  （面板 / 启动 / 卸载 三个 .lnk，目录空了就删掉）
      4. install.ps1 写下的 install-manifest.json（其中记录过的自定义路径也一并清理）
      5. HKCU\...\Run 下可能的旧式自启项（遗留兼容，不存在不报错）
    另：卸载前会尽量结束正在运行的 main.py 进程，否则托盘图标会残留。

.PARAMETER Root
    仓库根目录，默认取本脚本所在目录的上一级（即 D:\2.4P）。

.PARAMETER Yes
    跳过"确认卸载"提问（脚本调用 / 静默卸载）。

.PARAMETER NoPause
    结束时不等按键（脚本调用）。

.PARAMETER KeepRunning
    不结束正在运行的圆环进程。

.PARAMETER StartupOnly
    只删开机自启项（面板的「开机自启」勾选框用这个），其他入口不动。

.PARAMETER KeepManifest
    保留 install-manifest.json（默认删除）。

.PARAMETER DryRun
    只打印将删除什么，不实际删除。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

.EXAMPLE
    .\scripts\uninstall.ps1 -Yes -NoPause

.EXAMPLE
    .\scripts\uninstall.ps1 -StartupOnly -Yes -NoPause -Quiet
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Yes,
    [switch]$NoPause,
    [switch]$KeepRunning,
    [switch]$StartupOnly,
    [switch]$KeepManifest,
    [switch]$DryRun,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$Text) if (-not $Quiet) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan } }
function Write-Ok   { param([string]$Text) Write-Host "   [OK] $Text" -ForegroundColor Green }
function Write-Note { param([string]$Text) if (-not $Quiet) { Write-Host "   [--] $Text" -ForegroundColor Gray } }
function Write-Warn2 { param([string]$Text) Write-Host "   [!!] $Text" -ForegroundColor Yellow }

if (-not $Root) {
    if ($PSScriptRoot) { $Root = Split-Path -Parent $PSScriptRoot }
    else { $Root = (Get-Location).Path }
}
$Root      = [System.IO.Path]::GetFullPath($Root)
$scriptDir = Join-Path $Root 'scripts'
$manifest  = Join-Path $scriptDir 'install-manifest.json'
$appName   = '外设电量圆环'

Write-Host ''
Write-Host "$appName —— 卸载入口" -ForegroundColor White
Write-Note "仓库目录: $Root"
Write-Note '只删快捷方式与自启项，程序文件保留。'
if ($DryRun) { Write-Warn2 'DryRun 模式：只打印，不删除' }

# ------------------------------------------------------------------ 待删清单
$targets = New-Object System.Collections.Generic.List[object]

function Add-Target {
    param([string]$Path, [string]$Kind)
    if (-not $Path) { return }
    foreach ($t in $targets) { if ($t.path -ieq $Path) { return } }
    [void]$targets.Add([pscustomobject]@{ path = $Path; kind = $Kind })
}

$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { $startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
$desktopDir = [Environment]::GetFolderPath('Desktop')
if (-not $desktopDir) { $desktopDir = Join-Path $env:USERPROFILE 'Desktop' }
$programsDir = [Environment]::GetFolderPath('Programs')
if (-not $programsDir) { $programsDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs' }

$smDir = Join-Path $programsDir $appName

Add-Target (Join-Path $startupDir "$appName.lnk") '启动项'

if (-not $StartupOnly) {
    Add-Target (Join-Path $desktopDir "$appName.lnk") '桌面快捷方式'
    Add-Target (Join-Path $smDir "$appName 设置面板.lnk") '开始菜单(设置面板)'
    Add-Target (Join-Path $smDir "$appName.lnk") '开始菜单(启动圆环)'
    Add-Target (Join-Path $smDir "卸载 $appName.lnk") '开始菜单(卸载项)'

    # 清单里记录过的（可能用了非默认路径/自定义 PanelArg）
    if (Test-Path -LiteralPath $manifest) {
        try {
            $m = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($s in @($m.shortcuts)) {
                if ($s -and $s.path) { Add-Target $s.path '来自安装清单' }
            }
        } catch {
            Write-Warn2 "安装清单解析失败（忽略）: $($_.Exception.Message)"
        }
    }
}

# ------------------------------------------------------------------ 确认
Write-Step "计划清理 $($targets.Count) 个入口"

$present = @($targets | Where-Object { Test-Path -LiteralPath $_.path })
Write-Note "存在: $($present.Count) 个，缺失: $($targets.Count - $present.Count) 个（缺失会跳过，不报错）"
foreach ($t in $present) { if (-not $Quiet) { Write-Host "   - [$($t.kind)] $($t.path)" } }

if (-not $Yes -and -not $DryRun) {
    try {
        $ans = Read-Host '确认卸载？(Y/n)'
        if ($ans -and $ans.Trim() -match '^(n|no|否)') {
            Write-Host '已取消。' -ForegroundColor Yellow
            exit 0
        }
    } catch {
        Write-Note '非交互环境，直接继续'
    }
}

# ------------------------------------------------------------------ 结束进程
if (-not $KeepRunning) {
    Write-Step '结束正在运行的圆环进程'
    $killed = 0
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
                 Where-Object {
                     $_.Name -in @('python.exe', 'pythonw.exe') -and
                     $_.CommandLine -and
                     ($_.CommandLine -match 'main\.py' -or $_.CommandLine -match 'ui[\\/]panel\.py')
                 }
        foreach ($p in $procs) {
            if ($DryRun) {
                Write-Note "[dry-run] 结束 PID $($p.ProcessId): $($p.CommandLine)"
                continue
            }
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-Ok "已结束 PID $($p.ProcessId)"
                $killed++
            } catch {
                Write-Warn2 "结束 PID $($p.ProcessId) 失败: $($_.Exception.Message)"
            }
        }
    } catch {
        Write-Warn2 "进程枚举失败: $($_.Exception.Message)"
    }
    if ($killed -eq 0 -and -not $DryRun) { Write-Note '没有找到在跑的实例' }
} else {
    Write-Note '按 -KeepRunning 保留运行中的实例'
}

# ------------------------------------------------------------------ 删除入口
Write-Step '删除快捷方式'
$removed = 0
$missing = 0
foreach ($t in $targets) {
    if (-not (Test-Path -LiteralPath $t.path)) {
        Write-Note "跳过（不存在）: $($t.path)"
        $missing++
        continue
    }
    if ($DryRun) {
        Write-Note "[dry-run] 删除 $($t.path)"
        $removed++
        continue
    }
    try {
        Remove-Item -LiteralPath $t.path -Force -ErrorAction Stop
        Write-Ok "已删除: $($t.path)"
        $removed++
    } catch {
        Write-Warn2 "删除失败: $($t.path) —— $($_.Exception.Message)"
    }
}

# ------------------------------------------------------------------ 清理空目录
if (-not $StartupOnly -and (Test-Path -LiteralPath $smDir) -and -not $DryRun) {
    $left = @(Get-ChildItem -LiteralPath $smDir -Force -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0) {
        Remove-Item -LiteralPath $smDir -Force -ErrorAction SilentlyContinue
        Write-Ok "已删除空目录: $smDir"
    } else {
        Write-Note "开始菜单目录仍有其他文件，保留: $smDir"
    }
}

# ------------------------------------------------------------------ 旧式注册表自启项
if (-not $StartupOnly) {
    Write-Step '清理注册表 Run 项（遗留兼容）'
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (Test-Path -LiteralPath $runKey) {
        foreach ($name in @($appName, 'PeripheralBattery', '2.4W 电量监视', '2.4WBattery', 'BatteryTray')) {
            $exists = Get-ItemProperty -LiteralPath $runKey -Name $name -ErrorAction SilentlyContinue
            if ($exists) {
                if ($DryRun) { Write-Note "[dry-run] 删除 Run 值: $name" }
                else {
                    Remove-ItemProperty -LiteralPath $runKey -Name $name -Force -ErrorAction SilentlyContinue
                    Write-Ok "已删除 Run 值: $name"
                }
            }
        }
        if (-not $DryRun) { Write-Note 'Run 项检查完毕（无残留即无输出）' }
    } else {
        Write-Note '当前用户没有 Run 项，跳过'
    }
}

# ------------------------------------------------------------------ 安装清单
if (-not $StartupOnly -and -not $KeepManifest -and (Test-Path -LiteralPath $manifest)) {
    if ($DryRun) { Write-Note "[dry-run] 删除 $manifest" }
    else {
        Remove-Item -LiteralPath $manifest -Force -ErrorAction SilentlyContinue
        Write-Ok "已删除安装清单: $manifest"
    }
}

# ------------------------------------------------------------------ 小结
Write-Host ''
if ($DryRun) {
    Write-Host "DryRun 完成：计划删除 $removed 项（未实际删除）。" -ForegroundColor Yellow
} else {
    Write-Host "完成：删除 $removed 项，跳过 $missing 项缺失项。" -ForegroundColor Green
}
if (-not $Quiet) {
    Write-Host '程序文件仍然保留（main.py、ui\、devices\、scripts\）。想彻底删除，直接删除整个目录即可。'
    Write-Host ''
}

if (-not $NoPause) {
    try { Read-Host '按回车键关闭' | Out-Null } catch { }
}

exit 0
