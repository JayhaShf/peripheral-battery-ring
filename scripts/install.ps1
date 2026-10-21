#Requires -Version 5.1
<#
.SYNOPSIS
    安装「外设电量圆环」的入口：开机自启项 + 桌面快捷方式 + 开始菜单项。
    当前用户级安装，不写注册表、不请求管理员权限、不复制任何程序文件。

.DESCRIPTION
    所有快捷方式都指向同一条命令行：

        wscript.exe "<仓库>\scripts\run_hidden.vbs" [参数]

    由 run_hidden.vbs 用 scripts\python-path.txt 里固化的 pythonw.exe 无窗口启动
    main.py，因此不会闪黑框，也不用把解释器路径写死在本脚本里。

    创建/覆盖的位置（默认全建）：
      1. 启动文件夹   %APPDATA%\...\Startup\外设电量圆环.lnk   —— 开机自启（任务管理器可见）
      2. 桌面         <桌面>\外设电量圆环.lnk                  —— 默认带 --panel，直接开设置面板
      3. 开始菜单     ...\Programs\外设电量圆环\               —— 「设置面板」+「卸载」两个入口

    重复执行安全（幂等）：同名 .lnk 直接覆盖，不会产生重复项。

.PARAMETER Root
    仓库根目录，默认取本脚本所在目录的上一级（即 D:\2.4P）。

.PARAMETER PythonExe
    手动指定解释器绝对路径；不指定时自动探测（pythonw.exe 优先），
    并把探测结果写入 scripts\python-path.txt 供 run_hidden.vbs 复用。

.PARAMETER PanelArg
    桌面/开始菜单快捷方式透传给 main.py 的参数，默认 --panel（打开设置面板）。

.PARAMETER StartupArg
    开机自启项透传给 main.py 的参数，默认空（常驻圆环 + 托盘）。

.PARAMETER NoStartup
    不创建开机自启项。

.PARAMETER StartupOnly
    只处理开机自启项（桌面/开始菜单都跳过）。面板的「开机自启」勾选框用这个。

.PARAMETER NoDesktop
    不创建桌面快捷方式。

.PARAMETER NoStartMenu
    不创建开始菜单项。

.PARAMETER Force
    main.py 还不存在时也照常创建快捷方式（调试用）。

.PARAMETER DryRun
    只打印将要做什么，不落盘（不会写 python-path.txt / install-manifest.json）。

.PARAMETER Quiet
    只输出关键行（面板调用时用）。

.PARAMETER Uninstall
    等价于调用 scripts\uninstall.ps1（删除本脚本创建的入口，保留程序文件）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -DryRun -NoStartup

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -StartupOnly -Quiet
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$PythonExe,
    [string]$PanelArg = '--panel',
    [string]$StartupArg = '',
    [switch]$NoStartup,
    [switch]$StartupOnly,
    [switch]$NoDesktop,
    [switch]$NoStartMenu,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Quiet,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ 输出小工具
function Write-Step { param([string]$Text) if (-not $Quiet) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan } }
function Write-Ok   { param([string]$Text) Write-Host "   [OK] $Text" -ForegroundColor Green }
function Write-Note { param([string]$Text) if (-not $Quiet) { Write-Host "   [--] $Text" -ForegroundColor Gray } }
function Write-Warn2 { param([string]$Text) Write-Host "   [!!] $Text" -ForegroundColor Yellow }

