# #21 本轮执行记录（2026-10-20）

> 本文保留早期离线准备及费用 pilot 的历史记录。用户随后将评估重点改为 token，已另行运行真实 Flash 对照；当前改动、失败样本、完整分母及供应商 usage 见 [缓存分析](issue21-cache-analysis.md)。下文“未启动”等状态不代表后续 token 对照的当前状态。

## 工作区与保护

起点为包含已合并 #34 Docker 隔离的 main `b9be372ff9319dbe6e6887852921a1c64bab3c27`，已核对最新 issue 正文、实际远端及合并状态。工作区 `E:\codex\arc_agent`，分支 `codex/issue-21-offline-benchmark`。初始只有未跟踪 `HANDOFF.md`，没有修改或提交；相关路径没有适用 AGENTS.md 或 `.agents/skills`。

保留 Qing 提交身份，不添加 Co-authored-by。冻结标签 `ticketbooking-flash-30of30-20261020` 仍指向 `f75792c4a7b4377b4628163abd2c3445550a21e8`，冻结目录 11 个文件的 SHA256 与初始清单一致。

| 原始文件 | 字节 | SHA256 |
| --- | ---: | --- |
| agent.zip | 125604 | `8a2ec0f1c5d680426d5d6b514af852a745c66a369e144121dfd9dd6f3931f066` |
| generated-project-original.zip | 5082017 | `1d389b4ee83bab8cfe28a743d73c533419fb9d73a32c03e644de2cd13cf65baa` |

原始包未复制进 Git。没有修改原始需求、保护断言、认证、系统设置，或手工修补生成业务代码。

## 实现与证据范围

- 请求/阶段/Docker 结构化观测保留实际模型和原始供应商 usage，缺失值为 null，不记录提示词或响应正文，不推算缓存命中。
- 纯离线台账固定三类合成用例与原始 27 槽位矩阵，全部槽位仍未启动。开始但无终态计 interrupted；mock、真实 Docker、真实模型分开；证据哈希核对，至少三次才报告分布。
- 小范围缓存修改保留无关文件摘要，读盘验证相同内容；覆盖重叠批次、外部修改/删除、截断、压缩和回滚。详见 [缓存分析](issue21-cache-analysis.md)。固定 fake 回放累计消息字符 118,868 → 116,242（2.21%），不是实际 token、缓存、费用或完成率收益。
- 独立 Flash pilot 具备发送前持久化最坏费用预留、跨进程锁、未知用量保留、重启拒绝、金额/token/请求/时间上限、固定 Docker 镜像和保护断言。默认命令只离线预检。

## 验证记录

下表本地文件均在 `local-runs/issue21-offline-20261020/`，另有注明者除外。

