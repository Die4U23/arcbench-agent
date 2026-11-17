# 执行状态与回滚边界

开发与评测首先遵守仓库 [AGENTS.md](../AGENTS.md) 的泛化优先规则。单任务分数不构成稳定版本晋级依据；评分源码和详细失败断言不得用于优化。

v14 从本次任务与当前工程生成行动，不携带预设业务计划。ReAct 的一次请求可以返回一批工具动作；Plan-and-Execute 则先生成结构化依赖图，再逐步使用同一 ReAct 执行器实现。

## 计划与证据

计划步骤包含 `id`、`goal`、`requirement_ids`、`depends_on`、`acceptance`。控制器验证非空与大小上限、唯一标识、需求引用、完整覆盖、依赖存在性和 DAG，无效计划不执行写入。执行顺序为确定性的拓扑顺序，不并发修改共享工程。

每步由 `PlanExecutor` 持有一个源码检查点，统一决定接受或回滚。模型执行结束后，检查静态结构与当前步骤覆盖；启用步骤行为门禁时，还必须通过当前及已接受需求的累计诊断。全部必需检查通过后才提交该步骤；失败恢复步骤开始时的源码，保留之前成功步骤。重规划只包含未完成步骤所引用的需求，并带上成功步骤摘要和失败信息。重规划不能补充自己的预算。

规划提示包含实际的单步请求数与单次输出上限，要求从当前工程出发拆分依赖；空工程首先建立可验证的运行基础。执行提示以当前步骤为范围，完整需求包是参考上下文，不能把包内的整体验收要求误当成本步骤必须一次写完的指令。源码工具使用项目相对路径，`application/` 只是上下文展示前缀。

每个阶段开始时附加当前源码观察，包含有界文件目录、摘要和能完整放入额度的 UTF-8 文件。它覆盖初始包中较旧的应用源码观察；原始需求仍在需求包中。省略内容、二进制文件及不完整目录显式标记，模型可按需读取。观察重新从当前事务状态生成，回滚后的下一阶段不会收到失败编辑的旧缓存。原始需求镜像、评测测试、依赖锁文件及既有快照策略排除的凭据不进入这份附加上下文。规划器另收到本轮剩余总请求额度，执行器收到当前步骤的实际请求份额。

规划器还收到剩余 token、下一请求完整预留和保守可负担轮数估计，据此建议计划规模并合并共享模块的端到端工作。估计不能作为强制步数上限，避免把工作塞进超过单步额度的大步骤；原有计划长度和单步请求硬限制保持有效。每次请求前追加控制器实时额度，实施阶段最后三次请求转入整合、覆盖记录和验证，最后一次明确要求必要编辑与验证同批完成。消息本身纳入上下文检查；这只是决策反馈，不延长预算或自动声称完成。当前阶段已完整提供且摘要未变的文件，重复整文件读取返回指向该观察的引用；定向行读取、内容变化或未完整提供的文件仍正常读取。引用不冒充文件内容，不删除消息或推理协议；重复动作照常计数。

`update_coverage` 可批量更新明确指定的 kind/ref/status/files，保留其他条目并校验引用及源码路径；它不自动声称完成，不接受评测输入或元数据充当实现。验证当前计划步骤时，除结构检查外还检查该步骤的源码覆盖声明，缺失时允许在本步骤剩余预算内修正，不能提前提交该步骤。覆盖声明仍不构成行为验收。

`acceptance` 是计划提出的验收条件，不能由模型一句“完成”变成通过记录。未启用步骤行为门禁时，步骤证据为 `static_only`；启用时为 `synthetic_step_diagnostics`，仍不是正式验收。coverage 必须标记 covered 且引用实际源码，partial 和缺失文件仍属未完成；即使如此也只是覆盖声明。正式成绩来自源码冻结后的独立平台评测。

## 执行职责

| 所有者 | 负责的决定或状态 |
| --- | --- |
| `CompetitionModel` | 提供方协议、模型与工具回合、协议纠正；接收显式阶段上下文 |
| `PlatformGenerationModel` | 当前源码上下文、阶段提示、工具准入和步骤覆盖检查；计划路径只返回模型执行结果 |
| `PlanExecutor` | 步骤请求份额、最终静态与行为验证、候选隔离、唯一的步骤提交/回滚决定 |
| `RunControl` 与预算账本 | 共享执行限制、停止权限及实际消耗；账本独立保留硬性预算约束 |
| `StepBehaviorGate` | 冻结公开需求诊断、累计回归、绑定源码摘要的检查结果 |

模型执行结果、静态校验和行为诊断分别保存，不能用后一阶段的结果覆盖前一阶段的含义。控制器强制收尾保留模型原始停止原因，并独立记录收尾验证，不把“模型请求额度用尽”改写成模型自行完成。独立 ReAct、源码复查和修复入口仍需要自己的外层事务；单个工具写入的原子保护也继续保留。

阶段上下文显式传递当前请求份额、源码观察、读取回执、巡查次数和工具完成状态，不借用模型对象上的临时私有属性跨阶段通信。全局无进展与局部巡查额度是不同约束，切换阶段不重置全局计数。停止原因保留用于诊断；允许有界本地诊断不意味着允许新模型请求、源码编辑或释放未知用量的预留。

步骤诊断每次使用独立临时工程，结束或异常时清理；冻结诊断和隔离候选保留在证据目录。步骤之间不复用可被构建或应用改写的 `node_modules`，避免依赖目录中的运行数据影响下一步骤。累计检查不缩减为仅检查当前步骤。冻结文件的写入与摘要生成复用同一个实现。

步骤完成记录在源码提交后确定，再发布已接受诊断的记账状态。发布异常会中止后续执行，但不能把已经验证并提交的源码误记为回滚；验证或提交前发生的异常仍恢复步骤检查点。

## 评分测试隔离

生成入口只接受 `ARC_INPUT_POLICY=requirements_only`，其他策略在解析输入前拒绝。输入解析器不发现或计数测试；需求包排除任意层级 `tests/`、`__tests__/` 和 `*.spec.*` / `*.test.*` 文件。排除发生在读取文件内容之前，完整性检查使用同一范围。只改变这些评测文件不会改变生成上下文的来源摘要。需求原文及合法参考资料仍保留。

所有规划、实现、复查共享不含评分测试的包；seed 只能从需求获取。静态分析禁用评分测试定位信息，运行时仅执行控制器冻结的自生成诊断；评分断言和结果不能进入修复循环。已有应用包含上述测试路径时，在模型创建前拒绝并保留原文件，要求使用独立的纯源码工作区。工具也拒绝读写这些路径。

提示契约也按输入政策生成独立副本，需求包与可读取 evidence 使用同一版本。生成只依据公开需求、当前源码和开发诊断工作，不发出寻找评分测试或共享 helpers 的指令。规划、执行、复查与修复经过同一边界；低层保留的 `provided_tests` 兼容接口不能用于生成入口。

