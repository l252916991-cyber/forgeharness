# ForgeHarness 0.13.0 内部 Alpha 控制面执行报告

开始日期：2026-11-01；完成日期：2026-11-02（Asia/Shanghai）。

结论：本轮执行的 CP0–CP7 已完成，candidate-r4 通过 **130/130 完整门禁**、独立审计和同一 wheel 的额外迁移审计，可称为“通用邀请制内部 Alpha 控制面工程合格”。已归档本地 wheel/sdist；没有公网发布或云部署。完整生产 SaaS 与真实客户业务仍属后续 S1–S7。

本报告执行 [下一阶段计划](../20261101-next-plan/PLAN.md)。原0.12.0候选、旧冻结清单和迁移001/002保留原字节；不沿用历史122项结果认证本轮源码。

## 交付范围

| 工作包 | 实际交付与证据 |
| --- | --- |
| CP0 | ADR046、主体/组织/workspace/deployment/grant合同、权限矩阵、DTO/错误、旧actor兼容与在途撤权语义 |
| CP1 | PG schema3、稳定账户、组织/工作空间成员、不可变部署授权、独立identity/web/runtime角色、显式2→3迁移 |
| CP2 | 离线bootstrap、一次性邀请、已有账户跨组织验证、登录/退出/组织切换、会话过期撤销、失败节流、认证与管理审计 |
| CP3 | 原子run/checkpoint/binding/outbox受理，准入与mark-sent事务内当前授权重验；撤权保留已发送事实及unknown预留 |
| CP4 | 严格异步HTTP、本人查询/取消、运行分页、独立精确批准/拒绝、串行PG扫描worker、退避/停机/指标 |
| CP5 | 浏览器邀请/登录/组织/目录/任务/状态/用量/取消/审批/成员与授权管理；权限决定可见操作 |
| CP6 | 双组织/同角色不同账户/独立审批人/双合成应用；真实HTTP、Chromium、独立进程故障、外部业务oracle与wheel安装消费 |
| CP7 | 交叉审计、发现分级与回归、四次独立候选记录、完整源码/干净副本复验、同构建发行物和精确残余边界 |

新增25、修改18、删除0，共43条变更路径；冻结源码609文件。因工作区无Git元数据，采用完整文件摘要清单固定checkout-equivalent状态。见[变更清单](CHANGES.md)、[逐文件摘要](CHANGES.json)和[历史冻结/当前源码复核](CHANGES-verification.json)。

接入与升级指南：[CLOUD_CONTROL_PLANE](../../../docs/guides/CLOUD_CONTROL_PLANE.md)；
设计：[ADR046](../../../docs/adr/046-invitation-control-plane.md)。

## 最终候选与验证

- 源码摘要：`05bf6eaa6817100d22e1ddac760a05547a78c693a09fb1ceeb1eac7d69e6b984`
- 冻结清单：[source.json](candidate-r4/source.json)
- 原始完整门禁：[summary.json](candidate-r4/verification/summary.json)
- 完整attempt：`20261101T165326Z-e2129352`
- 源码及干净副本均与冻结一致，所有门禁退出码为0，没有失败/错误/跳过。

| 验证项 | 源码 | 干净副本 |
| --- | ---: | ---: |
| 核心测试 | 2050通过 | 2050通过 |
| PostgreSQL状态/执行及新增合同 | 227通过 | 227通过 |
| PG＋真实TCP HTTP组合 | 234通过 | 234通过 |
| 综合覆盖率 | 91.8081% | 91.8081% |
| 纯分支覆盖率 | 85.3435% | 85.3435% |
| 合成业务合同 | 120通过 | 120通过 |
| 框架组合实验 | 33通过 | 33通过 |

227与234是包含重叠用例的独立门禁运行，不能相加当成独特测试数。两项覆盖门槛仍为85%，旧质量、类型、评测、依赖安全、安装和兼容检查均保留。框架实验不证明原生核心优势；合成业务合同不等于真实业务合格。

同一次构建的wheel在独立环境中实际安装并运行API、worker和Chromium156.0.8078.4：[consumer证据](candidate-r4/verification/runs/20261101T165326Z-e2129352/control-consumer.json)。

- 14项完整契约全过；API返回202后退出，独立worker仍发现运行。
- 两应用共享平台、账户/API/工作台/执行合同；同角色不同账户及伪造路径不能读取他人运行。
- 审批专用账户不能提交任务；兼任使用/审批权限的owner仍不能自审，独立会话审阅精确指纹。
- 批准/拒绝并发仅一项生效，拒绝持久化并收敛；撤权阻止未获准新发送。
- 业务写入后SIGSTOP/SIGCONT及SIGKILL两种真实进程故障，新worker的epoch从2升至3；旧worker恢复不重复业务效果或费用。独立oracle每项效果/dispatch仅一次。
- 禁用JavaScript及阻断脚本资源两种模式下，登录/邀请原生表单均POST；秘密不进入URL或错误响应。无脚本提交安全拒绝，不承诺无脚本登录。
- 模型/业务均为本机合成资料，外部业务/模型请求为0，测试PG均已停止。

