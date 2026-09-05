---
id: 002
phase: track
title: 通用 Agent 基线 — 首次跑分
started: 2026-09-04
finished: 2026-09-04
status: done
plan_ref: ../generic-agent-baseline.md
---

## 目标

跑通导师建议的第三条基线：Codex CLI（`codex exec`，只读沙箱，无联网搜索）读原始 10-K 文档，回答 `eval_set.json` + `eval_set_tier3.json` 的 38 道题（3 道 v1 自己标 `retired` 被剔除），用与 v1 harness 相同的口径打分。见 [[generic-agent-baseline]]。

## 做了什么

- 写 `src/copilot/eval/generic_scoring.py`：复用 `harness.py`/`harness_tier3.py` 的 `_extract_number` / `_within_tolerance(_abs)` / `_is_refusal` / `_llm_judge` / `_llm_judge_graph`，按题目自带的 `type`/`scoring` 字段路由（`numeric` / `retrieval` / `graph_lookup` / `graph_trend` / `graph_fact` / `graph_comparison` / `graph_compute` / `unanswerable`）。
- 写 `scripts/run_generic_baseline.py`：逐题起独立 `codex exec` 进程，`-C baseline/raw_filings -s read-only --json -o <answer_file>`，抓答案文本 + `turn.completed` 的 token 用量。
- 写 `scripts/rescore_generic_baseline.py`：从已保存的报告 JSON 重新打分，不用重跑 codex——打分逻辑改了/以后有了 `OPENAI_API_KEY` 都可以离线重算。
- 冒烟测试（2 题）通过后，跑了全量 38 题，全部 `status=OK`（0 超时 0 报错）。
- **第一版打分**（朴素规则）结果明显偏低，逐条核对答案原文后发现大部分是打分逻辑的问题，不是 Codex 答错——于是重写 `generic_scoring.py` 里 tier3 的部分，再跑 `rescore_generic_baseline.py` 重新打分，不重跑 codex。

## 决策

### D1. 打分按题目自带的 `type`/`scoring` 字段路由，不用一套通用启发式
- **背景**：第一版用一个粗糙的启发式（"有没有 `expected_edges` 字段就走 tier3 文本比对"）统一处理所有 tier3 题，结果把 `graph_comparison`（v1 自己都用 LLM judge 打分）和 `graph_compute` 的 ranking 子类型也塞进了数值/文本比对，产出没有意义的分数。
- **选择**：改成完全照抄 `harness_tier3.py::score_item_t3` 的分支结构——按 `item["type"]` 分派，`llm_judge` 模式的题目复用 `_llm_judge_graph`，其余才走文本比对。
- **理由**：v1 自己的 harness 已经想清楚了每种题型该怎么判，重新发明一套更粗糙的规则只会引入新 bug。

### D2. Tier3 的 `threshold_only` 语义必须在文本比对里体现
- **背景**：`t3_aapl_suppliers_2024` 的 `expected_edges` 里 SWKS 那条是 `revenue_pct: 10.0, threshold_only: true`——意思是 v1 自己的抽取管线只从 SWKS 10-K 的 Item 1/1A 段落抓到了"超过 10%"这句模糊披露，**没有**抓到精确数字。第一版打分要求答案的数字必须落在 10±2 个百分点，Codex 说的"69%"因此被判错。
- **深挖发现**：直接 grep SWKS FY2024 10-K 原文，在财务报表附注（不是 Item 1/1A）里确认了原句："Apple ... in the aggregate accounted for 69%, 66%, and [58%] ... of net revenue"——**这个精确数字真实存在于该文件里**，只是在 v1 的 `extract_edges.py` 没扫到的章节。也就是说 v1 自己数据库里 SWKS 这条边的精度，比原始文件本身低。
- **选择**：`threshold_only` 的边，判分标准改成"答案数字 ≥ 披露的下限"，而不是"数字必须接近下限"。一个比下限更精确的数字不算错，可能反而是对的。
- **理由 / 影响**：这也直接影响 `t3_rank_apple_exposure_2024`（供应商影响排名题）——v1 的 `expected_ranking` 是 QRVO > CRUS > SWKS，因为它用 SWKS 的下限（≥10%）保守估算；Codex 用它找到的 69% 算出 SWKS 影响最大，排名 SWKS > QRVO > CRUS。**这道题的"正确排名"本身依赖于 SWKS 真实占比是多少**，Codex 的排名不一定错——这类题目 v1 自己标的打分方式也是 `llm_judge`，不是硬编码 ranking 比对，此处先转交 `_llm_judge_graph`，等 `OPENAI_API_KEY` 到位后判。

