# v13 平台生成器冻结基线

2026-11-04 冻结的开发基线。入口为 `platform-agent/main.py`，本目录包含完整源码、合成回归夹具和打包校验工具。正式平台状态为 **UNRUN**；该名称不表示 v13 已获得平台通过成绩。

源码来自交接指定的 `platform-source-agent-v13.zip`，原 ZIP SHA-256 为 `4b21eec9d88c08ca69813125092dafdcfdb8aa3f1615784fabdcf033fd78fe45`，668,904 字节、43 个成员。Linux CI 发现浏览器扫描访问不可读的 `/root/.cache/ms-playwright` 会失败，因此冻结基线包含一项权限兼容修复：跳过不可访问的候选目录，继续发现可用浏览器。42 个成员与原包逐字节一致，唯一变化为 `agent/platform_runtime_check.py`。`freeze-v13.json` 记录当前哈希、该文件的原始哈希及修复原因；原始源码另以 `platform-v13-original-20261104` 标签存档。Git 属性关闭源码目录的换行转换。

## 执行行为

- 任务输入来自本次需求目录及其附带的 `tests/`，没有附带测试时只做 HTTP 探测。发布包不包含任务注册表或公开任务测试集。
- 空应用目录才写通用脚手架；已有应用保持原布局。生成器写入 `docs/task-ir.json` 和 `docs/coverage.json`，覆盖缺项回送实现阶段。
- token 默认额度根据场景、工作项、实体数计算，限制在 150 万到 600 万之间，`ARC_TOKEN_LIMIT` 可覆盖。预算账本保留独占创建，位于生成应用 `.arc/platform-budget/`。
- 默认每批审核 4 次，所有批次合计 `min(64, 8 + 工作项数)`。运行时检查在临时副本执行，退化候选恢复为已保留的较好快照。
- `compile_checks` 和 `author_tests` 虽作为兼容模块存在，但不接入生成交付循环。合成回归会单独测试这些组件，不能把它们当成本次任务的验收套件。
- Python 依赖不固定 Playwright，避免替换 Runner 镜像里的版本。测试 CI 单独安装匹配的 Node Playwright/Chromium，它们不进入发布包。

## 复现离线回归

在仓库根目录准备 Python 3.12.6 和 `.nvmrc` 指定的 Node，然后在 `platform/platform-agent` 中安装 `requirements.txt`，以正确解析本地 Runtime SDK 路径。之后返回仓库根目录：

```text
python platform/check_release.py
python platform/run_tests.py --require-runtime
```

测试工具需要匹配的 `@playwright/test` 与 Chromium。CI 将 `@playwright/test@1.64.1` 装到系统临时目录的 `arc-runtime-check-playwright-cache`，再运行它的 `cli.js install --with-deps chromium`。本地若浏览器路径环境变量指向不存在的目录，先清除该变量。

完整集合至少 145 项：原始回归 139 项，另有冻结完整性回归 3 项和浏览器目录权限回归 3 项。测试入口禁止构造外部模型客户端，写出 `test-results/summary.json`；`--require-runtime` 使浏览器、Node 或安装失败引起的跳过变成非零退出。唯一允许的跳过是没有随包携带的公开任务输入检查。合成应用启动、npm 公共依赖准备和 localhost 浏览器测试不会调用付费模型。

本地冻结验证统计与 CI 证据说明见 [版本记录](../docs/releases/platform-v13-20261104.md)。

## 重建与完整性

```text
python platform/check_release.py --output /absolute/new/path/platform-agent-v13.zip
```

工具先检查成员集合和逐文件哈希，再扫描任务知识泄漏，最后生成并回读 ZIP。SDK 安装生成的 `arcbench-agent-runtime/build/`、其 `src/arcbench_agent_runtime.egg-info/` 及 Python 字节码缓存不属于发布内容；其他新增源码仍会被拒绝。输出路径必须不存在。重建 ZIP 使用固定时间和 STORE 压缩策略，成员字节以当前冻结 manifest 为准。修复版 ZIP SHA-256 为 `7901dae71ab8051a8f9d802493e6fb78b68b982d362a4fb2cfd3918c22478557`，不可将它与原始 v13 包混用。

后续大改应从本次合并与 `platform-v13-20261104` 标签建立新分支。改源码后冻结校验会失败，这是预期行为；新版本应更新自己的 manifest、版本记录和 CI 名称，保留该标签用于回退。根目录的历史 Agent 与旧测试继续由原 CI 验证。

## 已知边界

v12 的 17/35 是另一次平台运行，不能归给 v13。当前没有 v13 的正式成绩，Runner 对生成阶段安装、联网、浏览器和时限的支持仍需正式环境确认。IR 只解析认识的需求字段；未识别结构降级到原始需求。旧 `ModelClient.implement` 保留了入口不走的潜伏引用，本冻结未清扫或重新启用该路径。真实平台调用和上传仍需单独安排。

第三方 Runtime SDK、Playwright 静态工具的来源及许可证保留在源码对应目录。项目提交以维护者个人身份记录。
