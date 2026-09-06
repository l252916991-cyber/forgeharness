# ForgeHarness 大规模测试计划

## 🎯 目标

收集 1,000 道 Agent/RAG 相关测试题，运行测试，修复暴露的问题，直到达到同类项目平均水平。

## 📊 测试题来源策略

### 1. 开源 Agent 评测数据集

#### A. AgentBench (1,000+ 题)
- **来源**: https://github.com/THUDM/AgentBench
- **覆盖**: OS interaction, Database, Knowledge Graph, Digital Card Game, Lateral Thinking Puzzles, House-Holding, Web Shopping, Web Browsing
- **题量**: ~1,000 题
- **适用性**: ⭐⭐⭐⭐⭐ (Agent 能力全面评测)

#### B. ToolBench (16,000+ API 调用)
- **来源**: https://github.com/OpenBMB/ToolBench
- **覆盖**: 工具调用、API 使用、多步推理
- **题量**: 16,000+ 工具调用样本
- **适用性**: ⭐⭐⭐⭐⭐ (工具调用测试)

#### C. WebArena (800+ 任务)
- **来源**: https://github.com/web-arena-x/webarena
- **覆盖**: Web 交互、多步任务、信息检索
- **题量**: 812 个任务
- **适用性**: ⭐⭐⭐⭐ (Web Agent 测试)

### 2. RAG 评测数据集

#### D. BEIR Benchmark (17 个数据集)
- **来源**: https://github.com/beir-cellar/beir
- **覆盖**: 17 个检索任务数据集
- **题量**: ~300K queries
- **适用性**: ⭐⭐⭐⭐⭐ (RAG 检索质量)

#### E. MTEB (Massive Text Embedding Benchmark)
- **来源**: https://github.com/embeddings-benchmark/mteb
- **覆盖**: 58 个数据集，8 个任务类型
- **题量**: 数百万样本
- **适用性**: ⭐⭐⭐⭐⭐ (Embedding 质量)

#### F. RAGAs Evaluation Dataset
- **来源**: https://github.com/explodinggradients/ragas
- **覆盖**: RAG 评测指标（忠实度、相关性、上下文召回）
- **题量**: 可配置
- **适用性**: ⭐⭐⭐⭐⭐ (RAG 端到端)

### 3. 编程 Agent 数据集

#### G. SWE-bench (2,294 GitHub Issues)
- **来源**: https://github.com/princeton-nlp/SWE-bench
- **覆盖**: 真实 GitHub issue 修复
- **题量**: 2,294 个 issue
- **适用性**: ⭐⭐⭐⭐⭐ (Coding Agent 标准)

#### H. HumanEval (164 题)
- **来源**: https://github.com/openai/human-eval
- **覆盖**: Python 函数实现
- **题量**: 164 个编程问题
- **适用性**: ⭐⭐⭐⭐ (代码生成基础)

#### I. MBPP (1,000 题)
- **来源**: https://github.com/google-research/google-research/tree/master/mbpp
- **覆盖**: Python 编程问题
- **题量**: 1,000 个问题
- **适用性**: ⭐⭐⭐⭐⭐ (编程能力)

### 4. 知识问答数据集

#### J. NQ (Natural Questions, 300K+)
- **来源**: https://github.com/google-research-datasets/natural-questions
- **覆盖**: Google 搜索真实问题
- **题量**: ~300K 问题
- **适用性**: ⭐⭐⭐⭐ (知识检索)

#### K. HotpotQA (113K 多跳问答)
- **来源**: https://github.com/hotpotqa/hotpotqa
- **覆盖**: 多跳推理问答
- **题量**: 113K 问题
- **适用性**: ⭐⭐⭐⭐⭐ (复杂推理)

#### L. MS MARCO (1M+ queries)
- **来源**: https://microsoft.github.io/msmarco/
- **覆盖**: 文档排序、段落检索
- **题量**: 1M+ queries
- **适用性**: ⭐⭐⭐⭐⭐ (检索排序)

---

## 🎯 推荐测试组合（总计 1,000+ 题）

### 方案 A: 平衡型（推荐）
```
1. SWE-bench Lite (300 题) - Coding Agent 核心
2. MBPP (500 题随机采样) - 编程能力基础
3. HotpotQA (100 题) - 多跳推理
4. BEIR (100 题混合) - RAG 检索
5. AgentBench (100 题) - Agent 综合能力
---
总计: 1,100 题
```

### 方案 B: RAG 专精型
```
1. BEIR (300 题多数据集)
2. MS MARCO (300 题)
3. NQ (200 题)
4. HotpotQA (200 题)
---
总计: 1,000 题
```

### 方案 C: Coding Agent 专精型
```
1. SWE-bench (600 题)
2. HumanEval (164 题)
3. MBPP (236 题)
---
总计: 1,000 题
```

---

## 📋 实施步骤

### Phase 1: 数据收集 (1-2 天)

1. **下载数据集**
```bash
# SWE-bench Lite
git clone https://github.com/princeton-nlp/SWE-bench
python -m swebench.harness.get_dataset --split test --lite

# MBPP
wget https://github.com/google-research/google-research/raw/master/mbpp/mbpp.json

# HotpotQA
wget http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json

# BEIR
pip install beir
# 通过 API 下载

# AgentBench
git clone https://github.com/THUDM/AgentBench
```

2. **数据清洗与格式化**
```python
# 统一格式为：
{
    "id": "test_001",
    "category": "coding|rag|reasoning|agent",
    "difficulty": "easy|medium|hard",
    "question": "...",
    "expected_output": "...",
    "evaluation_criteria": {...},
}
```

