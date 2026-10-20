# Windows 候选安装包与数据迁移

本轮提供原生 Windows setup.exe 候选、离线 ZIP 中间产物和可复核构建脚本。候选基于已通过独立集成测试的 Monitor 修复版；不包含新的 GPT 网络一键配置，不把直接 IPv6 桥接原型作为可用功能。构建成功不等于最终用户验收通过。

## 当前产物与限制

- `installer/build_installer.py` 只使用已安装的 64 位 Windows Python、PyInstaller 6.20.0 和现有 Microsoft 签名 WebView2Loader.dll；不联网下载、不安装依赖。
- 离线 ZIP 包含应用 EXE、安装/迁移脚本、启动安装的 CMD、说明和带哈希的 manifest。完整解压后运行 `Install.cmd`。
- `installer/FlashMonitor.iss` 已用用户批准获取的官方 Inno Setup 6.7.3 便携编译器生成原生 setup.exe。原生生命周期使用同一 ISS 与同一 payload 的隔离测试构建验证；只有编译期目录、登记身份、互斥量和快捷方式位置不同。生产候选未安装到真实用户目录。
- 脚本与应用当前未签名。`finnheihei` 只是沿用的发布者文字，不是证书验证身份；SHA256 用于比对完整性，不认证发布者。不能承诺 SmartScreen/杀软无提示。
- 应用需要 Microsoft Edge WebView2 Runtime。安装器不安装运行时，也不关闭或绕过安全软件。

## 安装、升级、卸载契约

默认当前用户安装至 `%LOCALAPPDATA%\FlashMonitor`，创建当前用户开始菜单快捷方式和 HKCU 卸载登记，正常无需管理员权限。不创建桌面图标，不启动应用，不强制停止进程。安装前从托盘退出 Monitor；检测到互斥量或被占用 EXE 时拒绝修改。

默认不改变开机启动；明确选择 `Install.ps1 -EnableAutostart` 才写当前用户 Run 项。已有设置在升级时保留。卸载只删除仍指向本安装目录的 Run 值和快捷方式。

原位升级校验包内哈希，保留原 EXE、脚本、登记信息和快捷方式备份，使用原子文件替换；操作失败时尝试恢复。拒绝已知版本降级。备份在安装目录 `installer-backups`，不会自动清除。手动恢复旧程序前仍需评估数据库兼容性；备份不是自动降级授权。

卸载入口在 Windows“已安装的应用”。卸载仅移除明确由安装器管理的程序文件，默认保留 `data`、WebView2 状态、窗口状态和升级备份；没有清空用户数据选项。脚本候选与原生 Inno 安装器之间尚未验证直接跨安装器升级，检测到另一安装器时拒绝；先卸载程序文件并保留数据后再采用另一方式。

原有根目录 `install.ps1`、`uninstall.ps1`、`build.cmd` 和 GitHub 发布工作流本轮未改动。它们不是此候选的安装入口。旧 `dist/FlashMonitor.exe` 不是本轮源码构建产物，不应混入发布。

## 旧数据迁移

安装器不读取或搬移 `D:\Daily\Commen\Tun_monitor\data`。迁移助手默认只读预览：

```powershell
.\Migrate-Data.ps1 -Source 'D:\Daily\Commen\Tun_monitor\data' -PlanPath "$env:TEMP\monitor-migration-preview.json"
```

预览包含路径、文件大小及哈希，不输出设置内容或凭证。确认来源与目标后，退出使用这两个数据目录的程序，再显式执行：

```powershell
.\Migrate-Data.ps1 -Action Apply -PlanPath "$env:TEMP\monitor-migration-preview.json" -ConfirmMigration
```

仅向标准用户数据目录或受限测试目录复制；不删除源数据、不合并已有非空目标。复制期间锁定源文件，复核预览哈希和完整文件集合，包括数据库 WAL/SHM；目标先经暂存和校验再发布。链接、路径越界、源变化、文件占用、目标非空均拒绝。

成功后输出迁移日志路径。需要撤回且目标内容自迁移后未变时：

```powershell
.\Migrate-Data.ps1 -Action Rollback -JournalPath '<迁移输出的 journal 路径>' -ConfirmMigration
```

撤回先验证整个目标集合及全部哈希，再只删除本次复制的文件；目标新增或修改过数据时拒绝，保留源文件与日志。它不提供新旧累计数据合并，也不声称能在应用运行中在线迁移 SQLite。

