# Flash Monitor

Windows 上给 [FlClash](https://github.com/chen08209/FlClash) / Clash Meta（mihomo）用的本机流量监视。看哪个程序走代理、哪个走直连、流量去了哪个网站。TUN 和系统代理都能用。

桌面窗口（WebView2）+ 托盘。关窗口默认进托盘，采集不停。只监听 `127.0.0.1`，流量记录留在本机。

## 安装

PowerShell（不用装 Python）：

```powershell
irm https://github.com/finnheihei/FlashMonitor/releases/latest/download/install.ps1 | iex
```

装到 `%LOCALAPPDATA%\FlashMonitor`，并在桌面和开始菜单放快捷方式。Clash 客户端要先打开外部控制器。旧版「分光」的记录会自动迁过来。

卸载（默认保留流量记录）：

```powershell
powershell -File "$env:LOCALAPPDATA\FlashMonitor\uninstall.ps1"
```

加 `-PurgeData` 会连记录一起删。

## 能做什么

- **总览**：打开就是速率图、分流比例、正在用网的程序和正在访问的网站
- **排行**：按程序、节点、网站；近 1 小时 / 今天 / 7 天 / 30 天 / 累计。可钉住或忽略
- **分流**：代理、直连分两列
- **漏网**：开了 TUN 时，没进 TUN 的连接单独标出来
- **连接**：可断开单条 Clash 连接
- **设置**：启动、通知、Clash 接口、机场账期、数据与诊断、关于
- **更新**：右上角从 GitHub 下载最新 `FlashMonitor.exe` 并重启
- **单实例**：再开一次会唤起已有窗口

快捷键：`1`–`6` 切换页面，`/` 搜索，`Esc` 清空搜索。

## 从源码运行

需要 **Python 3.11+** 和已打开外部控制器的 Clash 客户端。

```bat
start.cmd
```

或 `python -u monitor.py --port 8787`。只要采集不要窗口时加 `--no-gui`。

打包：`build.cmd` → `dist\FlashMonitor.exe`。

## 接客户端

**FlClash**：设置里打开外部控制器。默认 `http://127.0.0.1:9090`。

**Clash Verge**：打开外部控制器（常见 `127.0.0.1:9097`），把地址和 secret 填进「设置 → 连接」。

## 注意

- 页面只有本机打得开。记录在 `%LOCALAPPDATA%\FlashMonitor\data`（源码运行则在程序目录 `data\`）。
- 关窗口默认藏到托盘。彻底退出用托盘「退出 Flash Monitor」。
- 打包版没有代码签名。Windows Defender 有时会把 exe 误报成 `Trojan:Win32/Bearfoos` 并删除。到「Windows 安全中心 → 保护历史记录」允许，或给安装目录加排除项。日常开发用源码运行即可。

## License

[MIT](LICENSE)