### D3. 数值题打分不出结果时标 `needs_review`，不强制算错
- **背景**：Retrieval 题（7 道）和两道 tier3 `llm_judge` 题在没有 `OPENAI_API_KEY` 时 `_llm_judge`/`_llm_judge_graph` 返回 `score=-1`。第一版把这种情况和"判为错"混在一起，第一版汇总里 retrieval 显示 0/7——但人工抽查这 7 条答案，内容明显是对的（比如 SWKS 依赖 Apple 69%/66%/58% 的趋势、Apple 五大产品分类），只是没有逐字引用 v1 黄金摘录里的原句。
- **选择**：`judge_available=False` 时返回 `correct=None` + `needs_review=True`，汇总口径改成"排除待复核题的准确率"，不再把"没法判"和"判错"划等号。
- **影响**：Phase 0b 配好 `.env` 后，`rescore_generic_baseline.py` 可以直接对着已保存的答案文本重新跑 judge，不需要重新调用 codex（省钱省时间）。

## 学到什么

- **v1 自己的 eval 数据集里，"unanswerable" 混了两种不同的东西**：一种是"任何文件都不会披露"（比如 procurement-share 那道 trap 题——没有任何一份 10-K 会从客户视角披露"我们采购额里有多少来自供应商 X"），另一种只是"v1 自己的 SQL schema 没建这张表"（比如中国区营收占比——其实 Apple 在 Segment Information 附注里明确披露了地区收入，只是 v1 的 `financial_facts` 表没摄取地理细分；股息率同理，需要股价数据，10-K Item 5 本身有季度股价区间表，但 v1 的 XBRL 摄取管线不含市场数据）。**这两种"不能回答"的性质完全不同**，前者是真的不可知，后者只是"这套工程选择不覆盖"。通用 agent 因为直接读文件，两种它都可能答上来，暴露了这个混淆。
- **手写抽取管线可能比原始文件本身更粗糙**：SWKS 这个案例证明 `extract_edges.py` 只扫了 Item 1/Item 1A 的固定措辞模式，漏掉了财务报表附注里更精确的重述——结构化数据库的"标准答案"不代表就是文件里能找到的最精确信息。这对"该不该信任预处理好的数据库"是个有意思的反例。
- **一个真正干净的"定制工程赢了"的案例**：`t3_unans_apple_supplier_share`（procurement-share trap）。Codex 没有识别出这是结构性不可披露的信息，而是自己拼了一个"供应商对 Apple 的营收 ÷ Apple 总销货成本"的代理指标，当成答案给出来。这正是 v1 的 `agent.py` system prompt 第 8 条规则专门针对的失败模式（docstring 里整段案例研究）。零定制的通用 agent 在这类"域内陷阱"上确实会中招。
- **数值题的 sign 丢失是文本抽取的通病，不是 Codex 独有**：`_extract_number` 从"净亏损 7032.2 万美元"或"下降 2.8%"这类叙述里只拿数字部分，符号信息在自然语言里（"亏损"/"下降"）不在数字本身，回归表达式一律丢失。v1_loop 之所以不受影响，是因为它优先读 `compute` 步骤的带符号浮点数，而不是从最终文本正则提取——generic agent 没有这个"结构化中间结果"可以借用，这是文本答案这种交互形式本身的局限，不是模型推理错。

## Eval / 指标

复核后（`generic_agent_baseline_20260904T234252Z_rescored.json`）：

| 指标 | 值 | 说明 |
|------|-----|------|
| Tier1 准确率（排除待复核） | 0.692 (9/13) | 20 题中 7 道 retrieval 待 judge |
| Tier2 准确率 | 0.80 (8/10) | 2 道错的都是"符号丢失"文本抽取问题 |
| Tier3 准确率（排除待复核） | 0.833 (5/6) | 8 题中 2 道待 judge；1 道 procurement-share trap 真实答错 |
| p95 延迟 | 56.4s | 单题最长 63.4s（t3_crus_apple_trend，跨 5 个财年） |
| 平均 input tokens/题 | ~131,953 | 原始 HTML 未预处理，读取成本高 |
| 总 output tokens | 30,866 | |
| 待复核题数 | 9 / 38 | 7 retrieval + 2 tier3（graph_comparison / graph_compute-ranking），等 `OPENAI_API_KEY` 后 `rescore_generic_baseline.py` 离线补跑 |

## 死胡同 / 坑

- 第一版 tier3 打分用启发式而不是按 `type` 路由，产出了看似"通用 agent 表现很差"的误导性数字（Tier3 0.5, Tier1 0.45）。**教训：打分逻辑要先核对几条具体样本的原文，再相信汇总数字**——尤其是新写的 scorer，没有先验的正确性保证。
- 一开始以为 `claude` CLI 装了，结果这台机器只有桌面 App 没有 headless CLI；改用户已装好的 `codex exec`，工具本身没问题，是环境探测没做在前面。

