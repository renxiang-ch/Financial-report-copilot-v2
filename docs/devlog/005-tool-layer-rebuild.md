---
id: 005
phase: 1
title: 工具层标准化重构 —— 最小 @tool 包装 → copilot/v2/tools/ 标准库
started: 2026-09-07
finished: -
status: planned
plan_ref: ../langgraph-migration-plan.md#phase-1--工具层标准化重构
---

<!-- 定案见 plan 的 Phase 1 表。这里只记 INSPECT/PLAN 的落地和执行痕迹。 -->

## GOAL

把 `copilot/v2/orchestration/graph/tools.py` 的 5 个最小 `@tool` 包装（直接调 `copilot.agent.tools` 冻结函数、返回 `json.dumps`、描述靠 `_desc()` 拼字符串）换成标准工具库 `copilot/v2/tools/`：`content_and_artifact` 信封、Pydantic `args_schema`、`ToolError` 类型化错误、独立 `resolve` 层。领域逻辑（SQL / RRF / 递归 CTE / AST 沙箱）保留。

**为什么**：服务目标 #1 的"工具即 API"一课（`ToolRuntime` / `content_and_artifact` / `wrap_tool_call` / Pydantic schema）；并关掉 Phase 2 A/B 里 citation 集波动的两个根因（`retrieve_text` 年份没锁、`graph_query` 漏传 `supplier`）。

**完成的样子**：见 plan Phase 1 退出标准 —— 5 集 `ab_compare` 不劣于 Phase 2 且年份 scope 差异测得到地收窄；每工具单测含错误路径；v1_loop 冻结不受影响；pytest 全绿。

## INSPECT

- **能复用**：
  - `copilot.agent.tools._resolve_ticker`（ticker + difflib typo 建议）、`_year_scope` / `_latest_filing_year`（fiscal-year scoping）、`_no_edges`（graph 空结果带原因）、AST 沙箱（`_alias_non_identifier_vars` / `_reject_unsafe` / `_depth` / `compute`）—— 逐字搬。
  - `copilot.agent.agent.advertised_metrics()`（从 DB 读 metric label 清单，带 fallback）—— `metric` 的 `Literal` 来源。
  - `copilot.retrieval.hybrid.retrieve_hybrid`、`copilot.storage.db.get_conn` —— 共享基础设施，import 用，不改。
  - `runner.py::_steps_from_messages` 已按 `tool_call_id` 配对 —— 改成读 artifact 即可。
- **新写**：`copilot/v2/tools/{base,resolve,schemas,financials,retrieval,graph,compute,registry}.py` + `tests/test_tools_*.py`。`orchestration/graph` 加 `wrap_tool_call` 错误 middleware + `context_schema`。
- **会影响**：`orchestration/graph/{tools.py 删,build.py,runner.py,middleware.py}`、`ab_compare`（artifact 形状）。**不碰** `src/copilot/agent/`（v1 冻结）、`src/copilot/{storage,retrieval}`（共享，只 import）。v1_loop 完全不动。

## PLAN

- **改什么**：见 plan Phase 1「构建顺序」8 步。
- **怎么验**：
  - `uv run pytest -q`（全绿）+ `tests/test_tools_*.py`（每工具正常 + 错误路径 + resolve 层）
  - `uv run ruff check src/copilot/v2/`
  - `uv run python -m copilot.v2.eval.ab_compare` 依次 eval_set / router / multiturn / tier3 / defects
  - 重点看：router `rt_qual_*` + tier3 `t3_*_dollar_impact` 的 citation 集有没有向 v1 收窄（年份 scope 生效）
  - 记 token / 延迟：artifact 不进上下文，input token 应降

## BUILD

-

## EVAL

-

| 指标 | 值 | 对比 baseline |
|------|-----|--------------|
| ab_compare 5 集 citations/refusal | | vs devlog 004 |
| 平均 input token / 问 | | vs devlog 004（artifact 分流后应降）|
| pytest | | 149 → ? |

## RECORD

### 决策

<见 plan Phase 1 定案表；执行中若有偏离在此记 D1/D2…>

### 死胡同 / 坑

-

### Learning（框架机制）

-

## 下一步 / 解锁了什么

- 工具层稳定后可进 Phase 3（持久化 / 错误恢复 / HITL / 可观测），错误恢复 middleware 直接建在 `ToolError.retryable` 上。
