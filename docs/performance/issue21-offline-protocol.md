# #21：离线观测与对照准备

本阶段只提供观测和离线台账，不执行模型、不自动启动 Claude Code、不启动平台任务，不据此声明性能提升。`benchmark_trials.py` 不导入 Agent 配置、不加载 `.env`，也没有模型或命令执行入口。本轮模型实验预算是 **0**。

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

操作 span 有嵌套，**不能相加当总耗时**。总耗时用外部 trial 起止时刻，模型阶段用逐请求阶段耗时，构建/验收用相应执行器的原始时刻。当前新增日志不足以自动推导“构建修复次数”及“首次完整验收”，台账要求从完整执行轨迹填入，缺失保留 `null`。本 PR 没有付费 runner 或货币预算执行器。

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

## 下一阶段预算提案（未授权、未执行）

拟申请总上限 **USD 120**，不是历史预算续用，也不是供应商报价：

| 用途 | 轮次 | 单轮费用硬上限 | 小计 |
| --- | ---: | ---: | ---: |
| 三类小规模真实试点 × 三组 × 三重复 | 27 | USD 3 | USD 81 |
| 候选最终正式验收预留 | 1 | USD 9 | USD 9 |
| 未返回用量、最后请求、账单延迟预留（不可用于自动补跑） | — | — | USD 30 |

试点每轮同时设 1,000,000 输入+输出 token、80 个模型 transport 尝试、20 分钟墙钟和 USD 3 上限；任一先到即停。正式完整候选的另一个槽位拟为 8,000,000 token、250 次、45 分钟、USD 9。以上额度是否足够须按实际服务商/Claude 计费方式审核；不足就停止并报告，不自动扩容。Flash 使用与基线相同的具体模型版本和 thinking/温度/上下文配置；Claude 模型和套餐/按量计费方式在用户批准前确定，不能用“Claude Code”代替模型名称。

**执行前置条件尚未齐备，不能拿这份文档直接开跑：**

1. 主助手向用户取得明确的新预算批准，确认币种、实际模型、轮次及账单来源；当前授权仍为零。
2. 实现并离线验证外部硬停止器；在每次发送前按可信实际价目和最大可能输入/输出预留费用和 token，不能依赖现有“已用量到阈值”检查（最后请求可能超出阈值）。
3. 将未返回用量视为不确定支出冻结对应预留，不重发；账单无法核对、计费包无法分摊、模型路由变化、隔离/验收哈希变化、未知依赖或人工介入时停止整组。
4. Docker 真实隔离检查通过；Claude 执行适配器同边界验证通过；镜像、源码和输入全部固定。
5. 总上限包含已有确认支出与在途最大预留；可用额度不足以覆盖下一请求的上限时不发送。总计/单轮 token、请求、时间或货币额度任一到限停止；三次不足只报告样本与阻塞，不补跑取最好结果。

预算获批也不自动允许合并。本 issue 仍开放，PR 保持 draft，最终正式 30/30 和公平实测收益须另行审查。
