# Flash Monitor 卸载。默认保留 %LOCALAPPDATA%\FlashMonitor\data
# 用法：powershell -File uninstall.ps1
# 同时删流量记录：powershell -File uninstall.ps1 -PurgeData
param([switch]$PurgeData)
$ErrorActionPreference = "Continue"
$installDir = Join-Path $env:LOCALAPPDATA "FlashMonitor"
Get-Process FlashMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath("Desktop")
@(
    (Join-Path $startMenu "Flash Monitor.lnk"),
    (Join-Path $desktop "Flash Monitor.lnk"),
    (Join-Path $startMenu "分光.lnk"),
    (Join-Path $desktop "分光.lnk")
) | ForEach-Object { Remove-Item $_ -Force -ErrorAction SilentlyContinue }
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $run -Name "FlashMonitor" -ErrorAction SilentlyContinue
if ($PurgeData) {
    Remove-Item $installDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "已删除 $installDir"
} else {
    Get-ChildItem $installDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne "data" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "已卸载程序。流量记录还在 $(Join-Path $installDir 'data')"
}
