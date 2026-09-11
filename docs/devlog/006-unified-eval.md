---
id: 006
phase: track-2
title: 评测统一 —— 一个数据集、一个 scorer、三个实现
started: 2026-09-10
finished: -
status: active
plan_ref: ../langgraph-migration-plan.md#3-eval-升级
---

## GOAL

让 **generic_agent / v1_loop / graph** 三方在**同一把尺子**上出分。

**为什么现在做**：Phase 1、2 做完后想比三方，才发现比不了 —— graph 从来没对过标准答案（只做过对 v1_loop 的 parity），generic_agent 用的是另一个 scorer。铁律「Eval 先行」要求闸门先立住，再继续重构。

**完成的样子**：一条命令换 `--impl` 就能出可比的 Tier1/2/3 + refusal + retrieval 数字；v1_loop 跑出来必须复现 Phase 0b 的已知结果（否则说明新 scorer 有偏差）。

## INSPECT

不可比有**三条**，不是一条：

1. **graph 没法打分**：`copilot.eval.harness.run_eval` 第 532/784 行写死 `from copilot.agent.agent import ask`。但 `harness.score_item(item, agent_result)` **本身 impl 无关** —— 它只要 `{answer, steps}`，正是 `v1_loop.ask()` / `graph.run()` 的返回形状。**只有 runner 写死，scorer 没有。**
2. **generic_agent 用另一个 scorer，且有正当理由**：它是 Codex 跑原始文件，**没有 `steps`**。空 steps 过 `harness.score_item`：unanswerable ✅、grounded ✅（纯文本）、numeric ✅（回落文本抽取）、**retrieval ❌**（`_check_key_phrase([], …)` → False → `correct` 恒 False）。**只有 retrieval 一个分支真坏。**
3. **三分桶只在 generic 侧**：`undisclosed` / `v1_schema_gap` / `out_of_scope` 是**数据集 ground truth 的属性**，不是实现的属性。v1_loop 的 refusal 100% 里有 3 道是"拒答了原始 10-K 里其实能答的题"，这个区别被埋掉了。

- **能复用**：`harness.{score_item, _build_tool_trace, _within_tolerance, _within_tolerance_abs}`、`generic_scoring.{_extract_answer_number, _UNANSWERABLE_REASON}`
- **新写**：`copilot/v2/eval/score.py`
- **会影响**：不动 `harness.py`（v1 移植冻结清单里），只 import。不动数据集。

## PLAN

- **改什么**：新增 `src/copilot/v2/eval/score.py` —— runner 注册表（v1_loop 实跑 / graph 实跑 / generic_agent **replay 已存报告**，重跑要 $5 + codex CLI）+ `score_one()` 包一层 `harness.score_item`。
- **两块指标，故意不合并**：
  - **steps-independent**（三方可比）：numeric、grounded、retrieval **judge 分**、refusal（带三分桶）
  - **steps-dependent**（仅 v1_loop / graph）：`passage_hit`、`all_inputs_fetched`、grounding flagged
  - retrieval 出**两个判定**：`correct_strict`（passage_hit ∧ judge≥2，harness 原口径）/ `correct_judge`（judge≥2，跨实现用这个）
- **顺带统一数值抽取**：文本兜底一律用 `_extract_answer_number`（认 `= <数>` 计算行、剥 markdown 链接、跳过 form 类型），比 `harness._extract_number` 稳；有 `compute` step 时仍优先 step。符号两向都试（散文带符号，数字不带）。
- **怎么验**：v1_loop 必须复现 Phase 0b —— Tier1/2 100%、retrieval judge 2.57、refusal 100%。

## BUILD

- `src/copilot/v2/eval/score.py`：`IMPLS` / `_run_v1_loop` / `_run_graph` / `_replay_index` / `_renumber` / `score_one` / `run` / `_summarize` / CLI。
- 报告落 `data/results/score_<impl>_<ts>.json`。

## EVAL

**generic_agent（replay）—— 精确复现已知数字 ✅**

