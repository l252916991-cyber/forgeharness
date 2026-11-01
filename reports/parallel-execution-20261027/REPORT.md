# 有界并行执行报告

2026-10-27。基线 SDK 0.8.6；最终交付 **SDK 0.9.1 / API v1**。

**P0–P6 已执行。candidate-r3 工程门禁 104/104 通过；正式模型评测 180/180 完成。
并行机制合格，当前本机 9B 配置的性能推荐门槛未通过，默认保持并发 1，可显式配置 2。**
整个项目、真实业务与生产部署不因本期交付自动完成。

计划：[执行计划](../../docs/archive/EXECUTION_PLAN_PARALLEL_EXECUTION.md)。
接入：[并行说明](../../docs/guides/PARALLEL_EXECUTION.md)。
架构：[ADR 035](../../docs/adr/035-bounded-model-parallelism.md)、
[调用身份修正 ADR 036](../../docs/adr/036-scoped-model-invocation-identity.md)。
审查与残余风险：[AUDIT](AUDIT.md)。

## 交付范围

- 同一有限 R2 图内，两个独立 final-only 模型节点可以重叠执行，前驱全部成功才综合。
- 单 owner/writer 提交状态、预算和 trace；发送前持久化规范请求与许可，worker 独立返回结果。
- 根预算原子预留/结算；provider 跨 run 共享容量、有界轮转队列，工具在本 run 内独占。
- 在途结果乱序、取消、撤权、超时、owner 崩溃、未知回执与重复恢复均有回归。
- 模型 invocation ID 绑定状态库路径、完整运行身份和 node；同一公开任务 ID 的不同
  私有状态目录不会互相释放容量。模型 resolver 查询该持久 ID，未知请求不自动重发。
- Collaboration 配置、授权任务树、执行阶段/节点时间、三进程 keyless 示例与接入文档完成。

| 批次 | 结果 |
|---|---|
| P0 | ADR、执行策略、发送/容量边界和版本拒绝规则完成 |
| P1 | 公共模型调用准备/发送边界拆分，默认串行行为回归通过 |
| P2 | 有界模型并行、共享容量、队列与工具屏障完成；事件屏障证明真实重叠 |
| P3 | 安全/恢复加固与 PE-012 红灯复现、修复、新旧 wheel 边界验收完成 |
| P4 | SDK/Collaboration、typed inspect、无模型并行/新进程恢复示例接入完成 |
| P5 | 开发 24 次、封存协议、正式 180 次与独立证据复核完成；收益阈值未通过 |
| P6 | 最终独立冻结、源码/干净副本、审查、依赖、发行物和最小安装完成 |

## 最终工程候选

权威证据：[冻结清单](candidate-r3/source.json)、
[验证摘要](candidate-r3/verification/summary.json)、
[工程结果索引](final-engineering-evidence.json)。

源码身份：**2adfdc8937a15e91026238a1b8bc836cadf0a2341a2dd313c3cc3f7c46879ad8**，
472 文件；工作区没有 Git 元数据，使用 archive checkout-equivalent 冻结。
最终模型评测和报告补记后再次核对，源码仍与冻结清单一致。

| 检查 | 结果 |
|---|---|
| 工程门禁 | **104/104**；qualified=true、source_unchanged=true |
| 源码测试 | **1331 passed**；0 failures/errors/skipped |
| 干净副本测试 | **1331 passed**；0 failures/errors/skipped |
| 纯分支覆盖 | 两者均 **86.1615%**，门槛 85% |
| 静态、类型、锁文件 | 全部通过；Python 3.12；没有新增 runtime 依赖 |
| 依赖审计 | 核心、全 extras、两个比较环境通过；保留已披露可选 NLTK FH-019，无新增豁免 |
| 故障与框架组合 | R2/并行故障回归、keyless 对照，以及源码/干净副本各 33 项共同宿主组合通过 |
| 发行物 | wheel/sdist 成员和字节摘要匹配冻结；最小安装、import、10 个示例通过 |

