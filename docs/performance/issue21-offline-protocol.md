# #21：离线观测与对照准备

本阶段提供观测和离线台账，不自动执行 Claude Code 或平台任务，不据此声明性能提升。`benchmark_trials.py` 不导入 Agent 配置、不加载 `.env`，也没有模型或命令执行入口。初始零预算授权已更新为 **仅 DeepSeek Flash，累计人民币 80 元**。后续新增的 `run_flash_pilot.py` 默认仅离线预检，必须显式 `--execute --expected-commit <SHA>` 才可能执行已授权试点。

## 起点与证据类别

- 当前主分支基线：`b9be372ff9319dbe6e6887852921a1c64bab3c27`，包含已合并 #34 Docker 隔离。
- 历史冻结标签：`ticketbooking-flash-30of30-20261020`，解析源码为 `f75792c4a7b4377b4628163abd2c3445550a21e8`。
- `local-runs/accepted-ticketbooking-flash-20261020/` 为原始证据，只读保留。历史一次 30/30 不是当前 main 或本候选的验收，更不是成功率。
- `mock`：脚本化响应或单元测试，只证明实现行为。
- `real_docker`：真实容器执行，但无真实模型；不能证明 Agent 任务完成率。
- `real_model`：已获新预算授权的真实试验，必须记录实际模型、供应商、工具版本、源码提交和 Docker image ID。
- `scripts/benchmark_cache_sessions.py` 的字符前缀指标仍只是离线代理，不能代替供应商缓存 token 或账单。

本 PR 不改变生成提示词、读取缓存策略、修复策略、原需求或受保护验收，不手工修补任何生成业务代码。先获得可靠观测，后续再据真实瓶颈选择优化。

## 结构化观测

INFO 日志增加 `ARCBENCH_METRIC ` 后接版本 1 JSON：

- `model_request_started` / `model_request_finished`：实际 transport 尝试的唯一 ID、请求序号、阶段、请求模型、供应商 hostname、响应实际模型、耗时、返回/错误状态及错误类型。
- 用量为供应商原字段：输入、输出、推理、缓存命中、缓存未命中；支持 `prompt_tokens_details.cached_tokens` 并保留来源。缺失为 `null`，不从输入减命中来伪造“供应商未命中字段”，不从字符长度估算 token。
- `operation_started` / `operation_finished`：模型 Agent 总墙钟时间和每次 Docker 调用的墙钟时间，含失败。Docker 耗时包括预检、快照、启动/执行、清理和回传，尚未拆分内部步骤。返回不代表通过；`local_passed` 仅在确有 VerificationResult 时记录。
- `tool_returned`：工具返回次数、耗时、返回字符数、工具错误标志。它不是工具语义验收，也不包含中途进程终止而未返回的调用；不要把它冒充完整的工具请求计数。

这些记录不包含提示词、工具参数/正文、源码、响应正文、异常消息、URL 查询参数或认证头。日志仍沿用现有日志配置；操作者应保存完整日志，不能只选择成功片段。

离线解析：

```powershell
.venv/Scripts/python.exe scripts/benchmark_trials.py requests `
  --log local-runs/<trial>/controller.log `
  --output local-runs/<trial>/requests.json
```

解析器按供应商、实际模型、请求模型、阶段分别汇总，不混合供应商。每字段同时给出完整总量（有缺失则 `null`）、已知小计和观测覆盖数。缓存比例按命中/(命中+未命中)加权，仅当该分组每次请求的两个原字段都存在时输出。开始后缺少结束的请求仍计入次数。重复日志、缺开始事件或配对信息变化会报错；解析失败不能被当作零请求。

操作 span 有嵌套，**不能相加当总耗时**。总耗时用外部 trial 起止时刻，模型阶段用逐请求阶段耗时，构建/验收用相应执行器的原始时刻。当前新增日志不足以自动推导“构建修复次数”及“首次完整验收”，台账要求从完整执行轨迹填入，缺失保留 `null`。受控修复试点另外保存每次 build/test 的执行顺序、退出码和耗时。

## 固定用例与预登记

`benchmarks/issue21/pilot-spec.json` 是一个 **合成小项目设施试点**，不是完整 Web 或 TicketBooking 性能基准。用 Node 内建功能覆盖：