这是可信输入布局下的数据流约束，不是恶意数据泄漏检测或操作系统沙箱：不能识别伪装为业务源码/需求文档的测试答案。正式实验仍须由外部控制器把评测文件放在生成进程不可访问的独立工作区，禁止人工修改生成应用或把评分用例改名放回输入。任务需求中明确提供的示例与前置数据是合法输入，不能为了避开示例而删除真实需求。

生成结束保存 `generation-boundary.json`，记录输入策略、需求包摘要和交付源码摘要。独立评测须先核对该摘要，并记录自己的测试版本、实际收集数及运行 ID。不要把测试文件摘要写回 Agent 提示。`provided_tests` 是显式兼容模式，不能把该模式的分数与未见测试的实验混在一起。反复用单任务分数选择提示/参数同样会产生选择偏差；泛化效果需要预先固定的留出任务和有限评测轮数。

## 共享限制

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `ARC_EXECUTION_MODE` | `plan_execute` | 可选 `react` 或 `plan_execute` |
| `ARC_REASONING_EFFORT` | 不发送 | 显式提供方推理设置：`none` / `low` / `medium` / `high` / `max`；部署方须核对模型支持 |
| `ARC_MAX_OUTPUT_TOKENS` | 16384 | 单次响应上限，同时计入上下文预留与请求前 token 预算预留 |
| `ARC_REQUEST_LIMIT` | 400 | 同一运行所有阶段的请求上限 |
| `ARC_TOOL_LIMIT` | 2000 | 所有阶段工具调用预留上限 |
| `ARC_EXECUTION_SECONDS` | 3600 | 执行器与运行时修复共享的墙钟截止时间 |
| `ARC_TOOL_BATCH_LIMIT` | 16 | 每个模型响应的工具数上限 |
| `ARC_REPEAT_LIMIT` | 3 | 同动作、同参数、同源码状态的重复上限；同时限制源码状态振荡 |
| `ARC_IDLE_TURNS` | 6 | 连续无源码变化或新读取证据的轮数上限 |
| `ARC_PLAN_STEPS` | 24 | 单份计划步骤上限 |
| `ARC_PLAN_REQUESTS` | 4 | 每次只读规划请求份额 |
| `ARC_STEP_REQUESTS` | 12 | 每个实现步骤的请求份额 |
| `ARC_REPLAN_LIMIT` | 1 | 整次运行可用的重规划次数 |
| `ARC_PROTOCOL_REPAIRS` | 2 | 计划结构、JSON 格式、工具批次纠正共享的整次运行上限；0 关闭纠正 |
| `ARC_WORKSPACE_CONTEXT_BYTES` | 200000 | 当前源码观察的序列化字节上限，进一步受剩余上下文限制；0 关闭，目录最多 300 项 |

既有 `ARC_IMPLEMENT_REQUESTS`、`ARC_REPAIR_REQUESTS`、`ARC_REVIEW_REQUESTS` 与 token 账本仍进一步限制阶段资源。每次请求和工具动作前后检查共享截止时间；实际 HTTP 请求期限也收紧至剩余执行时间。超时、截断、未知 usage 和连接中断不自动重发。

推理强度与输出额度独立配置，不按任务名称或评分结果自动调整。总量仍由 `ARC_TOKEN_LIMIT` 约束。提供方返回的 `reasoning_content` 在工具续接与一次有界 JSON 格式纠正时原样保留；格式纠正不能丢失协议字段。这里保留的是提供方续接协议，不将推理文本当作通过证据。

整批工具在副作用前检查工具是否暴露、参数是否为有界 JSON 对象、调用 ID 是否重复、validate_source 是否为末项，以及批量/全局额度。动作指纹包含规范化参数与当前源码摘要；有意义的源码变化允许重新读取，重复失败或无效写入不能通过切换阶段清零。

无效工具批次整体不执行，记录有界的拒绝原因并在剩余请求与共享协议额度内允许重新生成。被拒绝的调用只作为文本引用返回模型，不能伪造已执行的工具结果；提供方 reasoning_content 继续保留。计划先按严格字段、类型、覆盖与 DAG 规则校验，最多一次格式纠正后重新校验，不能忽略额外字段或放宽引用规则。纠正同样扣减请求、token 和时间，回滚/重规划不恢复协议额度。执行失败的具体 detail 进入重规划反馈。截断、超时、未知 usage 等传输故障仍不重发。

## 事务与并发

`SourceTransaction` 使用既有有界文件快照，最多 5000 个文件、单文件 8 MiB、总量 32 MiB。可编辑源码与 `.arc` 预算元数据、Git、依赖缓存和凭据分离。步骤失败、请求中断或工具部分写入都会尝试恢复源码；预算与执行计数不恢复。

恢复后核对整个源码摘要，并使工具读取修订号/静态校验状态失效。步骤只有在恢复成功后才标记 `rolled_back`；恢复失败记为 `rollback_failed`，并发写入阻止恢复记为 `rollback_blocked`，均保留具体原因并阻断后续正常交付。提交返回已确认源码的摘要，完成记录不再依赖提交后的另一次文件读取。底层多个文件替换不是文件系统级原子事务，进程被强制终止或磁盘损坏时不能承诺自动恢复。

入口以输出目录旁的 `.<目录名>.arc-generation.lock` 排除第二个控制器。正常退出和异常退出都释放本次持有的锁；进程强杀留下的锁不会自动过期，须确认原控制器已停止后处理。不同任务 ID 不能绕过同一输出目录的锁。非协作外部编辑在控制器检查边界通过源码摘要检测到时，停止并保留外部字节，不覆盖它们。此设计仍要求运行期间不要由编辑器或其他程序并发改写该输出目录；它不提供针对任意外部写入者的操作系统级隔离。

控制器账本与当前阶段日志用于诊断，不支持从中自动恢复未完成的模型请求。预算账本保留独占创建；相同账本路径的重复启动仍被拒绝。

## 泛化改动

已移除“完整源码就禁读”“固定每批写 4–8 个文件”、缺少登录调用就推断匿名访问，以及固定选择前端工具链/确认框交互的提示。通用空工程脚手架仍是可用起点，已有工程遵循输入布局。运行时 HOST/PORT、部署契约、原始需求与测试保护属于接口与安全约束，继续保留。

`llm.py` 现在只负责模型传输、用量和指标，旧的需求专用计划、实现、视觉复查与早期收敛链路已从平台包移除。提供方的传输兼容处理与已知响应模型别名仍保留；默认模型名由 Runner 显式提供，可以通过配置收紧允许列表。

## 验证

`test_platform_execution.py` 用脚本化响应覆盖计划依赖、异常回滚、批次预检、重复动作、无进展、状态振荡、预算边界、超时、未知 usage、重规划和并发/恢复失败。测试断言调用次数和最终文件内容，而不是仅检查返回码。

`test_platform_execution_entry.py` 使用真实入口、模型适配器与文件工具，在两种模式、陌生任务名、空工程和已有工程上运行，仅替换外部模型服务。既有合成 Node/Chromium 测试继续覆盖实际运行时与清理路径。上述开发回归与入口按仓库清理策略保留于本地归档，不随源码发布。仓库 CI 执行源码编译、Docker 镜像构建及包完整性检查。