# ------------------------------------------------------------------ 路径
if (-not $Root) {
    if ($PSScriptRoot) { $Root = Split-Path -Parent $PSScriptRoot }
    else { $Root = (Get-Location).Path }
}
$Root        = [System.IO.Path]::GetFullPath($Root)
$scriptDir   = Join-Path $Root 'scripts'
$entryPy     = Join-Path $Root 'main.py'
$panelPy     = Join-Path $Root 'ui\panel.py'
$vbsPath     = Join-Path $scriptDir 'run_hidden.vbs'
$pinFile     = Join-Path $scriptDir 'python-path.txt'
$manifest    = Join-Path $scriptDir 'install-manifest.json'
$uninstallPs = Join-Path $scriptDir 'uninstall.ps1'
$appName     = '外设电量圆环'
$sysRoot     = $env:SystemRoot
if (-not $sysRoot) { $sysRoot = 'C:\Windows' }
$wscriptPath    = Join-Path $sysRoot 'System32\wscript.exe'
$powershellPath = Join-Path $sysRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# -Uninstall 直接转交卸载脚本
if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $uninstallPs)) { throw "缺少卸载脚本: $uninstallPs" }
    $unArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $uninstallPs, '-Root', $Root, '-Yes', '-NoPause')
    if ($DryRun) { $unArgs += '-DryRun' }
    & $powershellPath @unArgs
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host "$appName —— 安装入口" -ForegroundColor White
Write-Note "仓库目录: $Root"
if ($DryRun) { Write-Warn2 'DryRun 模式：只打印计划，不写任何文件' }

# ------------------------------------------------------------------ 1. 环境检查
Write-Step '1/4 检查安装环境'

if (-not (Test-Path -LiteralPath $vbsPath)) {
    throw "缺少启动器: $vbsPath —— scripts 目录不完整，请重新获取完整仓库。"
}
Write-Ok "启动器: $vbsPath"

if (-not (Test-Path -LiteralPath $wscriptPath)) {
    throw "找不到 wscript.exe: $wscriptPath"
}
Write-Ok "wscript: $wscriptPath"

if (-not (Test-Path -LiteralPath $entryPy)) {
    if ($Force) { Write-Warn2 "main.py 还不存在（-Force 继续）: $entryPy" }
    else { throw "缺少入口脚本: $entryPy`n若只是先装入口壳子，可加 -Force 跳过此检查。" }
} else { Write-Ok "入口脚本: $entryPy" }

if (-not (Test-Path -LiteralPath $panelPy)) { Write-Warn2 "找不到设置面板: $panelPy（快捷方式仍会创建）" }
else { Write-Ok "设置面板: $panelPy" }

# ------------------------------------------------------------------ 2. Python
Write-Step '2/4 探测 Python 解释器'

function Test-PythonCandidate {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $item = Get-Item -LiteralPath $Path
        if ($item.Length -le 0) { return $false }                       # 微软商店 0 字节别名
        if ($item.FullName -match '\\WindowsApps\\') { return $false }
    } catch { return $false }
    return $true
}

function Get-PythonwPath {
    param([string]$Py)
    if (-not $Py) { return $null }
    $leaf = Split-Path -Leaf $Py
    if ($leaf -ieq 'pythonw.exe') { return $Py }
    if ($leaf -ieq 'python.exe') {
        $w = Join-Path (Split-Path -Parent $Py) 'pythonw.exe'
        if (Test-Path -LiteralPath $w) { return $w }
    }
    return $Py
}

$pyExe = $null
$pinHint = $null
if ($PythonExe) {
    if (-not (Test-PythonCandidate $PythonExe)) { throw "指定的解释器不可用: $PythonExe" }
    $pyExe = (Get-Item -LiteralPath $PythonExe).FullName
    Write-Ok "使用 -PythonExe 指定: $pyExe"
}
elseif ((Test-Path -LiteralPath $pinFile)) {
    $pinned = @(Get-Content -LiteralPath $pinFile -ErrorAction SilentlyContinue) |
              Where-Object { $_ -and $_.Trim() -and ($_.Trim() -notmatch '^\s*#') } |
              Select-Object -First 1
    if ($pinned) {
        $cand = $pinned.Trim().Trim('"')
        if (Test-PythonCandidate $cand) {
            $pyExe = (Get-Item -LiteralPath $cand).FullName
            $pinHint = 'python-path.txt'
            Write-Ok "沿用 $pinHint 中的解释器: $pyExe"
        } else {
            Write-Warn2 "python-path.txt 里的解释器已失效，重新探测: $cand"
        }
    }
}

if (-not $pyExe) {
    foreach ($name in @('pythonw.exe', 'python.exe')) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($cmd -and (Test-PythonCandidate $cmd.Source)) {
            $pyExe = (Get-Item -LiteralPath $cmd.Source).FullName
            Write-Ok "PATH 探测: $pyExe"
            break
        }
    }
}