| 验证 | 结果 | 日志 |
| --- | --- | --- |
| 初始默认沙箱聚焦测试 | 41 项：23 通过，18 项受临时目录权限错误影响，共 25 error 事件 | `local-runs/issue21-focused-tests.txt` |
| 初始全套 | 记录 8 ok / 2 error 后因相同权限错误停止，未完成 | `local-runs/issue21-all-tests.txt` |
| 初始 Docker 探测 | 命名管道权限拒绝，未启动容器 | 初始工具输出 |
| 用户授权桌面权限重跑 | 492 项：481 通过、1 失败、10 跳过；失败为旧安装 SDK | `local-unit-retry.txt` |
| 测试进程指定仓库 SDK | 492 项：482 通过、10 跳过、0 失败 | `local-unit-current-sdk.txt` |
| Windows 真实 Docker | 7/7 通过，26.703 秒 | `local-docker-retry.txt` |
| 缓存/预算代码 Windows 全套 | 508 项：498 通过、10 跳过、0 失败，25.021 秒 | `current-full-tests.txt` |
| affd336 Linux CI | 508 项：501 通过、7 跳过、0 失败，20.835 秒；真实 Docker 7/7，19.523 秒 | [CI 39414599652](https://github.com/Die4U23/arcbench-agent/actions/runs/39414599652) |
| pilot 后异常差额记录回归 | 12/12 通过，0.162 秒；全部 mock | `reconciliation-tests.txt` |

Windows 跳过为 7 项另跑 Docker 集成及 3 项 POSIX/symlink 夹具；Linux 只跳过另跑 Docker。旧 SDK 失败为 `core.hooksPath=nul not found in ['git', 'status', '--short']`，仅测试进程设置源码路径，与 main.py 一致，未修改安装或断言。

初始错误不是自动审批拒绝。用户明确授权后正常桌面权限执行获准，测试与 Docker 可用；未改 ACL 或使用宿主执行生成代码。Docker Linux engine 29.8.2，镜像内容 ID `sha256:3c9ee4121afa393a13ffa0d6d278b30545735781610e430aa2c4a08e7948296c`。

```powershell
$env:PYTHONPATH = 'E:\codex\arc_agent\arcbench-agent-runtime\src'
$env:ARCBENCH_RUN_DOCKER_TESTS = '0'
.venv/Scripts/python.exe -m unittest discover -s tests -v
$env:ARCBENCH_RUN_DOCKER_TESTS = '1'
.venv/Scripts/python.exe -m unittest discover -s tests -p test_docker_integration.py -v
.venv/Scripts/python.exe scripts/run_flash_pilot.py
```

最后一条默认不发送模型请求。

## 已授权 pilot：真实请求已发生，硬停止未解除

初始零预算后由用户明确更新为 **DeepSeek Flash 累计最多人民币 80 元**：pilot 10、后续最多 60、不可自动动用预留 10。每轮最多 10 元、2M token、150 请求、45 分钟，任一先到即停。Claude Code、Pro、平台付费调用未获授权且未执行。

2026-10-20 11:23:53–11:24:00 UTC，在 `affd33606ede8fcbbba0befecfeb625f79ea71ea` 执行合成定向修复 pilot，结果保留在 `local-runs/issue21-flash-80cny-20261020/{ledger.json,pilot-001/result.json}`。

| 指标 | 实测 |
| --- | --- |
| 分母 / 结局 | 启动 1，interrupted 1，通过 0；不报告重复分布 |
| 请求和返回模型 | `deepseek-flash`，官方 API |
| 模型请求 / 模型工具调用 | 1 / 0 |
| 输入 / 输出 token | 3,560 / 43 |
| 供应商缓存 hit / miss | 3,200 / 360 |
| 发送前费用预留 | ¥2.232768 |
| usage 峰时费用上界 | ¥0.001192 |
| 实际可核对费用 | **未核实** |
| 总耗时 | 6.985 秒，含 seed 检查 |
| 模型修复/验收 | 未发生，首次模型响应之后即停止 |

seed build 通过、保护 test 如预期失败，均在真实 Docker 中执行；不是模型修复通过。

**阻塞操作：继续 Flash 请求 / 目标：官方 DeepSeek / 原因：账户差额未落在 usage 对应价格区间。** 原始停止原因是 `Account delta does not reconcile with provider usage; stop all paid work`。请求已知执行、usage 已返回；不能重试这次请求。账本保持全局停止，重启拒绝继续。当前没有权限审批待处理，属于已触发的预算核对硬停止。

原实现只在核对成功后存差额，所以本次异常差额没有留存，不能推断为零、延迟扣费、价格错误或其他账户活动。已补充失败差额和预期范围持久化及回归测试；没有篡改原 pilot、放宽核对范围或解除停止。只读当前余额也无法还原丢失的请求前余额。

按 usage 上界计算还有 79.998808 元预算空间（含预留 10 元），但不是已核实剩余额度或继续执行许可。需先取得可归因本请求的收费证据并核对停止原因；当前不再发送付费请求。

## 未完成验收

同条件 Flash 基线/候选交错各至少三次实测尚未执行；本次 pilot 未到文件读取，不能评估缓存改动。真实 TicketBooking 三类冻结场景尚未完成，合成用例不能替代 Web 验收。Claude Code 对照、新候选平台正式 30/30 均未执行；历史单次成功不是候选验收。没有声称任何实测改善或追平。

[中文 draft PR #35](https://github.com/Die4U23/arcbench-agent/pull/35) 保持 open/draft，不关闭 #21、不自动合并。
