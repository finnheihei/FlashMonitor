# Flash Monitor 0.9.14

Windows x64 用户请下载 **FlashMonitor-0.9.14-setup.exe**，双击安装，无需安装 Python。

## 本次更新

- 默认英文界面，顶部按钮可切换中文，并记住语言偏好；切换时恢复当前页面、搜索、筛选与展开状态。
- 改进连接明细返回、筛选恢复、实时置顶和用量排序；明确全局速率与当前筛选列表的区别。
- 设置加载或保存失败时显示错误与重试入口，避免误用默认值或显示未保存的设置。
- 订阅用量缓存与订阅来源绑定，展示缓存、更新时间和读取失败状态，避免更换订阅后显示旧套餐余额。
- IPv6 屏蔽区分当前范围规则与其他规则的覆盖，明确等待生效和兼容限制。
- 简化日常提醒与高级维护入口，改进首次收进托盘的提示、清零保存和连接诊断。
- 更新程序等待旧进程退出后才替换文件；等待或替换失败时保留诊断与待更新文件。

## 安装与升级

升级前请从托盘退出正在运行的 Flash Monitor。安装至当前用户目录，正常无需管理员权限；卸载默认保留流量记录。运行需要 Microsoft Edge WebView2 Runtime。

`FlashMonitor.exe` 为独立程序文件；首次使用优先下载安装包。`SHA256SUMS.txt` 提供两个文件的 SHA256 校验值。程序和安装包尚未代码签名。

断开仅针对代理内核管理的当前连接，应用可能自动重连；绕过内核的连接暂不支持断开。IPv6 屏蔽不保证自动回退 IPv4，也不控制绕过内核的流量。

## English

Download **FlashMonitor-0.9.14-setup.exe** for Windows x64. This release includes English/Chinese switching, navigation and filter restoration, clearer settings and subscription errors, more accurate IPv6 rule status, and safer update replacement after the old process exits.

Exit the previous Monitor from its tray menu before upgrading. Python is not required; Microsoft Edge WebView2 Runtime is required. Historical traffic is retained by default when uninstalling. Standalone `FlashMonitor.exe` and `SHA256SUMS.txt` are also provided. Builds are unsigned.

Disconnect applies only to current connections managed by the proxy core. Apps may reconnect; connections bypassing the core cannot be disconnected here. Scoped IPv6 blocking does not guarantee IPv4 fallback or control bypass traffic.
