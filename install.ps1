# Flash Monitor：下载最新 FlashMonitor.exe 到 %LOCALAPPDATA%\FlashMonitor
# 用法：irm https://github.com/finnheihei/FlashMonitor/releases/latest/download/install.ps1 | iex
$ErrorActionPreference = "Stop"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$repo = "finnheihei/FlashMonitor"
$installDir = Join-Path $env:LOCALAPPDATA "FlashMonitor"
$exePath = Join-Path $installDir "FlashMonitor.exe"
$exeUrl = "https://github.com/$repo/releases/latest/download/FlashMonitor.exe"

New-Item -ItemType Directory -Force -Path $installDir | Out-Null

$legacyDir = Join-Path $env:LOCALAPPDATA "FenGuang"
$legacyData = Join-Path $legacyDir "data"
$newData = Join-Path $installDir "data"
if ((Test-Path $legacyData) -and -not (Test-Path $newData)) {
    Write-Host "正在迁移旧版分光的流量记录..."
    Copy-Item -Path $legacyData -Destination $newData -Recurse
}

Write-Host "正在下载 Flash Monitor 到 $installDir ..."

$tmp = Join-Path $installDir "FlashMonitor.exe.download"
Invoke-WebRequest -Uri $exeUrl -OutFile $tmp -UseBasicParsing
if ((Get-Item $tmp).Length -lt 1MB) {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    throw "下载失败：发行版里还没有 FlashMonitor.exe。请到 https://github.com/$repo/releases 查看。"
}
Move-Item -Force $tmp $exePath
Start-Sleep -Seconds 2
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "下载后文件不见了。Windows Defender 常把未签名的 FlashMonitor.exe 误报为 Trojan:Win32/Bearfoos 并删除。请到「Windows 安全中心 → 保护历史记录」允许这项，或给 $installDir 加排除项后再装。"
}
Unblock-File -Path $exePath -ErrorAction SilentlyContinue

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath("Desktop")
Remove-Item (Join-Path $startMenu "分光.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path $desktop "分光.lnk") -ErrorAction SilentlyContinue

function New-FlashMonitorShortcut([string]$path) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($path)
    $sc.TargetPath = $exePath
    $sc.WorkingDirectory = $installDir
    $sc.Description = "Flash Monitor — Clash 流量监视"
    $sc.IconLocation = "$exePath,0"
    $sc.Save()
}

New-FlashMonitorShortcut (Join-Path $startMenu "Flash Monitor.lnk")
New-FlashMonitorShortcut (Join-Path $desktop "Flash Monitor.lnk")

$unPath = Join-Path $installDir "uninstall.ps1"
$unUrl = "https://github.com/$repo/releases/latest/download/uninstall.ps1"
try {
    Invoke-WebRequest -Uri $unUrl -OutFile $unPath -UseBasicParsing
} catch {
    @'
param([switch]$PurgeData)
$installDir = Join-Path $env:LOCALAPPDATA "FlashMonitor"
Get-Process FlashMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath("Desktop")
@(
    (Join-Path $startMenu "Flash Monitor.lnk"),
    (Join-Path $desktop "Flash Monitor.lnk")
) | ForEach-Object { Remove-Item $_ -Force -ErrorAction SilentlyContinue }
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "FlashMonitor" -ErrorAction SilentlyContinue
if ($PurgeData) { Remove-Item $installDir -Recurse -Force -ErrorAction SilentlyContinue }
else { Get-ChildItem $installDir -Force | Where-Object { $_.Name -ne "data" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
'@ | Set-Content -Path $unPath -Encoding UTF8
}

Write-Host "已安装：$exePath"
Write-Host "开始菜单和桌面已放快捷方式"
Write-Host "卸载：powershell -File `"$unPath`""
Start-Process -FilePath $exePath -WorkingDirectory $installDir
Write-Host "已启动桌面窗口。"