1. 从零：仅 package.json，从固定指令构建 CommonJS 购物金额模块。
2. 定向修复：固定包含 quantity 缺陷的 seed，修复至固定断言通过。
3. 多轮修改：固定正确初始模块，按固定顺序加入折扣、非法输入拒绝、收据；每轮必须保留此前要求。

初始文件、逐轮输入、受保护断言和操作规则均内嵌在 spec 中。每个 trial 必须新建 seed；多轮由可信评估器在每轮将对应受保护文件提供为 `acceptance.cjs`，模型不可编辑。每次验收前后核对断言 SHA256；不能通过修改断言取绿。每轮完整 build 和该轮保护验收通过后才发送下一条指令；任一轮失败终止整个 trial。未添加已实现答案来伪造任何结果。

三组分别为 Flash 基线、同配置 Flash 候选、Claude Code 产品对照。每类每组 3 次，共 **27 个预登记槽位**；执行顺序按重复序号轮换组顺序，以降低总是先暖某组缓存的偏差。供应商缓存无法保证冷启动，必须记录时刻和真实缓存用量，不能宣称已控制供应商缓存驻留。

```powershell
.venv/Scripts/python.exe scripts/benchmark_trials.py prepare `
  --spec benchmarks/issue21/pilot-spec.json `
  --output local-runs/<study>/manifest.json

# 启动一个实际 trial 前先登记。此命令本身不会运行任何东西。
.venv/Scripts/python.exe scripts/benchmark_trials.py start `
  --manifest local-runs/<study>/manifest.json `
  --evidence local-runs/<study>/evidence `
  --slot fresh.flash_before.1 --kind mock

# result.json 中 artifacts 的相对路径以 evidence 目录为根。
.venv/Scripts/python.exe scripts/benchmark_trials.py finish `
  --manifest local-runs/<study>/manifest.json `
  --evidence local-runs/<study>/evidence `
  --slot fresh.flash_before.1 --result local-runs/<study>/result.json

.venv/Scripts/python.exe scripts/benchmark_trials.py report `
  --manifest local-runs/<study>/manifest.json `
  --evidence local-runs/<study>/evidence `
  --output local-runs/<study>/report.json
