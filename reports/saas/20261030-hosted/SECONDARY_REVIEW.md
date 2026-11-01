# 第二视角复审记录

日期：2026-11-01。审查者：`/root/hosted_e2e`。结论：**本记录所列范围没有尚未修复的 blocker/high；可以进入冻结候选的完整门禁。此结论不是发布资格或完整 SaaS 验收。**

## 范围与方法

逐段检查了模型 prepared/cache、`ModelBridge`、上下文 spill 和发行物 staging，重点检查实际发送字节与预检/证据是否相同、凭据边界、资源限额、路径链接及失败恢复。对 root 编写的 prepared/cache、bridge、spill、release staging 提供第二视角；响应体限额和先前 container 修复由本代理实现，因此其专项测试不能冒充独立于实现者的审查，仍需最终候选审计覆盖。

本次复查的源码 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `src/forgeharness/models/prepared.py` | `dd19c719703fef6d8eeacffe6a30bb5c28b43e80796ba5272b141ea2eef9232f` |
| `src/forgeharness/models/caching.py` | `ad8bbd97f16a6d81611e904fd57c2f0128a306db638e0e169e2775c06ce1f4cd` |
| `src/forgeharness/models/openai_compatible.py` | `d555e146ea03750cc4b163b8f09afdb784b27662bf94c6e3dd010af1439a756f` |
| `src/forgeharness/sdk/kernel.py` | `9935e4b54a10045de52bf17163fa7852d59cbb937ea2fae321c33f7061b43469` |
| `src/forgeharness/context/spill.py` | `c81eb1efa2ed28673f2f28d5389a886363461236e14f7d2f4673ecf389895887` |
| `benchmarks/release_artifacts.py` | `14815e38dc29db0f286feffcb0ce21c5163834f27fbfc8bccd7563c779b0b921` |
| `src/forgeharness/backends/container.py` | `21308fad3fda2bfd7294149da4f8d03bacd9732bb9fc1f3dc5513afefb751052` |

## 已确认并关闭的问题

| 问题 | 分类 | 修复及验证 |
| --- | --- | --- |
| EXPLICIT cache 在准入后改变 wire body，预检和 trace 未包含 marker | High，已关闭 | 在 prepare 时添加 marker，使用专用 format 校验；发送直接使用冻结的 payload。OFF/PREFIX/EXPLICIT 的真实 TCP body、prepared payload 和 SHA-256 均相同。独立 HTTP server 的 cached-token 回报也穿过 bridge，与 trace 核对。 |
| HTTP response 在校验前整包加载，可耗尽 host memory；持续小 chunk 可无限延长连接 | High，已关闭 | 默认 8 MiB、最多 16 MiB 的严格整数配置；Content-Length 检查及 raw stream 累计上限；identity 编码请求，拒绝压缩，避免先解压绕过限额；总 request/read deadline。关闭响应后，未知用量仍保留 PG 预留，另一 worker 不会重发。禁止注入 client 自动跟随 redirect 并提前缓冲中间响应。 |
| 发行物 staging 的路径写入可跟随注入 symlink；第二文件失败遗留半成品 | High，已关闭 | 受保护的 dirfd 链、NOFOLLOW/EXCL、私有随机 staging 目录，完整写入并 fsync 后 rename；失败清理。回归覆盖链接注入、parent 替换、第二文件失败。 |
| 发行物输入可换成 FIFO 或巨型文件，在 SHA 校验前阻塞或耗尽内存 | High，已关闭 | 输入通过 no-follow/nonblock descriptor 和 regular/single-link 检查；artifact 每个最多 64 MiB，证据文件最多 16 MiB，读取再次限额，然后核对 SHA。 |
| spill 并发同内容写入替换旧 inode，使已打开 fd 的 nlink 变 0，被误判为非法 hardlink | Confirmed defect，已关闭 | 保留 nlink > 1 的拒绝，允许已打开后被 unlink 的 nlink 0，并继续校验内容摘要。新增真实 fd/replace 的确定性回归。本轮首次失败证据保留，修复后 113 项目标测试全部通过。 |

未将现有问题通过重试或放宽断言消除。失败的 [首次目标测试记录](secondary-review-unit.xml) 与修复后的 [113/113 复验记录](secondary-review-unit-r1.xml) 分别保留。

## 当前验证证据

- 目标 unit 复验：113 tests，0 failure/error/skip；覆盖响应限额、缓存/preflight、spill、release artifact。
- [真实 PostgreSQL + loopback HTTP](../../saas-postgres/20261101T051035Z-http-response-bounds-a353052d/tests.xml)：7/7，通过三种 cache 模式、缺失 usage、Content-Length/分块两种超过默认 8 MiB 的响应，以及持续小 chunk 的总 deadline。所有模型/业务流量仅在本机，无收费调用。
- [独立 installed wheel 消费](../20261031-hosted-consumer-dev/hosted.log)：仅安装 `[cloud,postgres-test]`；生产模块来自消费者 venv 的 site-packages；一次调用、5 units、PG checkpoint/trace/physical settlement 均核对，临时服务器关闭。这是开发证据，不与后续源码自动绑定。
- 修改过的模型响应边界通过类型、Ruff 和格式检查。正式候选仍必须重新运行完整源码及干净副本门禁，开发证据不能拼成一个冻结候选资格。

## 实现边界与剩余风险

- 请求 sizing 是启发式预算预检；实际结算依赖可信 provider/business verifier。超限、超时、无可信 usage 不证明未执行，当前行为保持 unknown 和预留。
- ModelBridge 对已解码的 request/payload 应用所配置的 evidence transform；Cloud 另有强制凭据检查。新响应限额错误只包含固定原因，不记录被拒绝 body 的前缀或不可信 header 值。
- raw streaming 的限额覆盖普通 HTTPX 网络传输。显式注入的、先行物化响应的 transport 属于可信 host 代码；适配器会在解析前再次限额，但不能追回该 transport 自己已分配的内存。
- spill 限制的是单 blob（16 MiB），不是 workspace 的累计磁盘额度或资料生命周期。目录 descriptor/no-follow 流程针对 POSIX，本次未验证 Windows、网络文件系统或断电故障。digest 验证不会把已损坏或替换的 blob 当成原文。
- staging 依赖本地受控资格记录和 CI 工作流，不为本地被授权篡改资格记录的人提供签名真实性保证；本轮没有实际发布 PyPI。
- 先前 container 测试验证了受控 argv、容量、bounded subprocess output、真实本机子进程 timeout/cancel 与模拟 daemon cleanup。**本环境没有运行真实 Docker daemon 或真实容器，未证明内核/cgroup、网络隔离、镜像 UID 兼容性、daemon 故障后的容器清理和跨平台隔离。**这些不能从 runner/fake-daemon 测试推导为已完成的生产隔离验收。
- 本报告不覆盖完整 SaaS 的身份/API/UI、队列公平调度、月度组织额度、支付、真实云部署与持续运营验收；本批仍保持未完成边界。
