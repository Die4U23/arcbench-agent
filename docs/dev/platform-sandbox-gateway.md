# ArcBench 平台兼容：临时认证 Docker 网关

## 目的与执行边界

ArcBench 当前 runner 没有 Docker 命令或本地 socket，且 `unshare` 被拒绝。本方案让平台继续运行可信控制器和 Flash 模型调用，只把构建、测试与浏览器检查委托到本机已有的 Linux Docker。平台最终正式测试仍由 ArcBench 自己执行；不会上传旧的完成网站代替生成，也不会把本地自测当作平台 30/30。

平台上传包明确包含 `sandbox-remote.json` 时才启用远程传输。未配置时仍使用原有本地 Docker；远程不可用、令牌过期、镜像不匹配或文件发生外部变化时明确失败，不退回平台宿主执行。

## 可审查的实现

- [客户端](../../agent/remote_sandbox.py)：使用 HTTPS（仅本机回环测试允许 HTTP），验证证书、禁止重定向，不继承代理凭据。发送受原路径规则保护的文件快照，收到结果后再次检查快照和路径再导入。
- [网关](../../scripts/sandbox_gateway.py)：仅绑定 `127.0.0.1:18871`，只接受强随机 Bearer 令牌；最长四小时有效，过期停止服务。没有 shell、任意宿主路径、Docker API 或模型代理接口。
- 仅接受固定 Docker 操作白名单。请求与响应各最多 96 MiB，最多八个 HTTP 处理线程、一个执行任务、合计一千个任务，每任务最多 1,800 秒。异步作业避免长 HTTP 请求；相同作业 ID 不重复执行，旧结果释放后只保留小型去重记录。
- 网关解码文件到新建临时目录，再使用现有 `DockerSandbox` 执行。镜像 ID 固定；生成代码仍使用 non-root、断网、只读根文件系统、能力清空、资源限额和精确容器清理。它看不到本机项目、Docker socket、控制器文件或模型 Key。
- 构建和浏览器证据返回平台控制器；视觉模型评审继续在控制器执行，仍只允许 Flash。任务结束后停止网关与隧道，临时能力令牌不再可用。

## 外部连接需要单独授权

2026-10-21 经用户明确授权，使用 Cloudflare Quick Tunnel 临时映射回环网关，仍由网关 Bearer 令牌认证。零模型平台探针通过，正式运行暴露离线依赖与原生执行问题，未生成正式测试成绩。通道已关闭，后续开通需要重新确认范围。Cloudflare 中转生成源码、任务资料、检查日志和截图；DeepSeek Key 不经过网关。短期能力令牌只随私有 agent 上传包传递。

这会临时扩大本机服务的外部可达性，所以在实际开通前请求用户明确授权。确认仅覆盖本轮平台验证，最长四小时，不安装系统服务、不修改防火墙、不开放 Docker daemon、不建立通用远程 shell。Cloudflare Quick Tunnel 不承诺生产可用性；断连视为执行失败，不能伪造通过。

`sandbox-remote.json` 格式（示例占位，不能直接运行）：

```json
{
  "endpoint": "https://EXAMPLE.trycloudflare.com",
  "token": "PER_RUN_RANDOM_CAPABILITY_AT_LEAST_40_CHARACTERS",
  "expires": 0,
  "image": "sha256:EXACT_REVIEWED_LOCAL_IMAGE_ID"
}
```

此文件位于可信上传包根目录，不能放在生成项目中；Git 已忽略该路径，Docker 构建上下文也不包含它。不要把真实令牌写进 PR、issue、命令参数或日志。网关的本地配置保存在被忽略的 `local-runs/` 内。

## 验证与发布条件

本机回环真实集成已覆盖：HTTP 文件往返、UID 1000、凭据哨兵不可见、仅 loopback 网络、Docker socket 不可见、真实 Chromium 与后端交互、截图返回后由控制器评审。单元测试覆盖未认证拒绝、过期、镜像不匹配、越界路径、非白名单操作、重复作业不重跑、外部文件修改不覆盖、远程错误不回退。

零模型平台探针已证明公网往返与隔离路径，不能代替 ArcBench 正式验收。运行容器的 `/work` 显式允许原生依赖执行，保留 `nosuid,nodev`；`/tmp` 仍为 `noexec`，断网、UID 1000、只读根目录、能力清空与资源限制保持。真实集成增加 Vite、Vitest、esbuild 两个版本的检查。只有候选通过自测、平台正式 30/30 和最终提交的 CI，才按授权合并 PR。

运行期间的离线缓存缺失、依赖执行权限错误、远端传输失败、授权过期或镜像异常以 `SandboxInfrastructureError` 贯穿工具与检查边界，直接停止整轮。结构化 `repair_circuit_opened` 事件及 `.arc/infrastructure-failure.json` 记录原因代码、发生次数、重试次数和累计 token；不记录令牌或远端地址。完整事件复盘见[修复风暴日志](../performance/issue21-repair-storm-20261021.md)。

官方资料：[Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。