if (-not $pyExe) {
    Write-Warn2 '没探测到可用的 Python。'
    Write-Note '快捷方式仍会创建，但 run_hidden.vbs 启动时会弹提示。'
    Write-Note '解法：安装 Python 3.9+ 并勾选 Add to PATH，或重跑:'
    Write-Note '      .\scripts\install.ps1 -PythonExe "C:\Path\To\pythonw.exe"'
    $pythonw = $null
} else {
    # 用 python.exe（能拿到 stdout）做一次版本冒烟
    $verifyPy = $pyExe
    if ((Split-Path -Leaf $pyExe) -ieq 'pythonw.exe') {
        $sibling = Join-Path (Split-Path -Parent $pyExe) 'python.exe'
        if (Test-Path -LiteralPath $sibling) { $verifyPy = $sibling }
    }
    try {
        $verOut = & $verifyPy -c "import sys; print(sys.version.split()[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $verOut) { Write-Ok "版本: Python $($verOut | Select-Object -First 1)" }
        else { Write-Warn2 '解释器可执行但版本探测失败（不影响启动）' }
    } catch { Write-Warn2 "版本探测异常: $($_.Exception.Message)" }

    $pythonw = Get-PythonwPath $pyExe
    if (-not $DryRun) {
        $enc = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($pinFile, ($pythonw + [Environment]::NewLine), $enc)
        Write-Ok "已写入: $pinFile"
    } else {
        Write-Note "[dry-run] 写 $pinFile = $pythonw"
    }
}

# ------------------------------------------------------------------ 3. 快捷方式
Write-Step '3/4 创建快捷方式'

$startupArgs = ('"' + $vbsPath + '"').Trim()
if ($StartupArg) { $startupArgs += ' ' + $StartupArg }
$panelArgs = ('"' + $vbsPath + '"')
if ($PanelArg) { $panelArgs += ' ' + $PanelArg }
$iconArg = ''
if ($pythonw) { $iconArg = "$pythonw,0" }
$created = @()

$shellCom = New-Object -ComObject WScript.Shell

