# #21 本轮执行记录（2026-10-20）

## 已核对

- 电脑工作区：`E:\codex\arc_agent`；起始分支 main，HEAD/origin main/远端 main 均为 `b9be372ff9319dbe6e6887852921a1c64bab3c27`。
- GitHub 最新 issue 正文已读取；#34 为 MERGED，合并时刻 `2026-10-20T10:19:59Z`。未采用旧评论中从无 Docker 冻结版直接优化的路线。
- 当前工作分支：`codex/issue-21-offline-benchmark`。
- 起始未提交变化仅有未跟踪 `HANDOFF.md`，未读取其全文、未编辑、未提交。
- 根目录及祖先路径未发现适用 AGENTS.md；已检查的源码/测试/文档目录没有额外 AGENTS.md，仓库没有 `.agents`/`.agents/skills`。
- 现有提交身份保持 `Qing`；没有修改 Git 身份，没有添加 Co-authored-by。
- 没有读取 `.env` 内容、创建凭据、修改认证或系统设置。

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

该 Docker 探测使用 `sandbox_permissions=use_default`，不是一次被 auto-review 拒绝的提权操作。仅凭 stderr 无法区分宿主 ACL 与执行沙箱限制。没有尝试修改 ACL、启动系统服务、访问另一条 Docker 路径或宿主执行生成代码；没有重试被拒绝的操作。没有正式挂起的 Docker 提权工具审批。重试授权已作为对话问题提出，记录时未收到答案。

未来获准后的本机真实 Docker 命令应为：

```powershell
# 仅在权限阻塞解决且原镜像已核实/按可信 Dockerfile 构建后执行
$env:ARCBENCH_RUN_DOCKER_TESTS = '1'
.venv/Scripts/python.exe -m unittest discover -s tests -p test_docker_integration.py -v
```

仓库 CI 可独立验证 Linux；其结果不能替代此电脑 Windows Docker Desktop 的实际验证。本轮不会为本机权限失败移除或放宽 Docker 边界。

## 未完成与下一阶段

1. 本机完整回归和真实 Windows Docker 检查尚未通过；上述错误不能写成 skip 或 pass。
2. Claude Code 实际模型、版本、同 Docker 执行适配器以及付费前货币硬停止器尚未实现/核对。
3. 完整 TicketBooking 三类研究的 seed/修复补丁/多轮任务仍需冻结；合成试点不能替代。
4. 本轮付费预算为零，真实前后/Claude 对照均未执行；27 个槽位全部未开始，没有成功率或实测分布。
5. 新候选自测与平台正式 30/30 未执行；历史冻结成功不是候选验收。
6. 预算提案为 USD 120（27 次试点最多 USD 81、正式候选槽位 USD 9、不可自动动用预留 USD 30），详见协议。必须由主助手取得新授权并落实硬停止器才能执行；旧 issue 历史预算不适用。

PR 仅作为离线设施 draft，不关闭 #21，不自动合并。
