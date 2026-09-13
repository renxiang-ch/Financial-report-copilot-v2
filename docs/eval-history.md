# Eval 分数历史

> 每个里程碑完成时追加一行。用于看回归 / 进步趋势。
> `impl` 列三选一：`generic_agent`（零定制通用 agent + 原始 10-K，见 `docs/generic-agent-baseline.md`）/ `v1_loop`（手写 ReAct baseline）/ `graph`（LangGraph 版）。
> Tier 1–3 沿用 v1 harness 判分口径（数值容差 / 集合比对）；Tier 4 为 Phase 4 新增的分析题 rubric 均分（0–1）。
> 数字来源：`src/copilot/eval/`。空值 = 该阶段未测；`—` = 该 impl 不适用（如 generic_agent 无 tool 调用概念）。

| 日期 | 里程碑 | impl | Tier1 | Tier2 | Tier3 | Tier4 | p95延迟(s) | token/问 | tool调用/问 | 备注 |
|------|--------|------|-------|-------|-------|-------|-----------|----------|-------------|------|
| 2026-09-05 | generic-agent-baseline | generic_agent (Codex CLI, **gpt-5.6-sol**, n=1) | 0.85 (17/20) | 1.0 (10/10) | 0.75 (6/8) | — | 47 | ~132K in（87% cached）/ ~818 out | 4.2 | Tier1 只看答得了的题 17/17；3 个非满分是 `v1_schema_gap`/`out_of_scope` unanswerable（单独分桶）。Tier3 2 miss = procurement-share 陷阱 + rank 题与 v1 rubric 分歧。成本按 gpt-5.6-sol 计价 **$4.96/全套、$0.131/题**（vs v1_loop gpt-4o-mini ~$0.001/题，≈117x）。见 `docs/devlog/002` 补记 2/3 |
| 2026-09-04 | (preliminary，作废) | generic_agent (Codex 默认模型, n=1) | 0.85 | 1.0 | 0.75 | — | 56 | ~132K in | 4.5 | 用最终 scorer 重打后与 gpt-5.6-sol **逐题零差异**（0 item diff）；仅留作 scorer 迭代记录 |
| 2026-09-05 | Phase 0b | v1_loop (gpt-4o-mini) | 100% | 100% | 100% | — | — | ~7K in / ~140 out（30 题合计 209K/4.2K）| — | v2 环境精确复现 v1 发布结果。retrieval 100%（judge 2.57/3）· refusal 100% · router tool-selection 100% · grounding 0 flagged。3 harness 合计成本 $0.055（≈$0.001/题）。avg latency 2.5–3.2s/题。见 `docs/devlog/003` |
| —    | (待做) | v1_loop@strong (gpt-5.6-sol) | | | | — | | | | 用户已明确不做（v1_loop 已 100%，无 headroom）|
| 2026-09-06 | 004 Step 1 | graph (create_agent, gpt-4o-mini) | ✓ | ✓ | 未跑 | — | ~2.3s mean | 同 v1_loop 量级 | 1.7 | eval_set.json 30 题 `ab_compare` 对 v1_loop：citations/refusal 30/30 匹配，0 语义分歧。tier3/router 未跑（Step 3）。见 `docs/devlog/004` |
| 2026-09-07 | 004 Step 3 | graph (create_agent + routing middleware, gpt-4o-mini) | ✓ | ✓ | 未跑 | — | — | 同量级 | — | 路由 middleware（`before_model` refuse 短路 + `wrap_model_call` force_tool）。router 集 12 题 `ab_compare` 对 v1_loop：**refusal 12/12**、force_tool 首轮 pin `graph_query` 确认；citations 7/12（5 处差异为 wrapper 年份范围 + 模型非确定性，非路由回归）。回归：eval_set.json 仍 30/30 citations+refusal；`pytest` 143/0。见 `docs/devlog/004` |
| 2026-09-07 | 004 Step 4-6 (Phase 2 完成) | graph (create_agent + 6-hook middleware, gpt-4o-mini) | ✓ | ✓ | ✓ | — | 同量级 | 同量级 | — | slots/clarify/history-trim middleware + 多轮 `thread_id`+checkpointer。5 eval 集 `ab_compare` 对 v1_loop（67 组）：**refusal 0 不匹配、0 行为回归**。eval_set 30/30 cit+ref · router 7/12 cit（复跑 7–9）/12 ref · multiturn(11t) 10–11 cit/11 ref（年份继承生效：`mt_year_carries` t3 = $311,266,860 逐位对）· tier3 6/8 cit/8 ref · defects 5/6 cit/6 ref。citation 集差异全部 `retrieve_text` 广度 / `graph_query` 非确定性 / 代词范围，数处 graph 更紧。`_refused()` 改用 v1 `looks_like_refusal`。`pytest` 149/0（+6）。见 `docs/devlog/004` |
| 2026-09-08 | 005 (Phase 1 完成) | graph (create_agent + 标准工具库, gpt-4o-mini) | ✓ | ✓ | ✓ | — | 同量级 | **~4.4K in/问**（Phase 2 ~5.5–7.8K，artifact 分流）| — | 工具层标准库：`content_and_artifact` + Pydantic schema + `ToolError` + 预制 retry/error/limit middleware + `resolve` 层 + `state_schema.resolved`。A/B 5 集：**refusal 66/67、0 行为回归**。eval_set 29/30 cit（1 处模型没内联引用）/30 ref · router 9/12 cit（复跑 7–9）/12 ref · multiturn(11t) **11/11**（引用变满）· **tier3 8/8**（引用 6→8，年份 scope 修复）· defects 5/6 cit/6 ref。`pytest` 167/0（+18）。见 `docs/devlog/005` |

