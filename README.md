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

Python 版本以 `.python-version` 为准（当前为 3.12.6，需 64 位）；`scripts/setup.ps1` 会读取并校验该版本，已有 `.venv` 版本不匹配时会提示重建。项目直接依赖已按本机当前使用的 OpenAI Python SDK 和 PyYAML 版本固定；ARC-Bench Runtime SDK 作为源码随仓库提供。传递依赖仍由 pip 根据 SDK 元数据解析。

`.nvmrc` 记录用于本地 Web 项目构建与测试的 Node.js 版本；它不是 Agent Python 依赖，也不会由 `setup.ps1` 自动安装。使用 nvm 的开发环境可通过该文件切换版本；其他环境请安装相同版本的 Node.js。

```powershell
.\scripts\setup.ps1
```

本地离线模式不调用模型，但仍需要一个含 `requirements.yaml` 的任务目录。使用 `--demo` 可走确定性示例生成流程；不带 `--demo` 则走模型驱动流程，需要 ARC-Bench Runner 注入的模型环境变量。

模型实现阶段默认最多进行 36 轮模型响应和 96 次项目工具调用。可用 `--max-model-turns`、`--max-tool-calls` 覆盖，或分别设置 `ARCBENCH_MAX_MODEL_TURNS`、`ARCBENCH_MAX_TOOL_CALLS` 环境变量。`examples/auth-interface-task/requirements.yaml` 是可复现的登录注册网站任务样例。

## ARC-Bench 运行

Python 上传入口为仓库根目录的 `main.py`，依赖声明为 `requirements.txt`。平台运行时通过环境变量提供 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL`。Agent 不会复制模板覆盖 Runner 准备的目标目录；它直接读取和修改 `--output-dir` 中已有项目。

当前模型路径要求目标模型兼容 OpenAI Chat Completions 的工具调用接口。此能力、Runner 依赖安装方式和目标项目的 Node 构建/测试环境，均需在首次平台运行中确认。

## 项目结构

- `agent/requirements.py`：读取并校验 `requirements.yaml`。
- `agent/orchestrator.py`：按需求子树编排设计、实现、验证和有限修复。
- `agent/llm.py`：OpenAI 兼容模型调用与工具循环。
- `agent/tools.py`：项目文件浏览/编辑和受限 npm 脚本执行。
- `agent/verify.py`：构建/测试结果收集及离线示例验证。
- `arcbench-agent-runtime/`：官网下载 starter 随附的 Runtime SDK。
- `docs/`：课程项目 PRD 与技术选型文档。

## 课程信息

- 课程：CS3604
- 项目：ArcBench Agent
- 仓库：`Die4U23/arcbench-agent`
