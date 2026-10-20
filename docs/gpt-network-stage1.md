# GPT / Codex 网络：隔离阶段成果

本目录是独立代码副本，不是安装包源码，不可覆盖整个稳定项目。补丁只包含新功能所需文件。当前功能严格为诊断与预览；无客户端写入、应用、撤销路由，无 TXT 搬运入口。

## 已实现

- 有界范围条件树：完整 Windows 程序路径、精确域名/后缀、嵌套 AND/OR；浏览器与网站使用 AND。拒绝空组合、名称降级、控制字符和当前规则语法不能安全表示的值。
- 完整有序规则诊断：支持 DOMAIN、DOMAIN-SUFFIX、DOMAIN-KEYWORD、PROCESS-PATH/NAME、IP-CIDR(6)、NETWORK、DST-PORT、MATCH、AND/OR/NOT；GEOSITE、RULE-SET、未知类型、缺少身份/地址信息保持未知。先出现的未知条件阻止宣称确定命中。PASS 继续匹配，已知不支持 UDP 的目标继续匹配后续规则，未知 UDP 能力保守处理。
- 固定本机控制器读取，限定 GET 端点，拒绝重定向，不采用环境代理，每次响应最多4MiB；只输出必要代理组元数据，不输出原始节点配置、凭证或原始错误。
- 只读 /api/network-policy 与 /api/network-preview，沿用现有认证/Origin 防护。无 apply/undo 路由；浏览器传入能力字段不能解锁写入。
- 同一紧凑弹窗内选范围/代理组，IPv6阻断与UDP443阻断默认关闭，展示自然语言范围、风险、观察样本与拟新增规则。所有结果均为“未应用”，确认应用始终禁用。关闭或修改输入使旧异步响应失效。
- 未来桥接回执的纯状态投影：要求可信会话、实例/数据根/配置/策略/操作ID、修订和新鲜核验时间一致；保存、加载、连接证据分开。此函数不生成这些证据，也没有已接通的真实桥接。

## 限制与真实剩余工作

“阻止IPv6”不等于强制IPv4。这里不修改全局DNS、全局IPv6、节点传输、TLS指纹或共享代理组选择；UDP443阻断可能影响QUIC、语音/WebRTC。代理服务器最终出口地址无法仅由本地连接元数据证明。

规则诊断不是完整Mihomo解释器；当前没有读取已安装GEOSITE/RULE-SET内容，没有远端DNS求值，也不模拟嗅探重写及所有特殊规则。重复GET可以检测可见配置变化，但不是原子快照；真正提交前仍须客户端内部一致性锁和严格解析。

程序路径仅接受已核实且15秒内观察到的身份；当前拒绝UNC、设备路径及含逗号/括号/引号的路径和目标组，不退化为进程名。需要后续固定核心语法测试才能扩大支持。域名没有预置“完整OpenAI清单”，用户列出范围，不暗中扩大到共享CDN。

真实一键应用仍需：NativeBridgeHooks实现、所有原生writer协调、严格核心验证/规则加载、持久journal与中断恢复、可信同用户会话、Monitor到客户端接线、精确撤销及真实客户端生命周期验收。没有为获得可运行结果而把strictBridgeParserAudited改为true。provider/standard转script路线未重启。

## 构建工具核验（2026-10-15）

现有：Go1.26.4、CMake4.3.3、Git2.55.0.windows.4、Python3.11.9。PATH未找到Flutter/Dart/rustc/cargo/gcc/cl；常见VisualStudio Installer/vswhere路径不存在。这不是遍历全盘证明不存在。

隔离FlClash源码CI固定Flutter3.47.1、Go1.26.4；pubspec要求Dart>=3.10，但lock要求>=3.13。rust-toolchain.toml明确1.95.0，列出九个目标；Windows x64最小构建不应默默安装全部九个目标，需在隔离构建配置中明确目标范围。

官方来源：
- Flutter归档：https://docs.flutter.dev/install/archive
- Windows C++要求：https://docs.flutter.dev/platform-integration/windows/setup （Desktop development with C++）
- Rust：https://rust-lang.org/tools/install/ ，固定组件清单：https://static.rust-lang.org/dist/channel-rust-1.95.0.toml
- Go已满足固定版本：https://go.dev/dl/

构建复核更正：此前404是清单URL多写了一层/flutter/，不是版本缺失。正确官方清单 https://storage.googleapis.com/flutter_infra_release/releases/releases_windows.json 确认Flutter3.47.1、Dart3.13.1，SDK提交6655482ec06e547f90abf8ae7590466f4415978d与官方tag一致。Windows x64 ZIP为1,929,093,631字节，声明解压文件合计3,481,316,032字节，保留ZIP合计约5.039GiB。官方预期SHA256为4cbf94fde1f5f8d6b9fc50b2483b57cf2077f61712282c2f4cf92560168f442b；未下载全包，未做本地全包校验。仅读取约3.58MB ZIP目录元数据。源码、实际安装Core内嵌提交与官方成功CI均对应7c61c90ac20493b474d19c75ec96262b478b4b88，但Core标记vcs.modified=true，不可宣称干净源码字节级复现。子模块须固定70f0570405c3c2c47bb113b88db95006d239b346。详细只读报告在evidence/build-audit/report.md。

Rust官方HEAD确认：rustup引导器12,721,664字节；rustc1.95.0 x64 71,577,136字节；cargo10,006,024字节；rust-std22,068,624字节。三个最小组件合计103,651,784字节（约98.85MiB），加引导器约110.98MiB；不含解压空间、crate下载、其他目标、Flutter和MSVC/Windows SDK。校验值与URL见build-prerequisites-metadata.json。VisualStudio工作负载大小取决于所选组件，未运行安装器，尚不能给出准确下载量。

## 下一阶段需要的明确授权

优先提供/批准隔离Windows构建VM，使用可核实的固定Flutter/Dart、Rust1.95.0、C++/Windows SDK和固定Clash.Meta子模块。若在本机安装，建议Flutter/Rust/cache置于专用用户目录C:/Users/finhe/AppData/Local/FlashMonitorBuild，进程级PATH，不改全局PATH；MSVC/SDK安装会修改系统组件，必须单独确认组件清单及体积。先补齐Flutter清单和MSVC组件预算再提出实际下载请求。

还须授权获取锁定的pub/cargo/go依赖与子模块（只入构建环境）。安装/替换真实FlClash、schema迁移、Helper配套升级、TUN/系统代理变化属于后续独立部署批准，不包含在代码与隔离测试授权中。

## 复现

使用现有Python3.11，python -B -m unittest tests.test_network_policy。HTTP测试需将tests加入sys.path；browser脚本使用已安装Playwright和Edge：python -B tools/check_gpt_network.py --output evidence/gpt-browser。全部浏览器请求被fixture拦截，不能用monitor.py启动真实上下文代替。


## 独立验收修订2

协议缺失/无效且目标UDP能力不能证明支持时，规则跟踪保持unknown并保留可能DIRECT回落。真实clash_to_flow现在提供有效destination_port；缺失/无效端口不能证明UDP443命中。PROCESS-NAME与路径规则一致拒绝冲突身份。未来回执的now、checked_at、applied_at、connection_at均要求有限数字，缺少连接时间不声称已验证。未开放写入或修改UI。
