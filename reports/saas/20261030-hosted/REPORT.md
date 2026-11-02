# 托管串行执行与运行时执行报告

日期：2026-11-01。**0.12.0 candidate-r2 已通过全部122项工程门禁。**
源码身份：`22bec79d6b7f187bb5798bc59e18690ba9e03add729921163cedad8cb42cac24`。
本批托管串行执行与运行时工程资格通过；**完整生产级 SaaS 尚未完成**。

本批把现有 AgentRuntime 接入 PG 唯一状态权威：完整输入/资料/证据、独立精确审批、
规范工具调用、真实模型/工具发送、各维用量、完整结果和回执、取消与跨进程恢复。
两个应用共享同一执行入口，独立合成业务账本核对效果；真实回环 HTTP 核对实际发送
字节、预算、用量、缓存和响应资源上限。未知结果保留预留，不自动重发或猜零费用。

同时审查全部未发布功能，修复技能安装、文件/容器边界、待办恢复、缓存、可选依赖和
发布流程中的已确认缺陷，并补回归。实际超额仍保留成本与业务效果，收敛到 EXHAUSTED。

## 最终验证

| 验证 | 结果 |
| --- | --- |
| 完整工程门禁 | 122/122，通过 |
| 源码及干净副本核心测试 | 各1980通过；0失败/错误/跳过 |
| 源码及干净副本真实PostgreSQL18.6 | 各183通过；0失败/错误/跳过；私有Unix socket，服务已停止 |
| 联合覆盖率 | 两侧均91.95% |
| 纯分支覆盖率 | 两侧均85.73%，门槛85% |
| 合成业务/框架组合契约 | 两侧各120/33项通过；不证明真实业务质量或原生框架优势 |
| 业务安全、工作台及页面脚本竞态 | 两侧通过；页面脚本使用模拟DOM/fetch |
| core/all extras/独立比较环境依赖审计 | 通过；仅沿用历史NLTK精确例外 |
| wheel/sdist成员、内容与源码摘要 | 通过 |
| 基础包隔离安装、独立Web/Cloud消费 | 通过；Cloud消费者实际加载已安装wheel并完成PG托管执行 |
| 冻结清单及源码未变 | 通过 |
| 资格后发行物staging | 完整源/manifest/gate/SHA核对后复制同次wheel/sdist，通过；未发布 |

最终证据：[完整摘要](candidate-r2/verification/summary.json)、[源码冻结](candidate-r2/source.json)、
[发行物绑定](BINDINGS.json)、[阶段状态](EXECUTION.json)、[审查与残余边界](AUDIT.md)、
[发现分类](FINDINGS.json)。运行明细为 `reports/saas/20261030-hosted/candidate-r2/verification/runs/20261101T052652Z-577615aa`。

已验发行物：wheel（`reports/saas/20261030-hosted/verified-artifacts/forgeharness-0.12.0-py3-none-any.whl`）、
源码包（`reports/saas/20261030-hosted/verified-artifacts/forgeharness-0.12.0.tar.gz`）。
wheel SHA256：`b35126fc4fc25a0545ea6f58e992e9f91edbd4b5aef6c530c45a997d9e4f039c`。
sdist SHA256：`b862ade8dd624994f3f0d3f3569cf2665a48bb88ee7462fbfd184a17264f644d`。

## 首次失败与修正

[candidate-r1](candidate-r1/verification/summary.json) 完整运行122项门禁，114通过、8未通过，
源码和冻结未变。两侧各1970项核心中1968通过、2失败，各183项真实PG全部通过；
覆盖率和安装包消费通过。原失败manifest、日志、发行物和协议保持原样。

- 参考应用仓库映射绕过公开SDK契约，改为 SDK 公共安全文件迭代接口，并验证早停关闭与
  仅Python文件计数；没有放宽架构规则。
- sdist期待白名单漏写受审托管示例，补入精确示例，没有扩大公共输入范围。
- 依赖审计确认12项告警，升级root LangGraph 1.2.15 / SDK 0.4.7、pypdf 6.21.4，
  及隔离deepagents环境pyasn1 0.6.5，并增加新安装要求与实际锁版本回归。
  上游依据：[LangGraph公告](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-3fx2-cqw9-xr3c)、
  [pypdf修复版本](https://github.com/py-pdf/pypdf/releases/tag/6.21.4)、
  [pyasn1修复版本](https://github.com/pyasn1/pyasn1/releases/tag/v0.6.5)。
  没有新增漏洞豁免。

修正后另行声明protocol-sdk0120-r2并冻结candidate-r2，重新运行全部源码和干净副本门禁，
没有把开发测试或第一次通过项拼接成最终资格。比较场景和顶层引擎版本保持不变。

## 产品目标仍须完成

本轮是可信内部宿主服务，不提供通用客户登录/API/UI；成员和应用范围仍由可信宿主绑定。
整数单位预留不是 CNY1000/月预算。真实Docker daemon、云部署、持续SLO、生产灾备、
供应商账单及真实客户业务没有获本批证明。

后续必需工作为通用组织/身份/工作空间及授权控制面、用户API/UI、队列公平调度、
PG R2/并行/会话、资料生命周期、组织月额度/价格账单支付、云运维与真实上线验收。
下一切片的只读设计见 [NEXT_CONTROL_PLANE](NEXT_CONTROL_PLANE.md)，尚未实施为新产品功能。

用户已有大陆服务器和模型服务，后续本机配置接入；首个业务未定，先完成通用平台。
本批没有连接这些资源、没有收费模型调用、没有部署或发布PyPI。
