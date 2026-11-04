# 通用平台生成器 v14

当前开发版本实现 ReAct 与 Plan-and-Execute、步骤回滚和跨阶段共享的执行限制，跟踪 [issue #44](https://github.com/Die4U23/arcbench-agent/issues/44)。正式平台状态仍为 **UNRUN**；离线脚本化模型与浏览器夹具不能替代真实模型效果或平台成绩。

已验证的 v13 基线保留在标签 `platform-v13-20261104`，交接原包在 `platform-v13-original-20261104`。本目录继续开发 v14；历史 `freeze-v13.json` 不改写，当前发布校验使用 `release-v14.json`。两个版本的包不能混用。

## 运行方式

入口为 `platform-agent/main.py`。Runner 必须显式提供 `MODEL`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`；默认不限制具体模型名称，部署方可设置 `ARC_ALLOWED_MODELS`。额度、HTTPS 端点约束、未知 usage 停止以及预算账本独占创建继续生效。

`ARC_EXECUTION_MODE=plan_execute` 为默认模式：只读规划、校验依赖、按步骤运行 ReAct、验证后提交步骤、失败回滚并有限重规划。设置 `ARC_EXECUTION_MODE=react` 可直接使用行动—观察循环。源码阅读按需要开放，动作和下一步由本次需求及观察结果决定。

计划、实现、覆盖复查、运行时修复共享同一 RunControl，不能通过换阶段或回滚重置资源限制。源码回滚不退还 token、请求、工具次数或时间。执行状态与必要的计划/观察摘要写入 `.arc/platform-budget/<task>/context.jsonl` 和 `actions.json`；不依赖模型自述推断通过。

模型只能操作允许的源码工具。原始需求、附带测试及控制器创建的任务 IR 不可写。默认 `ARC_INPUT_POLICY=requirements_only`：评分测试不进入上下文、seed 推断、静态反馈或运行时修复，生成期间只进行源码校验、构建和 HTTP 探测。输出目录不得含评测测试，评测在源码冻结后由独立评测器执行。

`ARC_INPUT_POLICY=provided_tests` 显式恢复旧模式：允许读取并用本次附带测试修复，不能用于宣称“未见评分测试”的实验。兼容的测试编译模块不接入生成交付路径。生成结束的 `generation-boundary.json` 记录输入策略、需求来源与交付源码摘要，供独立评测器核对；该文件本身不代表行为通过。

详细状态、预算和回滚边界见 [EXECUTION.md](EXECUTION.md)。

部署时可显式配置 `ARC_REASONING_EFFORT` 与 `ARC_MAX_OUTPUT_TOKENS`，避免提供方默认推理强度与响应额度不匹配。省略推理设置时不发送该参数；模型支持范围由部署方核对。提高单次输出上限不会提高 `ARC_TOKEN_LIMIT` 总预算，也不会开启截断重试。

## 离线验证与打包

准备仓库指定的 Python/Node，在 `platform/platform-agent` 安装 `requirements.txt`，再回到仓库根目录：

```text
python platform/check_release.py
```

开发回归按仓库清理策略保留在本地归档，不随发布源码提交。当前 CI 检查源码编译、Docker 镜像构建和发布包完整性；它不等价于合成行为回归或正式平台评分。

```text
python platform/check_release.py --output /absolute/new/path/platform-agent-v14.zip
```

该工具按当前 manifest 验证成员集合、文件大小与哈希，扫描任务专用内容，再生成并回读规范化 ZIP。SDK 安装生成的 build/、egg-info 和 Python 缓存不属于发布输入，其他未列出的源码会被拒绝。修改源码后应审查变更并更新 v14 manifest；不得移动历史冻结标签。

根目录历史 Agent 仍使用独立源码编译/Docker 构建 CI，不能用其通过结果宣称新执行器或正式平台验收通过。第三方来源及许可证保留。