这些测试能验证编排与边界，不能证明对任意需求的语义实现质量；正式运行结果需独立记录。

启用当前工作区快照时，跨步骤复用的任务 packet 仅嵌入原始任务输入、契约与 IR 摘要，不嵌入易陈旧的应用源码及派生 IR 全文。应用文件仍保留在带 hash 校验的 repository；每阶段通过 current_workspace 提供当前内容，遗漏或更改的文件可显式读取。关闭工作区快照时保留原始 packet 源码后备路径。不会截断或删除模型已有对话，也不会缩减原始公开需求。

## 依赖规划与分层验证

规划上下文提供公开IR的显式需求依赖、父级分组和覆盖声明指向的共享源码文件。共享文件最多展示64项并报告遗漏数量，属于未验证线索，不能据此声称功能正确。当前待办内的显式前置依赖必须同一步实现，或通过计划depends_on连接到祖先步骤；已完成/范围外需求不被伪造为新节点。重规划重新读取当前线索，不引用回滚前的状态。

完整单次预留不足时，token账本持久停止后续模型请求；调用前可用额度检查避免复查阶段继续空转。时间与源码安全允许时，模型额度耗尽仍可在隔离副本进行构建/HTTP诊断，但不能额外调用模型复查或修复。执行截止、恢复失败或并发源码变更继续阻断。

每次交付生成 .arc/platform-budget/<task>/verification.json，绑定交付源码摘要，并分别列出静态结构、源码覆盖声明、运行时诊断与正式行为评测。requirements_only的基础运行时范围为build_and_http；若启用下述冻结行为诊断，则单独标记build_http_and_authored_diagnostics。正式行为始终UNRUN，病例数未知；源码覆盖、HTTP和自生成诊断通过均不能提升为正式行为通过。

## 公开需求驱动的行为诊断

### 成功交付的必要条件

生成入口只有在完整自测通过后才返回零并发送 `mark_run_completed`。静态结构有效、源码覆盖声明完整、HTTP 可用或模型说“完成”，均不能单独满足这一条件。最终冻结套件必须非空，绑定全部原子需求，实际浏览器测试全部通过，无失败、跳过或未满足的前置条件；运行证据的源码摘要、套件摘要必须匹配最终交付。缺测试、部分需求未覆盖、测试未运行、环境不可用、预算不足或检查被关闭，都返回非零并记录未完成。

最终测试及 `plan.json`、`report.json` 保存在应用的可见 `self-tests/` 目录。该目录由控制器独占创建，已有同名目录不会被覆盖；模型工具不能读取、编辑或搜索它，源码快照、事务和摘要也排除它。失败的冻结套件同样保留，报告区分预期病例数与实际执行结果。报告与测试导出失败也不能报成功。只有作者尚未产生有效套件时，无法留下可执行测试，此时保留明确的未完成报告，不伪造测试文件。

后续作者批次失败时，已冻结的合法用例仍通过同一个运行器执行，记录实际结果和缺失需求。部分套件只做本地诊断，不触发模型修复或源码复查；即使这些用例全部通过，完整需求验收仍失败。该路径继续受原有时间、工具和源码安全限制，不增加运行额度。

实现阶段在原共享预算内保护一次作者请求，并为最终作者和本地检查预留时间。初始 token 总额能容纳两次完整请求上界时，额外保护一次作者的 token 缓冲；否则记为 `token_reserve_unfunded`，避免新增缓冲使实现完全无法开始，每次实际请求的完整上界预留仍不变。最终按有界批次补齐缺失需求，已有冻结用例不重写；释放的是未来阶段准入预留，不是已消费或用量未知的额度。工具或其他硬额度不足仍停止并报告未完成。预留提高自测执行机会，不保证任意复杂任务都能在任意预算内完成。

自测成功仅表示这些公开需求驱动的诊断通过。每个需求至少一个用例不代表所有分支都已验证，也不等同于正式平台验收；正式状态仍由独立评测器确定。

诊断作者和引用校验使用同一份需求引用来源：需求标题/描述，以及绑定到该需求、标记为从公开输入解析的场景Given/When/Then原文。引用仍须为同一需求来源中的精确子串；不接受其他需求、无归属或模型推测的场景。场景名称可能是自动生成的标识，因此不作为新增原文来源。这些引用用于约束自生成诊断的依据，不能证明断言本身正确。

`ARC_BEHAVIOR_CHECK=1`（默认）在 requirements_only 且启用运行检查时生成公开需求诊断。默认 plan_execute 模式在步骤既有额度内为缺失诊断留出作者请求，最后汇总已有冻结诊断并补齐缺失需求；react 模式在实现后生成诊断。只读模型阶段返回有界声明式操作计划，控制器确定性编译为可执行测试；每个操作流程引用一个公开需求ID及其原文。当前源码仅用于定位控件，不得成为预期行为的唯一依据。关闭行为或运行检查只能得到未完成结果，不能成功交付。

控制器校验数量、引用、原文、操作和定位器白名单；状态转换流程必须包含交互及后续正向断言，纯初始展示需求可用入口导航加一个或多个正向界面断言；导航、重载或未验证的交互不能冒充展示断言。每次作者响应最多12个流程、每个24步、总计192步。确定性编译器生成Playwright代码，同源导航与请求、零重试、每例60秒。冻结文件在不可编辑的`.arc`区域；每轮前后检查计划及脚本摘要。修复不能修改检查，也不能退还请求/token/工具预算。

消失断言要求同一完整定位器（包括 `within`）先通过 `visible`，再发生交互，最后检查 `hidden`；`text`、`value`、`count` 不替代显式可见性证据。作者提示与校验错误使用相同规则，错误附带 case 和 step 下标，便于在既有纠正额度内修正。

默认计划执行在静态检查后、提交步骤检查点前执行冻结诊断。当前步骤每个需求ID至少需要一个有公开原文依据的浏览器观察；零测试、跳过、缺浏览器、HTTP-only、环境失败或不完整结果均不能完成步骤。该条件是采样开发证据，不代表全部需求分支或正式验收。缺少可支持的诊断时报告未验证，不编造业务断言。

后续步骤保守重跑全部已提交需求的冻结诊断，不通过静态依赖猜测缩小回归范围。失败回滚当前步骤，保留先前提交；按需求ID保存诊断，改步骤名或重规划不重写失败用例。当前及回归用例的原文、摘要、源码摘要、数量和失败反馈记录在 `step-behavior/`。最后运行阶段复用已提交的同一套诊断。每步最多一个检查尝试，重规划仍受既有限制；累计检查与最终最多4轮运行共享绝对时间截止和工具额度，作者请求计入步骤及整个生成阶段额度。

模型调用失败、未知消耗及模型请求额度停止后，仍可在剩余时间/工具额度内对冻结源码执行只读本地验证；不恢复模型、作者、修复或源码复查调用，不清除停止状态，不释放未知消耗预留。工具/时间额度、回滚失败和并发源码变化继续阻断；源码安全错误优先于先前模型停止原因。只读检查也正常计工具账，不能把无余量变成一次免费重试。交付的诊断状态与模型停止原因分别记录。

