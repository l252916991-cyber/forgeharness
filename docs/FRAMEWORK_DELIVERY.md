# 框架首版交付与范围

版本：0.2.0；SDK API：1；计划：`FRAMEWORK_EVOLUTION_PLAN.md`。
源码来自本地 ZIP 导出，当前目录无 Git 元数据。基线与最终候选分别以
`reports/framework-baseline-source.json`、`reports/framework-candidate-source.json`
逐文件 SHA-256 记录，不编造 Git 提交或独立作者身份。

## F0–F6 交付

| 阶段 | 实现与证据 |
| --- | --- |
| F0 | 523 项原始测试基线；归档来源/文件摘要；ADR 010–012；历史报告适用范围整理 |
| F1 | 公开嵌入式 SDK；通用模型能力与业务类型脱钩；Coding 验收与 RepositoryMap 移出核心；最小 wheel 无 Web/模型平台依赖 |
| F2 | 可信 actor/tenant/resource/workspace；上下文策略；同库持久审批与精确执行资格；审批者重新认证和当前权限检查 |
| F3 | 第 3 代恢复；有序持久验收证据；当前策略与参数/副作用声明校验；调用/物理尝试/稳定幂等键；结果摘要验证；上下文来源与版本；隐私变换与可重试删除 |
| F4 | Coding 使用公开 SDK；商品咨询与模拟转接工单参考应用；无核心电商分支；跨 run 的业务请求唯一约束 |
| F5 | SDK_GUIDE、两个应用接入范例、可选 HTTP 认证边界、可选 extras、0.2.0 wheel；独立环境从构建产物运行两种示例 |
| F6 | 冻结候选后质量/控制/兼容/依赖门禁；审计分类和缺陷回归；干净源码等价目录重新验证；最终结果保存在 framework-release-audit.json |

这份文档描述实现边界。最终是否通过以绑定候选摘要的
`reports/framework-release-audit.json` 和日志为准；文档本身不能替代门禁结果。

## C1–C8 接口定位

| 契约 | 公开入口与实现 |
| --- | --- |
| C1 | SDK ApplicationSpec / Actor / RunContext / ResourceScope / Harness；domain/models.py |
| C2 | SDK Model / ModelRequest / ModelResult / ModelCapability；models/capabilities.py；可选 HTTP 模型适配器 |
| C3 | SDK Tool / ToolSpec / ToolContext / ToolOutput / BusinessReceipt；dispatcher 与调用日志；attempt_id 与稳定 key |
| C4 | SDK AuthorizationRequest / ContextualPolicy；SQLiteApprovalLedger；SDK owner/approver 绑定与 live policy |
| C5 | SDK CheckpointStore / InvocationJournal / ApprovalLedger；SQLite 原子审批 admission；安全恢复矩阵 |
| C6 | SDK ContextProvider / ContextMaterial / ContextCompiler；来源/版本/范围/有效期检查；可选知识与记忆模块保持独立 |
| C7 | SDK Verifier / VerificationRequest / ToolEvidence；Coding 测试时序验收；电商完整回执验收 |
| C8 | 事件 v1、hash-chain、实际经过处理的模型输入、完整持久 observation、Step/Recorded Replay 兼容；作用域查询与删除 |

目录采用逐步迁移：已有中立模型/SQLite/MCP 模块仍在原路径，公开应用通过 SDK
使用契约。历史兼容 HTTP/Coding API 保留原行为；新身份与审批保证适用于新 SDK
控制路径。没有从外部配置任意 import 插件，也未引入 LangGraph 核心。

## 审查发现与修复

