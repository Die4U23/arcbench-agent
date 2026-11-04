# 通用平台生成器 v14

当前开发版本实现 ReAct 与 Plan-and-Execute、步骤回滚和跨阶段共享的执行限制，跟踪 [issue #44](https://github.com/Die4U23/arcbench-agent/issues/44)。正式平台状态仍为 **UNRUN**；离线脚本化模型与浏览器夹具不能替代真实模型效果或平台成绩。

已验证的 v13 基线保留在标签 `platform-v13-20261104`，交接原包在 `platform-v13-original-20261104`。本目录继续开发 v14；历史 `freeze-v13.json` 不改写，当前发布校验使用 `release-v14.json`。两个版本的包不能混用。

## 运行方式

入口为 `platform-agent/main.py`。Runner 必须显式提供 `MODEL`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`；默认不限制具体模型名称，部署方可设置 `ARC_ALLOWED_MODELS`。额度、HTTPS 端点约束、未知 usage 停止以及预算账本独占创建继续生效。

`ARC_EXECUTION_MODE=plan_execute` 为默认模式：只读规划、校验依赖、按步骤运行 ReAct、验证后提交步骤、失败回滚并有限重规划。设置 `ARC_EXECUTION_MODE=react` 可直接使用行动—观察循环。源码阅读按需要开放，动作和下一步由本次需求及观察结果决定。

计划、实现、覆盖复查、运行时修复共享同一 RunControl，不能通过换阶段或回滚重置资源限制。源码回滚不退还 token、请求、工具次数或时间。执行状态与必要的计划/观察摘要写入 `.arc/platform-budget/<task>/context.jsonl` 和 `actions.json`；不依赖模型自述推断通过。

模型只能操作允许的源码工具。原始需求、附带测试及控制器创建的任务 IR 不可写。只有输入任务附带测试时才运行那些测试；没有测试时只做 HTTP 探测。兼容的测试编译模块不接入生成交付路径。

详细状态、预算和回滚边界见 [EXECUTION.md](EXECUTION.md)。

## 离线验证与打包

准备仓库指定的 Python/Node，在 `platform/platform-agent` 安装 `requirements.txt`，再回到仓库根目录：

```text
python platform/check_release.py
python platform/run_tests.py --require-runtime
```

浏览器夹具使用临时目录的 `arc-runtime-check-playwright-cache`，安装 `@playwright/test@1.64.1` 及其匹配 Chromium。Linux CI 还安装系统浏览器依赖。统一测试入口禁止外部模型客户端，检查实际收集数量，并拒绝除 `no public inputs in working copy` 之外的任何跳过。报告在 `platform/test-results/summary.json`。

```text
python platform/check_release.py --output /absolute/new/path/platform-agent-v14.zip
```

该工具按当前 manifest 验证成员集合、文件大小与哈希，扫描任务专用内容，再生成并回读规范化 ZIP。SDK 安装生成的 build/、egg-info 和 Python 缓存不属于发布输入，其他未列出的源码会被拒绝。修改源码后应审查变更并更新 v14 manifest；不得移动历史冻结标签。

根目录历史 Agent 仍使用独立 Python/Docker CI，不能用其通过结果宣称新执行器或正式平台验收通过。第三方来源及许可证保留。