`behavior-author.json`区分未运行、生成失败、无效和已冻结；`runtime-check.json`将结果标为authored低置信度诊断。定位或预期可能错误，必须重新对照公开需求判断，不能按自生成断言修改业务或伪造覆盖。无浏览器、无有效流程、篡改和未执行都不能记为通过。官方评分测试继续被隔离，正式评测仍在最终源码冻结后独立进行。

## 执行中的资源视图

每次模型请求可见当前计划的剩余步骤、需求引用、已完成步骤和依赖；重规划及退出清除旧视图。预算账本用已结算请求的峰值消耗估计还能负担的请求数，并显示完整下一次请求预留后的余额。估计只是调度提示，后续源码增长可能使其下降；它不改变完整预留、单步上限、共享总额度或完成判定。未知用量、熔断或额度不足时可准入请求数为零。

计划阶段另行显示包含当前请求在内的剩余请求配额；每步可分配请求估计取阶段余额与全局可负担估计的较小值，再为收尾留出空间。重规划沿用阶段开始时的请求上限，已经消耗的请求不会返还；缺少 token 消耗估计时仍显示阶段配额，不能把全局余额误当作当前计划可用额度。

最终步骤还须检查原计划范围内全部需求的源码覆盖依据，防止后续编辑使先前声明失效；重规划保留原范围。原范围恰好涵盖全部公开IR需求时，最终检查也包含未绑定到单个需求的场景和UI条目；局部计划不强求无关任务的覆盖。缺少依据时返回具体缺口，在原额度内补全或回滚，不能自动标记覆盖或把源码声明当作行为验收。

决策视图同时显示剩余工具额度，以及单次响应可包含的工具调用上限，取控制器、会话余额与批次上限的较小值；最后一次请求同样遵守这些边界，其他预算仍可进一步拒绝准入。阶段失败时，重规划保留已经计算出的具体源码校验诊断，避免将缺口信息压缩成单个失败布尔值；诊断只作为失败依据，不能覆盖失败状态或触发额外校验、重试。

模型请求指标额外记录经过类型和范围校验的HTTP状态，便于区分服务不可用、鉴权和协议故障；不记录响应体、异常消息或请求头。HTTP错误不会自动重发，未知usage继续保留预留。

工具参数拒绝反馈提供工具名、schema字段路径和数组数量边界，不回显参数值或未知字段名。写文件工具描述从真实常量生成每次文件数和总字符上限；较大修改须由模型分成合法调用。整批预检、共享协议纠正额度和请求上限保持不变，不自动执行非法批次的一部分。计划中的基础、集成和验证步骤同样必须关联待办需求；验证步骤不能用空引用绕过覆盖约束。

`ARCBENCH_MODEL_TRANSPORT` 可显式选择 `json` 或 `assembled_stream`。默认 `auto` 保留原选择：直连 DeepSeek 使用流式组装，其他地址使用完整 JSON。仅在目标接口确认支持 SSE 和最终 usage 后启用流式组装；端点仍由 Runner 注入，不绕过代理。流式传输不会把部分输出交给控制器：必须收到完整工具参数、结束标记与用量后才返回响应，缺失或中断时保留未知用量预留并停止，不自动重试或回退到另一传输方式。

源码复查按已完成计划的需求组安排，再参考公开IR的依赖、父子关系和当前共享源码声明归并，避免固定顺序逐项分配额度导致后续需求没有复查机会。组数由原总请求额度和单组上限确定；每个有效需求恰好分配一次，声明只作为分组线索。每组仍获得完整任务上下文及本组全部IR片段，共用原请求、token与时间上限。

复查使用明确的系统级范围：完整任务包作为需求参考，只审查当前分组并修复有依据的缺口，不重新规划或重写整个应用。工具循环、源码校验、回滚与预算保持同一实现。

`review-schedule.json` 记录分组、额度、实际阶段状态和未执行项目；中断后的剩余组记为跳过，禁用复查时保留未分配需求。完成状态仅表示源码复查阶段完成，不证明真实行为正确。分组不会增加协议纠正、重试、回滚或模型请求额度。

构建、静态或运行诊断触发的修复使用独立的 `repair_sources` 入口，复用已有有界 ReAct 源码复查执行器；完整任务包仅作需求参考，修复沿受影响依赖处理已观察到的缺陷，不重新创建覆盖全部需求的计划。初始实现仍按配置规划。修复请求配额、共享计数、工具预检和源码事务不变；未完成或中断仍回滚，静态有效仍须接受后续运行验证，不能直接记作行为修复成功。

行为诊断的 `hidden` 操作检查目标隐藏或移除，但首次消失断言要求同一定位器先经过 `visible`，之后发生交互。通过 `hidden` 建立的消失证据可跨刷新复核；刷新仍清空当前页面的可见证据，因此 `visible → 交互 → reload → hidden` 不能绕过刷新前的消失确认。新交互清除旧消失证据，再次 `visible` 使该目标的消失证据失效。每个用例独立记录，最后一次交互或刷新后仍必须有正向结果断言，`hidden` 本身不算正向断言。`count=0` 继续拒绝。浏览器编译器使用固定 `toBeHidden`，没有自定义脚本、额外重试或断言降级；需求引用、总步数、冻结和修复边界不变。

流结束但完整性条件未满足时，`incomplete_stream` 额外记录收到的块数、选择数、缺少结束标记的选择数以及是否出现 usage。只允许有界整数与布尔值，不记录响应正文、工具参数或凭据；模型会话包装保留这些字段。它们用于区分可观察的缺失条件，不能据此确认服务端根因或已消费 token 数。响应准入、未知用量预留、回滚和不自动重发均保持不变。

平台受控的长响应不再因累计达到 300 秒而中断。默认单次响应服从当前阶段与任务原有截止时间，底层另有 86400 秒有限上界；`ARCBENCH_MODEL_DEADLINE_SECONDS` 可设置更短的请求总时限，但不能延长共享预算。`ARCBENCH_MODEL_IDLE_SECONDS` 单独限制连续无网络读取的时间，默认 300 秒（原直连 Pro 的 900 秒推理等待保留）；连接、写入和连接池分别最多等待 10、30、10 秒。HTTP 超时在每次请求开始时按当前剩余时间重建，外层总截止仍会终止持续 keepalive 的连接，SDK 重试保持关闭。没有平台控制器约束的调用默认仍以空闲窗口作为总时限；JSON 模式收到完整响应前可能没有网络进度，仍受读取空闲期限限制。

最终测试作者预留运行窗口的 10%，不再封顶 60 秒；本地运行测试仍使用原 25% 策略，所有窗口都在原共享期限内。增加长响应等待不代表允许未知用量后自动重试：实际连接中断或到达截止时，仍保留完整请求预留、撤回未验收步骤并输出失败报告。只有同源完整自测及导出校验通过才能成功。

