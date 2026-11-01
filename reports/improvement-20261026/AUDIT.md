# 本轮审计与发现分类

日期：2026-10-26；实施与审查均为本聊天中的 Codex，自审，不是独立专业审计。
范围为架构/正确性/测试/权限与隐私边界/依赖/文档/简历表述。
当前源码冻结在 `candidate-r3/source.json`；最终通过与否只读取该候选的完整验证摘要。
0.8.4 candidate-r2 和宿主故障演练绑定其原源码，保留为历史证据。

| 发现 | 严重级别 | 处理与回归 |
|---|---|---|
| Brotli 1.2.0 新内存安全公告 | blocker（可选依赖发布门禁） | 定向升级 1.2.1；`test_optional_dependency_security_floors`；重新审计，不加例外 |
| setuptools 84.0.0 Windows 旧安装路径公告 | low；门禁失败仍修复 | 升级 84.1.0；同上版本下限回归 |
| 独立 Pydantic Harness 0.56.0 CodeMode 嵌套工具限制公告 | medium；比较环境门禁失败 | 升级 0.59.0 / companion 2.59.0；重新声明/封存协议，33 场景重跑；`test_comparison_harness_security_fix_and_protocol_are_bound` |
| 新宿主 driver resolve 虚拟环境 Python，丢失 SDK site-packages | high（演练可信度） | 保留解释器 symlink 的绝对路径；`test_framework_python_keeps_venv_symlink_instead_of_base_interpreter`；保留 hosts-final 的三轮失败，修复后 hosts-final-r2 全量重跑 |
| 初版实验把 HTTP 507 当作策略失败后仍标测量有效 | high（不成立结论风险） | 基础设施失败保留分母并使测量失效；失败前 preflight；HTTP 507/内存准入回归；live-dev-r1 的旧 true 标志以独立 disposition 明确作废 |
| contract 模型 synthetic tokens 初版进入成本字段 | high（错误成本表述） | synthetic 不进入 token/cost 指标，`cost_metrics_valid=false`；逐角色用量仍用于接线检查 |
| 只按行数检查 complete 可漏掉重复/缺失样本 | medium | 检查 case/arm/sample 唯一性与 arm 完整性；重复行回归 |
| 27B 开发首轮在 final-only 阶段暴露自动工具调用 | high（评测接线可信度） | ADR 033；逐调用省略 schema 而不改变权限；7 项新回归覆盖 payload/trace/用量/并发/拒写/非法 flag/三角色；保留中断 dev-r1，开发与正式使用新候选 |
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

F2 剩余：已下载并核验 27B 模型，修正 final-only 目录后重新运行开发；正式状态从最新实验报告读取。现有 42 道短合成诊断题不能替代
更广的实际开发/业务任务，作者见过全部题目；三角色实现只规划材料顺序且调用串行。
即便未来采样成功，也只能报告该配置下的探索性结果，不能宣传普遍多 Agent 优势。

本次 0.8.5 自审：变更只在 ModelRequest 构造前选择目录，未绕过 prepared request
一致性验证或 shared invocation；并发路径持锁、默认值维持旧行为、注册表未变、写权限未扩大。
没有新增依赖、外部付费调用或模型自动执行代码。协议声明 A/B/C 的工具目录差异；
目录与 prompts 均在 trace 留证。17 个模型文件（包括模板）另有固定 revision 校验和，
运行时/配置身份另行封存。不把下载验证等同模型业务能力。

正式采样中的数据/评分边界复核（不改变协议或重评分）：参考答案的规则推演未发现算术/规范错误，
但精确字符串评分对命名与格式变体敏感。例如 eval-p05 参考为 `api,runtime`，
模型输出 `api/core.py, runtime/core.py` 在语义上可能指向相同模块；题面未完整约束模块
命名层级。这类结果仍保留封存的精确评分失败，但不能直接叫“推理错误”。
本轮成功率明确称为“规范答案精确匹配率”，不等同人工语义质量。
另有 eval-p02/p03/p04 输出材料键 `1` 而非端点/测试/服务名，属于实际答案类型不符。
后续若改善题面或增加语义评分，须在新的独立材料与协议下重测，不追改本轮主指标。

正式轮最终核验：270 个预期唯一样本齐全、270 条 trace 哈希链有效，原始统计独立重算一致；
源码、17 个模型文件、服务 PID/版本/设置均通过前后比对。正式协议和预先固定的分析脚本
SHA-256 未变，分析阶段没有新推理调用。没有基础设施错误或未知用量。
规范答案精确匹配为 A42/90、B36/90、C39/90；C 的质量门槛、成本门槛及三族延迟例外
均不通过。reviewer 将 3 个重复样本从精确评分失败改为通过，仅对应 1 个任务：
移除 `retrieve` 答案的 Markdown 代码围栏，答案语义未变；不能当成 3 项独立推理发现。
失败分类：A48 次全部为工具预算耗尽；B54 次中规范字符串不匹配30、无效 JSON18、
答案类型不匹配6；C51 次分别为30、15、6。规范字符串不匹配项未独立作语义评分。
题面命名歧义分类为 medium 测量局限，接受条件是只报告封存的精确匹配，明确不作
语义能力/泛化/生产资格声明；未来新题/新协议再测，不回填本轮主评分。
未发现本次运行时目录控制修改的 blocker/high 未修复缺陷；F2 的外推与独立评审条件、
F4 真人接入与 F5 个人无 AI 排障仍未满足，整个计划不能标 complete。