### 统一 scorer 口径（2026-09-11 起，`copilot.v2.eval.score`）

> 以下三行是**第一次三方同尺子**的绝对分数（此前 graph 只有对 v1_loop 的 parity，从未对过标准答案）。数据集 `eval_set.json`（30 题）。指标分两块：**steps-independent** 三方可比；**steps-dependent** 仅 v1_loop / graph（generic_agent 无 tool trace，结构上无定义）。retrieval 用 `correct_judge`（judge≥2）跨实现比。

| 日期 | impl | Tier1可答(17) | Tier2(10) | retrieval judge | refusal | steps-dep (strict/hit/inputs/flagged) | in-tokens | 延迟 | 备注 |
|------|------|---|---|---|---|---|---|---|------|
| 2026-09-11 | **generic_agent** (Codex CLI + gpt-5.6-sol, replay) | **100%** | **100%** | **2.57** | 0% (0/3)：`v1_schema_gap` 0/2 · `out_of_scope` 0/1 | — | — | 31.3s | 三道不可答全是"原始 10-K 能答、v1 schema 没有"，判错是 v1 口径所致，非能力问题。真 `undisclosed` 陷阱在 tier3（它 0/1 失败）|
| 2026-09-11 | **v1_loop** (gpt-4o-mini) | **100%** | **100%** | **2.57** | **100%** | 100/100/100/0 | 208,695 | 3.52s | 精确复现 Phase 0b，用作新 scorer 无偏差的验收 |
| 2026-09-11 | **graph** (create_agent + 标准工具库, gpt-4o-mini) | **100%** | **100%** | **2.57** | **100%** | 100/100/100/0 | **170,748**（−18% vs v1_loop）| **2.36s** | 跑 2 遍逐项一致。统一 scorer 抓到并修复两个 Phase 1 回归：EPS `:,.0f` 显示成 6（→`_fmt`）、`retrieve_text` `[:700]` 丢 76% 文本（→不截断，token 仅 +5.2%）。`ab_compare` multiturn 10/11 cit + 11/11 ref（唯一分歧是 eval 集自标不稳定的 compute 轮，两边同为正确值 $311,266,860）。见 `docs/devlog/006` |

| 2026-09-11 | **graph** — Phase 3（持久化 / 降级 / grounding 回边）| **100%** (17/17) | **100%** | **2.43** | **100%** | 100/100/100/0 | 170,724 | 2.52s | **补记（2026-09-12 发现本行当时漏归档）**。judge 2.57→2.43 是噪声：`correct_judge` 仍 100% (7/7)，只是某题 judge 3→2。A/B 5 集 58/67 cit + **67/67 ref**。`pytest` 173/0。见 `docs/devlog/007` |
| 2026-09-13 | **graph** — 3.2 LangSmith + 移除 `ToolRetryMiddleware` | **100%** (17/17) | **100%** | **2.57** | **100%** | 100/100/100/0 | 170,758 | **2.41s** | **零回归**（移除的是从未触发的死代码）。judge **2.43→2.57 回弹**，三次观测全落在 ±0.14 band 内 —— 噪声底的推导由此有了多点支撑。A/B 5 集 **59/67 cit + 67/67 ref**（eval_set 29/30 · router 7/12 · tier3 **8/8** · defects 5/6 · multiturn 10/11，全在已记录波动带）。`pytest` **186/0**（+5 个工具错误恢复行为测试）。见 `docs/devlog/007` §3.2、`docs/devlog/008` |

### 关于 retrieval judge 的噪声底（2026-09-13 确立）

7 道检索题、judge 是 0–3 整数 → **一道题变动 1 分 = 均分变动 1/7 ≈ 0.14**，这就是该指标的最小刻度。三次观测 2.57 / 2.43 / 2.57 全部落在一个刻度内，且 `correct_judge` 始终 100%。

**含义**：**低于 ±0.14 的 judge 变化不构成信号。** citation 匹配同理，在 67 组上有 ±4 的逐次波动（58 / 59 / 62）。Phase 3 全部改动的可测效应是 **0**，小于这两个噪声底 —— 这是"扩陷阱题 + 建 Tier 4"优先级上移的量化依据，见 `docs/devlog/008`。

**结论**：eval_set 上三方在**可答题上完全打平**（Tier1/Tier2/retrieval judge 逐项相同）—— 该集**已饱和、无分辨力**。唯一差异在 refusal，而 eval_set 那 3 道**没有一道是真 `undisclosed` 陷阱**。下一步评测投入应放在**扩陷阱题**和**建 Tier 4**，而非重复跑此集。