## 额外独立迁移审计

[协议](migration-audit/protocol.json)、[独立消费者](migration-audit/consumer.py)、[r4最终结果](migration-audit/r4-final/summary.json)绑定同一wheel SHA，使用隔离Python `-I`，不导入仓库源码/tests/evals，并逐字节核对177个已安装package文件与wheel。

真实HostedRuntime生成全部11张非空schema2事实表，包含2个checkpoint、29条证据、3次execution call、1次logical拒绝、4个invocation、3个receipt、独立已消费approval及30条outbox。完整行快照、预算/用量、unknown的9单位预留、owner及inspect/trace在升级前后字节一致。中途DDL失败完整回滚，成功升级不创建账户或接管旧运行。

生产升级必须先盘点并实际停写，然后显式升级；安装schema3兼容的0.13运行时，并由离线迁移主体重新应用 `grant_runtime` 等最小权限授权。原0.12 wheel不能自动当作schema3兼容运行时。维护标记不是分布式停写锁。

独立消费者脚本SHA：`e5c0ae922b5a89aa878a5e14498d03db5a9e822228d888e833e1cef4487effd0`；
协议SHA：`358f0f30af3be20646aac8d5a4f620e8cedb68295f062c2de030c35d7aaa046d`。

## 审计、失败候选与修复

完整分类见[AUDIT](AUDIT.md)和[独立资格审查](QUALIFICATION_REVIEW.md)。所有确认的blocker/high已修复并有回归；没有接受的blocker/high残余。

| 候选 | 结果与处置 |
| --- | --- |
| r1 | 冻结后真实PG复现CP-12：等待run锁后JOIN可能读取旧checkpoint并接受旧指纹；中止，保留[ABORTED](candidate-r1/ABORTED.json) |
| r2 | 完整128/130；审批专用用户正确隐藏任务区，但浏览器helper错误等待任务区；保留[失败摘要](candidate-r2/verification/summary.json) |
| r3 | 原130/130全过，但最终真实无JS审查确认CP-15：默认GET表单把凭据放URL；另记[审计拒绝](candidate-r3/REJECTED.json)，不覆盖通过摘要 |
| r4 | 修复锁后新语句读取、POST表单及所有回归；130/130、同wheel完整浏览器/进程故障与独立迁移审计通过，最终接受 |

CP-12有真实PG锁等待回归；CP-15有[原始证明](cp15-nojs-probe/result.json)与[修复后证明](cp15-nojs-probe/fixed-result.json)，并纳入最终wheel两种脚本不可用回归。不能仅以正常流程或自动门禁通过替代安全审查。

发行工具也会强制拒绝任何 `REJECTED.json`/`ABORTED.json`候选，不能用旧的通过摘要越过审计。已在真实r3验证未生成发行目录：[拒绝保护证据](REJECTION_GUARD.json)。

## 已归档发行物

仅复制本次完整资格构建的精确字节，没有重新构建或发布到PyPI。

| 发行物 | SHA-256 |
| --- | --- |
| [wheel](release-0.13.0/forgeharness-0.13.0-py3-none-any.whl) | `31017b4afde8c1c547bd62129d5019d054101cdc54f70f0990534ca1bdb434a7` |
| [sdist](release-0.13.0/forgeharness-0.13.0.tar.gz) | `11deabd0f92721c19db2dbe2cdde5eefb928f232f656ba4b0f5acfcab690dacc` |

## 接受的残余边界

- 批准有效期五分钟。过期/审批人撤权后，原不可变决议不能换人续批；须取消并新建运行。无效grant不能发送，原运行按取消/期限收敛。这是中等级可用性限制。
- 一般管理元数据目录最多100项；运行/邀请分页。组织pending最多100、默认20，单worker循环并发1；多worker公平性/容量和队列SLO后续验收。
- 升级为离线停写操作，没有分布式维护锁或降级DDL；恢复旧版本需匹配备份与配置。组织停用后需要离线恢复。
- 本地scrypt邀请适配器没有生产IdP/MFA/恢复，也没有机器API Key完整生命周期；全局入口防滥用及资料/凭据保留生命周期仍在后续。
- 依赖审计保留原已接受 `PYSEC-2026-3740`（仅all-extras可选LlamaIndex比较路径，FH-019）；没有移除原审计门槛或把它当作零风险。
- 整数用量与提交上限不等于1000元/月人民币硬额度；供应商计价、月度预留和账单对账仍待S4。
- 本轮没有真实客户资料/真实模型费用，没有公网发布、已有服务器接入、云TLS/备份恢复/SLO观察或真实业务验收。

主要大陆地区、约10名使用者、已有服务器/模型及云服务与模型合计1000元/月约束保持。后续按计划S1–S7继续生产身份、执行容量、资料与密钥生命周期、人民币账务、受控部署及真实试点的独立验收。
