# SaaS 首批执行报告

日期：2026-10-28。结论：**0.10.0 首批本地工程候选 candidate-r3 已通过全部 112 项工程门禁，源码与干净副本各 1400/1400 项测试通过，无失败、错误或跳过。** 业务和 SaaS GA 资格仍为 false。

candidate-r1 因后续审查发现反向接管问题主动中断，candidate-r2 因依赖审计失败停止并修复，两次记录保留，不覆盖为通过。本报告不宣称 W0–W11 或完整 SaaS 已交付。

## 本批交付

1. 新建邀请制账户工作台，使用 commerce-workbench v1/policy1，包含管理员离线初始化、真实登录、邀请/接受邀请、咨询、历史、独立审批、授权工单与诊断入口。
2. 每次 HTTP、SessionService 操作、模型/工具调用和资料读取重验当前身份/权限；不同用户与工作空间保持绑定，凭据不写入 RunContext。
3. 修复已确认身份缺陷：旧角色快照仍可管理、审批登录绕过节流、跨空间凭据撤销、FastAPI 局部依赖解析失败、并发撤权导致 500、退出响应丢失删除 Cookie。
4. 新工作台状态具备应用绑定标记，停机备份清单绑定准确应用身份；不自动接管旧 consultation 状态。身份库和待审批续跑通过恢复回归。
5. 明确 SQLite→PG 的操作级事务契约、租约/epoch、unknown 预算/容量、迟到回执及证据对象提交边界；尚未实现分布式后端。
6. 新增安装后的 Web-extra 消费者验证，保留基础 wheel 的无 Web 依赖检查与旧工程门禁。
7. 公开角色 demo 的 build/web/CLI 均拒绝打开私有工作台状态；页面使用版本检查与请求取消处理换账户/会话的迟到响应，实际页面脚本 14 项回归通过。
8. 依赖审计发现 optional msgpack 1.2.2 的 CVE-2026-114613，将锁升级至 1.2.4 后重新运行新候选，无新增漏洞例外。

## 证据与边界

逐阶段状态：`EXECUTION.json`；初始基线/外部依赖：`BASELINE.json`；问题分类：`FINDINGS.json`。

本批验证不复用旧 0.9.2 的通过数字。已独立冻结源码、运行源码与干净副本门禁，并构建新发行物。SDK API v1 沿用；旧运行保留旧版本，不能因为版本更新就由新 wheel 接管。

| 验证 | 最终结果 |
| --- | --- |
| 工程门禁 | 112/112 |
| 源码/干净副本全量测试 | 各 1400/1400，无跳过 |
| 纯分支覆盖率 | 两侧均 85.42%，门槛 85% |
| 业务安全选择 | 78 个节点展开为 86 项，全部通过 |
| 页面脚本异步竞态与幂等 | 两侧各 14 场景通过，模拟 DOM/fetch |
| 合成业务契约 | 120 项通过，measurement_valid=false、business_qualified=false |
| 框架组合验证 | 两侧各 33 项通过，不证明原生核心优势 |
| 控制/Agent 开发集/R2/并行故障 | 全部通过 |
| 核心/全 extras/比较环境审计 | 通过，只沿用历史 NLTK 例外 |
| wheel/sdist 成员与摘要 | 通过 |
| 基础 wheel 无 Web 依赖安装 | 通过 |
| 独立 Web-extra 消费者的工作台/CLI | 通过 |
| 最终源码未变 | 通过 |

候选源码身份：`8470f6cd90becaad8bb561ab76508ed2b8d67b30370827281e6efe1135adc194`。
wheel SHA-256：`6c93763dcdba23b2b5b75f1c2708df1ffea8c47baafa91c23f5e04f65350cce0`。
sdist SHA-256：`40133cedc7383b6f2cadaa5fb1a2a719269a36d70a3a5c7d79df40a39a02f35c`。

权威证据：[candidate-r3 验证摘要](candidate-r3/verification/summary.json)、[候选源码清单](candidate-r3/source.json)、[审查与残余风险](AUDIT.md)、[应用与发行物绑定](BINDINGS.json)。

工作台仍使用确定性模型和显式合成目录，默认回环监听，账户存储为单机 SQLite。OIDC、平台 org 模型、PG 集群、多节点调度、真实业务评估、计费和 GA 运维尚未完成。

## 后续阻塞与可执行入口

本机 Docker daemon 不可连接，未自动启动原有商家服务。随后在独立 uv 工具环境中以 pgserver 0.1.4 启动临时 PostgreSQL 16.2，SHOW server_version 成功，SHOW listen_addresses 为空（仅 Unix socket），探测后关闭服务并清理合成状态。该旧二进制仅用于隔离可行性探测，未加入依赖或生产配置。[上游工具来源](https://github.com/orm011/pgserver)

W2 首个 PG 原型仍需锁定正式测试版本、编写后端并执行真实事务/多进程/RLS 矩阵；本次服务端探测不证明这些已实现。

用户已确认中国大陆为主、约 10 名使用者、月度云服务与模型总预算 1000 元人民币，见[试点范围](../../../docs/archive/SAAS_PILOT_SCOPE_2026-10-28.md)。云/区域、真实数据、模型、开发运维负责人和商业收款配置仍待定版。

- [工作台启动指南](../../../docs/guides/COMMERCE_WORKBENCH.md)
- [SaaS 总计划](../../../docs/archive/SAAS_PRODUCTION_MASTER_PLAN_V3_2026-10-28.md)
- [状态事务契约](../../../docs/guides/SAAS_STATE_CONTRACT.md)
- [ADR 039](../../../docs/adr/039-saas-host-and-state-transition.md)
