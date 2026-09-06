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
