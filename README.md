# ArcBench Agent

本仓库用于完成 **CS3604** 课程项目：设计并实现一个能够在 ArcBench 上运行、评测和参与排行榜竞争的 Agent。

## 项目目标

- 实现课程要求的 Agent；
- 接入 ArcBench 的任务与评测流程；
- 持续改进 Agent 的推理与任务解决能力；
- 记录实验结果，并以 ArcBench 排行榜成绩验证改进效果。

## 当前状态

已初始化 Agent 基础架构：Python 入口、需求树解析、显式编排、OpenAI 兼容模型客户端、受控文件/项目脚本工具、构建与测试验证和 ARC-Bench Runtime SDK 适配。首版模型驱动流程聚焦 `web` 任务；当前尚未在 ARC-Bench Runner 上完成首次端到端验证。

详细需求见 [docs/PRD.md](docs/PRD.md)，技术边界与选型见 [docs/TECH_SELECTION.md](docs/TECH_SELECTION.md)。

## 本地环境

Python 版本以 `.python-version` 为准（当前为 3.12.6，需 64 位）；`scripts/setup.ps1` 会读取并校验该版本，已有 `.venv` 版本不匹配时会提示重建。项目直接依赖包括固定版本的 OpenAI Python SDK、PyYAML 和 Playwright；ARC-Bench Runtime SDK 作为源码随仓库提供。传递依赖仍由 pip 根据 SDK 元数据解析。

`.nvmrc` 记录用于本地 Web 项目构建与测试的 Node.js 版本；它不是 Agent Python 依赖，也不会由 `setup.ps1` 自动安装。使用 nvm 的开发环境可通过该文件切换版本；其他环境请安装相同版本的 Node.js。

```powershell
.\scripts\setup.ps1
```

本地任务目录可提供 `requirements.yaml`，或提供 ARC-Bench 任务页对应的 `README.md` / `requirements.md`。Requirement Reader 会把任一格式归一化为同一需求树；两者同时存在时优先使用 `requirements.yaml`，原始 Markdown 保持不变。Markdown 中的图片引用会作为视觉输入交给规划与实现模型：本地图片限制为 PNG/JPEG/GIF/WebP 且不超过 8 MiB；公开 HTTP(S) 图片 URL 会直接传给兼容的模型服务。可用 `VISUAL_MODEL` 指定视觉模型，可用 `VISUAL_REVIEW_MODEL` 指定独立的视觉验收模型；未配置时两者回退到 `MODEL`。包含图片的任务要求所选模型支持图像输入。本地离线模式不调用模型，使用 `--demo` 可走确定性示例生成流程；不带 `--demo` 则走模型驱动流程，需要 ARC-Bench Runner 注入的模型环境变量。

对含视觉参考的网页任务，Agent 还要求生成 `arcbench-visual-acceptance.json`，记录参考图对应的页面、视口、裁剪区域和浏览器交互流程。验证器启动生成的网站，用 Playwright/Chromium 截图并运行交互步骤，再让视觉模型对照参考图评审；有重大视觉差异或交互失败时，Agent 会把失败信息及对应的实际截图交给实现模型，进行一次有界修复。首次验收若没有 Chromium，Agent 会尝试下载；Runner 需允许访问 Playwright 浏览器下载源。截图与机器可读报告写入生成项目的 `artifacts/visual-acceptance/`。浏览器不可用或验收清单缺失时会明确报告未通过，不会只凭 build/test 标记完成。

为控制费用，单次 Agent 运行默认最多发起 **250 次模型请求**、累计使用 **8,000,000 token**（输入与输出之和）；每次请求的输出上限为 12,000 token。请求次数覆盖规划、实现和视觉评审，token 用量以模型服务返回的统计为准，在请求前检查，并记录返回后的实际超额；单次请求的用量无法预先精确确定，因此可能使总量超过阈值。可分别设置 `ARCBENCH_MAX_MODEL_REQUESTS` 和 `ARCBENCH_MAX_TOTAL_TOKENS` 为正整数调整；达到上限会停止继续生成、保留已有文件并报告未完成，不会把局部构建通过误报为任务完成。实现阶段默认开启思考，可用 `ARCBENCH_IMPLEMENTATION_THINKING=enabled/disabled` 配置；聚焦编辑时可能暂时关闭思考以使用受限工具选择。运行器在验证前自动准备 npm 依赖，依赖清单不变时复用成功安装；模块同时分配 token 和请求次数，为后续模块及整站集成保留额度。小任务可主动调低单次上限。本地构建、自测通过和平台验收分开记录。实现阶段的 `--max-model-turns`、`--max-tool-calls`（或同名 `ARCBENCH_` 环境变量）仍可额外限制每次实现轮数和整次运行的工具调用数。`examples/auth-interface-task/requirements.yaml` 是登录注册界面任务样例；`examples/auth-real-auth-task/requirements.yaml` 是本地真实认证任务样例。

实现阶段从第 16 轮开始定期运行现有构建和测试脚本，并把最新失败结果送回模型修复。最终会重新验证整个项目；若失败内容在修复后发生变化，Agent 会继续修复，直到验证通过或同一失败重复出现。中途检查通过不代表任务验收通过。

源码需求审核失败会记录具体问题及文件路径。修复这类问题时，检查点必须同时通过根目录构建/测试和新的源码审核，才能提前结束修复；自测转绿不能清除尚未解决的需求审核问题。平台 Stage 3 的正式测试结果仍独立记录。