相较 0.8.6 的 1262 项测试，本期增加 69 项；旧通过数字不用于替代新候选验证。
框架共同宿主组合不推导原生框架优势。

未发布候选发行物：

- [wheel](candidate-r3/verification/runs/20261026T214249Z-f6e8b811/dist/forgeharness-0.9.1-py3-none-any.whl)，
  SHA-256 **46ce3341d23d0f54c59f17a3326e9db1f2e2559c80083e402388d7b956f162b7**。
- [sdist](candidate-r3/verification/runs/20261026T214249Z-f6e8b811/dist/forgeharness-0.9.1.tar.gz)，
  SHA-256 **4bc3ce2c5f4396f17b012642da890456140db6240fae7317032297e6b97304bd**。

## 真实模型对照

证据：[开发摘要](local-dev-9b-r2/summary.json)、
[封存协议](protocol-9b-r2.json)、
[正式摘要](local-evaluation-9b-r2/summary.json)、
[全部正式样本](local-evaluation-9b-r2/attempts.json)、
[来源清单](local-evaluation-9b-r2/manifest.json)。

本机 Apple M4 Pro / 48 GiB，oMLX 0.7.0、MLX 0.32.2、
mlx-lm 0.31.4.dev132+g94cdcae13；已有 Qwythos-9B-v2-OptiQ-4bit。
权重/配置文件 SHA-256 在开始、协议和结束时核对一致，不将模型名称当作上游 revision 证明。
温度 0、thinking/MTP 关闭、每次最多 256 output tokens；同一图、资料、角色指令、
预算与 grader，仅改变并发 1/2。首后次序按题目/重复序号交替，后端缓存未逐题清空。

12 开发题 × 2 配置 × 1 次 = 24；30 评价题 × 2 配置 × 3 次 = 180。
每个阶段另有两个校准/预热根运行，共 208 条运行 trace；预热不混入正式分母。
正式两组各 90 次、各 270 次模型调用；没有基础设施故障或缺失正式样本。

下表 p50/p95 包含全部正式尝试；通过数同时要求 runtime/schema 和外部答案评分通过。
配对成功任务的耗时也独立报告在摘要中。

| 任务族 | 串行/并行通过 | p50 串行 → 并行（秒） | p50 降幅 | p95 串行 → 并行（秒） |
|---|---|---|---|---|
| 两名独立专家 fork | 18/30 → 18/30 | 3.675 → 3.193 | **13.10%** | 4.561 → 4.087 |
| 依赖链 chain | 18/30 → 18/30 | 3.831 → 3.831 | 约 0.02% | 4.709 → 4.695 |
| 耗时不均 uneven | 21/30 → 21/30 | 4.669 → 4.241 | **9.17%** | 5.416 → 5.003 |

两组总通过数均 **57/90（63.33%）**；各任务族按 task 聚类的配对成功率差
95% 区间均为 [0, 0]。每组实际 provider-reported input 85080 / output 5199 tokens，
每成功任务消耗比例均为 **1.00**，包含失败消耗。无付费 API；电力/硬件成本未计价。
每组 33 个未通过样本中，21 个是协调者复述 JSON schema 导致结构拒绝，12 个是
结构合法但外部答案评分不通过；失败和消耗全部保留，未事后修改提示词或评分标准。

fork/uneven 观察到客户端请求峰值 2，串行与 chain 为 1；不据此宣称后端同时 GPU 推理。
预登记要求可并行任务族 p50 不超过串行 0.80 倍、p95 不超过 1.10 倍，
质量差下界不低于 −5 个百分点、每成功任务 tokens 不超过 1.10 倍。
本轮质量差与 tokens 条件满足，但 fork/uneven 的 p50 比例约 **0.869 / 0.908**，
未达到 20% 提速要求。因此 measurement_valid=true、performance_qualified=false，
此本机配置继续推荐并发 1；并发 2 保留显式选择。