```

所有输出采用 exclusive create，不能覆盖旧 manifest/start/result/report。重报使用新文件名；不能删除失败结果来补跑。已 start 未 finish 按 interrupted 计入分母；未开始槽位独立显示。结果文件的 artifact SHA256 在录入和重报时实际核对，拒绝目录外路径、隐藏路径、链接及内容变化。这里只提供完整性检查，不是防恶意篡改签名系统。

结果至少包含：

```json
{
  "outcome": "timeout",
  "evidence_kind": "real_model",
  "provider": "<actual provider>",
  "actual_model": "<actual resolved model>",
  "tool_version": "<actual tool version>",
  "source_commit": "<exact source>",
  "image_id": "<actual Docker image ID>",
  "first_build_passed": false,
  "first_acceptance_passed": null,
  "build_attempts": null,
  "build_repairs": null,
  "model_requests": null,
  "tool_requests": null,
  "total_seconds": null,
  "model_seconds": null,
  "tool_seconds": null,
  "build_seconds": null,
  "acceptance_seconds": null,
  "verified_cost": null,
  "artifacts": {"controller.log": "<actual 64-character SHA256>"}
}
```

该例只是字段说明，不是可导入的实测记录。`outcome` 可为 passed/failed/timeout/interrupted/blocked。完成率及首次通过率分母都是该组全部 started；首次结果未知另报 unknown。样本少于 3、配置身份不同或指标缺失时，不输出该指标的 min/median/max。失败记录缺少模型身份不会被分到另一个组而抬高原组成功率。mock、Docker、真实模型永远分开；缺失费用不当零。

`verified_cost` 有值时必须包含 `amount`、`currency`、`billing_reference`，并将可核对的用量/账单证据纳入 artifacts。不同币种或不同配置不合并费用。本项目不会自动推断价格、汇率或账单，导入者仍需核实账单对应请求范围。

## 完整 TicketBooking 对照仍须完成的准备

合成试点只验证设施。正式比较需要另外冻结完整研究 spec：

- 从零使用原始 requirements/输入资产；两组从完全相同的空项目和任务资产开始，不能借用历史成功源码。
- 修复/多轮研究从历史交付包的**独立复制品**建立固定 seed。先审查并冻结缺陷补丁、每轮需求及对应保护验收；原始包不修改。此项尚未实施，不能以试点代替。
- 固定 seed/需求/验收/依赖锁文件 SHA256、基线与候选完整 commit、相同工具链镜像的实际 ID、CPU/内存/PID/tmpfs、单线程串行、网络及人工介入规则。Claude Code 对生成代码也只能调用相同 Docker 执行器，不能默认在宿主执行。
- 付费前将 spec 中候选提交、实际镜像 ID、Claude Code 实际模型/版本占位符替换为核对值，重新登记 manifest。记录两组实际模型配置；Claude Code 是产品对照，不能把不同模型收益归因于架构。Flash 前后比较独立。
- 依赖固定为已审查离线缓存版本。不得临时联网安装缺失包、升级模型或放宽隔离。任何人工介入终止该 trial；重跑若获批准必须占新槽位，旧失败保留。
- 新候选最终仍需自测、保护回归与平台正式 30/30。当前所有这些真实候选结论均未产生。

## 更新后的预算（总额已授权，首轮后已硬停止）

用户于本轮后续消息明确授权 **人民币 80 元，仅 DeepSeek Flash**。取代此前零预算与未获批准的 USD 120 提案；不允许 Claude Code 新增调用、Pro、平台自动付费提交或追加预算。

2026-10-20 **11:14:56 UTC 起的复核**已读取 [DeepSeek 官方人民币计价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：调用 ID `deepseek-flash`，当前服务版本 `DeepSeek-V4.1-Flash-1017`；高峰每百万输入命中/未命中/输出分别 **¥0.04 / ¥2 / ¥8**，空闲时段减半。老 Flash ID 会路由至当前版本，历史冻结运行不能假定与今天同模型。预留一律使用高峰价，不依赖低价时段。当前上下文上限公布为 1M。

既有工作区配置检查仅输出存在性与非敏感字段：官方 `https://api.deepseek.com`、三个模型字段均 `deepseek-flash`、key 存在。只读 `GET /user/balance` 返回 HTTP 200、账户可用、币种 CNY；未输出 key 或余额，没有模型请求。请求禁用环境代理和重定向，只访问原授权官方端点。Claude Code CLI 已安装，`--version` 为 2.1.218；未验证其账户/实际模型，也未调用其模型。Claude 本轮仅保留离线研究计划，27 槽位旧试点 manifest 不能直接变成付费计划。

受控阶段建议如下，不要求花完额度：

| 用途 | 轮次 | 单轮费用硬上限 | 小计 |
| --- | ---: | ---: | ---: |
| 固定小规模修复预试验（候选） | 1 | ¥10 | ¥10 |
| 预试验经复核后，同条件 Flash 基线/候选各三次 | 6 | ¥10 | ¥60 |
| 在途、异常、账单不确定预留，不能自动用于补跑 | — | — | ¥10 |

每轮上限同时为 **¥10、2,000,000 输入+输出 token、150 次 transport 尝试、45 分钟墙钟**。固定非思考、温度 0、每请求最大输出 4,096 token、不携带图片、不使用服务商搜索/抓取；未来基线与候选须使用完全相同配置、观测及预算保护。小规模修复是合成设施试点，不能据此外推完整 Web 成功率。缓存修改仅有离线代理指标；实测须选自然涉及多文件读取的固定任务，不预设收益。

每次请求前可使用整个上下文窗口的保守上界预留，而不是字符近似：按 **1,100,000 输入 token 全部未命中 + 4,096 输出 token**，单次最坏预留为 `1,100,000 × 2 / 1e6 + 4,096 × 8 / 1e6 = ¥2.232768`。这个输入界高于官方公布的 1M 标准窗口；执行器必须限制为该标准模型/窗口，拒绝扩展上下文或额外付费工具参数，重新核对官方限制后方可使用。

**不得直接用 `main.py` 开跑；专用守卫必须满足以下条件：**

