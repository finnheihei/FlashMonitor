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
    $sc.Description = "Flash Monitor — Clash TUN 流量监视"
    $sc.Save()
}

New-FlashMonitorShortcut (Join-Path $startMenu "Flash Monitor.lnk")
New-FlashMonitorShortcut (Join-Path $desktop "Flash Monitor.lnk")

Write-Host "已安装：$exePath"
Write-Host "开始菜单和桌面已放快捷方式"
Start-Process -FilePath $exePath -WorkingDirectory $installDir
Write-Host "已启动。页面：http://127.0.0.1:8787/"