以上是作者自建库存算术微任务，非独立未见业务集；每族只有 10 题、无独立领域评分。
不能据此认证专业正确性、实际仓库修复能力、生产 SLO 或通用质量收益。

## 证据完整性与接受的诊断限制

[最终来源/trace 核对](final-evidence-check.json)和
[主 trace/用量复核](canonical-evidence-review.json)独立核实：

- 208 条主 hash trace 均有效，正式每组 270 条 model.request 与 270 条 model.action；
  每根完整 3 次，规范输入、结果及用量均可重建，合计与状态账本一致。
- 成功批次全部 capacity claims 为 ended；没有待释放的物理容量。
- 正式协议、manifest、最终源码和权重身份匹配，所有正式尝试保留。

PE-013 是接受的低风险辅助诊断限制：一个串行样本的已完成 HTTP Request 对象地址复用，
使旁路 HTTP 元数据只留 269 条；主 trace 与状态仍完整记录 270 次。根计时、评分、实际
token 汇总和在途峰值不依赖该已完成对象的地址，不影响上述负向性能结论。
不宣称旁路网络元数据完全无缺项；原始样本保持不变，缺项位置详见主 trace 复核。

## 失败候选与资源校准

| 候选/批次 | 结果与处置 |
|---|---|
| candidate-r1 / 0.9.0 | 97/104；旧恢复示例断言版本 4、旧比较协议声明 0.8.6，发行阻断；原结果保留 |
| candidate-r2 / 0.9.0 | 当时 104/104，但随后确认 PE-012 跨存储容量票据混同；已取代，不作最终资格 |
| candidate-r3 / 0.9.1 | 修复并独立完整重验，104/104；不借用前两次通过证据 |
| 27B 首次校准 | 动态准入约 8.4 GB，低于约 17.0 GB 权重；0 次根运行，preflight 停止 |
| 9B 首次校准 | preflight 后动态内存下降，首个校准请求 HTTP 507；1 个未成功校准、0 个正式样本，未知用量保留预留 |
| 9B 新批 | 用户授权释放 ComfyUI 空闲缓存后，开发/封存/180 次正式采样完成，无基础设施故障 |

PE-012 使用 r2 原 wheel 独立复现：
[失败记录](PE012-old-wheel-reproduction.log)。
新 0.9.1 绑定状态库路径、完整运行身份和节点，回执查询使用持久票据，并拒绝接管旧
0.9.0 运行。全部发现的分级、修复、回归和接受边界见 AUDIT。

早期模型失败的证据仍在
[27B 诊断](local-dev-r1/preflight.json)、
[9B 校准诊断](local-dev-9b-r1/summary.json)及各自 supervisor 退出记录中；
不覆盖失败、不放宽内存保护、不把未决用量填零，也不混入成功正式批的分母。

## 资源释放与当前边界

按用户明确授权，先核对 ComfyUI 实时 running=0/pending=0，再请求卸载空闲模型和
执行缓存，随后观察可用系统内存约 26.1 GB。见
[缓存释放记录](comfy-cache-release.json)。
本次三个 oMLX 服务均使用工作区内独立 base-path，未修改用户全局配置。

最终本批 PID 61408 **已退出、8000 端口已释放**，见
[退出记录](live-supervisor-9b-r2.json)。
模型权重与 trace/回执/评测证据保留；本次临时服务目录已清理，
必要配置保存为去凭据版本，见 [配置](service-settings.sanitized.json)、
[清理记录](owned-service-cleanup.json)。没有修改主 hash trace。

当前能力是单机 POSIX/SQLite、一个 dispatcher 进程/容量域、固定图、最多两个模型请求；
工具保持本 run 独占。未知调用会保守停发，并需可信回执或有证据的管理确认。
旧 R2 运行保留原 wheel，无隐式状态迁移。自主子运行、动态递归、隔离并行写入、
分布式 HA、完整 SessionService/HTTP/UI 接线和真实业务验收仍属于后续产品范围。