| 指标 | 新 scorer | devlog 002 已知 |
|---|---|---|
| tier1 可答 | 17/17 = 100% | 17/17 |
| tier2 | 10/10 = 100% | 1.0 |
| retrieval judge 均分 | **2.57** | 2.57 |
| retrieval correct(judge) | 100% (7/7) | 7/7 |
| refusal | 0/3 | 0/3 |
| ↳ `v1_schema_gap` | 0/2 | ✓ |
| ↳ `out_of_scope` | 0/1 | ✓ |
| `steps_dependent` | `null`（正确省略）| — |

**v1_loop —— 精确复现 Phase 0b ✅（scorer 无偏差的验收）**

| 指标 | 新 scorer | Phase 0b 已知 |
|---|---|---|
| Tier1 可答 (17) | 100% | 100% ✅ |
| Tier2 (10) | 100% | 100% ✅ |
| retrieval judge 均分 | **2.57** | 2.57 ✅ |
| retrieval correct | 100% | — |
| refusal_accuracy | 100% | 100% ✅ |
| passage_hit / tier2 inputs | 100% / 100% | — |
| grounding flagged | 0 | 0 ✅ |
| input tokens | 208,695 | ~209K ✅ |

**graph —— 统一 scorer 抓到两个 Phase 1 回归，修完后与 v1_loop 打平**

| 指标 | 首测 | 修 EPS 精度后 | **修截断后（跑 2 遍，逐项一致）** |
|---|---|---|---|
| Tier1 可答 (17) | 94.1%（16/17）| 94.1% | **100%（17/17）** |
| Tier2 (10) | 100% | 100% | **100%** |
| retrieval judge | 2.57 | 2.29 | **2.57** |
| retrieval correct | 100% | 85.7% | **100%** |
| refusal_accuracy | 100% | 100% | **100%** |
| steps_dependent 四项 | 100/100/100/0 | 同 | **100/100/100/0** |
| input tokens | 162,343 | 162,343 | **170,765 / 170,748** |
| 平均延迟 | 2.74s | — | **2.36s** |

**三方对比（eval_set 30 题，同一 scorer）**

| | generic_agent | v1_loop | graph |
|---|---|---|---|
| **steps-independent** | | | |
| Tier1 可答 (17) | **100%** | **100%** | **100%** |
| Tier2 (10) | **100%** | **100%** | **100%** |
| retrieval judge 均分 | **2.57** | **2.57** | **2.57** |
| retrieval correct(judge) | 100% | 100% | 100% |
| refusal_accuracy | **0%** (0/3) | 100% | 100% |
| ↳ `v1_schema_gap` | 0/2 | 2/2 | 2/2 |
| ↳ `out_of_scope` | 0/1 | 1/1 | 1/1 |
| over-refusal | 0 | 0 | 0 |
| **steps-dependent** | — *(结构上无定义)* | | |
| retrieval strict / passage_hit | — | 100% / 100% | 100% / 100% |
| tier2 inputs fetched | — | 100% | 100% |
| grounding flagged | — | 0 | 0 |
| **成本** | | | |
| input tokens | — | 208,695 | **170,748**（−18%）|
| 平均延迟 | 31.3s | 3.52s | **2.36s** |
| 成本/题 | ~$0.131 | ~$0.001 | ~$0.001 |

## RECORD

### 决策

### D1. 不强行统一成一个 scorer，而是分两块报
- **背景**：generic_agent 没有 tool trace，`passage_hit` / `all_inputs_fetched` 对它**无定义** —— 这是真实属性，不是待补的缺口。
- **选择**：steps-independent / steps-dependent 两块；retrieval 出双判定。
- **否决**：(a) 强行让三方过同一套含 steps 的检查 → generic 恒 0 分，假的不可比；(b) 把 steps 检查全删 → v1_loop/graph 丢掉真实信号。

### D2. 恢复 v1 的「step["output"] 永远是 dict」不变式（`runner.py`）
- **背景**：graph 首测直接崩在 `harness._build_tool_trace` 的 `out.get("found")`。v1 的 `_run_tool` 错误路径也返回 dict，harness **8+ 处**无保护地 `step["output"].get(...)`；Phase 1 之后工具报错时 `ToolMessage` 无 artifact，`runner` 输出了**字符串**。
- **影响面比崩溃大**：同一个坑埋在 `_collect_citations` / `build_provenance` / `verify_answer` 底下，5 轮 A/B 没触发只因为那些跑里没有工具报错。
- **选择**：`runner._steps_from_messages` 里 `if not isinstance(out, dict): out = {"found": False, "error": str(out)}`。一处修复保护所有 v1 消费者。

