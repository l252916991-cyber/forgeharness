# 并行执行审查与风险记录

2026-10-27。Codex 实施自审；不是独立第三方或真实用户验收。
审查范围：执行与恢复协议、状态/预算、权限边界、trace、provider 容量、依赖、打包、
公开接入文档、简历/能力声明和性能实验口径。最终资格读取同一候选验证摘要。

## 开发发现与处理

| ID | 等级 | 发现 | 处理与回归 |
|---|---|---|---|
| PE-001 | high | 发送许可写入与协程真正进入适配器之间存在撤权窗口 | 增加只读发送边界 guard；明确未发送的拒绝按零消耗结算并释放容量；`test_resource_revoked_after_sending_permit_before_provider_entry` |
| PE-002 | high | 沿用完整授权 guard 提交在途结果，会在撤权后丢失已发生的用量 | 单独 owner/revision/identity fenced 记账，仍禁止新发送与对外披露；actor/resource 两类 `test_revocation_hides_output_but_settles_incurred_cost` |
| PE-003 | high | run 上限为 1 不足以保证共享 provider 上限为 2 时的模型实例安全 | 只要共享容量允许并发，就要求宿主并发安全声明；`test_policy_and_owner_validation` |
| PE-004 | high | owner 被 fencing 后若记账失败，也必须清理独立的物理容量记录 | 清理所有 worker 后分别处理已确认/未知容量，旧 owner 不写根账；`test_stale_owner_cannot_settle_but_releases_confirmed_physical_capacity` |
| PE-005 | high | 含有 partial usage 的普通 transport 异常不一定代表最终响应，不能释放根预留 | 仅已收到响应的规范协议错误接受最终已知用量；其余仍 unknown；`test_transport_error_partial_usage_does_not_settle_as_final` |
| PE-006 | medium | post-commit/outbox 故障后的重复结果必须幂等，冲突结果不能静默当作同一回执 | 相同 output/usage 只清理容量，冲突明确拒绝；对应 duplicate/post-publish 回归 |
| PE-007 | medium | provider 用量缺失/部分字段缺失可能被历史适配器的兼容默认值填零 | 新增可选严格 `require_usage`；并行真实评测显式启用；6 类缺失/非法 usage 回归；不偷偷改变旧配置的缺失字段兼容策略 |
| PE-008 | medium | clean-copy 验证的报告位置位于 clean 根目录外，会违反新 runner 的输出范围约束 | 在 clean 自身 reports 运行，再复制该批证据到候选目录；keyless runner 输出范围测试覆盖 |
| PE-009 | low | 新恢复测试以脚本运行时，`examples` 命名空间不在 sys.path | 子进程显式绑定本候选根目录 PYTHONPATH，不读其他 checkout；保留初次开发失败事实 |
| PE-010 | medium，发行阻断 | candidate-r1 的旧 `durable_demo.py` 仍硬编码 recovery 4，新 SDK 为 5；源码单测通过但发行示例失败 | 已修正为明确的 recovery 5，并补三进程可执行示例回归；candidate-r2 完整重验 |
| PE-011 | medium，发行阻断 | 原框架对照协议声明 SDK 0.8.6，使新 0.9.0 被正确分类为环境不匹配、未测，不能通过 33 项门禁 | 新增 `protocol-sdk090.json/.sha256`，保留旧协议；仅更新声明元数据，场景/阈值/其他框架版本保持一致；版本声明回归与 comparison-declaration-review.json 核对 |
| PE-012 | high，r2 门禁后的确认缺陷 | 不同状态目录中相同公开 run/node ID 的容量票据重名；一个目录的已完成节点可误释放另一个目录的未决容量 | 先复现失败，再绑定状态库路径+完整运行身份+node，持久复用 ID，模型 resolver 使用相同 ID；新增跨存储/相同上下文任务图回归、回执键回归和旧版本拒绝回归；SDK 0.9.1、ADR 036、candidate-r3 独立全量重验 |
| PE-013 | low，接受的旁路诊断限制 | HTTP 辅助观察器使用 Python 对象 id 作 key，串行 chain-07/sample2 的一个已完成 Request 地址被复用，辅助记录为 269 而非 270 | 不以该旁路字段计费或计算根耗时。独立逐根核对主 hash trace 的 model.request/model.action、状态用量：两组各 270 调用、85080 input/5199 output tokens，完整一致。根计时、grader 与并发峰值不受该已完成对象地址复用影响；保留原始记录并在 canonical-evidence-review.json 披露，不修改冻结源码或原始样本 |

