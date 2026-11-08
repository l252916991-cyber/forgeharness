# 安全策略

## 报告漏洞

请不要在公开 issue 里描述漏洞。使用 GitHub 仓库的
[Private vulnerability reporting](https://github.com/l252916991-cyber/forgeharness/security/advisories/new)
提交，内容包括受影响的版本、复现步骤和影响范围。收到后会在 7 天内回复。

## 支持的版本

只维护最新的发布版本（当前 0.14.x）。项目尚未发布到 PyPI，也没有生产部署。

## 范围

关注运行时的治理边界：精确审批是否可被绕过、调用日志与恢复是否会重复执行副作用、预算是否可被突破、
trace 脱敏是否会泄露凭据、托管执行的租户隔离与授权。已知的接受风险和历史审查结论见
[docs/review/SECURITY.md](docs/review/SECURITY.md) 与 [docs/review/FINDINGS.json](docs/review/FINDINGS.json)。

路径限制不等于操作系统沙箱；执行命令需要隔离时请使用容器后端。
