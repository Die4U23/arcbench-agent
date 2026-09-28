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

为控制费用，单次 Agent 运行默认最多发起 **24 次模型请求**、累计使用 **300,000 token**（输入与输出之和）；每次请求的输出上限为 12,000 token。请求次数覆盖规划、实现和视觉评审，token 用量以模型服务返回的统计为准，在下一次请求前检查，因此最后一次请求可能使实际总量超过阈值。可分别设置 `ARCBENCH_MAX_MODEL_REQUESTS` 和 `ARCBENCH_MAX_TOTAL_TOKENS` 为正整数调整；达到上限会停止继续生成、保留已有文件并报告未完成，不会把局部构建通过误报为任务完成。思考模式保持开启。实现阶段的 `--max-model-turns`、`--max-tool-calls`（或同名 `ARCBENCH_` 环境变量）仍可额外限制每次实现轮数和整次运行的工具调用数。`examples/auth-interface-task/requirements.yaml` 是登录注册界面任务样例；`examples/auth-real-auth-task/requirements.yaml` 是本地真实认证任务样例。

实现阶段从第 16 轮开始定期运行现有构建和测试脚本，并把最新失败结果送回模型修复。最终会重新验证整个项目；若失败内容在修复后发生变化，Agent 会继续修复，直到验证通过或同一失败重复出现。中途检查通过不代表任务验收通过。

使用 DeepSeek API 时，规划、实现和视觉评审默认保留思考模式。实现阶段会完整回传保留的工具调用轮次中的 `reasoning_content`，以符合 DeepSeek 的接口要求；旧轮次仍会做有界压缩。若要单独比较非思考模式，可设置 `ARCBENCH_IMPLEMENTATION_THINKING=disabled`。

实现请求把系统指令、需求子树和实施计划保持为相同的消息前缀；之后连续追加完整工具调用轮次，让下一次请求复用上一轮的前缀。只有上下文超过 100,000 字符、检查反馈改变，或首次图片已经送达时，才把旧轮次压缩为简短进度并开始新的连续段。图片不会在每轮重复发送。运行日志逐次记录模型返回的 `cache_hit_tokens` 和 `cache_miss_tokens`，结束时汇总实际可观察的缓存命中率；若模型服务不提供这些字段，则显示 `unavailable`。缓存由服务商决定，不能保证命中率，也不能从输入 token 总数推断。

模型返回 `finish_reason=length` 时会明确报告截断；工具支持唯一匹配的 `replace_text` 局部替换。修复时优先处理能关联到失败检查的需求模块，最后仍运行全量验收；基础构建或结构检查失败时不会启动视觉模型评审。日志会记录模型及工具调用耗时。优化范围、完整测试和离线曲线见 [性能报告](docs/PERFORMANCE_REPORT.md)；该离线对照不代表平台实测费用或完成率。

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