每项源码审核缺口必须引用对应原始要求及已读取生产源码的片段；要求编号、引文或源码证据不匹配，以及 pass/repair 与问题列表矛盾时，拒绝该结论，不计作通过。审核不得增加原始要求未规定的能力，也不得把已满足的行为或可选建议列为缺陷。失败测试修复先核对断言与测试隔离：独立用例清理 localStorage/sessionStorage；只有明确的刷新场景保留存储，并用单项及完整测试区分状态污染和业务缺陷。检查点日志保留有长度限制的具体失败信息。

Railway Ticket Booking Demo 的规划、实现和源码审核现在附带平台公开的30个案例正文及共享交互辅助代码，按当前模块筛选。原始需求不变；精确标签、控件角色、输入操作和严格定位断言保留原样，不能调整公开测试来迎合生成网站。逐例面板没有提供完整spec的imports和文件内辅助函数，因此这些证据不是完整可执行官方测试包。本地补齐辅助函数后的DOM回归、真实浏览器检查与平台Stage3结果分别报告。

最终完成与整合修复检查点还会运行独立的公开交互回归：挂载真实App，启动真实后端3000服务，运行30个公开案例正文，补齐的辅助函数使用jsdom适配器。该检查由Agent生成在工具禁止编辑的frontend/.arc目录中，未通过时不能以自编测试转绿结束。缺少测试依赖、启动失败、结果文件缺失或实际用例数量不符均为失败。它是本地门槛，不能替代平台Playwright测试。

同一运行中，只有完整审核输入（需求和所读取的生产源码）不变时才复用上一份审核结论；只扩写测试不会清除已有的业务缺陷。生产源码或需求发生变化后必须重新审核，避免未改业务却重复付费审核或因审查措辞变化重新开始修复。

使用 DeepSeek API 时，规划、实现和视觉评审默认保留思考模式。实现阶段会完整回传保留的工具调用轮次中的 `reasoning_content`，以符合 DeepSeek 的接口要求；旧轮次仍会做有界压缩。若要单独比较非思考模式，可设置 `ARCBENCH_IMPLEMENTATION_THINKING=disabled`。

实现请求把系统指令、需求子树和实施计划保持为相同的消息前缀；之后连续追加完整工具调用轮次，让下一次请求复用上一轮的前缀。只有上下文超过 100,000 字符、检查反馈改变，或首次图片已经送达时，才把旧轮次压缩为简短进度并开始新的连续段。图片不会在每轮重复发送。运行日志逐次记录模型返回的 `cache_hit_tokens` 和 `cache_miss_tokens`，结束时汇总实际可观察的缓存命中率；若模型服务不提供这些字段，则显示 `unavailable`。缓存由服务商决定，不能保证命中率，也不能从输入 token 总数推断。

模型返回 `finish_reason=length` 时会明确报告截断；工具支持唯一匹配的 `replace_text` 局部替换。修复时优先处理能关联到失败检查的需求模块，最后仍运行全量验收；基础构建或结构检查失败时不会启动视觉模型评审。日志会记录模型及工具调用耗时。优化范围、完整测试和离线曲线见 [性能报告](docs/PERFORMANCE_REPORT.md)；该离线对照不代表平台实测费用或完成率。

实现模型会根据工具描述先定位已有代码、批量读取并使用脚本退出码验证结果；当剩余请求或显式轮数/工具额度较少时，会收到一次完成核心行为和验证的提醒。提醒不会重置整次运行的调用计数，也不会关闭思考模式。

如果模型连续三轮重复完全相同的失败工具调用，第二轮后会收到纠偏提示，第三轮仍未改变则停止该实现轮次并报告失败，避免一直消耗请求额度；改用有效调用时可继续。

如果实现阶段连续 12 轮工具调用都没有改动项目文件，第 6 轮会提示模型采取具体行动。达到上限时，若本轮已写入文件且构建与测试脚本通过，则结束该模块并继续下一个需求；否则报告无进展，不把它记为完成。可用 `ARCBENCH_MAX_IDLE_TOOL_TURNS` 设置不少于 2 的正整数调整阈值。脚本通过仅说明本地检查通过，仍需平台功能验收。

## ARC-Bench 运行

Python 上传入口为仓库根目录的 `main.py`，依赖声明为 `requirements.txt`。平台运行时通过环境变量提供 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL`。Agent 不会复制模板覆盖 Runner 准备的目标目录；它直接读取和修改 `--output-dir` 中已有项目。

当前模型路径要求目标模型兼容 OpenAI Chat Completions 的工具调用接口。此能力、Runner 依赖安装方式和目标项目的 Node 构建/测试环境，均需在首次平台运行中确认。

## 项目结构

- `agent/requirements.py`：读取 YAML 或 Markdown 需求文件，并归一化为内部需求树。
- `agent/orchestrator.py`：按需求子树编排设计、实现、验证和有限修复。
- `agent/llm.py`：OpenAI 兼容模型调用与工具循环。
- `agent/tools.py`：项目文件浏览/编辑和受限 npm 脚本执行。
- `agent/verify.py`：构建/测试结果收集及离线示例验证。
- `agent/visual_acceptance.py`：Playwright 页面截图、交互覆盖与视觉验收报告。
- `arcbench-agent-runtime/`：官网下载 starter 随附的 Runtime SDK。
- `docs/`：课程项目 PRD 与技术选型文档。

## 课程信息

- 课程：CS3604
- 项目：ArcBench Agent
- 仓库：`Die4U23/arcbench-agent`
