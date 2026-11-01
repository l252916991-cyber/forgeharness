# 0.5.0 本地模拟候选使用说明

> 2026-10-27 产品化工作树更新：`commerce-consultation` application/policy v2 已增加
> 无商品匹配时的显式人工转接。该请求仍需独立 supervisor 审批，按 tenant/request_id
> 去重并保留可信工单回执。此工作树尚未冻结或完成新候选门禁，下文 0.5.0 证据不认证
> 新行为；设计见 [ADR 037](../adr/037-general-commerce-handoff.md)。

2026-10-17 新增的单商品只读接入见 [COMMERCE_READONLY](COMMERCE_READONLY.md)，
完整请求预算见 [REQUEST_PREFLIGHT](REQUEST_PREFLIGHT.md)。这两个增强使用独立的新候选门禁，
不沿用下文旧 0.5.0 冻结的通过报告。

本阶段执行 `reports/MODULAR_NEXT_PHASE_PLAN.md` 的 S2–S5 本地部分，并修复 S1
来源撤回、失败删除和历史验证问题。是否通过完整门禁，以
`reports/business-release-audit.json` 和其绑定的冻结清单为准。

## 可以运行的链路

```bash
uv sync --extra dev --extra cli --extra platform --extra benchmark --extra frameworks --frozen
uv run --no-sync python examples/import_tasks_demo.py
uv run --no-sync python examples/legal_pilot_demo.py
uv run --no-sync python examples/commerce_demo.py
uv run --no-sync python examples/commerce_demo.py --serve
```

浏览器打开 `http://127.0.0.1:8765/commerce/console`。访问凭据使用公开的演示角色名：
`customer` 查询；`reviewer` 审核；`publisher` 发布；`supervisor` 批准转接；
`support` 查看工单。它们是合成演示值，不能用于生产认证。服务只监听回环地址。
页面凭据不写入浏览器存储；真实宿主必须替换认证回调，并对 cookie 认证实施 CSRF 保护。

- 比较：SKU 输入 `SYN-NB-001,SYN-NB-002`；提交后显示两件商品的价格、库存和查询时点。
- 追问：清空 SKU，选择“沿用上一轮商品”，重新提交。后台重新查询台账。
- 冲突：选定 `SYN-NB-001`，FAQ 输入 `faq-01`；声明冲突的知识触发待转接。
- 人工：选定商品并选择“请求人工”；出现待审批轮次后，切换到 `supervisor`，
  点击审批并继续。工单内容包括问题及来源锚点；相同业务请求不会创建第二个工单。
- 没有商品证据时返回 `needs_human/no_evidence`；用户显式选择“请求人工”后可进入
  独立审批并建立通用工单。页面使用结构化选择和确定性模型，
  不承诺自由自然语言理解、开放式推荐或自动澄清质量。

目录含 36 个合成 SKU（演示商家 30、隔离商家 6）与 24 份 FAQ（20＋4）。
价格库存读取模拟实时台账，FAQ 读取审核发布的 RevisionStore。
资料撤回或版本改变后，依赖它的历史拒绝读取；应新建会话，不能用旧价格历史继续推断。

## 公共接口

`forgeharness.sdk` 导出 `ImportService`、`MaterialParser`、`MaterialValidator`、
`ParsedDocument`、`ParsedSpan`、`RetentionPolicy`、`AuthorizedIndex`、
`TaskStore`、`TaskRunner` 及其结果/错误类型。

导入链为暂存 → 解析/校验 → 草稿 → 独立审核 → 发布 → 索引刷新。
解析器与验证器由宿主提供；例子只处理 UTF-8 合成文本，不宣称 OCR 或扫描件质量。
索引按 application、tenant、resources 限定候选，动态验权后评分，返回前再次核对版本。

可选 `sdk.task_control.task_router` 提供任务列表、详情、取消和状态页。
它与 `sdk.session_control.session_router` 一样，每次操作调用宿主认证函数；
核心 SDK 不导入 FastAPI。宿主创建/运行任务，不接受浏览器声明副作用类别。
取消尚未开始的任务可立即生效；正在执行的工作保留实际结果，不声称回滚。
中断任务需宿主核对后进入终态或有预算的重试。删除后的任务 ID 和业务键不能复用。

暂存身份绑定 application、tenant、actor 和内容摘要，各身份文件独立。
清理只处理当前授权的租约；所有活动导入仍保护其暂存文件。
任务结果不应携带资料全文；记录、索引、会话证据与宿主备份有独立生命周期。
全局孤儿文件回收、分布式任务调度及外部写入 outbox 仍未提供。

## 法律适配、笔记与回退

`SyntheticCaseAdapter` 提供两个合成案件的案件/页级读取和字面检索；页级授权在搜索前生效。
问答工具只返回当前发布且与适配器原文一致的页，引用保存版本、摘要和精确范围。
法律演示在同一应用范围内把引用页面交给 `legal_review_workflow`，完成执行审批后
保存 `draft` 笔记；它没有内容审核人，需独立审核与发布。

`reports/legal-pilot-comparison.json` 的对照对象是合成适配器的原始页/命中范围。
**没有运行 LexVault，也没有完成真实流程同题对照。** 精确引用验证不验证法律推理。
实际接入须用原系统的当前身份、页级权限、稳定页版本与撤回信号实现 CaseAdapter，
再使用独立数据和专业评审校准。尚未修改任何外部仓库；回退时停止此独立服务即可，
原系统可继续运行。本阶段没有旧数据库自动迁移。

## 评测与发布边界

`uv run --no-sync python -m benchmarks.business_evaluation` 执行 40 法律和 80 电商
开发契约场景。它们共享少量合成资料族；场景数不代表独立业务题目数。
报告强制 `measurement_valid=false`、`business_qualified=false`；语义正确性、
专业复核、真实 token 和真实模型时延均没有结果。记录的 p50/p95 包含本地资料装载，
只供确定性回归观察，不是服务 SLO。

`evals/business/safety.json` 独立列出至少 40 项权限、审批、注入、撤回、取消和恢复回归。
`data/sealed/commerce-final.synthetic.json` 为另行生成的 60 个合成终评夹具，
元数据位于 `evals/business/sealed-metadata.json`，开发运行器不读取它。
这不是第三方独立命题集，尚未用于终评；专业业务终评仍需真实模型、独立资料与评审。
目标与评分模板见 `evals/business/quality-targets.json`。正式打开前必须创建不可覆盖的
`business_seal.Targets` 声明，绑定模型、源码、数据摘要、评分版本及评审人。
封存消费标记先于解码和执行落盘；失败也不能重跑同一封存集以挑选结果。

候选门禁：`uv run --no-sync python -m benchmarks.business_verification --freeze`。
已冻结后省略 `--freeze`；源码变化会拒绝继续，不能沿用旧通过报告。
门禁包括源码/独立副本的完整质量检查、开发场景、安全用例、依赖审计、
wheel/sdist 文件哈希与最小安装演示。封存正文不进入默认评测或发布 sdist。
真实模型终评、自然语言澄清、专业法律判断和生产部署仍未交付。