启用运行验证时，先冻结可用的公开需求行为诊断，再执行构建/HTTP及诊断修复循环；可选源码复查延后到运行基线通过之后。仅在原轮次、时间和模型额度仍允许后续检查时启动一次复查计划。源码未变则复用同一摘要的运行证据，源码改变必须在同一个运行循环内重测，共享原截止时间、轮次、预算及依赖缓存，不能追加一套验证额度。基线失败先修复已观察到的问题；没有剩余验证轮次则跳过可选复查。显式关闭运行验证或缺少 Node 时仍可作源码复查，但不得宣称运行通过。

运行报告分别记录实际检查的源码摘要和当前源码摘要。复查或修复退化后恢复较优快照，须在清空旧构建产物和持久数据的新检查轮次中重新运行；恢复后无剩余轮次、时间或执行许可时，结果为 `unverified`，不能引用恢复前的通过记录作为当前验收。回滚不返还任何请求、工具、token 或轮次消耗。

同一源码的新检查结果替代其历史状态和修复反馈，包括由通过变为失败；不能按历史最好分覆盖新的失败证据。复查期间模型控制器的绝对截止时间收紧到本运行循环的截止时间，退出后不清除停止状态或返还已消耗量。运行检查前后、复查返回后及恢复写入前均核对控制器预期源码，检测到外部编辑直接停止并保留外部字节，不把并发异常转换为可恢复的检查环境错误。

公开需求中的角色、数量、作用域与前置条件是实现依据，IR 的 multiplicity/persistence/group/auth 提取结果可能不完整。`multiple=false` 不等于要求整页只有一个同名控件；严格单目标交互仍须在需求指定的实际作用域内唯一。持续可用的分组不自动变成 navigation，显式 list 与 region 保留各自语义；缺少 auth 解析结果属于未知，不能推断匿名访问，也不能给公开操作擅自增加登录门。原始需求、解析输出和覆盖校验均保留，只修正实现、复查与静态修复提示对这些线索的解释。合成浏览器反例验证了作用域与角色差异，不代表模型已经正确实现任意业务需求。

每次分组复查继续收到完整公开任务包及本组全部 IR 片段；通用交互说明在任务包中保留一份，分组反馈不再重复附加同一段。去重不删除需求、场景或当前源码，也不改变请求、token、协议纠正和工具预算。


整任务范围由入口按完整原子需求集合明确标记为 `whole_task`，经计划进度传递到最后一步，重规划保留原始范围。最终整合要求父级工作项及未归属场景/UI同样具备源码覆盖依据；局部批次不扩大范围。缺口在当前步骤既有请求额度内反馈与修正，额度耗尽后由控制器检查现有候选；未通过仍停止并回滚，不增加重试或自动补覆盖声明。


计划步骤的目标、依赖和验收条件通过 `current_plan_step` 控制器消息提供一次，不放入每步变化的系统消息前缀。稳定规则与固定需求在前，当前源码观察和步骤数据在后，以保留可复用前缀并消除重复步骤负载。当前源码仍逐阶段重新观察；预算、范围、验证与回滚边界不变。前缀字节变化不等于已经测得提供方缓存命中、token节省或分数提升。
## Requirement text shapes

The deterministic task IR accepts requirement descriptions as either a string
or a mapping with a string `text` field. Both forms preserve the same public
text for review slices, UI annotation parsing and behavior-check citations.
Image metadata and unsupported values are not coerced into prose; the existing
top-level `text` fallback remains available when the description is not textual.

## 模型非成功终态

JSON 与流式响应遵循同一执行边界：只有 `stop` 或 `tool_calls` 允许进入动作执行；
`length` 仍作为截断停止，其他终态作为 `model_terminated` 停止并阻止后续阶段请求。
中止响应的部分工具内容不执行。响应模型和用量均通过校验后才结算；缺少终帧、
用量未知、用量越界或模型不一致仍保留预留。只有下述已分类的流格式故障允许一次改变传输方式；外部连接中断、超时、模型不一致和越界用量仍停止。

## 上下文入口与阶段收尾

每次请求仅保留控制器自己插入的最新资源状态；原始需求、模型推理和工具历史保留。控制状态完整写入审计日志，已发送的请求快照不受后续替换影响。

同阶段、源码摘要未变的成功读取可返回更短的内容引用；遗漏、变更及失败读取仍返回实际结果，引用不能作为原文凭证。日志记录避免重复的字节数，不宣称实际 token 或费用降幅。

实现阶段连续**未增加当前上下文可见信息**的只读工具轮次由 `ARC_INSPECTION_TURNS` 限制，默认3。新增信息按实际返回的来源、版本和内容范围/目录项计算；已有内容、重叠范围、空结果、工具错误与缓存引用不补充额度。实际源码变化或新信息会清零这个局部无信息计数。到限前提示编辑或验证；到限后不再发起下一次模型请求，返回 `inspection_limit` 并回滚当前未验证事务。探索始终受原有阶段和全局请求、token、工具、上下文及时间限制。

读收据与可见范围属于单次模型上下文。换阶段/回滚后新上下文需要重读时返回真实内容，不能引用已丢失的历史消息。重复动作保护对只读工具计入无新信息的动作，其历史不随阶段清空；新内容读取仍完整消耗共享请求和工具额度。缓存复用日志明确标记未执行底层工具，计账不减免。写入、校验与源码振荡保护保持原语义。自生成文件的完整写入内容也计入当前可见范围。

adaptive 控制策略下，纯粹获取新信息不累加实现质量停滞窗口，但不清除既有质量债务、警告或恢复状态，也不构成任何质量通过。代码修改仍须及时进入实际验证；只有原有真实行为验证证据可以消除质量警告。计划阶段读取不计作实现质量停滞。最终成功仍要求同源、完整、实际执行通过的自产测试以及成功导出。

源码审查凭据只在文件工具实际执行后记录；缓存引用不经过这个入口，普通文件里的 JSON 文本不能被误当缓存信号。文件工具路径始终相对项目根目录，规范化后再加一次 `application/` 审查命名空间；真实同名子目录保留，不覆盖根文件凭据。完整 UTF-8 读取保留原始换行，使冻结原始字节哈希与引用字符位置一致。截断或行范围读取不能伪装完整文件，引用仍须匹配冻结哈希、精确摘录和当前审查分区；提交审查记录时仍校验源码未变。源码审查结论不是行为测试通过或最终生成成功。

`repeated_action`、`no_progress`、`source_oscillation`、`replan_limit`、`tool_batch_limit` 同样仅终止模型执行；源码安全、时间和工具额度允许时仍可进行本地诊断。停止原因不清除，也不会自动恢复模型请求。未验证修改仍回滚；只有经过检查的步骤或运行基线可作为保留依据，不能为了省 token 留下未经验证的改动。

初始展示诊断允许附带 URL 断言和刷新，但最终刷新后必须有正向 DOM 断言；只有 URL 或最后一次交互/刷新后没有断言仍拒绝。该错误反馈标出用例及步骤位置。合法 JSON 的契约错误要求纠正方案内容，语法错误要求修复 JSON；两者仍共用原协议纠正次数与阶段额度。

