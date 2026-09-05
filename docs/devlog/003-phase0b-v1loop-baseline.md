---
id: 003
phase: 0
title: Phase 0b — 灌库 + v1_loop baseline
started: 2026-09-05
finished: 2026-09-05
status: done
plan_ref: ../langgraph-migration-plan.md#phase-0
---

## GOAL

在 v2 仓库里复现 v1_loop 的 Tier 1-3 baseline：用 v1 自带数据灌库 → 跑 eval harness → 记 `eval-history.md`。产出 v1_loop 真实分数（三方对比用）+ 8 个 DB 依赖 pytest 转绿。**不做 `v1_loop@strong`**（gpt-5.6-sol 那版），用户暂缓。

## INSPECT

- **能复用**：`pipeline/{seed,embed_chunks}` + `eval/{harness,harness_tier3,harness_router}`，全现成，`--out` 存 JSON。
- **新写**：无。
- **会影响**：DB `financial_copilot` 已有 4 张表的**残缺数据**（companies 6/15、filings 18/148、facts 7368/38966、chunks 969/16342，缺 `supply_edges`）→ `seed --load` 需 `--force`（`TRUNCATE CASCADE` + 重灌全量）。残缺、可从 `data/seed/` 重建、dev 库 → 安全。
- `.env` 的 `DATABASE_URL` 密码仍是占位符 `your-db-password`，但 Postgres.app 本地 trust 认证免密，psycopg2 已验证连通（PG 18.4 + pgvector）。

## PLAN

无代码。执行序列：
1. `seed --load --force`
2. `embed_chunks`（本地 bge-small，~16K chunks）
3. `harness --out data/results/v2_v1loop_baseline.json`
4. `harness_tier3 --out data/results/v2_v1loop_t3.json`
5. `harness_router --out data/results/v2_v1loop_router.json`

**怎么验**：seed fingerprint 匹配 manifest；`pytest -q` 8 个 DB 失败转绿；harness Tier1/2/3 与 v1 历史（100/100/100）同量级；记 latency/token/cost。

## BUILD

- `seed --load --force`：建表 + truncate 残缺 + 灌 `data/seed/*.csv.gz`。结果 companies 15 / filings 148 / financial_facts 38966 / text_chunks 16342 / supply_edges 128 (named 103)，**fingerprint 匹配 v1 快照**。`rewired 0 edge -> chunk links` 是预期（`supply_edge_chunk_links.csv.gz` 0 行，manifest `resolvable: 0`，源库 chunk 指针本就 dangling；`supply_edges` 自带 `source_text` 内联，graph_query 不受影响）。
- `embed_chunks`：16342/16342 embedded（3173 需 window mean-pooling），存 `text_chunks.embedding vector(384)`。
- 3 个 harness 顺跑，全 exit 0，结果存 `data/results/v2_v1loop_{baseline,t3,router}.json`。

## EVAL

| harness | 数据集 | 结果 | latency | 成本 |
|---|---|---|---|---|
| `harness` | eval_set.json v1.4 (30) | Tier1 **100%** · Tier2 **100%** (input-fetch 100%) · retrieval **100%** (passage-hit 100%, judge 2.57/3) · refusal **100%** · **overall 100%**；grounding 33 figures / 0 flagged | 2.53s/题 | $0.0339 |
| `harness_tier3` | eval_set_tier3.json (8) | graph_lookup/fact/trend/comparison/compute **各 100%** · refusal 100% · **overall 100%**；grounding 48 figures / 0 flagged；`t3_unans_apple_supplier_share` **正确拒答** | 3.17s/题 | $0.011 |
| `harness_router` | eval_set_router.json (12) | tool-selection **100%**（dependency→graph_query / qualitative→retrieve_text / procurement_share→refuse 各 100%）| 2.47s/题 | $0.0097 |

回归：`uv run pytest -q` → **143 passed / 0 failed**（灌库前 135/8，8 个 DB 依赖测试全转绿）。

**v1_loop 精确复现 v1 发布结果**（v1 仓库 `eval_results_v1.4_final2.json` / `eval_results_t3_graph.json` 也是 100/100/100）。总成本三个 harness 合计 ~$0.055。

## RECORD

### 决策

### D1. Phase 0b 单独开 devlog 文件（003），不并进 001
- **背景**：001 是 Phase 0a（脚手架），已经很长；Phase 0b 是独立可交付（DB 就绪 + v1_loop baseline）。
- **选择**：003 独立文件，milestone 表加一行（同属 "Phase 0" 但分 0a/0b）。
- **理由**：dev-workflow 里里程碑级任务开独立文件；001 已 done 状态不动。

### D2. `--force` reseed 而不是保留残缺数据
- **背景**：DB 有前次半成品（4 表部分数据、缺 supply_edges）。
- **选择**：`--force` truncate + 重灌全量 v1 快照。
- **否决**：在残缺数据上补差异——fingerprint 对不上，harness 分数不可比。
- **代价**：TRUNCATE 破坏性，但数据残缺 + 可从 `data/seed/` 重建 + dev 库。

### 死胡同 / 坑

- `.env` 的 `DATABASE_URL` 密码是占位符，靠 Postgres.app trust 认证碰巧能连。换个 PG 配置（要密码）就会挂。**后续要把真密码填进去或改成显式 trust DSN。**

### Learning

- **v1_loop 在 v2 环境里 100% 复现**——说明移植（`copilot.agent` 原样拷贝 + `copilot.{retrieval,storage,pipeline}` 路径不变）是干净的，冻结基线可信。
- **v1_loop vs generic_agent（gpt-5.6-sol）三方对比第一版**：
  | | v1_loop (gpt-4o-mini) | generic_agent (gpt-5.6-sol) |
  |---|---|---|
  | 事实/计算题（Tier1-2 答得了的）| 100% | ~100% (17/17 answerable, 10/10) |
  | 供应链结构化查询（Tier3, 带 graph）| 100% (8/8) | 75% (6/8)——没有 graph 工具 |
  | procurement-share 陷阱 | **正确拒答** | **中招**（编代理指标）|
  | schema-gap unanswerable ×3 | 正确拒答 | 都答了（部分事实对）|
  | 每题成本 | ~$0.001 | ~$0.13（≈120x）|
  | 每题延迟 | 2.5–3.2s | ~32s |
  - **"定制工程值不值"的定量答案**：贵 ~120x、慢 ~10x；基础事实/计算打平；定制工程的钱买到的是 **(a) graph 工具的结构化关系查询、(b) 拒答纪律/grounding**——generic agent 恰好在这两处落后。

### Retro

无 workflow 层面的东西要改。`.env` 密码占位符的坑记进本文件 Learning，够了。

## 下一步 / 解锁了什么

- Phase 0（0a+0b）**完成**。三方对比有真实数字。
- 可开 **Phase 1 — 工具层标准化重构**：先定 plan 里的"待确认设计点"清单（信封形态 / 错误枚举 / 粒度 / resolve 层边界）。基线启示：graph 工具接口 + 拒答/grounding 值得多花心思。
- （可选，用户暂缓）`v1_loop@strong`：v1_loop 在 gpt-5.6-sol 上重跑，隔离"模型 vs 架构"。