1. `agent/experiment_budget.py` 先将 `¥2.232768` 和最坏 token 数持久化预留，再允许发送；确认返回 usage 后才用供应商真实命中/未命中/输出字段结算并释放剩余预留。任何缺字段、超出预留或未返回 usage 均冻结预留并停全部付费请求，不自动重发。
2. 全局 ledger 必须独占锁、原子持久化并在重启后保留失败/在途预留。所有阶段合计最多可分配 ¥70，另 ¥10 不可自动动用；下一请求若不能同时被该轮费用/token/请求/时间余量及总额覆盖，不发送。现有 Agent 仅在已使用量达到阈值后检查，且 `main.py` 固定 10M/250，**不能充当这套守卫**。
3. 运行前后用 [官方余额接口](https://api-docs.deepseek.com/api/get-user-balance/) 与供应商账单核对费用；保存原始 usage、请求 ID 和非敏感账单引用。高峰价计算只是保守费用上界，不是已核对实扣。只有同一 key 无其他并发消费且币种/赠送余额变化可解释时，余额差才能辅助归因；有歧义停止，不伪称精确成本。
4. 执行器应串行、可中断，限制 transport 和容器 deadline，并保存每次 owned-container 身份以核对终止清理；保护断言与 seed 哈希在每轮前后核对。固定 candidate commit、实际镜像 ID、受控配置及完整原始轨迹；禁止宿主生成代码执行。
5. 价格/模型路由变化、账单或记账缺失、隔离/保护断言破坏、重复同类失败且两轮无新证据、人工介入、任一上限到达均立即停止。三次不足仅报告样本，不补跑取最好结果。预试验失败就先分析，不启动预分配的六轮。

上述模型额度授权不包含 Claude Code 对照或平台自动提交。首个 pilot 已发出 **1 次 Flash 请求**，因账户差额与 usage 不能核对而 interrupted；usage 峰时费用上界 **¥0.001192**，实际可核对费用未知。账本保持全局停止，不能自动重试、重置或开始 A/B。详见 [执行记录](issue21-offline-report.md)。issue 仍开放，PR 保持 draft，最终正式 30/30 和公平实测收益尚未完成。

## 预算守卫与首个试点入口

`scripts/run_flash_pilot.py` 仅支持登记一个固定 repair 预试验，已有 round 则拒绝重复运行。费用账本固定在 `local-runs/issue21-flash-80cny-20261020/ledger.json`；未来同条件 A/B 必须复用它，不能改目录重新获得 80 元。独占锁不自动清除崩溃残留，未知请求保留全额预留，重启不释放；已结算金额与原始 usage 不符时拒绝继续。

输入限制同时检查实际序列化请求不超过 65,536 UTF-8 bytes、仅文本/本地函数工具、固定非思考 Flash、固定 4,096 输出上限、无扩展上下文参数。**货币预留不把这 65,536 bytes 猜成 token**，而是覆盖官方整个标准 1M 窗口（按 1,100,000 输入 token 保守包络，包括模板/工具协议开销）。因此不依赖未公布的序列化/tokenizer 比例；若官方窗口或价目变化，日期门禁拒绝跨日直接执行，必须重新核验。

每次付费发送前后访问官方只读余额接口，delta 必须落在同一份 provider usage 的空闲至高峰费用区间，否则停止；不打印账户余额。模型请求使用新建的 HTTP client，禁用环境代理和重定向、SDK retry=0。原始响应（含请求 ID/usage，无 key）保存到项目之外。生成代码只在已实测的 Docker 镜像内容 ID 中执行；package.json/acceptance.cjs 每次工具前后逐字节核对，破坏即停止。

守卫/传输/读取缓存新增测试与全量 Windows 回归：**508 项，498 通过、10 跳过**。首次定向测试发现新截断夹具恰好等于 30,000 字符上限、并未触发截断；夹具改为 `MAX_FILE_CHARS + 1` 后完整回归通过，未删除或放宽断言。

```powershell
# 只打印非敏感离线政策，不加载 key，不发模型请求
.venv/Scripts/python.exe scripts/run_flash_pilot.py

# 仅在本轮授权、固定提交、已验证镜像及所有离线检查成立后执行一次
.venv/Scripts/python.exe scripts/run_flash_pilot.py --execute --expected-commit <reviewed-SHA>
```
