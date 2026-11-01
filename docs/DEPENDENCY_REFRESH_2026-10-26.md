# 本轮依赖审计修复

2026-10-26。首个本轮冻结候选 `reports/improvement-20261026/candidate-r1/` 保留完整失败证据，
不将 2026-10-23 的无已知漏洞结论沿用到今天。

修复后的源码/发行候选标为 0.8.4，使用独立 candidate-r2，避免把不同 wheel 都称为 0.8.3。
SDK API 与持久状态 schema 没有变化，两个宿主的原绑定不自动升级。
R2 的完整运行身份包含框架版本，旧运行仍使用原 wheel；schema 不变不等于跨版本恢复已获准。

| 依赖 | 首轮锁定 | 修复版本 | 来源与边界 |
|---|---|---|---|
| Brotli | 1.2.0 | 1.2.1 | [GHSA-9v8h-qqgm-4m35](https://github.com/advisories/GHSA-9v8h-qqgm-4m35)、PYSEC-2026-4322；可选依赖，解码不可信压缩数据存在内存安全风险 |
| setuptools | 84.0.0 | 84.1.0 | [GHSA-xmj3-9gf7-h52w](https://github.com/advisories/GHSA-xmj3-9gf7-h52w)；Windows 旧 wheel 安装路径问题，本轮仍升级，不增加豁免 |
| Pydantic AI Harness | 0.56.0 | 0.59.0 | [GHSA-3589-pxp7-3gj8](https://github.com/advisories/GHSA-3589-pxp7-3gj8) / CVE-2026-114386；独立比较环境的 CodeMode 嵌套调用工具限制问题 |

根 lock 只定向刷新 Brotli/setuptools；Pydantic 比较环境单独刷新其精确 harness 依赖及必须的
companion，比较协议对应版本在重新运行前重新声明并重封 SHA-256。场景、评分器、权限和恢复
标准保持不变；不会把版本调整后的通过归到旧候选。33 个共同宿主组合仍不证明原生核心优势。

两个业务宿主的 vendor wheel、源码和依赖锁不在本次修改范围，仍分别使用已绑定的 0.8.3 与
0.5.0；框架核心依赖未因这些可选比较包改变。宿主各自的依赖全面审计需其自身发布流程，
本轮不据框架审计声称宿主全依赖无漏洞。

回归：`tests/unit/test_security_patch_locks.py` 检查安全版本下限及 protocol/独立 lock 一致性；
最终新候选重跑源码与干净副本、四组依赖审计、33 项组合与安装门禁。
原有唯一 NLTK `PYSEC-2026-3740` 例外保留其 FH-019 范围，没有扩大豁免。