| ID | 等级 | 状态 | 处理与回归 |
| --- | --- | --- | --- |
| FW-001 | high | fixed | Coding 测试早于最后写入仍被接受；test_tests_before_last_write_are_stale |
| FW-002 | high | fixed | 审批恢复丢失先前验收证据；证据进入 checkpoint，Coding SDK 审批/测试用例 |
| FW-003 | high | fixed | 消费批准与 checkpoint 存在断裂窗口；同库原子提交与故障注入、冲突回滚、并发批准用例 |
| FW-004 | high | fixed | 调用结果/参数/声明未经恢复一致性校验；摘要、schema、参数与 effect 校验，未知副作用拒绝 |
| FW-005 | medium | fixed | 重复 run 在拒绝前追加 Trace；保存 red 证据与 test_duplicate_start_has_no_extra_trace_events |
| FW-006 | high | fixed | 活动 worker 与恢复/删除可能竞争；SDK 本地非阻塞锁与竞争测试 |
| FW-007 | medium | fixed | 最小 Coding 安装隐式依赖 YAML；按需加载 Skills 与独立 wheel 运行两应用 |
| FW-008 | high | fixed | model-visible 与其他持久副本可能保留不同秘密；统一 EvidenceTransform 与跨存储凭据回归 |
| FW-009 | medium | fixed | DB 删除后文件删除失败无法重试；tombstone 与幂等清理、unlink 故障回归 |
| FW-010 | medium | fixed | 工作区根路径替换会扩大新 Scope；可信工具上下文拒绝已改变的根路径 |
| FW-011 | medium | fixed | 已存在证据目录权限过宽；SDK 要求 owner-private state_dir |
| FW-012 | high | fixed | 可选依赖存在新公告；升级 multidict/PyJWT/pypdf/urllib3/Werkzeug，并保留逐包官方服务重审证据 |
| FW-013 | high | fixed | 产品回执版本可能过期、工单 ID 可能脱离账本；验收器使用可信上下文重新检查当前业务账本 |
| FW-014 | high | fixed | 默认 sdist 包含本地原始 ZIP/数据/报告；改为源代码、许可、文档与示例白名单，并检查实际构建成员 |

审查方式：维护者式自审、依赖方向检查、针对真实代码路径的故障测试与干净目录复验。
未声称获得独立外部安全认证。当前 release audit 对 blocker/high 的关闭仅限下面的
SDK 支持范围；旧增强版 FH-015–017 仍是旧计划的开放项。

## 接受的限制与后续工作

1. **单机 POSIX + 本地 SQLite**。无 Windows、分布式 lease/fencing、网络文件系统、
   通用 exactly-once 或其他数据库的跨组件原子保证；替换适配器须新增契约验证。
2. **宿主身份与可信代码**。宿主负责认证、实时撤权、注册实现、领域数据访问。
   HTTP callback 和 Python RunContext 不是通用身份提供者。恶意注册插件不在隔离边界内。
3. **Coding 执行边界**。文件工具检查路径/版本；测试命令属于可信操作者代码，
   不是 OS 沙箱。不承诺抵抗恶意测试进程或敌对并发文件替换；处理不可信仓库须先提供
   宿主系统沙箱，不能将 cwd 或 sanitized env 当作沙箱。
4. **隐私与保留**。默认遮盖仅识别常见凭据；领域 PII、外部附件/账本/备份和 SSD
   安全擦除由宿主管理。删除运行证据后不能恢复或完整回放。
5. **证据强度**。内部 hash-chain 没有独立终态 anchor；不能证明没有截尾/整体替换。
   未新增真实模型成功率/成本或泛化主张；本次数据为确定性框架回归。
6. **业务范围**。电商仅有商品查询与模拟人工作单。支付、订单变更、内容运营、视频、
   员工训练和正式渠道接入属于后续电商计划。旧知识平台不因 SDK 示范通过而完成。
7. **可选依赖**。全 extra 的已知漏洞与接受项以当次依赖审计记录为准；旧 NLTK
   advisory（FH-019）只影响可选对照实现，不能据此放宽核心依赖门禁。

运行：`uv run --no-sync python examples/sdk_demo.py`。
开发接入：`docs/SDK_GUIDE.md`。所有后续行为变化继续补测试、ADR 和冻结候选复验。
