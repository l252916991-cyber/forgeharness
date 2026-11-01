# 请求预算预检与电商只读接入：最终执行结果

2026-10-17。**本轮两项增强及最终审查修复已通过工程验收。**
真实业务、生产部署与整个框架的完成状态没有因此自动通过。

## 已交付

1. **完整模型请求预算预检。** 执行循环和规划阶段都计算完整序列化输入、工具定义、请求选项、声明的附加开销及输出预留；新工具与后续步骤重新计算。超限在调用前拒绝，保留必要消息和完整工具轮次，最多三次本地准备，不扩大限额。
2. **不可变请求与证据。** 保留旧 Model 接口，增加可选 PreparingModel；内置 chat-completions 适配器发送预检过的相同字节。trace 记录载荷、摘要、明细和未知项，HTTP mock 对照实际 body；凭据检查先解码 JSON。
3. **聚焦的电商只读入口。** `commerce_readonly_application` 只开放单商品读取，复用 tenant/resource 策略与台账结果验收，要求宿主当前授权。新增 `ApplicationSpec.authorize_run` 是可选控制入口，新工厂显式启用，查询后撤权能阻止下一次模型调用；重建后 inspect 也重新验权。
4. **重复调用编号缺陷修复。** 实际复现了旧运行时因重复模型 call ID 抛 JournalError、留下 running 的问题。现在拒绝该模型动作并持久化 failed，工具仍只执行一次。工具内部正常重试/恢复继续复用原逻辑编号。

没有新增运行依赖或数据库 schema，没有接入真实店铺、真实模型或付费调用。

## 验收证据

冻结身份：`1312bcf11a8ad36028e718db9f4e5803af729cc26f20a70640451ca1069f3c5f`，378 个文件。

| 项目 | 最终结果 |
| --- | --- |
| 源码全量测试 | 1044 通过，无失败/错误/跳过 |
| 干净副本全量测试 | 1044 通过，无失败/错误/跳过 |
| 纯分支覆盖 | 两侧均为 86.934% |
| 完整门禁 | 87 项全部通过 |
| 锁定版本三方集成 | 两侧各 33 个组合场景通过；不是完整原生故障矩阵 |
| wheel/sdist 与最小安装 | 校验通过，七条演示均通过 |
| 依赖审计 | 核心无新增依赖；原可选 NLTK 例外保留，未新增例外 |
| 冻结一致性 | 门禁结束及最终审计时源码仍匹配冻结 |

[最终审计](request-preflight-2026-10-17/audit.json)、
[完整验证摘要](request-preflight-2026-10-17/verification/summary.json)、
[不可覆盖尝试](request-preflight-2026-10-17/verification/runs/20261017T075541Z-aa788289/summary.json)。

首个候选 `098b51cf...` 的 1043 测试/87 门禁通过记录保留在 archive/ 及原 attempt 中；
补重复编号修复后重新冻结和重跑，不沿用首次报告。旧缺陷的
[修复前观察](request-preflight-2026-10-17/legacy-duplicate-id-observation.json) 与
[修复后结果及 trace](request-preflight-2026-10-17/duplicate-id-resolution.json) 均保留。
历史观察中的暂时接受状态不代表最终候选仍保留该缺陷；最终状态为 fixed_with_regression。

## 使用入口

```bash
uv run --no-sync python examples/commerce_readonly_demo.py
```

接入说明见 [COMMERCE_READONLY](../docs/guides/COMMERCE_READONLY.md)，预算配置和边界见
[REQUEST_PREFLIGHT](../docs/guides/REQUEST_PREFLIGHT.md)。架构与审查记录为 ADR 020/021 和
[REQUEST_PREFLIGHT_AUDIT](../docs/review/REQUEST_PREFLIGHT_AUDIT.md)。

同一源码复验：

```bash
uv run --no-sync python reports/request-preflight-2026-10-17/verify_candidate.py
```

## 精确边界

- 默认 token 计数仍是字符启发式，不是实际 tokenizer 上界或真实费用。窗口/内部开销仅为宿主和适配器声明；未知项保留未知。
- generic Model 只覆盖公共 ModelRequest；内置 chat-completions 覆盖实际发送的 body。提供商内部包装、旧独立 RAG/chat、传输重试计量不在本轮范围。
- 输出预留不自动设置远端生成上限，累计实际用量仍单独计量。三次准备可能保守拒绝，序列化有 CPU/内存成本。
- `authorize_run` 对旧应用默认不开启；它不替代工具策略、业务原子授权、来源验证与宿主认证。
- 电商入口是合成台账上的单次读取，`product_lookup` 不是带来源撤回保护的多轮历史工具。多轮需另行使用声明 governed_sources 的工具、正确回执和匹配的来源验证器，不能直接把本例升级成该能力。
- 未做独立接入工时测试、真实自然语言效果、真实店铺、生产认证或分布式恢复；未打开业务封存集。FH-015–FH-018 继续按原记录开放，不声明原生框架优势。
