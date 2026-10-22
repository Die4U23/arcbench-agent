# 2026-10-22 平台验证记录

## 候选 d1cc3e8：停止逻辑生效，依赖清单仍有缺口

[提交 f52004b21238](https://arc-bench.com/playground/task-bank/web/ticketbooking/submissions/f52004b21238) 使用源码 `d1cc3e830de650d3a2dadfebc6ef50927fd671be`，上传包 SHA256 为 `e1b4f83ad194cb4504dc998b732eb01ba5fb136da1f0c8e7469d8b01e4009e51`。平台最终显示退出码 1，未进入正式 30 项测试。

12 次已完成响应均为 `deepseek-flash`，输入 319,011、输出 51,509，总 token 370,520；输入缓存命中 269,568、未命中 49,443，命中率 84.5%。不能将失败运行与历史成功运行直接比较成节省率。

首次离线安装缺少 `@testing-library/user-event` 时，平台日志立即出现 `repair_circuit_opened`：原因 `dependency_cache_missing`、范围 `run`、次数 1、重试 0、动作 `stop_run`。之后只输出最终用量与失败信息，没有新的模型请求。这证明上轮的跨模块无效修复风暴已被阻断，但不表示任务完成。

读取失败现场的清单后发现，模型还选用了不在缓存中的 React 18、Vite 5、Vitest 2 等旧版本，并引用未缓存的 React 插件、supertest。原实现只在 worker 安装失败后给出可用版本，模型生成前不知道缓存边界。

## 修正

- 将镜像使用的可信 `sandbox/toolchain/package.json` 版本清单提前放入 Web 规划、实现的稳定系统上下文，要求选择目录内的精确版本，保持清单和锁文件一致。该目录不来自生成项目。
- 补充 `@testing-library/user-event@14.6.1`、`@vitejs/plugin-react@5.0.4`、`supertest@7.1.4` 及锁定的传递依赖。
- 增加真实断网 Docker 回归：无项目锁文件时安装上述栈，运行 React 插件配置、jsdom 中的输入与点击，以及 Express/supertest HTTP 断言。
- 仍保留首次基础设施异常立即停止，不能为了通过而放开生成代码联网或回退到宿主执行。

原始日志、截图、镜像构建及回归输出保存在被忽略的 `local-runs/platform-resume-20261022/`。临时能力令牌与模型凭据不进入仓库。后续候选需要新的平台正式 30/30 和对应提交 CI 才能合并。

修正后，本地 550 项测试中 537 通过、13 按环境条件跳过；真实 Docker 与认证网关集成 17 项全部通过。本机最初使用旧安装的 Runtime SDK 导致一项既有隔离测试失败，改为显式加载仓库 SDK 后通过；平台上传包与镜像均使用仓库 SDK。
