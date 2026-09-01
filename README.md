# Flash Monitor

Windows 上给 [FlClash](https://github.com/chen08209/FlClash) / Clash Meta（mihomo）用的本机流量监视。看哪个程序走代理、哪个走直连、流量去了哪个网站，以及有没有漏出 TUN。

只监听 `127.0.0.1`，运行不用装第三方库。

## 快速安装

Windows PowerShell（不用装 Python）：

```powershell
irm https://github.com/finnheihei/FlashMonitor/releases/latest/download/install.ps1 | iex
```

会下载到 `%LOCALAPPDATA%\FlashMonitor`，并加入开始菜单。Clash 客户端要先打开外部控制器。旧版「分光」的流量记录会自动迁过来。

卸载：删掉该文件夹，以及开始菜单里的「Flash Monitor」。

## 功能

- **排行**：按程序、节点、网站；近 1 小时 / 今天 / 7 天 / 30 天 / 累计
- **分流**：走代理、走直连分两列；点开程序能看到访问过的域名
- **漏网**：IPv6、旁路 VPN、没进 TUN 的连接会单独标出来
- **连接**：可断开单条 Clash 连接；第一次出现的程序会提示
- **设置**：开机自启、托盘、日流量提醒、Clash 地址和密钥

## 从源码运行

需要 **Python 3.11+** 和已打开外部控制器的 Clash 客户端。

```bat
start.cmd
```

或：

```bat
python -u monitor.py --port 8787
```

浏览器打开 http://127.0.0.1:8787/

打包成 exe（会安装 PyInstaller）：

```bat
build.cmd
```

生成 `dist\FlashMonitor.exe`。流量记录写在 exe 同目录的 `data\`。

## 接客户端

**FlClash**  
设置里打开外部控制器。Flash Monitor 默认连 `http://127.0.0.1:9090`。

**Clash Verge**  
默认不开放 HTTP 接口。打开外部控制器（常见 `127.0.0.1:9097`），把地址和 secret 填进 Flash Monitor 的「设置」。

## 注意

- 页面只有本机打得开。
- `data\` 是流量和设置，不要提交。
- 关窗口默认藏到托盘，采集不停。彻底退出用托盘菜单或窗口里的「退出」。

## License

[MIT](LICENSE)
