# 0.12.0 基线 → 0.13.0 控制面候选 r4 变更清单

比较对象是两个冻结源码清单。当前目录没有 Git 元数据，因此本清单使用路径与 SHA-256 比较，不冒充 Git diff。完整逐文件旧/新摘要见 [CHANGES.json](CHANGES.json)。r3 原清单已按原字节保留为 [CHANGES-r3.json](CHANGES-r3.json) 与 [CHANGES-r3.md](CHANGES-r3.md)；r3 的逐文件摘要以归档 JSON 为准。

- 基线：584 个文件，`22bec79d6b7f187bb5798bc59e18690ba9e03add729921163cedad8cb42cac24`。
- 候选 r4：609 个文件，`05bf6eaa6817100d22e1ddac760a05547a78c693a09fb1ceeb1eac7d69e6b984`。
- 新增 **25**、修改 **18**、删除 **0**；另有 **566** 个文件摘要相同。

## 按功能查看

| 功能 | 主要文件 | 已实现范围 |
| --- | --- | --- |
| 身份、组织与显式迁移 | `cloud/identity.py`、`identity_models.py`、`migrations/003-control-plane.sql`、`provision.py` | PG 邀请制账户、会话、组织/空间/部署授权、审计及独立最小权限角色；schema2→3 显式升级。 |
| 当前授权与异步执行 | `cloud/control.py`、`worker.py`；修改 `store.py`、`calls.py`、`execution_calls.py`、`execution_state.py`、`runtime.py` | 原子受理绑定、发送前权威重验、串行 worker、独立批准/拒绝、分页与额度限制；已发送事实继续结算；锁等待后使用新的 checkpoint 快照。 |
| HTTP 与浏览器工作台 | `cloud/control_api.py`、`workbench.py`、`cloud/assets/workbench.{html,js,css}` | 登录、邀请、组织切换、任务与审批、成员和授权管理；严格输入、Cookie/CSRF/Origin/Host、最小 DTO 与错误归一化。敏感表单显式同源 POST，JS 禁用或资源加载失败时安全拒绝且秘密不进入 URL。 |
| 回归与独立消费者 | `evals/saas_postgres/tests/test_control_{identity,runtime,snapshot}.py`、`evals/saas_control_plane/`、`tests/unit/test_control_api.py`、`test_control_plane_verification.py`；修改迁移测试 | 真实 PG、真实 HTTP、锁等待快照竞态、安装 wheel、双应用及真实浏览器验证；包含 disabled-JS/blocked-JS 原生表单安全回归。源码与干净副本完整门禁由独立报告记录。 |
| 架构合同与能力边界 | `docs/adr/046-invitation-control-plane.md`、`docs/CLOUD_CONTROL_PLANE.md`；修改 `README.md`、`docs/CURRENT_CAPABILITIES.md`、`docs/STATUS.md` | 权限矩阵、API/部署说明、已实现与后续范围、审批有效期与其他接受风险。 |
| 版本、发行与质量接线 | `pyproject.toml`、`uv.lock`、`Makefile`、`benchmarks/control_plane_{postgres,verification}.py`、`benchmarks/saas_postgres.py`、comparison 驱动与新协议 | 0.13.0 候选构建、迁移/资产打包、完整验证驱动与新协议绑定。 |
| 终止/拒绝候选的发行保护 | 修改 `benchmarks/release_artifacts.py` 与 `tests/unit/test_release_artifacts.py` | 发行检查显式拒绝存在 `ABORTED.json` 或 `REJECTED.json` 的候选；不依赖旧通过摘要覆盖终止/拒绝事实，包含回归。 |

上述 `cloud/` 路径均位于 `src/forgeharness/cloud/`。完整 **43** 个变更路径及旧/新 SHA-256 均保存在 JSON，不包含历史报告或生成的验证目录。

## 历史基线与迁移保护核对

旧清单的文件映射重新计算后，仍等于既有冻结身份 `22bec79d6b7f187bb5798bc59e18690ba9e03add729921163cedad8cb42cac24`。其 JSON 文件 SHA-256 为 `0754cbf1112664a55a215262b84b3664d9e8670f40ae9cb34844b0d4d2326df6`，与旧资格报告原先记录的 `candidate_manifest_sha256` 完全相同，因此历史候选清单未被改写。

| 保护文件 | 基线、r4 与当前源码共同 SHA-256 |
| --- | --- |
| `src/forgeharness/cloud/schema.sql` | `c8589445b9d841ce43e8e14fc8cec343c28454fed08dbab0ae46d90984ef8ac0` |
| `src/forgeharness/cloud/migrations/002-execution-evidence.sql` | `0b1ad7d6157c46ac778768eb735fd23401a797080d59396cdece9463a0123b13` |

两份清单各自的摘要均已重算验证；当前源码与 r4 的 609 项清单完全一致。本次仅在 reports 下归档 r3 清单并刷新主清单，未修改源码、冻结清单或历史资格报告。本清单说明变更范围；候选资格与最终门禁结果以 r4 独立验证报告为准。