测试方案纠正在当前未提交候选上进行。方案无效、诊断不可用或验证环境异常不再触发应用重规划：将有界源码快照隔离保存于诊断目录 `unverified-source-*`，在失败记录中写入 `candidate_source`，恢复此前已验证源码并以 `verification_blocked` 停止模型请求。阶段预算让位单独保留原始原因，走下述候选自测交接。隔离候选不计完成；已执行的行为失败仍保留冻结诊断并按原流程处理，不能通过改断言获得通过。


计划执行与诊断作者使用按当前需求及依赖裁剪的输入包，保留父级/全局契约和可检索的原文；当前源码仍逐阶段刷新。多需求步骤因实现额度耗尽而失败后，重规划必须先安排依赖有序的较小步骤，不能仅改名后重试同一整块范围；最终整合仍检查原始完整范围。

单步请求上限不变：需要新诊断时只预留一次作者请求，实现提前结束的剩余额度可供作者使用（仍受原作者上限约束）；冻结诊断复用时不再预留。实现正常用尽局部请求额度时，控制器计入工具及时间预算执行静态与覆盖校验，不额外调用模型。通过后仍须通过原行为门禁才能提交；截断、用量未知、安全或全局预算停止不能走此收尾路径。

## 任务阶段、执行许可与软预算

现有 `RunControl` 同时记录任务阶段与执行许可；它仍是唯一执行控制器，原 token 账本、步骤事务和运行器继续使用。任务阶段为 `PLAN/CODE/BUILD/TEST/COMPLETE/STOPPED`，执行许可为 `RUNNING/WARNING/CIRCUIT_OPEN/RECOVERING/PROBATION/TERMINATED`。阶段标签描述正在做的工作，不代替证据：静态语法检查不等同于构建通过，需求覆盖声明不等同于行为测试通过。只有完整、同源的最终自测与导出检查通过才允许完成。

`ARC_CONTROL_POLICY=adaptive`（默认）启用警告与有界恢复；`legacy` 保留旧的停滞终止策略。读取新内容或修改源码只算活动，只有实际验证才能提供质量证据并解除对应警告。正常 BUILD/TEST 等待不当作 CODE 停滞。恢复复用当前步骤检查点的源码回滚，也允许下述一次传输方式切换；不是自动重启任意服务，也不会另起任务循环。`ARC_RECOVERY_ATTEMPTS` 默认 1，可设为 0，范围 0–10；两类恢复共享此额度，还受原有全局重规划、请求、工具与截止时间限制。硬额度耗尽、未知用量、时间截止、源码并发变化和回滚失败不因恢复重置。

### 流格式故障的有界调整

质量警告下的失败步骤回滚可消耗一次既有恢复额度进入 `PROBATION`，不清零历史质量债务或全局协议修复计数。重规划仍受原 PLAN 上限及全部全局预算约束，但不消耗实现/验证的四次恢复请求窗口；CODE、BUILD 和 TEST 请求共享该窗口，作者预留也在其中。写入和静态检查不能解除质量警告，只有当前源码的真实行为检查通过才能恢复；失败或窗口耗尽仍停止。覆盖更新提示使用工具 schema 的实际条数上限，要求当前步骤的增量声明分开调用，不能拼接 JSON 或放宽参数校验。

平台适配器仅在 assembled stream 出现 `JSONDecodeError` 或缺少完整终帧/用量时，尝试一次改用完整 JSON 响应。同一模型、注入端点、原始阶段/计划/全局请求上限和截止时间保持不变。下一请求说明前次响应已丢弃，继续依据已确认的工具结果工作；部分工具调用不执行。第二次失败不再切换或循环重发。已有源码恢复试运行不能借此重置窗口，质量告警也不因传输恢复解除。

失败请求的完整 input+output 预留原子转存到 `unknown_reservations`，永久计入后续准入，不退款，不冒充真实 token 用量。只有完整下一请求与最终测试作者保留额均可负担时才能转存并继续；否则原 pending/停止状态保留。写盘失败、身份不符、模型变化、用量越界、时间耗尽和源码并发变化均不允许恢复。原 SQLite campaign 账本及进程重启冻结语义不变。

这是替换原先“所有未知用量均永久停止”的窄范围策略，不是忽略异常。恢复成功只证明新的响应与用量通过协议检查；最终源码仍须经过完整自生成测试、真实执行、摘要绑定与报告导出才能成功。当前 300 万 token 配置未增加：一次完整未知预留可能使实现恢复与最终作者无法同时负担，届时必须准确失败。合成高额度测试只验证机制，不构成新的付费预算授权。

`ARC_BUDGET_POLICY=adaptive`（默认）在计划执行中，只从依赖已完成且控制器允许的步骤选择下一步。初始收益使用公开需求数量的低置信度估计，考虑可解锁的依赖工作；实际行为验证反馈与请求消耗更新后续选择。未知质量保留有界探测机会，必需工作不会只因估计收益为零就被当作完成或删除。决策记录候选、收益依据、预计成本、选择原因和软请求份额；每次实际请求仍按原完整输入/输出上界准入。策略不修改已消费计数和未知用量预留。`legacy` 使用原步骤顺序与份额分配。

这是一项单任务内、计划步骤级的确定性启发式策略。它尚不包含跨任务调度、自动模型切换或基于正式评分的收益学习；本地模拟结果不能证明真实模型成本降低或正式得分提升。

## 阶段预算让位后的候选自测

`self_test_reserve` 和 `self_test_time_reserve` 表示实现阶段把资源交给最终验证，不再统一升级为 `verification_blocked`。步骤恢复原检查点，同时记录未验证候选与原检查点的源码摘要。交接前检查候选位置、摘要以及交付源码是否变化，保持此前已验收版本不被未验证代码覆盖。

在原有额度与截止时间允许时，最终作者与既有运行器针对隔离候选执行自测；该路径不进行可选模型修复或复查。完整覆盖、实际测试、源码/套件摘要与导出契约全部通过后，才受控写入交付目录。检查或导出失败时保留原交付源码、合法测试、候选摘要和失败报告，明确区分被测源码与交付源码。回滚成功并不等于任务成功；预算让位也不保证剩余额度足以完成自测。


## Independent API recovery and explicit live pause (v23 candidate)

API recovery and source rollback now own different counters. `ARC_RECOVERY_ATTEMPTS`
continues to limit source/quality recovery (default 1). API admission uses
`ARC_API_RETRIES_PER_CALL` (default 2, range 0..4), `ARC_API_RETRIES_TOTAL`
(default 4, range 0..20), and `ARC_API_WAIT_SECONDS` (default 60, range 0..300).
These are additional ceilings, not token allocations. Every physical request
still increments the existing durable requests/token ledger and RunControl request
count. Step, phase, planning, author reserve, and PROBATION windows still count
physical requests. `logical_requests` separately records controller invocations;
this version does not convert task capacity to logical-request accounting.
Protocol correction and business replanning remain ordinary charged invocations.

