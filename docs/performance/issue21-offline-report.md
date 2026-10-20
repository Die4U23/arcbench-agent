# #21 本轮执行记录（2026-10-20）

## 已核对

- 电脑工作区：`E:\codex\arc_agent`；起始分支 main，HEAD/origin main/远端 main 均为 `b9be372ff9319dbe6e6887852921a1c64bab3c27`。
- GitHub 最新 issue 正文已读取；#34 为 MERGED，合并时刻 `2026-10-20T10:19:59Z`。未采用旧评论中从无 Docker 冻结版直接优化的路线。
- 当前工作分支：`codex/issue-21-offline-benchmark`。
- 起始未提交变化仅有未跟踪 `HANDOFF.md`，未读取其全文、未编辑、未提交。
- 根目录及祖先路径未发现适用 AGENTS.md；已检查的源码/测试/文档目录没有额外 AGENTS.md，仓库没有 `.agents`/`.agents/skills`。
- 现有提交身份保持 `Qing`；没有修改 Git 身份，没有添加 Co-authored-by。
- 初始离线阶段未读取 `.env`；后续获授权后仅在本机进程中核对既有配置并访问原官方端点的只读余额接口，没有输出 key、创建凭据或修改认证/系统设置。

冻结标签解引用为 `f75792c4a7b4377b4628163abd2c3445550a21e8`，源码对象存在。原始目录未修改：

| 原始文件 | 字节 | SHA256 |
| --- | ---: | --- |
| `agent.zip` | 125604 | `8a2ec0f1c5d680426d5d6b514af852a745c66a369e144121dfd9dd6f3931f066` |
| `generated-project-original.zip` | 5082017 | `1d389b4ee83bab8cfe28a743d73c533419fb9d73a32c03e644de2cd13cf65baa` |

全目录文件摘要另存于本地 `local-runs/issue21-offline-20261020/frozen-evidence-sha256.json`。这些原始证据未复制进 Git。

## 实现与分析结论

已有 Agent 提供文本用量日志、工具计时和离线前缀探针，但异常/缺失用量不能简单作为完整成功响应计费。新增结构化观测覆盖正常响应、API 异常、传输中断、实际模型和缺失字段，保持原有预算、请求参数、返回值及隔离行为。

新增离线台账预登记三个任务类别、三个组、各三次；缺终态的 started 计 interrupted，planned 不计分母，跨配置和证据类别不混合。追加记录不覆盖旧证据，artifact 内容用 SHA256 实际核对。提供固定合成用例及完整 Web 对照的后续冻结清单。

这些是测量设施，不是实测性能优化。尚无证据定位当前 Docker main 的实际耗时瓶颈；没有声称减少请求、修复轮次、耗时、成本，也没有声称追平 Claude Code 或达到 99% 缓存。

## 本机验证记录

