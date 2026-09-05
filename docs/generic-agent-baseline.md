# 通用 Agent 基线（导师建议）

> 导师建议：科研/公司项目都要有基线。除了"过去别人的项目成果"（= v1_loop），还有一条更根本的基线——**一个不做任何定制工程、只给通用 agent 原始数据和通用工具**的对照组。要求把它加进项目，作为亮点。

---

## 1. 为什么这条基线比 v1_loop 更根本

Phase 0 已经有一条基线（v1_loop：手写 ReAct 循环 + SQL grounding + 混合检索 + 供应链图）。但那是"我们自己做过定制工程后的成果"，回答不了更前置的问题：

**这些定制工程（Postgres schema、hybrid retrieval、graph_query、compute 沙箱、system prompt 里 15 条铁律……）到底值多少分？**

一个**零定制**的通用 agent——只给原始 10-K 文档和通用工具，不给任何领域基础设施——如果已经能答对大部分问题，那 v1/v2 这套工程的价值主张就要重新论证；如果它在数值精度、多跳计算、域内陷阱上明显拉胯，那正是这个项目存在理由最有说服力的证据。

**首次跑分（2026-09-04）已经产出两个站得住的定性发现，不受下面 validity 问题影响：**
1. **一个干净的"定制工程赢了"案例**：`t3_unans_apple_supplier_share`（procurement-share 陷阱题）。这是结构性不可披露的信息（没有任何 10-K 会从客户视角披露"采购额里有多少来自供应商 X"）。Codex 没识别出来，自己拼了个"供应商对 Apple 营收 ÷ Apple 总销货成本"的代理指标当答案——正是 v1 system prompt 第 8 条规则专门防的失败模式。
2. **一个"手写抽取管线精度不如原文件"的反例**：v1 数据库里 SWKS→Apple 这条边只存了 `threshold_only` 的">10%"（`extract_edges.py` 只扫了 Item 1/1A 的固定措辞）。直接 grep SWKS FY2024 10-K 原文，财务附注里明确写着"Apple ... accounted for 69%, 66%, and 58% ... of net revenue"。Codex 读原文找到了精确数字，v1_loop 的 `graph_query` 工具**结构性够不到**这个数——它的天花板就是数据库里存的那个值。

---

## 2. 三/四方对比框架

| 基线 | 定制程度 | 数据访问 | 模型 | 工具 |
|---|---|---|---|---|
| **generic_agent** | 零定制 | 原始 10-K 文档，一个目录 | **`gpt-5.6-sol`（钉死）** | 通用（Read/Grep/Bash/ripgrep），无领域基础设施 |
| **v1_loop** | 手写 ReAct 循环 + 5 个定制工具 | Postgres SQL + pgvector | `gpt-4o-mini`（v1 原配） | 5 个领域工具 + 15 条 system prompt 规则 |
| **v1_loop@strong**（建议新增） | 同 v1_loop | 同 v1_loop | **换成 GPT-5 级模型** | 同 v1_loop |
| **graph**（Phase 2+） | LangGraph 编排 | 同 v1_loop | 同 v1_loop | Phase 1 标准化工具库 |

**为什么要 `v1_loop@strong` 这一行**：v1_loop 原配 `gpt-4o-mini`（小、非推理、2024 代），generic_agent 用 GPT-5 级推理模型。直接对比 generic_agent vs v1_loop，分差里混了两个变量——**架构**（有没有定制管线）＋ **模型档次**。加一行"v1_loop 换强模型"，就能把对比收敛成只剩"架构"一个变量：同一个脑子，不同脚手架。v1 的 `model_router.select_model` / config 本来就支持换 OpenAI 模型，改一行配置的事。

---

## 3. 已完成的部分

### 3.1 原始数据（已抓取）
`scripts/fetch_raw_filings.py`：从 `data/seed/filings.csv.gz` 的 `doc_url` 直接抓 SEC EDGAR 原文（不重新调 API）。按 `eval_set.json` + `eval_set_tier3.json` 覆盖的 6 家公司过滤，**58 份真实 10-K**，172MB，存于 `baseline/raw_filings/<TICKER>/<year>_<form>_<accn>.htm`（`baseline/` 已 gitignore）。

**不用** `data/seed/*.csv.gz`——那是 v1 pipeline 已经抽取、结构化好的数据，给通用 agent 用等于喂半成品，不公平。

