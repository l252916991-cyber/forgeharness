# 当前状态复查与改进建议

复查日期：2026-10-14（Asia/Shanghai）。本次检查现有候选，不修改实现，不替换历史审计。

## 结论

0.4.0 已落地计划首轮 S0＋S1，并保留完整的本地候选验证证据；S2–S5 尚未交付。
本次额外抽查确认了来源撤回和删除恢复两项缺口。因此，历史报告的 `qualified: true`
只能说明当时冻结门禁通过；当前建议为**先修复并重新验收，再进入真实资料接入**。

## 当前证据

- 当前冻结范围 270 文件逐项匹配，源码摘要：
  `497f07c6483047fe4d8fbf5ee8627a68e9f209900dd071085c77b84c5578f2b8`。
- 原审计列出的 8 个证据哈希、wheel/sdist 哈希、最新 summary 与该轮副本一致；
  55 项命令/断言均通过且日志存在。源码和干净副本的原始 XML 均为
  699 测试、0 失败/错误/跳过；纯分支覆盖分别为 86.639% / 86.691%（阈值 85%）。
- 本次实际重跑：`uv run --no-sync pytest`，699 passed in 14.57s；
  Ruff lint、247 文件格式检查、mypy 100 源文件、`examples/session_demo.py` 全部通过。
  本次未重复依赖联网审计或完整干净环境门禁；上述 55 项为已核对的原候选证据。
- 已实现：持久会话、固定身份/资源绑定、稳定请求键、历史预算、审批与恢复、
  workflow 关联、可选 HTTP 路由及两个合成业务客户端。
- 未交付：S2 版本化导入与通用持久任务、S3 LexVault 接入、S4 完整电商 MVP、
  S5 真实业务终评。FH-015–018 仍开放。只支持单机 POSIX/SQLite 和可信宿主，
  合成测试不证明真实模型质量或生产业务可用性。

## 新发现

### STATE-001：工具来源没有纳入历史失效检查（high，已复现，待修复）

位置：`src/forgeharness/sdk/sessions.py:184`、`:383`；
`src/forgeharness/applications/records.py:104`。

复现使用现有公开接口：发布一个合成 record → 将 `record_application` 直接交给
SessionService → 提问成功 → `RevisionStore.withdraw` 撤回资料 → 查询会话历史和运行。

实测结果：

```json
{
  "status": "succeeded",
  "recorded_materials": 0,
  "published_read_denied": "RecordUnavailable",
  "history_after_withdrawal_contains_fact": true,
  "inspect_after_withdrawal_contains_fact": true
}
```

原因：只有 ContextProvider 的材料进入 `turn.materials`。工具读取的来源存在于
tool evidence/答案中，但 `_validate_turn` 只检查 materials、运行存在性和历史祖先。
工具应用没有 provider 时无需来源校验器，撤回后的内容仍可由会话返回或作为历史输入。
示例通过额外安装 provider 规避了一条固定来源路径，没有覆盖一般工具来源。

建议：定义工具结果来源依赖契约，持久化具体资源/版本/digest，并在历史读取、
模型输入、运行查询、审批和恢复前检查。可提供强制的宿主结果校验器作为接入边界；
不能仅要求文档提醒或以 `lambda: True` 代替安全保证。加入“工具读取→撤回/更新→
历史、下一轮输入、inspect/events、审批与恢复拒绝”的跨模块回归。

### STATE-002：无 checkpoint 的失败轮次无法完成删除（medium，已复现，待修复）

位置：`src/forgeharness/sdk/sessions.py:721`、`:754`。

复现：provider 在准备阶段抛错，轮次被记录为 failed，但 Harness 尚无 checkpoint。
依次调用 delete_turn、重试 delete_turn、delete(session)、重试 delete(session)，
四次均抛出 `ScopeError: run unavailable for this execution binding`。

原因：先写 deleted 墓碑，随后调用 controller.delete；代码仅在原状态为 starting
时容忍无 checkpoint。failed 或已写墓碑的 deleted 状态无法区分“从未开始”与
“待清理”，重试不能推进。会话删除遇到此轮次也无法继续清理其他轮次。

建议：持久化独立的执行登记/清理进度，以经过绑定校验的 nullable lookup 区分
从未执行、已删除和权限拒绝；保持墓碑先行，并使清理可重入。覆盖准备失败、登记后
崩溃、墓碑后崩溃、单轮重复删除、整会话删除与中断重试。

### STATE-003：历史校验开销不受 history_limit 限制（medium，部分实测）

位置：`src/forgeharness/sdk/sessions.py:383`。

`history_limit=1` 的 30 轮公开 API 实验中，读取最新轮次仍触发 30 次轮次查询及
30 次 controller.find。原因是每轮递归校验所有历史祖先；每次提交又在多处重复校验。
递归链还存在 Python 栈深限制风险，但本次未跑千轮，不把 RecursionError 当作已复现。

建议：使用迭代遍历、批量读取、单操作共享去重缓存，进一步设计扁平的来源依赖索引；
不能为降低成本直接截断祖先校验。增加 100/1000 轮和长 trace 的基准与资源上限。
删除祖先后后续轮次拒绝访问符合保守安全策略，应向客户端提供明确的新会话/重建提示。

## 建议推进顺序

1. 先以 STATE-001/002 建立失败回归，修复后重新冻结候选，更新审计并运行完整门禁。
2. 将构建、sdist/wheel 成员校验及最小安装示例加入 CI 的独立发布任务。
   当前 `.github/workflows/ci.yml` 调用 `make gate`，不包含这些本机独有的验证。
3. 清理 `docs/archive/STATUS.md` 当前结论与历史描述的冲突：retry 在第 102 节附近已实现，
   第 169 行又称未实现；holdout 第 138 行已运行，第 166/173 行又称未运行。
   保留一份当前状态表，历史移入明确日期的归档；不重写既有原始实验报告。
4. 推进 S2 的两个增量：先版本化资料“暂存→校验→审核→发布→索引”，再持久任务
   “状态→重启恢复→取消请求→不明结果核对”。先验证撤回、取消、重试与故障点，再接界面。
5. 同步准备两业务的授权开发资料、评分规则及独立终评资源，保持开发集与封存集隔离。
   目前无需提前增加多 Agent 运行机制、微服务或分布式调度。

当前目录没有 Git 元数据；内容哈希可验证快照，但日常变更审查、回退与 PR 流程仍应
在正式 Git 仓库中建立。验证工作副本/venv 当前约占 1.3 GB；可在保留清单、日志和
构建产物后制定清理策略，本次未删除任何历史证据。
