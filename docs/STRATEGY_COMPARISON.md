# 三组策略对照：当前协议与运行方法

2026-10-26。开发题 12，评价题 30（parallel/chain/error 各 10），正式每组每题 3 次，
共 270 次。最新基线、正式采样状态与结果见
`reports/improvement-20261026/REPORT.md`，运行记录在 `qwen38-27b/` 子目录。
历史 9B HTTP 507 批次保留为基础设施诊断；27B 首轮开发发现 final-only 节点仍
暴露自动工具选择，已按 ADR 033 修复并保留中断记录。不得将这些批次当作正式结果。
preflight 在动态内存不足时保存诊断并以退出码 2 停止。

| 组 | 实际接线 | 模型调用与资料 |
|---|---|---|
| A single | `Harness` ReAct | 自行调用 `read_material`，最终结构化回答 |
| B fixed | 现有 `RunKernel` 自定义只读编排 | 按冻结顺序读取所有资料，一次模型综合 |
| C multi | 同一 kernel 的三角色编排 | planner 决定资料读取顺序；worker 综合；reviewer 复核。三个角色全部记账 |

三组使用相同已注册工具、权限、资料和最终外部 grader。A 向模型暴露工具 schema；
B 的综合节点、C 的 planner/worker/reviewer 只接收文本，因此显式省略工具 schema；
工具由编排确定性调用，省略目录不改变权限。预算为 12 模型决策/8 工具调用、24,000 input / 3,072
output tokens、单调用 256 output、120 秒准入时限。实际消耗单独统计，预算相同不表示实际消耗相同。
当前 kernel 串行调用，不能用此实现证明并行延迟优势。C 的 planner 只规划材料顺序，不进行自由
任务拆解；多角色结论限于这一种配置。

这些是开发诊断微任务：权限、事务、调用账本、版本、依赖、错误恢复等规范推演。
每题有独立材料和可检查 JSON 目标，不执行模型生成代码。它们不是实际仓库修复任务，
不能替代计划中更广的真实开发/业务任务实验。开发与评价题采用不同题目模板；作者在构建时
见过全部题目，不称独立未见 holdout。未安排适当的独立人工质量评分，外推范围必须受限。

源码冻结后，先运行无需模型的接线检查，再完成实际本机开发基线：

```bash
uv run --no-sync python -m benchmarks.strategy_comparison --mode contract --output reports/strategy-contract-new
uv run --no-sync python -m benchmarks.strategy_comparison --mode local --output reports/strategy-dev-new
```

本地模式只访问 `127.0.0.1:8000`、禁用代理和付费凭据；使用已有
`Qwen3.8-27B-oQ4e-mtp`、温度 0（运行命令应显式传 `--model Qwen3.8-27B-oQ4e-mtp`）。
其来源为 `Jundot/Qwen3.8-27B-oQ4e-mtp`，revision
`b47cb89d5da40c67a334403330a826c7591fbc09`；17 个文件已逐项核对固定 revision 的
SHA-256 / Git blob 摘要，证据见 `reports/improvement-20261026/model-download-qwen38-27b/`。
本次关闭 thinking 和 MTP，单调用 max_tokens=256。检查 oMLX 当前内存准入上限，
保留模型文件、本机版本/配置摘要及所有 prepared request 与 response trace。
正式样本在同一机器、服务配置、模型文件下完成；runner 协议中的上游验证标志
只覆盖 runner 自身动作，额外 revision 核验由独立下载清单提供。

获得完整有效开发基线后，审查资源估算和下面的阈值，再封存：

```bash
uv run --no-sync python -m benchmarks.strategy_comparison --seal --dev-report reports/strategy-dev-new --protocol reports/strategy-protocol-new.json
uv run --no-sync python -m benchmarks.strategy_comparison --mode local --split evaluation --samples 3 --protocol reports/strategy-protocol-new.json --output reports/strategy-evaluation-new
```

协议绑定 runner/题集/预算/源码/模型文件身份和预期 270 次；不覆盖旧输出或协议。
若缩小样本，需要在正式采样前更改协议和分析限制；当前工具只支持完整 30×3×3，不中途挑样。
任何中断均保留逐次 result/trace，不将缺失运行当作完成。基础设施故障使整次测量无效，
已发生失败仍在分母中。每题三次按 task 聚类 bootstrap 95% 区间，不当作 90 个独立业务任务。
全部运行进入 p50/p95、实际 tokens、每成功任务 tokens、重复调用与人工介入率。
本地 API 无付费，电力/硬件未计价；脚本模型 tokens 明确 synthetic，不进入成本指标。

阈值草案由 seal 写入协议：质量提升需 C 相对 A、B 的配对 task 聚类区间下界均大于零；
每成功任务 token 消耗不得高于任一基线的 1.20 倍。按任务族的延迟例外要求配对质量下界
不低于 -0.05，且 p50 不超过两基线各自的 0.80 倍；当前串行实现不预期获得并行收益。
质量不明确且资源增加时默认 A/B。每族仅 10 个作者自建微任务、无多重比较校正，结论仅探索性。
最终阈值仍须在有效开发基线后、正式终评前审核封存；不得看到正式结果后更换规则。

此对照只检验编排策略，不比较框架原生核心。原生比较继续沿用
`evals/comparison/protocol.json`；完整故障对照、独立接入与真实业务终评继续未完成。