PE-001–PE-012 已修复的行为均有回归或声明一致性证据；未以降低断言、扩大权限或
丢弃失败样本处理。candidate-r1 97/104、不合格；7 个失败门禁归因于 PE-010/PE-011。
candidate-r2 从新冻结独立重验，不会将 r1 通过的子项当作发行资格。
candidate-r2 后续虽 104/104 通过，但已被 PE-012 的确认缺陷取代，不能作为最终资格。
最终仅认证 SDK 0.9.1 的 candidate-r3；原 0.9.0 源码、wheel 与全部结果保留。
冻结候选后的发现如需修改实现，必须新候选、重新运行完整验证；不改旧清单。

## 已接受的实现边界

- 单机 POSIX/SQLite，一个 dispatcher 进程管理一个配置容量域；不同目录、域名或
  外部客户端调用不受同一 limiter 保护。跨主机/跨进程协调仍未实现。
- 宿主声明适配器支持并发并配合取消，是注册边界；无法强行终止任意阻塞 Python 或
  远端推理。不可取消的本地 worker 会阻止 dispatcher 正常关闭，需宿主处置进程。
- 未知发送会保守暂停整个 provider 域的新发送，优先防重复与资源超卖，牺牲可用性。
  容量发送标记早于 run 标记的崩溃窗口可能需要宿主提供后端已结束/空闲的管理证据。
- 物理容量确认与未知预算对账分开。没有可靠用量回执时，run 保持保守预留；清空容量
  或删除 run 不推导零成本。管理确认接口不作为 agent 工具或未认证路由暴露。
- 工具保持本 run 独占；没有并发写入、自主子运行、动态拆解、分布式 exactly-once 或 HA。
  全 SessionService/HTTP/UI 产品接线、真实身份提供者和业务闭环仍独立验收。
- 系统 vm_stat/swap 采样不是单个模型的 GPU 峰值计量。客户端请求重叠不是后端同时
  推理的证明。真实实验仅限本机单模型与作者自建合成库存题，不能外推业务质量。
- 默认并发仍为 1；目标配置满足预登记性能、质量与 tokens 条件后才推荐显式并发 2。
  无新增依赖豁免；历史可选 NLTK FH-019 是否继续可接受由本候选依赖审计复核。

## 历史 candidate-r1

`candidate-r1/source.json`：467 文件，源码身份
`03701f162f01ea48de6f3758c711e4fbb34dc2b1ea7ae06db6f68671e3895203`。
本文件与报告位于 reports，属于候选证据，不修改冻结源码输入集合。
该候选 97/104，不合格；不得用其通过子项认证新源码。

## 最终工程候选 candidate-r3

SDK 0.9.1，472 文件，源码身份：
`2adfdc8937a15e91026238a1b8bc836cadf0a2341a2dd313c3cc3f7c46879ad8`。
104/104 工程门禁通过；源码与干净副本各 1331 tests、0 failures/errors/skipped，
纯分支覆盖均 86.16149795201872%。wheel/sdist、最小安装及 10 个示例独立通过。
新旧模型调用身份边界已补回归；旧 0.9.0 wheel 的 PE-012 复现日志保留。

当前实现无未处理 blocker/high。已接受风险仍包括单 dispatcher 容量域、可信宿主
并发适配器声明、未知调用的保守停发和受限的真实评测外推范围。
P5 的性能/质量结论独立从正式报告读取，不由本工程资格推出。
PE-013 仅涉及辅助 HTTP 观察器；完整模型可见输入、规范结果与消耗均可从主 trace
重建，208 条运行 trace 全部通过 hash 校验。该限制已披露，不宣称旁路网络元数据无缺项。
