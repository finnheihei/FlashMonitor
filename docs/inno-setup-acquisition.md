# Inno Setup 编译器获取与便携准备记录

核实日期：2026-10-15。目标是编译原生 `setup.exe`；离线 ZIP 只是中间候选，不能替代最终交付。

用户已明确批准本提案。固定资产的下载大小、SHA256 与有效 Pyrsys B.V. 签名均核验通过；便携准备退出码为 0，实际工具文件共 **28,932,337 字节**，编译器引擎输出版本 **6.7.3**。准备前后的用户/机器 PATH、相关卸载登记、`.iss` 关联及 Inno 快捷方式检查一致；没有全局安装或创建便携卸载器。原生候选已经成功编译。ISCC 文件版本资源为 `0.0.0.0`，因此工具版本依据引擎输出及固定官方资产哈希，而非此资源字段。

## 固定版本及来源

采用 **Inno Setup 6.7.3**，与本项目现有 `installer/FlashMonitor.iss` 的 6.x 目标一致。它是官方当前提供的 6.x 下载，发布日期 2026-05-26；此次不切换 7.x。

- [官方下载页](https://jrsoftware.org/isdl.php)
- [固定版本发布页](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3)
- [精确安装程序链接](https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe)
- [官方发布资产元数据](https://api.github.com/repos/jrsoftware/issrc/releases/tags/is-6_7_3)

官方元数据核实下载大小为 **10,592,232 字节**（约 10.10 MiB），SHA256：

`9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732`

执行前必须同时核对版本、文件哈希和有效 Authenticode 签名，预期签名发布者为 **Pyrsys B.V.**；任何不符都停止，不关闭或绕过安全软件。[官方签名验证说明](https://jrsoftware.org/isdl-verify.php)

## 可以便携编译，但仍需要执行下载的安装程序

官方 6.7.3 [setup.iss](https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/setup.iss) 包含便携支持；[isportable.iss](https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/isportable.iss) 读取 `/PORTABLE=1`，关闭卸载注册、跳过开始菜单，并在主脚本中排除文件关联及桌面图标任务。因而可把编译器文件展开到专用工具目录，使用其中的 `ISCC.exe` 编译，无需全局安装或修改 PATH。

这不是“不执行任何新二进制的解压”：官方只提供安装程序，便携模式也会运行它。获得批准后已按下述参数运行，未获取第三方解包工具。官方安装程序未要求账户、付款、重启或提权；编译器显示 `Non-commercial use only`，本轮只制作本地候选，未进行商业发布或接受额外付费条款。

## 已获批并执行的具体动作

1. 下载上述固定版本到工作区 `tool-downloads\innosetup-6.7.3.exe`，核对 SHA256 和有效签名。
2. 仅在全新工具目录 `tools\inno-6.7.3-portable` 准备便携编译器。以当前用户隐藏运行、等待退出，指定 `/PORTABLE=1 /CURRENTUSER /VERYSILENT /SP- /NORESTART /NOICONS /TASKS=""`、绝对 `/DIR` 和工作区 `/LOG`。不启动 IDE，不关联 `.iss`，不建立系统或用户 PATH，不触碰 Monitor 或 FlClash 实例。
3. 检查编译器版本与实际文件占用，保存准备日志；把 `ISCC.exe` 的绝对路径传给 `build_installer.py --iscc ...`，生成原生候选 setup.exe。
4. 对原生候选另做独立安装/升级/卸载验收。真实用户目录安装、真实数据迁移、系统网络修改不包含在本次工具准备批准内。

## 空间预算与剩余不确定性

下载需要 10,592,232 字节，展开后的工具文件实测 28,932,337 字节。获批预算为便携工具目录与准备临时文件 **200 MiB**，候选构建和隔离测试另 **512 MiB**；这是本项目工作预算，不是官方最低需求。若后续构建将超过预算，先报告再继续扩大。

核实时 C 盘可用空间为 160,812,994,560 字节，明显大于上述预算。批准后仍应在执行前重新检查空闲空间。工具准备完成后不自动删除工作区证据或备份。

所有命令参数的行为以 [官方命令行说明](https://jrsoftware.org/ishelp/topic_setupcmdline.htm) 为准；`/CURRENTUSER` 对官方安装器的权限覆盖有定义，但便携准备仍需验证其退出码和日志。