## 下一步 / 解锁了什么

- Phase 0b 配好 `.env`（`OPENAI_API_KEY`）后：`uv run python scripts/rescore_generic_baseline.py data/results/generic_agent_baseline_20260904T234252Z.json` 直接补上 9 道待复核题的分数，不用重跑 codex。
- 这条基线的方法论（型别路由打分、threshold_only 语义、needs_review 而非强制判错）后续给 v1_loop / graph 的 A/B 对比也适用，值得在 Phase 2 的 `ab_compare.py` 里参考。

## 补记（2026-09-05）：计划修订 + driver 升级

首跑用的是 Codex 默认模型（`gpt-5-codex` 级），且只跑了 1 趟。用户要求把基线做完整，`docs/generic-agent-baseline.md` 已整体重写。改动：

- **模型钉死 `gpt-5.6-sol`**：`codex exec -m gpt-5.6-sol`，已验证 slug 可用（`-m` 冒烟通过）。
- **`run_generic_baseline.py` 升级**：`--model`（默认 `gpt-5.6-sol`）/ `--runs N`（多趟）/ flags 前置（避免被当 stdin）/ 完整 token 分项聚合（input / cached / cache_write / output / reasoning）/ 延迟 p50·p95·max·mean / 每题工具调用次数（数 `command_execution` 事件）/ 多趟一致性（哪些题 flaky）。
- **`generic_scoring.py`**：`unanswerable` 分三桶——`undisclosed`（真陷阱，只有 `t3_unans_apple_supplier_share`）/ `v1_schema_gap`（文件里有，v1 没摄取：中国区营收、股息率）/ `out_of_scope`（TSMC 根本不在语料里）。汇总分开统计，只有 `undisclosed` 桶算"该拒答"。
- **待执行**：`gpt-5.6-sol` 全量 38 题跑 3 趟；之后 Phase 0b 补 judge + v1_loop@strong 同档模型重跑。首跑的 `20260904T234252Z*` 结果保留为"preliminary n=1（默认模型）"，不进正式对比表。

## 补记 2（2026-09-05）：gpt-5.6-sol 正式跑 + 评分框架修复

**按 `docs/dev-workflow.md` 六步做的第一个真实任务**（track-1）。EVAL 两次打回 BUILD。

`gpt-5.6-sol` 全量 38 题跑完，38/38 status OK。用户选了 n=1（不是 3）。首版打分 Tier1 0.69 / Tier2 0.70 / Tier3 0.67 —— **抽查发现又是 scorer 的锅**。两轮修 `generic_scoring.py`：

1. **`_extract_answer_number`**：优先抓答案里最后一个 `= <数字>` 算式结果（Codex 每次显式写算式，等价于 v1 优先读 `compute` 步骤），保留算式符号；无算式则回退 `_extract_number`，**不猜符号**——`score_numeric` 把 `±got` 都当候选，让 `expected_value` + 容差判。修掉 net loss / decreased by X% 的符号丢失。
2. **`_CALC_RE` 放宽**：`= **$311.3 million**` 结果被 markdown 加粗包着 → `[*_~≈$ ]*` 跳过 markdown/货币符号（不跳 `(`，避免抓中间子表达式）。
3. **`_LINK_RE`**：markdown 链接绝对路径 `.../Financial Report Copilot v2/...` 里的 "v2" 被读成 2.0 → 抽取前去掉 `](...)`。
4. **`_safe_judge`**：占位符 key（`sk-your-key-here`）非空，绕过 `harness._llm_judge` 的空检查，真调 OpenAI 然后 401 崩 → try/except，任何 judge 失败降级成 `needs_review`。
5. **`tests/test_generic_scoring.py`**（新，13 case，每个是 run 里被误判的真实答案）。

### EVAL（rescore，不重跑 codex）

| impl | Tier1 | Tier2 | Tier3 |
|---|---|---|---|
| gpt-5.6-sol (n=1) | 0.769 (10/13) | 1.0 (10/10) | 0.833 (5/6) |
| preliminary (默认模型, n=1, 同 scorer 重打) | 0.769 | 1.0 | 0.833 |

**逐题零差异**——之前看到的"模型差距"100% 是 scorer artifact。9 道待 judge（`.env` 还是占位符 key）。唯一真实"该拒答没拒答"：`t3_unans_apple_supplier_share`（procurement-share 陷阱，两模型都中）。3 个 `v1_schema_gap`/`out_of_scope` 单独分桶不算 fail。

**成本**：p50 31s / p95 47s / mean 32.5s；~132K input tok/题（87% cached）；4.2 工具调用/题；reasoning token 126/题。

### Learning