3. **创建测试加载器**
```python
# src/forgeharness/evaluation/large_scale_tests.py
class TestLoader:
    def load_swebench(self, limit=300)
    def load_mbpp(self, limit=500)
    def load_hotpotqa(self, limit=100)
    def load_beir(self, limit=100)
    def load_agentbench(self, limit=100)
```

---

### Phase 2: 基线测试 (2-3 天)

1. **运行基线测试**
```bash
# 运行完整测试集
uv run python benchmarks/large_scale_test.py \
  --datasets swebench,mbpp,hotpotqa,beir,agentbench \
  --limit 1000 \
  --output reports/baseline-1000.json
```

2. **收集指标**
```python
metrics = {
  "coding": {
    "pass@1": 0.XX,
    "compile_rate": 0.XX,
    "test_pass_rate": 0.XX
  },
  "rag": {
    "recall@5": 0.XX,
    "mrr@10": 0.XX,
    "ndcg@10": 0.XX
  },
  "reasoning": {
    "accuracy": 0.XX,
    "f1": 0.XX
  },
  "agent": {
    "task_success_rate": 0.XX,
    "avg_steps": X.X,
    "tool_call_accuracy": 0.XX
  }
}
```

3. **对比行业水平**
```python
# 同类项目基准（参考数据）
baselines = {
    "AutoGPT": {"swebench": 0.10, "mbpp": 0.35},
    "MetaGPT": {"swebench": 0.13, "mbpp": 0.42},
    "GPT-Engineer": {"swebench": 0.08, "mbpp": 0.38},
    "LangChain": {"beir": 0.45, "hotpotqa": 0.52},
    "LlamaIndex": {"beir": 0.48, "hotpotqa": 0.55},
}
```

---

### Phase 3: 问题分析 (1 天)

1. **失败模式分析**
```python
# 自动分类失败原因
failure_categories = {
    "parsing_error": [],  # 输出解析失败
    "tool_call_error": [],  # 工具调用错误
    "context_overflow": [],  # 上下文溢出
    "timeout": [],  # 超时
    "incorrect_logic": [],  # 逻辑错误
    "retrieval_failure": [],  # 检索失败
}
```

2. **生成优先级列表**
```python
# 按影响面排序
priority_fixes = [
    {"issue": "工具调用参数验证不足", "impact": 150, "priority": "P0"},
    {"issue": "上下文窗口管理", "impact": 80, "priority": "P0"},
    {"issue": "检索召回率低", "impact": 60, "priority": "P1"},
    ...,
]
```

---

### Phase 4: 迭代修复 (循环)

```python
while current_score < target_score:
    # 1. 识别最高优先级问题
    issue = get_highest_priority_issue()
    
    # 2. 修复问题
    fix_issue(issue)
    
    # 3. 运行回归测试
    regression_results = run_tests(affected_tests)
    
    # 4. 运行完整测试（每 5 次修复）
    if fix_count % 5 == 0:
        full_results = run_full_tests()
        update_progress(full_results)
    
    # 5. 记录进度
    log_progress(issue, results)
```

---

### Phase 5: 达标验证

**目标基准**（基于同类项目）:
```python
target_benchmarks = {
    "swebench_lite": {
        "pass@1": 0.08,  # GPT-Engineer 水平
        "resolved_ratio": 0.10,  # AutoGPT 水平
    },
    "mbpp": {
        "pass@1": 0.35,  # 行业中位数
        "pass@10": 0.55,
    },
    "beir_avg": {
        "ndcg@10": 0.45  # LangChain 水平
    },
    "hotpotqa": {
        "f1": 0.50,  # LlamaIndex 水平
        "em": 0.35,
    },
}
```

---

## 🛠️ 技术实现

### 测试框架架构
```python
# src/forgeharness/evaluation/large_scale.py


class LargeScaleEvaluator:
    def __init__(self, config):
        self.test_loader = TestLoader()
        self.agent_runner = AgentRunner()
        self.metrics_collector = MetricsCollector()
        self.result_analyzer = ResultAnalyzer()

    async def run_evaluation(self, datasets, limit=1000):
        # 1. 加载测试
        tests = self.test_loader.load(datasets, limit)

        # 2. 批量运行
        results = []
        for test in tqdm(tests):
            result = await self.agent_runner.run(test)
            results.append(result)

            # 每 100 个保存一次
            if len(results) % 100 == 0:
                self.save_checkpoint(results)

        # 3. 计算指标
        metrics = self.metrics_collector.compute(results)

        # 4. 分析失败
        analysis = self.result_analyzer.analyze(results)

        return {"metrics": metrics, "analysis": analysis, "results": results}
```

---

## 📊 预期时间线

```
Week 1: 数据收集与清洗
  Day 1-2: 下载数据集
  Day 3-4: 格式统一
  Day 5: 测试加载器

Week 2: 基线测试
  Day 1-3: 运行 1,000 题
  Day 4: 指标计算
  Day 5: 对比分析

Week 3-4: 迭代修复
  每天: 修复 2-3 个高优先级问题
  每天: 运行回归测试
  每周: 完整测试验证进度

Week 5: 最终验证
  Day 1-2: 完整测试
  Day 3: 报告生成
  Day 4-5: 文档更新
```

---

## 📈 成功标准

### 必达指标
- [ ] 完成 1,000 道测试题运行
- [ ] 识别并修复 Top 10 高频问题
- [ ] 至少一个维度达到行业平均水平

### 目标指标
```
Coding (SWE-bench + MBPP):
  - Pass@1 >= 0.08 (AutoGPT 水平)
  
RAG (BEIR + HotpotQA):
  - NDCG@10 >= 0.45 (LangChain 水平)
  
Agent (AgentBench):
  - Task Success >= 0.40
```

---

## 🚀 立即开始

我将首先创建测试框架，然后开始下载和运行测试。