Only explicit HTTP 503 and the existing classified assembled-stream protocol
fallback are admitted. HTTP 503 preserves the current transport. Authentication,
configuration, other HTTP errors, deadlines, ambiguous transport failures,
changed sources and sticky controller termination do not gain a retry. API retry
may proceed in a live PROBATION only while its original physical request window
still permits it; it neither clears quality debt nor consumes/resets source
recovery. SDK automatic retries remain disabled.

Each HTTP retry waits with equal jitter between half and all of an exponentially
growing base, capped at 30 seconds. A valid Retry-After delay-seconds or aware
HTTP date is a minimum. Invalid, negative, nonfinite or more than 30 seconds
stops rather than shortening the server's requested wait. All admission also
checks cumulative wait and the original phase/task deadlines. The planned wait
and retry are durably precharged together with retained unknown token liability;
actual elapsed waiting above the precharge is added before another request.
Interrupted waits and unknown usage are never refunded. No monetary allocation
or dynamic parallel scheduling is implemented here.

The optional CLI `--pause-before-request N` pauses the **same live owner** before
logical invocation N, after earlier synchronous calls/tools have returned and
before the next request is reserved. It retains the Python stack, transactions,
original ledger, controller and exclusive ProjectLease. The original monotonic
deadline keeps running. The controller stores an atomic checkpoint under its
`.arc/platform-budget/<task>/live-pause/` directory and logs its SHA-256.
The checkpoint retains source bytes, identities, current plan/step position,
request messages/tools, confirmed counters, unknown liability and deadline anchors.
Known credentials are rejected in structured message keys/values and source bytes;
no environment, endpoint credential or SDK headers are serialized.

From the packaged agent directory, an operator may run:

```text
python -m agent.execution_checkpoint inspect CHECKPOINT --sha256 TRUSTED_SHA256
python -m agent.execution_checkpoint continue CHECKPOINT --sha256 TRUSTED_SHA256
```

The separate command only submits a bound continuation request. The live owner
checks the original lease, checkpoint, source, public requirements, agent code,
configuration, messages, controller and durable ledger identity before continuing
exactly at its original boundary. Concurrent or repeated continuation is rejected;
no confirmed tool action is replayed. Existing final self-test acceptance still
runs after continuation. A resumed event alone is never completion evidence.

Cross-process recovery of this **live request checkpoint** is not implemented. The explicit
`resume-process` action refuses execution. A crashed process's ledger cannot be
reopened and its stale lease is not automatically removed or stolen. The saved
candidate remains available for inspection. The checkpoint is not a serialized
PlanExecutor/Python stack, and cannot be used to replay unknown external operations.
UTC deadline data supports inspection; monotonic timestamps are never reinterpreted
as a new process deadline. Platform automatic reattachment/relaunch capability is
unconfirmed: bundled paused/resumed APIs only emit state events.

### Optional error pause and clean final-verification transfer (v24)

`--pause-on-api-error` opts into supervised pauses after an eligible HTTP 503 or
incomplete stream has already passed recovery admission. Unknown usage stays
fully reserved; the admitted API retry is already charged. A separate checkpoint
is published under `api-error-pause/<cumulative-retry>/`. Use the same `continue`
command and trusted digest above. Actual pause time consumes the cumulative API
wait limit as well as the original phase/global deadline. Exhausted waits, retries,
physical request limits or token reservations cannot be reopened by continuing.
No in-flight request or tool is replayed. Without this flag no error pause waits
for an operator or control channel.

The separate `--handoff-final-verification` boundary supports a **clean intentional
same-host, same-OS-boot transfer**, after successful generation and complete test
authoring. It refuses incomplete generation, failed/quarantined candidates, pending
requests and stopped controllers. Source, suites, requirements, configuration,
effective event/traceability paths, agent code, ledger, self-test directory identity,
plan result and original deadlines are bound in `final-verification-handoff/checkpoint.json`.
There is no pending model request at this boundary. An exception unwinds the old
owner; only after successful lease release does the entrypoint publish `ready.json`
and return **75**. This is paused/incomplete, never success or formal acceptance.

```text
python main.py REQUIREMENTS --output-dir APPLICATION --handoff-final-verification
python main.py REQUIREMENTS --output-dir APPLICATION --resume-final-verification CHECKPOINT --checkpoint-sha256 TRUSTED_SHA256
```

The second process takes an ordinary exclusive project lease, verifies every
binding before runtime initialization, and exclusively creates a permanent
`claimed.json`. It constructs no model client and never calls `generate`; it only
builds/runs the frozen browser suite on a scratch copy and uses the same final
acceptance/export gate. Test files and report remain in `self-tests`. Controller
transitions are appended to the existing context log; `finished.json` records the
new outcome and cumulative accounting. Tool checks are additionally charged;
requests, retry counts, waits, token use and unknown liabilities are never refunded.

The OS boot marker binds system-wide monotonic time. The resumed deadline is no
later than the original monotonic or UTC deadline; downtime counts, and expiry,
clock reversal or boot changes reject resume. This does not restore a PlanExecutor
stack or support arbitrary crash recovery: a crash before clean release cannot
publish readiness; a crash after claim cannot repeat verification. Stale leases
are never stolen. A failed browser result stays failed and cannot promote a failed
generation candidate. The default platform entry enables neither flag. Platform
storage reattachment and restart scheduling remain **UNCONFIRMED**.
The handoff binds all released package members, including JavaScript validators
and dependency declarations; only Python caches and SDK installation build/egg-info
outputs are excluded. It does not freeze machine-installed Python, Node, npm or
browser versions. Actual verification runs again in the receiving process, so an
environment failure remains a failed/incomplete result rather than cached success.


## Explicit step behavior outcomes

PlanExecutor records `verification_status` as PASS, FAIL, DEFERRED or BLOCKED in behavior results, step records and `step_verification_outcome` events. FAIL means an observed behavior failure; only existing self_test_reserve / self_test_time_reserve handoffs are DEFERRED. Missing, invalid or unavailable verification is BLOCKED. A deferred candidate remains unverified and quarantined; the source transaction may still roll back to the last accepted source. This field does not refund usage, extend deadlines, grant retries, bypass acceptance, or implement crash recovery. Legacy status/reason fields remain unchanged.


### Independent service gate

The run ledger tracks AVAILABLE, RETRY_WAIT and terminal OPEN separately from source/quality control. Only existing HTTP 503 recovery can enter the service retry path. Every existing request, token, phase, self-test reserve and wait bound remains authoritative. OPEN blocks subsequent model entry before counters, reservations or pause hooks; no half-open probes or additional retry allowance exist. A complete accepted response exits RETRY_WAIT, but never refunds unknown usage or retry/wait costs. Terminal transitions retain a bounded diagnostic reason alongside the original stop reason. State persistence failure fails closed in memory; reopening a ledger after process restart remains unsupported. This is not a cross-run provider health monitor.


### Interrupted behavior evidence