### 3.2 原文处理：方案 A（不预转纯文本）
让 Codex 自己用 `rg`/`bash` 处理原始 inline-XBRL HTML。首次跑分证实可行：Codex 用 `rg -o -P` 精准定位大文件里的数字，没有整份读入爆上下文。

### 3.3 打分器（已写，`src/copilot/eval/generic_scoring.py`）
- 复用 `harness.py`/`harness_tier3.py` 的 `_extract_number` / `_within_tolerance(_abs)` / `_is_refusal` / `_llm_judge` / `_llm_judge_graph`——一行没改，容差带和拒答判定与 v1 一致。
- 按题目自带的 `type`/`scoring` 字段路由（`numeric` / `retrieval` / `graph_lookup` / `graph_trend` / `graph_fact` / `graph_comparison` / `graph_compute` / `unanswerable`），照抄 `score_item_t3` 的分支结构。
- `threshold_only` 的边：判分标准是"答案数字 ≥ 披露下限"，不是"接近下限"——比下限更精确的数字不算错。
- judge 不可用（没 `OPENAI_API_KEY`）时返回 `correct=None` + `needs_review=True`，汇总用"排除待复核题的准确率"，不把"判不了"当"判错"。

### 3.4 首次跑分结果（复核后，n=1，`gpt-5-codex` 默认模型）
| Tier | 准确率（排除待复核） | 备注 |
|---|---|---|
| Tier1 | 0.69 (9/13) | 20 题里 7 道 retrieval 待 LLM judge |
| Tier2 | 0.80 (8/10) | 2 道错的都是"符号丢失"文本抽取问题 |
| Tier3 | 0.83 (5/6) | 8 题里 2 道待 judge，1 道 procurement-share trap 真实答错 |

p95 延迟 56.4s，平均 ~132K input token/题。**这版是初步结果，不进正式报告**——见下面 §5 的 validity 问题。

---

## 4. 本次修订要做的（让基线"更完整"）

### 4.1 钉死模型 `gpt-5.6-sol`
`codex exec` 加 `-m gpt-5.6-sol`。已验证该 slug 可用。理由：基线必须可复现，不能半年后重跑悄悄变成别的默认模型。报告里明确写"generic_agent = Codex CLI + gpt-5.6-sol"，不写"通用 agent"这种抽象说法。

### 4.2 完整记录 token 与延迟
每题记录（从 `codex exec --json` 的 `turn.completed.usage`）：
- `input_tokens` / `cached_input_tokens` / `cache_write_input_tokens` / `output_tokens` / `reasoning_output_tokens`
- wall latency（`subprocess` 计时）
- 工具调用次数（从 `item.*` 事件里数 `command_execution`）

汇总：各项 token 总和 / 均值；延迟 p50 / p95 / max；总 reasoning token 占比。

**成本说明**：ChatGPT 订阅认证不返回美元成本，token 数是唯一可靠 artifact。报告里以 token 计，不折算美元（或按 OpenAI 公开 API 价目单独标注"若按 API 计价约为 …"）。

### 4.3 多趟跑，给方差
推理模型 + agentic ripgrep = 跑一次和跑一次不一样。**跑 3 趟**，报每题的 correct 一致性 + 分数均值/波动 + 延迟/token 的趟间差异。v1 自己的 harness 有 `run_multiturn_repeated` 就是干这个的，思路一致。

### 4.4 重新给 "unanswerable" 分桶
`eval_set.json` 里的 `answerable: false` 混了两种：
- **真不可知**（任何文件都不披露）：如 procurement-share。Codex 答了 = 真失败。
- **v1 的 schema 没覆盖**：如中国区营收占比（Apple 在分部信息附注里明确披露了）、股息率（需股价，10-K Item 5 有季度股价区间）。Codex 读原文答对 = 不该算失败。

跑分器要能区分这两类（给 item 加个 `unanswerable_reason: "undisclosed" | "v1_schema_gap"` 的标注，或单独维护一个 id 列表），分开统计。

### 4.5 建议同时把 v1_loop 在强模型上重跑
见 §2 的 `v1_loop@strong`。这一步依赖 Phase 0b（要先有能跑 v1_loop 的数据库 + key），不阻塞 4.1–4.4。

---

## 5. Validity 清单（进正式报告前必须处理）