function New-ShortcutFile {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Arguments,
        [string]$WorkDir,
        [string]$IconLocation,
        [string]$Description
    )
    if ($DryRun) {
        Write-Note "[dry-run] $Path"
        Write-Note "          -> `"$Target`" $Arguments"
        return
    }
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $lnk = $shellCom.CreateShortcut($Path)
    $lnk.TargetPath       = $Target
    $lnk.Arguments        = $Arguments
    $lnk.WorkingDirectory = $WorkDir
    if ($IconLocation) { $lnk.IconLocation = $IconLocation }
    if ($Description)  { $lnk.Description  = $Description }
    $lnk.Save()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($lnk)
    Write-Ok $Path
}

try {
    # --- 3.1 开机自启（启动文件夹）---
    if ($NoStartup) {
        Write-Note '按 -NoStartup 跳过启动项'
    } else {
        $startupDir = [Environment]::GetFolderPath('Startup')
        if (-not $startupDir) { $startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
        $link = Join-Path $startupDir "$appName.lnk"
        New-ShortcutFile -Path $link -Target $wscriptPath -Arguments $startupArgs `
            -WorkDir $Root -IconLocation $iconArg `
            -Description "$appName（开机自启，常驻任务栏圆环 + 托盘）"
        $created += [pscustomobject]@{ kind = 'startup'; path = $link; target = $wscriptPath; arguments = $startupArgs; workdir = $Root }
    }

    # --- 3.2 桌面 ---
    if ($StartupOnly) {
        Write-Note '按 -StartupOnly 只处理启动项'
    }
    elseif ($NoDesktop) {
        Write-Note '按 -NoDesktop 跳过桌面快捷方式'
    } else {
        $desktopDir = [Environment]::GetFolderPath('Desktop')
        if (-not $desktopDir) { $desktopDir = Join-Path $env:USERPROFILE 'Desktop' }
        $link = Join-Path $desktopDir "$appName.lnk"
        New-ShortcutFile -Path $link -Target $wscriptPath -Arguments $panelArgs `
            -WorkDir $Root -IconLocation $iconArg `
            -Description "$appName 设置面板（托盘图标被隐藏后也用它打开）"
        $created += [pscustomobject]@{ kind = 'desktop'; path = $link; target = $wscriptPath; arguments = $panelArgs; workdir = $Root }
    }

    # --- 3.3 开始菜单 ---
    if ($StartupOnly) {
        Write-Note '按 -StartupOnly 跳过开始菜单项'
    }
    elseif ($NoStartMenu) {
        Write-Note '按 -NoStartMenu 跳过开始菜单项'
    } else {
        $programsDir = [Environment]::GetFolderPath('Programs')
        if (-not $programsDir) { $programsDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs' }
        $smDir = Join-Path $programsDir $appName

        $link = Join-Path $smDir "$appName 设置面板.lnk"
        New-ShortcutFile -Path $link -Target $wscriptPath -Arguments $panelArgs `
            -WorkDir $Root -IconLocation $iconArg `
            -Description "$appName 设置面板"
        $created += [pscustomobject]@{ kind = 'startmenu'; path = $link; target = $wscriptPath; arguments = $panelArgs; workdir = $Root }

        $linkRun = Join-Path $smDir "$appName.lnk"
        New-ShortcutFile -Path $linkRun -Target $wscriptPath -Arguments $startupArgs `
            -WorkDir $Root -IconLocation $iconArg `
            -Description "$appName（启动常驻圆环）"
        $created += [pscustomobject]@{ kind = 'startmenu_run'; path = $linkRun; target = $wscriptPath; arguments = $startupArgs; workdir = $Root }

        $linkUn = Join-Path $smDir "卸载 $appName.lnk"
        $unArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $uninstallPs + '"'
        New-ShortcutFile -Path $linkUn -Target $powershellPath -Arguments $unArgs `
            -WorkDir $Root -IconLocation '' `
            -Description "卸载 $appName 的启动项与快捷方式（保留程序文件）"
        $created += [pscustomobject]@{ kind = 'uninstall'; path = $linkUn; target = $powershellPath; arguments = $unArgs; workdir = $Root }
    }
}
finally {
    if ($shellCom) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shellCom) }
}

# ------------------------------------------------------------------ 4. 清单
Write-Step '4/4 写入安装清单'

$data = [ordered]@{
    app         = $appName
    root        = $Root
    installedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    panelArg    = $PanelArg
    startupArg  = $StartupArg
    python      = $pythonw
    entry       = $entryPy
    panel       = $panelPy
    launcher    = [ordered]@{
        target    = $wscriptPath
        arguments = $panelArgs
        workdir   = $Root
    }
    shortcuts   = $created
}

if ($DryRun) {
    Write-Note "[dry-run] 写 $manifest"
} else {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($manifest, ($data | ConvertTo-Json -Depth 6), $enc)
    Write-Ok $manifest
}

# ------------------------------------------------------------------ 小结
Write-Host ''
if ($DryRun) {
    Write-Host 'DryRun 完成：以上为计划，未写入任何文件。' -ForegroundColor Yellow
} else {
    Write-Host '安装完成。' -ForegroundColor Green
}
if (-not $Quiet) {
    Write-Host ''
    Write-Host '下一步：' -ForegroundColor White
    Write-Host "  * 双击桌面的「$appName」打开设置面板（托盘图标被隐藏时也用它）。"
    Write-Host '  * 圆环右键菜单：打开面板 / 立即刷新 / 显示托盘图标 / 开机自启 / 退出。'
    Write-Host '  * 只想命令行看一眼：'
    Write-Host '        python main.py --once      # 打印一次所有设备读数'
    Write-Host '        python main.py --status    # 配置与进程状态'
    Write-Host '  * 卸载入口（不动 main.py / ui\ / devices\ 程序本体）：'
    Write-Host '        powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1'
    Write-Host ''
}

exit 0