应用自身 `flashmonitor/paths.py::migrate_legacy_data()` 已改为无文件读写的兼容入口，不再扫描其他目录或按旧日志大小复制并覆盖设置。首次启动的数据目录由存储/日志模块照常创建。已位于选定数据目录内的旧 JSON 仍按原有 Store 逻辑转换成 SQLite；这与跨目录迁移是两件事。首次启动、空目标、目标仅有配置、旧源更大、现有 SQLite/WAL、正常 Windows 路径以及同目录转换有对应回归测试。正式发布仍须记录独立验收，不能把构建成功等同于发布完成。

## 可复核构建与测试

```powershell
python -B installer\build_installer.py --source . --output C:\Builds\FlashMonitor-candidate --acceptance-report '<独立 real-store-ui report.json>'
python -B installer\build_installer.py --source . --output C:\Builds\FlashMonitor-native --acceptance-report '<独立 real-store-ui report.json>' --iscc '<已核验便携工具目录>\ISCC.exe'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\test_installer.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\test_candidate.ps1 -ZipPath '<候选 ZIP 路径>'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\test_native_installer.ps1 -Compiler '<已核验便携工具目录>\ISCC.exe' -PackageRoot '<完整离线候选目录>'
```

输出目录必须不存在或为空，构建器不抹除既有结果。源码复制到独立临时快照，构建前后校验哈希；验收报告中的 UI 与四个后端模块哈希必须一致。Python 文件按白名单进入构建，`ipv6_bridge.py` 明确排除并检查 PYZ；用户数据、凭证、测试与 integrations 原型不打包。输出 `build-report.json`、ZIP、payload EXE 和构建日志。

普通脚本测试覆盖 9 组：首次安装与快捷方式、显式开机启动/升级保留、降级与篡改拒绝、运行互斥量、被占用 EXE、卸载保留、预览与确认、占用/过期预览、WAL 文件复制及冲突安全撤回。真实候选测试覆盖 4 组：解压白名单、真实 payload 安装哈希、原位重装备份、卸载后测试数据不变。仅使用 `%TEMP%\fm-installer-test-*`，登记写入模拟 JSON，快捷方式也位于沙箱；不修改真实 HKCU，不启动应用。测试数据库内容为合成字节，用于验证文件搬运，不是 SQLite 数据库语义测试。

原生检查另外覆盖安装、升级、卸载和重装，使用真实 SQLite 临时测试库验证字节保留、累计值和完整性；测试登记位于唯一 `FlashMonitorTest<GUID>` 卸载项和独立 Run 测试键，不写真实 Monitor 登记。测试还核对默认不开机启动、明确选中后保留、降级拒绝、运行互斥量、指定目录限制、跨安装器限制及外部 Run 值保留。动态降级测试覆盖登记版本分支；EXE 版本分支经过静态审查。

这些检查没有覆盖全新 Windows/WebView2 的应用启动、企业执行策略或生产候选在真实用户目录的安装。`-ExecutionPolicy Bypass` 仅用于当前安装脚本进程，不持久修改系统执行策略，不能覆盖组织策略。

## 原生安装器工具与后续验收

用户已批准获取并便携准备官方 6.7.3；获取和验证记录见 `docs/inno-setup-acquisition.md`。它是构建时依赖，不捆绑给应用用户。官方 [PrivilegesRequired](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm) 支持 `lowest` 当前用户安装；[关闭应用设置](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm) 与本定义的 `CloseApplications=no` 一致。原生定义使用 [StrToVersion](https://jrsoftware.org/ishelp/topic_isxfunc_strtoversion.htm) 和 [ComparePackedVersion](https://jrsoftware.org/ishelp/topic_isxfunc_comparepackedversion.htm) 拒绝版本降级。

把便携 ISCC 的绝对路径通过 `--iscc` 传入构建器即可生成 setup.exe。最终交付验收仍应检查原生向导的交互、目标 Windows 环境中的 WebView2 和应用启动；需要真实用户目录安装时另行安排，不能直接在运行实例上覆盖。`TestRoot`/`TestId` 仅为编译期测试参数，测试构建不作为用户安装包交付。

最终发布前统一新版本号及安装器渠道，审查应用内裸 EXE 更新路径是否绕过安装器生命周期；不要同时把旧裸 EXE、旧网络安装脚本和新安装器当成等价入口。正式验收、代码签名与发布审批是独立步骤。