| 问题 | 状态 | 处理 |
|---|---|---|
| 模型能力混淆（v1_loop=4o-mini vs generic=GPT-5 级） | 未处理 | §4.5：v1_loop@strong 重跑；报告明确标注模型档次 |
| n=1，非确定性 | 未处理 | §4.3：跑 3 趟报方差 |
| 打分黑盒 + 9/38 待 judge | 部分 | Phase 0b 后 `rescore_generic_baseline.py` 补 judge；黑盒 vs v1 白盒打分的差异在 devlog 002 记了 |
| 题库照 v1 能力出的，"unanswerable" 有歧义 | 未处理 | §4.4：重新分桶 |
| 模型未钉死 | 本次修订处理 | §4.1：`-m gpt-5.6-sol` |
| 训练数据污染（Tier1 知名数字） | 已接受 | 报告注明 Tier3 才是干净信号 |

---

## 6. Agent 调用方式（技术细节）

```
codex exec -m gpt-5.6-sol -C baseline/raw_filings -s read-only --json -o <answer_file> "<prompt>"
```

- `-m gpt-5.6-sol`：钉死模型
- `-C baseline/raw_filings`：工作根目录，Codex 只看得到这个目录，看不到 v2 的代码/prompt
- `-s read-only`：只读沙箱，禁止写文件（安全阀）
- 不传 `--search`：默认关闭联网搜索
- `--json`：JSONL 事件流 → 取 `turn.completed.usage`（token）、数 `command_execution`（工具调用次数）
- `-o <file>`：最终回答单独写文件，省得从 JSONL 里挖
- **flags 全部放在 prompt 前面**（`codex exec [OPTIONS] [PROMPT]`），否则可能被误解析成读 stdin

每题一个全新 `codex exec` 进程——题目之间零共享上下文，否则等于给了记忆能力。

系统提示只给分析师角色，不含 v1 SYSTEM 里那 15 条针对性规则（那些本身就是定制工程）。

---

## 7. 打分口径

题库：`eval_set.json`（Tier 1-2）+ `eval_set_tier3.json`（Tier 3），剔除 `retired: true`，**38 题**。不新造题。

判分：`src/copilot/eval/generic_scoring.py`，按 `type`/`scoring` 路由，复用 v1 harness 的判分函数。与 v1 的关键差异（详见 `docs/devlog/002-generic-agent-baseline-run.md`）：v1 能看 tool-call `steps` 做过程校验，本打分器只有最终文本，是纯结果校验——所以偏弱、偏保守，尤其在"复述原话 vs 意思对但换说法"的边界上。

产出：`data/results/generic_agent_baseline_<ts>.json` + `.md`（每趟一份）；多趟汇总另出一份。摘要抄进 `docs/eval-history.md`（`impl=generic_agent (gpt-5.6-sol)`）。

---

## 8. 决策记录

1. **通用 agent：Codex CLI**（2026-09-04）。这台机器没装 `claude` headless CLI，只有桌面 App；`codex` 已装好并登录（ChatGPT auth）。
2. **模型：`gpt-5.6-sol`，`-m` 显式钉死**（2026-09-05）。原先用 Codex 默认（`gpt-5-codex` 级）。
3. **原文处理：方案 A**（2026-09-04）。不预转纯文本，让 Codex 自己用 bash/ripgrep 处理原始 HTML。
4. **打分按题目 `type`/`scoring` 路由**（2026-09-04），照抄 `score_item_t3` 分支，不自造启发式。
5. **`threshold_only` 边按"≥下限"判**（2026-09-04）。
6. **judge 不可用时 `needs_review`，不强制判错**（2026-09-04）。
7. **重跑要求：钉死模型 + 记全 token/延迟 + 3 趟方差 + unanswerable 重新分桶**（2026-09-05）。

---

## 9. 待办

- [ ] 更新 `scripts/run_generic_baseline.py`：`--model`（默认 `gpt-5.6-sol`）、`--runs N`、flags 前置、完整 token/延迟/工具调用次数聚合、报告里记模型
- [ ] `generic_scoring.py` / driver：`unanswerable` 分两桶（`undisclosed` vs `v1_schema_gap`）
- [ ] 跑 3 趟 `gpt-5.6-sol` 全量 38 题，出多趟汇总
- [ ] Phase 0b 后：`rescore_generic_baseline.py` 补 9 道 judge；`eval-history.md` 填正式数字
- [ ] （依赖 Phase 0b）v1_loop 在 `gpt-5.6-sol` 或同档模型上重跑一遍，出 `v1_loop@strong`
