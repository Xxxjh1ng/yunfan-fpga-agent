# 在桌面创建「云帆 YunFan 控制台」快捷方式。
# 快捷方式直接指向 pythonw.exe（无黑窗口），不依赖 .vbs 这类容易被杀软误报的文件。
# 用法：双击 make_shortcut.cmd，或直接运行本文件。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "云帆 YunFan 控制台.lnk"

# 优先项目自带的虚拟环境，其次系统 Python
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonw = $cmd.Source
    } else {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd) { $pythonw = $cmd.Source }
    }
}

if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Host "[错误] 没找到 Python。请先安装 Python 3.8+ 并勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}

$icon = Join-Path $root "yunfan\console\icon.ico"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = '-m yunfan.console --port 8765 --open'
$lnk.WorkingDirectory = $root
$lnk.Description = "云帆 YunFan · 高云 FPGA 自动化设计控制台"
if (Test-Path $icon) { $lnk.IconLocation = $icon }
$lnk.WindowStyle = 7
$lnk.Save()

Write-Host "已创建桌面快捷方式：" -NoNewline
Write-Host $lnkPath -ForegroundColor Green
Write-Host "  指向：$pythonw -m yunfan.console --port 8765 --open"
Write-Host "  目录：$root"
Write-Host "双击图标即可打开控制台（无黑窗口；若已在运行则直接打开已有页面）。"
