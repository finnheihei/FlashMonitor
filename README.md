# 分光

本机查看 FlClash / Clash Meta（mihomo）的 TUN 分流：哪个程序走代理、哪个走直连、有没有漏网。只监听 `127.0.0.1`，不需要第三方运行时依赖。

## 需要

- Windows
- Python 3.11+（从源码运行）
- FlClash 或其它 Clash Meta 客户端，并打开 **外部控制器**（默认 `http://127.0.0.1:9090`）

## 运行

```bat
start.cmd
```

或：

```bat
python -u monitor.py --port 8787
```

浏览器打开 <http://127.0.0.1:8787/>

打包成 exe：

```bat
build.cmd
```

生成 `dist\FenGuang.exe`。双击运行，流量记录写在 exe 同目录的 `data\`。

## 客户端

**FlClash：** 设置里打开外部控制器。分光默认连 `9090`。

**Clash Verge：** 默认不开放 HTTP 接口。在设置里打开外部控制器（常见为 `127.0.0.1:9097`），把地址和 secret 填进分光的「设置」。

## 说明

- 页面仅本机可开。
- `data\` 里是流量和设置，不要提交到 git。
- 关窗口默认藏到托盘，采集不停；要退出请用托盘菜单或窗口里的「退出」。