### D3. 数值格式化不能损失精度（`financials.py`）
- **背景**：`f"{value:,.0f}"` 把 EPS 6.08 显示成 "6"，模型答 $6.00，golden 6.08 → 失败。artifact 里是精确值，但模型只读 `content`。
- **选择**：`_fmt()` —— 整数值无小数尾，其余保留原位数。
- **A/B 为什么没抓到**：`ab_compare` 只比 citations + refusal。两边引用同一 accession、都没拒答 → 判为匹配。**答案数字错了结构上不可见。**

### D4. `retrieve_text` 段落不截断（`retrieval.py`）
- **背景**：`body[:700]` ≈ 500-token chunk 的 **¼**（10-K 散文 5.4–5.9 chars/token，chunk ≈ 2,900 字符）。BM25 和稠密索引都对**整个 500 token** 打分，所以 chunk 可能正因为第 2,000 字符处的句子而进 top-5，而模型只看到前四分之一。**检索成功了，呈现层把"它为什么成功"扔了。**
- **实测代价**：`ret_glw_business_segments` judge 3→1，retrieval 均分 2.57→2.29。
- **选择**：去掉截断。token 只涨 **5.2%**（162.3K→170.7K），**仍比 v1_loop 低 18%** —— 所以压缩方案（`EmbeddingsFilter` 那类）**不需要**，关闭。
- **教训**：原来的 700 是**没量就拍的数**。真要削上下文，应按**相关度**（丢低相似度句子）而不是按**位置**（砍前 N 字）。

### Learning（框架 / 方法）

- **scorer 和 runner 要分开**：`harness.score_item` 早就是 impl 无关的，只有 `run_eval` 把 runner 焊死了。评测代码里"跑"和"评"耦合，是后来加一条对比臂时最先撞的墙。
- **"可比"不等于"同一个数"**：能力不同的实现之间，有些指标结构上就无定义。诚实做法是分块 + 标注，而不是凑一个能对齐的数字。
- **ground truth 的分桶属于数据集，不属于实现**：三分桶原来只在 generic scorer 里，导致 v1_loop 的 100% refusal 掩盖了"其中几道是相对 v1 schema 才不可答"。
- **parity 测试测不出质量**：`ab_compare` 5 集全绿的同时，graph 身上带着两个真回归（EPS 显示成 6、检索文本丢 76%）。**只比"两边是否一致"的闸门，对"两边一起错"和"内容错但引用对"完全失明。** 绝对分数的闸门是另一件事，两个都要。
- **`content_and_artifact` 引入了一类 v1 不可能有的 bug**：模型看 `content`、程序看 `artifact`，这个划分可以搞错，而且**静默**。本轮三次同类（段落进 artifact / 数值四舍五入 / 段落截断），全是"`content` 没有无损携带模型完成任务所需的东西"。v1 只 dump 一个 dict，不存在这个划分。
- **指标驱动优化的陷阱**：截断是为了压 input token（Phase 1 被考核的数），而当时手上的工具**结构上测不到质量代价**。修复后实测：无损全文只多 5.2% token，那次"优化"从头到尾是净损失。

### Retro

`ab_compare`（parity）作为唯一闸门跑了整个 Phase 1/2，直到这轮才发现它测不到答案内容。**建议**：Phase 3 起 `score.py`（绝对分）和 `ab_compare`（parity）都进验证清单，不是二选一。已同步到 `docs/dev-workflow.md` 的验证清单表。

## 下一步 / 解锁了什么

- 三方对比表填满了，`docs/eval-history.md` 按同一口径可追加。
- graph 第一次有**绝对质量分**，不再只有 parity —— 并且立刻还债两个回归。
- 结论：**eval_set 上三方在可答题上完全打平**（Tier1/Tier2/retrieval judge 逐项相同）。这个集**已饱和、无分辨力**。真正的差异只剩 refusal（而 eval_set 的 3 道全是 `v1_schema_gap`/`out_of_scope`，**一道真 `undisclosed` 陷阱都没有**）。
- 因此优先级上移：**扩 `undisclosed` 陷阱题（1→8~10 道）** 和 **建 Tier 4**，否则 Phase 4 做完无法证明其价值。
