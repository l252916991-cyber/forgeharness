# 应用产品化执行记录：W0 + W1

日期：2026-10-28。本轮执行的是 [应用产品化计划](../../docs/archive/APPLICATION_PRODUCT_PLAN_2026-10-28.md)
的 W0（范围与验收）与 W1（身份、角色与对象级权限）。真实模型、工作台界面、工单写入、
部署与试用仍属后续工作包，未在本轮落地。

## 1. 本轮产物

| 类型 | 路径 | 说明 |
| --- | --- | --- |
| 计划书 | `docs/archive/APPLICATION_PRODUCT_PLAN_2026-10-28.md` | A/B 交付计划，C 阶段条件触发；不含求职材料部分 |
| 范围 | `reports/application-20261028/SCOPE.md` | 应用级范围、外部依赖与表述上限 |
| 权限矩阵 | `reports/application-20261028/permission-matrix.json` | 与代码常量一致的角色→权限、分离规则、负向用例 |
| 验收契约 | `reports/application-20261028/acceptance-contract.json` | A/B 验收项与硬性安全阈值 |
| 身份层 | `src/forgeharness/applications/commerce/identity.py` | 账户、成员、邀请、令牌、绑定、审计、对象级检查 |
| HTTP 层 | `src/forgeharness/applications/commerce/identity_http.py` | 登录/退出/邀请/审批路由；owner 与 approver 依赖 |
| 设计文档 | `docs/guides/APPLICATION_IDENTITY.md` | 数据模型、流程、部署要求、已知限制 |
| 决策记录 | `docs/adr/038-application-identity-boundary.md` | 为什么身份自持、为什么审批独立通道 |
| 回归测试 | `tests/unit/test_commerce_identity.py` | 22 项 |

## 2. 解决的问题（对应计划第 3 节 G1/G2/G4 中的 G1、G2）

| 现象 | 本轮处理 |
| --- | --- |
| 角色字符串充当 `actor.id`，同角色多人无法区分 | `user:<uuid>` 稳定主体；两名同角色用户测试断言 actor 不同 |
| 请求头 `Bearer <role>` 自证身份 | 改为密码登录 → 服务端会话 → 每次请求重读账户与成员关系 |
| 撤销/禁用不即时 | 禁用与撤权在下一次请求返回 401，测试覆盖 |
| 审批可能是同一身份 | 独立审批令牌通道；兼任角色仍不能自批 |
| 对象 ID 被当作凭证 | 会话绑定表 + 工单对象级检查；越权返回与不存在相同的响应 |

未处理（后续工作包）：浏览器每次点击生成新 `request_id`（G4，W6）、工单写接口与
`requester_id`（G6，W5）、真实模型（G5，W3）、知识管理界面（G7，W4）。

## 3. 实现要点

- 令牌只存 SHA-256 摘要；密码 `scrypt`（n=2¹⁴）加盐，`hmac.compare_digest` 比较。
- 未知邮箱同样执行一次派生，登录失败文案统一，减少账户枚举。
- 失败计数写在独立事务中提交：认证失败抛错时不会被回滚吞掉（实现过程中由
  “节流不生效”测试发现并修正）。
- 审批上下文确定性绑定：`run_id=approver-<user>`，否则 SDK 的
  `grant.approver_context != approver` 校验会在续跑时拒绝（同由端到端测试发现）。
- 会话标识只对具备 `consult` 的身份生效；支持与审批身份不会误抓会话头。
- 引用应用只使用 `forgeharness.sdk` 公共契约（`test_sdk_architecture.py` 强制）。

## 4. 验证

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 身份回归 | `uv run --locked pytest tests/unit/test_commerce_identity.py -q` | 22 项全通过 |
| 架构约束 | `uv run --locked pytest tests/unit/test_sdk_architecture.py -q` | 5 项全通过 |
| 单元全量 | `uv run --locked pytest tests/unit -q` | 1359 项全通过（基线 1337 + 新增 22） |
| 静态检查 | `uv run --locked ruff check src tests examples` | 通过 |
| 类型检查 | `uv run --locked mypy` | 133 个源文件无问题 |

未执行：集成测试套件（需要独立 3.12 环境与 `LAW_REVIEW_FORGE_PYTHON`）、真实模型评估、
浏览器端到端、部署与恢复演练。以上不得表述为已通过。

## 5. 版本与冻结说明

本目录无 Git 元数据，沿用逐文件摘要的 archive 冻结方式，本轮不产生提交号，
也不更新既有 candidate-r1 身份（该候选只认证其冻结文件集）。后续 W9 候选冻结需重新
生成文件清单与摘要。

## 6. 下一步

1. W2：按 `application-bindings.json` 的字段契约导入首批商品与 FAQ，保留来源版本与撤回。
2. W3：接入一个真实模型，先走“模型选工具、系统给事实”的路径，输出契约与失败降级分开评估。
3. W4：把登录页与咨询工作台接到本轮的身份依赖上，替换演示角色入口。
4. W5：工单写接口（领取/解决/取消）与 `requester_id`、乐观锁。