Python 为 3.12.6，使用仓库 `.venv/Scripts/python.exe`。所有模型响应测试使用本地 fake/mock，没有 Flash、Claude Code 或平台模型调用。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 首批请求观测回归 | 8/8 通过 | 首次 unittest 工具输出 |
| 最新新增测试中的无临时目录子集 | 17/17 通过；两项文件测试未包含 | `local-runs/issue21-offline-20261020/nonfilesystem-tests.txt` |
| 定向回归（新观测/台账、Docker mock、缓存、检查点、压缩） | 运行 41 项；FAILED，25 error 事件，涉及 18 个测试（包含清理的重复 error），23 项通过 | `local-runs/issue21-focused-tests.txt` |
| 全量 `unittest discover -s tests -v` | 未完成；已记录 8 ok / 2 error 后遇到相同临时目录问题，主动停止，无最终汇总 | `local-runs/issue21-all-tests.txt` |
| 台账 prepare/report CLI | 成功，planned=27 / not_started=27 / started=0；无模型运行 | `local-runs/issue21-offline-20261020/{manifest,trials}.json` |
| 本机 Docker 引擎读取 | 失败：访问 Linux 引擎命名管道被拒绝；未启动容器 | 下方原始错误 |
| `git diff --check` | 通过 | 本地命令输出 |
| 用户新授权后，正常桌面权限完整回归 | 492 项：481 通过 / 1 失败 / 10 跳过；唯一失败定位为 `.venv` 旧 SDK | `local-runs/issue21-offline-20261020/local-unit-retry.txt` |
| 测试进程使用当前仓库 SDK 后完整回归 | **492 项：482 通过 / 0 失败 / 10 跳过**，24.061 秒 | `local-runs/issue21-offline-20261020/local-unit-current-sdk.txt` |
| 用户新授权后，本机真实 Docker 隔离集成 | **7/7 通过**，26.703 秒；真实 Docker，无付费模型 | `local-runs/issue21-offline-20261020/local-docker-retry.txt` |
| Linux CI 单元测试 | **492 项：485 通过 / 0 失败 / 7 跳过**，19.875 秒 | [CI 39410463496](https://github.com/Die4U23/arcbench-agent/actions/runs/39410463496) |
| Linux CI 真实 Docker 隔离集成 | **7/7 通过**，18.412 秒；从可信 Dockerfile 构建 | 同上，源码提交 `509ddc59d71508828e84cdf55b739a49951a55de` |

定向命令：

```powershell
.venv/Scripts/python.exe -m unittest tests.test_request_metrics tests.test_benchmark_trials tests.test_docker_sandbox tests.test_prompt_cache tests.test_checkpoint_context tests.test_compaction_source_evidence -v
.venv/Scripts/python.exe -m unittest discover -s tests -v
docker version --format '{{.Server.Os}} {{.Server.Version}}'
```

定向 error 的已知原因是默认执行沙箱为 Python 提供的临时目录读写/清理权限失败，例如：

```text
PermissionError: [Errno 13] Permission denied:
C:\Users\7\AppData\Local\Packages\sandbox.{f1da84dc-12ff-41e1-a85f-0d20616492be}\AC\Temp\tmp7kzf_0fn\result.json
```

Docker CLI 存在，原始 stderr：

```text
permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

初始 Docker 探测使用 `sandbox_permissions=use_default`，不是被 auto-review 拒绝的提权操作。用户随后明确批准正常桌面权限下各重试一次。两项 `require_escalated` 操作获准后执行；Docker 在同一路径成功，说明初始阻塞来自执行沙箱访问限制。没有修改 ACL、系统服务或认证，没有改用另一条 Docker 路径或宿主执行生成代码。现在没有挂起的权限审批。

获准后的实际本机命令：

```powershell
$env:ARCBENCH_RUN_DOCKER_TESTS = '1'
.venv/Scripts/python.exe -m unittest discover -s tests -p test_docker_integration.py -v

# 原 .venv 的已安装 SDK 缺少 #34 的 Git 隔离参数。
# 仅为测试进程指定当前源码，与 main.py 的 SDK 路径逻辑一致，不修改安装。
$env:PYTHONPATH = 'E:\codex\arc_agent\arcbench-agent-runtime\src'
$env:ARCBENCH_RUN_DOCKER_TESTS = '0'
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

本机引擎为 Linux Docker 29.8.2，现有受审查镜像 ID 为 `sha256:3c9ee4121afa393a13ffa0d6d278b30545735781610e430aa2c4a08e7948296c`。没有重建或下载替代本机镜像。CI 另从当前提交构建镜像，两个环境的结果分别记录。

Windows 单元套件的 10 个 skip 是 7 项单独运行的 Docker 集成，以及 3 项需 POSIX/可创建 symlink 的链接夹具；没有将它们计为单元通过。Linux 单元套件只跳过 7 项单独 Docker 测试。旧 SDK 失败原文为 `core.hooksPath=nul not found in ['git', 'status', '--short']`，修正测试的源码搜索路径后通过，未通过修改断言取绿。

新授权后的只读配置核验：官方 DeepSeek、Flash、现有 key 存在，`GET /user/balance` 返回 200/可用/CNY。仅保存这些布尔/非敏感结果于 `config-availability.json`，未输出 key 或余额。Claude Code 2.1.218 已安装，仅执行 `--version`，未调用模型。

## 未完成与下一阶段

1. 完整回归和真实 Docker 已完成，早期失败日志保留；三个 Windows 链接夹具仍跳过，Linux 对应覆盖通过。
2. Claude Code 实际模型/账户、同 Docker 执行适配器，以及 Flash 付费前持久化货币硬停止器尚未实现/核对。
3. 完整 TicketBooking 三类研究的 seed/修复补丁/多轮任务仍需冻结；合成试点不能替代。
4. 初始零预算后，用户新增授权仅 Flash 合计人民币 80 元。**新增模型请求 0、已发生模型费用 ¥0**；真实前后/Claude 对照均未执行。27 个原始研究槽位全部未开始，Claude 组本轮禁止付费，没有成功率或实测分布。
5. 新候选自测与平台正式 30/30 未执行；历史冻结成功不是候选验收。
6. 旧 USD 120 提案已被取代。新执行提案：1 次修复预试验最多 ¥10，复核后最多 6 次 Flash A/B 合计 ¥60，异常预留 ¥10；每轮 2M token/150 请求/45 分钟/¥10，任一先到即停。必须先落实持久化请求前预留和强制停止器；当前 `main.py` 固定 10M/250 的后验预算检查不足以保证货币上限，故尚未启动模型。官方计价、保守预留公式及可核对费用规则见协议。

PR 仅作为离线设施 draft，不关闭 #21，不自动合并。
