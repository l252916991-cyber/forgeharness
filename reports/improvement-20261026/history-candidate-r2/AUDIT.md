# 本轮审计与发现分类

日期：2026-10-26；实施与审查均为本聊天中的 Codex，自审，不是独立专业审计。
范围为架构/正确性/测试/权限与隐私边界/依赖/文档/简历表述。
当前源码冻结在 `candidate-r2/source.json`；最终通过与否只读取该候选的完整验证摘要。

| 发现 | 严重级别 | 处理与回归 |
|---|---|---|
| Brotli 1.2.0 新内存安全公告 | blocker（可选依赖发布门禁） | 定向升级 1.2.1；`test_optional_dependency_security_floors`；重新审计，不加例外 |
| setuptools 84.0.0 Windows 旧安装路径公告 | low；门禁失败仍修复 | 升级 84.1.0；同上版本下限回归 |
| 独立 Pydantic Harness 0.56.0 CodeMode 嵌套工具限制公告 | medium；比较环境门禁失败 | 升级 0.59.0 / companion 2.59.0；重新声明/封存协议，33 场景重跑；`test_comparison_harness_security_fix_and_protocol_are_bound` |
| 新宿主 driver resolve 虚拟环境 Python，丢失 SDK site-packages | high（演练可信度） | 保留解释器 symlink 的绝对路径；`test_framework_python_keeps_venv_symlink_instead_of_base_interpreter`；保留 hosts-final 的三轮失败，修复后 hosts-final-r2 全量重跑 |
| 初版实验把 HTTP 507 当作策略失败后仍标测量有效 | high（不成立结论风险） | 基础设施失败保留分母并使测量失效；失败前 preflight；HTTP 507/内存准入回归；live-dev-r1 的旧 true 标志以独立 disposition 明确作废 |
| contract 模型 synthetic tokens 初版进入成本字段 | high（错误成本表述） | synthetic 不进入 token/cost 指标，`cost_metrics_valid=false`；逐角色用量仍用于接线检查 |
| 只按行数检查 complete 可漏掉重复/缺失样本 | medium | 检查 case/arm/sample 唯一性与 arm 完整性；重复行回归 |
| 工具默认效果类别阻止 one-shot B/C 路径 | medium | 显式 READ_ONLY；三条真实 Harness 路径用量/trace 回归；保留最初 contract 失败 |
| 电商宿主用 unittest 而非 pytest | low（命令选择） | 修正 runner，保留 host-drills-r1 的 No module named pytest；不安装/修改宿主环境 |
| 历史 F1c 待办和 README 能力否定与当前 R2 不一致 | medium（文档/简历） | 当前能力索引，明确历史版本与 one-shot/R2 差异；不重复实现既有能力 |
| 不同构建继续使用 0.8.3 会混淆发行物身份 | medium | 修复候选标为 0.8.4，API/schema 未变；旧宿主 wheel 与旧 R2 运行保持原版本 |

架构边界：无新 runtime、无 LangGraph 核心依赖、无分布式平台；实验复用 Harness/kernel，
故障重复器复用现有 fixture、JUnit、trace 和外部合成回执。新增工具写入报告目录，
宿主只读检查/测试的数据与缓存均指向本工作区。没有修改两个宿主的源代码、vendor 或锁文件。
宿主代码只保存摘要，没有将私有仓库源码或客户数据复制进框架 trace。

可接入性：quickstart 代码块可自动执行及在另一进程重开，但这只算作者自动化自测。
没有独立开发者、没有代填真人首次成功耗时、没有代写本人无 AI 排障记录。

接受的残余边界：单机 POSIX/SQLite；local owner fencing；外部回执必须可信；未知用量不填零；
取消不能回滚已提交效果；失败终态的分支不被自动重试；路径限制不是 OS sandbox；
outbox 至少一次；原有 NLTK FH-019 例外未扩大。真实业务、生产 HTTP/UI、专业法律质量、
原生完整框架故障对照与独立接入都没有以自动化门禁替代。

F2 剩余：本机服务拒绝加载模型，正式 270 次采样没有开始。现有 42 道短合成诊断题不能替代
更广的实际开发/业务任务，作者见过全部题目；三角色实现只规划材料顺序且调用串行。
即便未来采样成功，也只能报告该配置下的探索性结果，不能宣传普遍多 Agent 优势。
