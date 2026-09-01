# 分光：下载最新 FenGuang.exe 到 %LOCALAPPDATA%\FenGuang
# 用法：irm https://github.com/finnheihei/FenGuang/releases/latest/download/install.ps1 | iex
$ErrorActionPreference = "Stop"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$repo = "finnheihei/FenGuang"
$installDir = Join-Path $env:LOCALAPPDATA "FenGuang"
$exePath = Join-Path $installDir "FenGuang.exe"
$exeUrl = "https://github.com/$repo/releases/latest/download/FenGuang.exe"

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Write-Host "正在下载分光到 $installDir ..."

$tmp = Join-Path $installDir "FenGuang.exe.download"
Invoke-WebRequest -Uri $exeUrl -OutFile $tmp -UseBasicParsing
if ((Get-Item $tmp).Length -lt 1MB) {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    throw "下载失败：发行版里还没有 FenGuang.exe。请到 https://github.com/$repo/releases 查看。"
}
Move-Item -Force $tmp $exePath
Unblock-File -Path $exePath -ErrorAction SilentlyContinue

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnk = Join-Path $startMenu "分光.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $exePath
$sc.WorkingDirectory = $installDir
$sc.Description = "分光 — Clash TUN 流量监视"
$sc.Save()

Write-Host "已安装：$exePath"
Write-Host "开始菜单：分光"
Start-Process -FilePath $exePath -WorkingDirectory $installDir
Write-Host "已启动。页面：http://127.0.0.1:8787/"