An ExecutionStopped raised while invoking or checking a behavior validator is recorded in the plan outcome snapshot with `interrupted: true` and an explicit verification status. The original returned evidence, if any, remains under `observed_validation`. Existing phase-reserve classification applies, including contradictory-pass and observed-failure precedence; hard stops remain BLOCKED. This is report-only metadata: the live outcome used for recovery and budget observation, stop propagation, quarantine eligibility and rollback are unchanged. Exceptions before behavior validation and failures in later bookkeeping do not relabel behavior evidence. No extra event callback is invoked while handling a stop. Other unexpected exceptions still propagate unchanged; this is not general exception recovery.

### Bounded verification and repair (v29 local candidate)

With adaptive control and a step behavior gate, a quality warning during CODE
hands the current candidate to BUILD and TEST immediately (`verification_due`).
Source edits alone do not clear the warning or count as verified progress. This
handoff lets the controller obtain the missing quality evidence before spending
the warning window on more implementation turns. Repeated actions, oscillation,
hard limits and protocol failures retain their original stop behavior.
Read-only tool turns do not trigger this early handoff: after a rollback, the
controller may still carry quality debt while the model reads the restored
source. Reading still spends budget and retains that debt; existing repeated
action and request limits remain in force. A subsequent editing turn, explicit
completion or the original request-allowance closeout still reaches verification.

A concrete static failure or behavior FAIL may receive one defect-directed
repair inside the same source transaction, before rollback and replanning. Its
request allowance is bounded by the original step and phase allowance, global
remaining requests and `recovery_requests`; any needed test-authoring request
stays reserved. The bounded repair returns to verification without consuming
quality-warning turns solely for editing. It cannot reset counters, extend the
deadline, reopen a hard stop, or repair a BLOCKED/DEFERRED verification result.
PROBATION does not gain this extra correction path. This ordinary correction does
not consume a source-recovery attempt, but every physical request/tool still costs
budget. Both attempts are retained in plan history.

The controller repeats static checks and the same frozen step browser suite,
including prior accepted journeys. Only a passing check permits acceptance;
another failure follows the existing rollback/replan/stop path. Model-authored
diagnostics remain fallible development evidence. Repair must be justified by
public requirements and cannot rewrite the suite. Final whole-task self-test
acceptance remains mandatory and is not a formal score.

Large-context implementation batches debit their shared soft request pool by
physical requests actually charged to the durable ledger. Unspent allocations
remain available to later batches. Planning, authoring, retries and unknown-usage
attempts retain their physical charges. This reuses unused allocation; it does not
refund real usage or enlarge the global budget. `PLATFORM_BATCH_BUDGET` records
allocation, actual spending, released allocation and the remaining pool.
# Candidate delivery and full verification

`ARC_DELIVERY_POLICY=verified` preserves strict complete self-test acceptance.
The explicit `runnable_candidate` policy separates construction from acceptance:
static-valid source may be retained with `constructed_unverified` status when
public constraints cannot be compiled, or after a bounded repair still leaves
an observed behavioral failure. Those steps are not added to verified steps,
accepted requirements or verified gain. Hard stops and integrity/harness failures
do not become construction success.

Under this policy the controller records each step's verification capability.
An unavailable semantic oracle is not a failed quality measurement; activity
stagnation, repeated actions, oscillation and all original hard budgets remain
active. Previous quality warnings and consumed resources are not reset.

Final delivery builds and starts an isolated copy, opens it in Chromium using a
controller-frozen smoke test, reruns known authored suites, and rechecks source
and suite hashes. The terminal state is `READY_FOR_EVALUATION`, distinct from
`COMPLETE`. Exit 0 reports successful export of this evidenced candidate, not
complete requirement acceptance. `self-tests/report.json` retains `accepted`,
all original reasons and exported suites, plus the independent `delivery`
receipt with actual counts, failures, skips, unresolved requirements and source
identity. Official evaluation remains `UNRUN` until the independent evaluator runs.

Candidate mode checks request admission against the original phase/hard cutoff
before spending a physical request. Its allowance is the greater of 30 seconds
and 1.25 times the slowest of the last eight complete responses, capped by an
explicit provider request ceiling. This estimate is not a completion guarantee.
Recovery must also fit its backoff. A refused request yields the phase without
changing the hard deadline or refunding earlier work.

If a request still times out, bounded candidate closeout can use the remaining
diagnostic time after an eligible model-only stop. Closeout seals new model calls
and source writes, binds the committed source and observed stop epoch, charges
real checks to the shared tool budget, and requires a new exact-source runtime
receipt plus final export integrity. A later stop, hard deadline, exhausted tool
budget or integrity failure still rejects delivery. The original controller
remains `STOPPED`/`TERMINATED`; only the separate delivery receipt may become
`READY_FOR_EVALUATION`. Pending/unknown usage, failures and unverified requirements
remain in the exported record. No requirement gain or `COMPLETE` state is granted.

### Public interface identity

Parsed UI identity retains the public page, role, accessible name, container scope,
and repeated mention origins. Explicit page aliases must resolve uniquely against
public declarations before model enrichment. Otherwise coverage stays unresolved.
Plans bind the initial route with explicit navigation and URL assertions; initial
DOM evidence also requires a following URL assertion before the first interaction.
The compiler additionally scopes initial DOM locators, including the first action,
to the public URL with an isolated selector engine, so auto-wait cannot select a
matching control after navigating to another document. An event-time URL witness
also detects same-document URL changes while an action waits on an existing node;
document-level capture survives node replacement. Declared interactions may
navigate. Exact count predicates preserve the public
collection scope and cannot invent a narrower container to produce a passing count.

HTTP probe identity uses the method and complete path, including query. Invalid
status types, invalid probe modes and duplicate declarations reject the optional
contract before runtime. Required root/health smoke reads remain enabled; explicit
status expectations are retained. Probe-limit exclusions remain visible as skips.
These probes check HTTP availability/status only. Request and response JSON shapes
remain `not_checked`; source review is not executable type verification.

The offline regression runner captures failing temporary fixture artifacts before
cleanup, excluding linked paths and dependency/cache trees. Capture errors remain
diagnostic metadata and never change the original test verdict or process exit.


### Deferred candidate selection during closeout

In runnable-candidate mode, a checkpoint deferred only by the existing final-test request or time reservation can be considered for delivery. Selection seals model requests and model writes before probing and binds the isolated candidate and its accepted baseline to one closeout permit. It never resumes implementation, clears a stop, refunds consumed or unknown usage, or extends the original deadline.

The controller checks the exact isolated source with static validation, build/start and browser readiness using the remaining diagnostic budget. Only a runnable candidate can replace the baseline, through the existing guarded promotion transaction. A rejected candidate allows at most one switch to its prebound baseline, whose checks also spend the remaining budget. Equal-source candidates use the ordinary single-source check. Integrity changes and hard stops remain failures, and failed evidence is retained.

Successful candidate delivery continues to mean readiness for independent evaluation. It does not mark unverified public requirements as passed. Export or delivery failures after promotion restore the previous source when safe and record the actual delivered digest. This mechanism is supported by local synthetic regression; it does not establish a formal score increase.
