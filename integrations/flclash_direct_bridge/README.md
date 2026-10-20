# 直接保存桥接原型（不可部署）

这份原型不替换当前 FlClash，不启动接口、不生成持久凭证，也不把 Monitor 的实验 SQL/PUT 执行器开放为生产。当前 Monitor 仍不会“保存即生效”。

## 已实现的代码

- `overlay/lib/bridge/journal.dart`：schema 4 同库操作表，await 原生 RulesDao、原生 snowflake 与 fractional order；新增规则/canonical mapping/归属日志同事务，精确删除检查完整行与所有引用。日志保留 profile 历史标识，不阻止原生删除 profile。
- `overlay/lib/bridge/coordinator.dart`：可重入串行配置协调器。规则编辑、profile 切换、订阅发布、源恢复、配置加载五类 writer 未全部接入时不可提交。
- `overlay/lib/bridge/service.dart`：prepare/commit/status/undo；会话/配置/修订与短 token 绑定；持久、运行、未知结果分开；失败补偿只删自有差量，结果未知不盲目补偿。状态核验不能只回放旧 verified。
- `overlay/lib/bridge/session_server.dart`：仅随机 loopback 端口、随机内存会话 token，拒绝 Origin、任意动作/字段和超过64KiB请求。没有自动启动或磁盘 discovery 文件。
- Core 新严格 RPC 与 Dart CoreController/CoreHandlerInterface 对应方法；完整 ParseWithBytes 发生在发布前，错误不调用 DefaultRawConfig；成功采用预解析候选，未知 apply 结果单独报恢复。**解析副作用审计常量为 false**，当前拒绝执行。
- `flashmonitor/ipv6_bridge.py`：后端私有会话适配器；协议/schema/实例/root/模式/完整验收标志均须通过。凭证不进网页；丢失或无效回执置 unknown，重试只查原 operation，不重复写入；持久/编译/运行规则 hash 与非规则/无关规则回执必须一致。

## 明确尚未完成的接线

`NativeBridgeHooks` 的真实 Riverpod/固定profile纯编译/严格加载/ordered audit 实现、五类原生 writer 的统一接线、可信 Windows bootstrap/peer身份交接、隔离 GUI 数据根/单实例改造或 VM、跨进程冷启动后的自动恢复接线、Monitor API/UI 到真实会话的绑定都未完成。能力门因此保持关闭。不能手工把 flags 改成 true 来替代这些工作。

## 可复查产物

- `flclash-prototype.patch`：对官方对照提交 `7c61c90ac20493b474d19c75ec96262b478b4b88` 的13文件补丁；`git apply --check --ignore-space-change` 已通过。该参数用于当前缓存源码的CRLF上下文。
- 隔离副本：`C:/Users/finhe/AppData/Local/Temp/fm-flclash-direct-bridge-20261015/source`。
- `apply_overlay.py` 仅允许指定命名的隔离 source 副本；`export_patch.py` 导出差异。没有改 `D:/Daily/Commen/FlClash` 安装目录或官方缓存原稿。

## 测试边界

Monitor 会话与回执契约、随机 loopback 合成客户端往返、SQLite真实DDL的日志原子性契约、Go标准库提交顺序守卫可运行。Go守卫测试不是实际 mihomo/完整客户端测试。`overlay/test/bridge/journal_test.dart` 的3项真实 Drift 测试已编写，但未运行。

完整 Core 离线编译已尝试，因 Go 依赖未缓存、Clash.Meta 子模块为空而失败；Flutter/Dart、Rust工具链当前未找到。没有新装工具链或下载替代未知子模块，也没有编译出可部署客户端。

## 后续批准与依赖

需要先准备隔离 Windows 构建/验收环境：官方 Flutter 3.47.1（Dart语言约束>=3.8,<4）、Rust/cargo、固定提交的 Clash.Meta 及Go依赖；Windows C++ SDK、GCC/打包依赖需核实。修改数据库版本后要运行codegen，不能手改generated文件。

完成真实 hooks、writer接线、固定解析器副作用审计与完整客户端生命周期/并发/坏配置保旧验收后，再讨论部署。

部署需用户另外批准：安装/替换定制 FlClash 整套构建，schema 3→4迁移、启用进程内会话控制接口，以及可能需要重新构建/注册具有新Core SHA256的Helper。不能只换Core绕过Helper校验。切换当前客户端和全量重新加载可能影响现有连接/TUN，应在批准中明确；本轮未执行这些动作。