- **文本答案数值打分的核心难点：信号在散文里不在数字上**——符号（loss/decreased）、精度（headline 四舍五入 vs 算式精确值）、噪音（"10-K" 的 10、路径里的 "v2"）。v1 harness 用"优先读 `compute` 步骤"绕过全部（它有结构化中间结果）；文本 scorer 的等价物 = "优先读答案里的 `= X` 算式行"（强模型总会写出算式）。
- `needs_review` 三态设计经受住考验：占位符 key 让 judge 全挂，`_safe_judge` + `correct=None` 让 sweep 不崩、结果可用、key 好了 `rescore` 补，不用重跑 codex（省 5M token / 20 分钟）。
- 两个 off-the-shelf 推理模型在这批题上打平 → 差别不在模型，在"有没有域内工程"。

### 死胡同 / 坑

- 第一版 `_NEG_WORDS` 从措辞猜负号，把 `t3_qrvo_dollar_impact`（"decrease of $347M" 是正的幅度）过度取反。教训：别从措辞猜符号，`±` 都当候选。
- 每修一处 EVAL 又冒一处（符号 → 加粗 → 链接路径 → judge 崩）。文本解析边界情况是长尾，靠"抽查真实答案 → 加测试 → 再抽查"收敛。

### 待办（不阻塞）

- 想要方差 `run_generic_baseline.py --runs 3`（用户选 n=1）。
- 依赖 Phase 0b：v1_loop 在 `gpt-5.6-sol` 同档重跑，出 `v1_loop@strong`。

## 补记 3（2026-09-05）：judge 打分完成 + 成本

用户填了真 `OPENAI_API_KEY`（`sk-proj-`），rescore 补完 9 道 judge 题。

### BUILD 又一轮：retrieval 判分去掉 verbatim gate

首次 judge 结果 retrieval 只有 1/7。查：7 道里 6 道 judge=3（"accurately captures all key facts"），全因 `hit=False`（答案没逐字引用黄金 SEC 原句）被 `correct = hit and judge>=2` 挡掉。v1 harness 的 `passage_hit` 查的是**检索到的 chunk**，generic agent 没有检索步骤，拿最终答案去逐字比对是在测"引用"不是"理解"。改成 `correct = judge_score >= 2`，`passage_hit` 只当上报字段。+2 个 monkeypatch 测试。

### 最终结果（gpt-5.6-sol, n=1）

| Tier | 准确率 | 说明 |
|---|---|---|
| Tier1 | 0.85 (17/20) | 答得了的题 17/17；3 个非满分是 `v1_schema_gap`(中国区营收/股息率) + `out_of_scope`(TSMC) 的 unanswerable，Codex 答了（有的事实正确），单独分桶 |
| Tier2 | 1.0 (10/10) | |
| Tier3 | 0.75 (6/8) | miss 1 = `t3_unans_apple_supplier_share`（procurement-share 陷阱，真错，编代理指标）；miss 2 = `t3_rank_apple_exposure`（judge=0，Codex 用 SWKS 真实 69% 排名，与 v1 用下限的 `scoring_notes` 分歧）|

**preliminary run（默认模型）用最终 scorer 重打：Tier1 17/20 · Tier2 10/10 · Tier3 6/8，与 gpt-5.6-sol 逐题零差异。**

### 成本（按 gpt-5.6-sol 计价：$4 in / $0.40 cached / $20 out per 1M）

| | 全量 38 题 (n=1) | 每题 |
|---|---|---|
| uncached input 649K | $2.60 | |
| cached input 4.36M | $1.74 | |
| output 31K（含 reasoning 4.8K）| $0.62 | |
| **合计** | **$4.96** | **$0.131** |

对比 v1_loop（gpt-4o-mini，v1 仓库历史 run）：$0.033/30 题 ≈ **$0.001/题** → generic 每题贵 **~117x**（模型单价 ~25x × input 量 ~19x：读原始 HTML vs SQL 返回紧凑结构化结果）。跑 3 趟 ≈ $14.9。

### Learning

- **retrieval 判分：judge 是权威**（它有黄金答案 + rubric），任何"额外 gate"（verbatim phrase）都会把 paraphrase-but-correct 的答案误杀。v1 harness 的 `passage_hit` 是给"有检索步骤"的 agent 设计的，迁移到黑盒文本答案时语义变了。
- **成本结构**：generic agent 贵在"读原始文档"——87% input 命中缓存也还是 132K token/题。定制管线（SQL/vector 返回紧凑结果）在 token 量级上就赢了一个数量级，叠加小模型单价，总成本差两个数量级。这是"定制工程值不值"里**成本维度**的答案：贵 100x，Tier1-2 准确率打平，只在 Tier3 结构化查询和拒答纪律上换来质量。
