---
id: 005
phase: 1
title: 工具层标准化重构 —— 最小 @tool 包装 → copilot/v2/tools/ 标准库
started: 2026-09-07
finished: 2026-09-08
status: done
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
  - `runner.py::_steps_from_messages` 已按 `tool_call_id` 配对 —— 改成读 `ToolMessage.artifact` 即可。
  - **预制 middleware**（不手写）：`ToolRetryMiddleware` / `ToolErrorMiddleware`（需 `langchain>=1.3.14`，pin `1.4.0` ✓）/ `ToolCallLimitMiddleware`。MCP 核对：LangChain 无原生 tool-result 缓存中间件 → 进程内缓存手做。
- **新写**：`copilot/v2/tools/{base,resolve,schemas,financials,retrieval,graph,compute,registry}.py` + `tests/test_tools_*.py`。`orchestration/graph`：`build.py` 加 3 个预制 middleware + `state_schema`（`resolved` 字段）；`middleware.py` 加 `_resolve` `@before_model` hook。
- **会影响**：`orchestration/graph/{tools.py 删,build.py,runner.py,middleware.py}`、`ab_compare`（改读 `.artifact`）。**不碰** `src/copilot/agent/`（v1 冻结）、`src/copilot/{storage,retrieval}`（共享，只 import）。v1_loop 完全不动。
- **修正（2 轮 MCP 核对）**：resolve 结果原计划塞 `context_schema` —— 错，`context` 是 invoke 静态只读、middleware 写不了。改走自定义 `state_schema`（端口首次真正需要 `state_schema=`，理由正当）。错误处理原计划手写 `@wrap_tool_call` —— 改用预制 `ToolRetryMiddleware`（inner，`on_failure="error"`）+ `ToolErrorMiddleware`（outer）。

## PLAN

- **改什么**：见 plan Phase 1「构建顺序」8 步。
- **怎么验**：
  - `uv run pytest -q`（全绿）+ `tests/test_tools_*.py`（每工具正常 + 错误路径 + resolve 层）
  - `uv run ruff check src/copilot/v2/`
  - `uv run python -m copilot.v2.eval.ab_compare` 依次 eval_set / router / multiturn / tier3 / defects
  - 重点看：router `rt_qual_*` + tier3 `t3_*_dollar_impact` 的 citation 集有没有向 v1 收窄（年份 scope 生效）
  - 记 token / 延迟：artifact 不进上下文，input token 应降

## BUILD

新增 `src/copilot/v2/tools/`：

- `base.py` —— `ToolErrorKind`(StrEnum) + `ToolError`(异常，`retryable` 从 kind 默认)、`pack(content, artifact)`、`_Memo`(JSON-key 的有界 FIFO)、`financial_tool(name, args_schema, cache=)` 装饰器（= `@tool(response_format="content_and_artifact")` + 可选 memo）。
- `resolve.py` —— `known_tickers` / `latest_filing_year` / `edge_sides`（`@lru_cache`，DB）、`resolve_ticker`（未知 ticker → `raise ToolError(UNKNOWN_TICKER, did_you_mean=…)`）、`relation_side_error`（问错方向 → `raise WRONG_RELATION_SIDE`；两边都给或真无边 → 不 raise）、`year_scope`（保留 v1 的 `(year, reason)`，含 latest-filing 兜底）。
- `schemas.py` —— 每工具 Pydantic `args_schema`。`QueryFinancialsArgs.metric`：`json_schema_extra={"enum": _METRICS}` + `field_validator` 对不在 DB label 集里的 `raise ToolError(BAD_ARGUMENT, did_you_mean=…)`。`RetrieveTextArgs` 无 `query` 字段。
- `financials.py` / `retrieval.py` / `graph.py` / `compute.py` —— 一工具一模块。SQL / RRF / 递归 CTE / AST 沙箱**逐字**从 `copilot.agent.tools` 搬。返回 `pack(一行摘要, 完整 dict)`。`retrieve_text(runtime: ToolRuntime, ...)`：question 从 `runtime.state["resolved"]` 取（兜底最后一条 HumanMessage）、year 用 `模型给的 arg or resolved["fiscal_year"] or year_scope(...)`。
- `registry.py` —— `TOOLS = [query_financials, list_metrics, retrieve_text, graph_query, compute]`。

改 `orchestration/graph/`：

- `build.py` —— `GraphState(AgentState)` 加 `resolved: NotRequired[dict]`；`tools=` 换成 `copilot.v2.tools.registry.TOOLS`；middleware 列表尾部加 `_tool_middleware()` = `ToolRetryMiddleware(retry_on=<ToolError.retryable>, on_failure="error")` + `ToolErrorMiddleware(on_error=on_tool_error)` + `ToolCallLimitMiddleware(run_limit=12, thread_limit=40)`；`create_agent(state_schema=GraphState)`。
- `middleware.py` —— 加 `_resolve` `@before_model`（每轮首调写 `state["resolved"] = {question, fiscal_year(from slots)}`）；模块级 `on_tool_error(exc, request)`（`ToolError` → 披露 `kind`+`hint`+`did_you_mean`；其它 → `None` 传播）。列表变 `[_trim, _resolve, _guard, _active_context, _force_first_tool]`。
- `runner.py` —— `_steps_from_messages` 优先读 `ToolMessage.artifact`（就是 v1 形状的完整 dict），error message 无 artifact 时回落 content。
- **删** `orchestration/graph/tools.py`（最小包装）。

`tests/test_v2_tools.py` —— 18 个：resolve（known/typo/wrong-side/year_scope）、metric validator、每工具 happy + error 路径、`on_tool_error` 只披露 `ToolError`、`_Memo` 缓存、registry 形状（`retrieve_text.args` 不含 `runtime`）。

**中途修**：`ToolErrorKind(str, enum.Enum)` → ruff 要 `enum.StrEnum`（Py3.12）。

## EVAL

- **单元**：`pytest` **167/0**（+18）；`ruff` 干净。
- **工具直调 smoke**：5 工具 happy + 5 error 路径逐一验证（`APPL`→UNKNOWN_TICKER retryable、FY1999→NOT_FOUND terminal、`graph_query(customer=CRUS)`→WRONG_RELATION_SIDE、`__import__`→BAD_EXPRESSION 等）。
- **端到端 smoke**（单轮 4 题）：`query_financials` / `refuse` / `retrieve_text`(自动 scope 到 CRUS FY2026) / `graph_query` 均正常；input token/问 ~4.4K（Phase 2 同题 ~5.5–7.8K，**artifact 分流生效**）。
- **A/B 5 集回归**（v1_loop vs graph，gpt-4o-mini）：

| 集 | citations | refusal | vs Phase 2 (devlog 004) |
|---|---|---|---|
| eval_set (30) | 29/30 | **30/30** | cit 30→29（那 1 处是模型没内联引用，非确定性）|
| router (12) | 9/12（复跑 7–9）| **12/12** | 持平（`graph_query` 参数非确定性 + 引用面）|
| multiturn (11t) | **11/11** | 11/11 | cit ↑（10–11 → 11，年份继承稳）|
| tier3 (8) | **8/8** | 8/8 | **cit ↑ 6→8**（年份 scope 修复）|
| defects (6) | 5/6 | 6/6 | 持平 |

**5 集合计：refusal 66/67，0 行为回归。tier3 引用 6→8、multiturn 引用变满 —— 年份 scope 的目标达成。**剩余 citation 集差异逐条查过：`graph_query` 选 `trend` vs latest、模型偶尔不内联引用，均非工具层引入。

- **成本**：单轮 smoke input token/问 ~4.4K（Phase 2 同题 ~5.5–7.8K）—— `content_and_artifact` 把结构化数据移出上下文。
- **单元**：`pytest` **167/0**（+18 `test_v2_tools.py`）；`ruff` 干净。

## RECORD

### 决策

见 plan Phase 1 定案表。执行中的偏离：

### D1. `retrieve_text` 的 `content` 必须带段落全文，不能只放 artifact
- **背景**：第一版按数值工具的思路做 —— `content` = `"5 passage(s)... Top: '<160字预览>'"`，完整段落进 artifact。A/B 首轮 eval_set 退化到 24/30 cit + 26/30 ref，6 道 `ret_*` 定性题模型拿到 5 段却答"I cannot find"。
- **根因**：`retrieve_text` 的任务就是把 prose 喂给模型，段落文本**是模型完成任务所需**，不是"仅供程序追溯"。
- **修复**：`content` = 每段 ≤700 字全文 + 其 citation；artifact 仍留完整记录（score/section）。重测退化全消（29/30 + 30/30）。
- **教训**：`content_and_artifact` 的划分标准是"模型完成任务所需 vs 仅供程序追溯"，不是"摘要 vs 全量"。每个工具单独判。

### 死胡同 / 坑

- `ToolErrorKind(str, enum.Enum)` → ruff `UP042` 要 `enum.StrEnum`（Py3.11+）。
- 计划过用 `context_schema` 传 resolve 结果 —— `context` 是 invoke 静态只读，middleware 写不了。改 `state_schema` 的 `resolved` 字段（`GraphState(AgentState)` + `NotRequired[dict]`）。

### Learning（框架机制）

- **`content_and_artifact`**：`@tool(response_format="content_and_artifact")` 返回 `(text, dict)` → `ToolMessage(content=text, artifact=dict)`。`content` 进模型上下文，`artifact` 只给程序（`runner.py` 读它重建 v1 形状 `steps`）。省 token，但"什么该进 content"要按工具判（见 D1）。
- **错误处理不用手写**：`ToolRetryMiddleware(retry_on=<判 ToolError.retryable>, on_failure="error")` 放 inner + `ToolErrorMiddleware(on_error=…)` 放 outer + `ToolCallLimitMiddleware(run_limit=…)`。我们只写 `ToolError` 异常类型和 15 行 `on_tool_error` 披露策略。
- **`state_schema` 何时才该加**：Phase 2 没加（route/slots 是 messages 的纯函数）；Phase 1 加了（`resolved` 是 middleware 每轮加工、跨节点传、messages 里没有的派生值）。判据：能从 messages 推的现算，middleware 加工要跨节点传的进 state，调用方 invoke 时就知道的静态依赖进 context。
- **`ToolRuntime`**：`runtime: ToolRuntime` 参数（保留名，schema 里不出现），给 `.state`/`.context`/`.store`/`.tool_call_id`。取代 `InjectedState` 等散装注入。传 `args_schema` 时它仍被识别注入（`retrieve_text.args` 不含 `runtime`，但运行时能拿到 state）。
- **横切逻辑的位置**：跨轮的（年份继承，需要完整历史）放 `_resolve` middleware，每轮一次；单次调用的（"没给年→用最新 filing"）留在工具里。别把需要历史的塞进工具。
- **重接口不动领域逻辑**：SQL / RRF / 递归 CTE / AST 沙箱逐字从 `copilot.agent.tools` 拷；变的只有信封、错误契约、schema、横切层。A/B 只比端到端就是为了让"接口变了行为没变"可验证。
- **先查预制件**：错误处理、循环熔断两处第一版都想手写 middleware，MCP 查文档才知有 `ToolRetryMiddleware`/`ToolErrorMiddleware`/`ToolCallLimitMiddleware`，且文档写明组合顺序。

## 下一步 / 解锁了什么

- **Phase 1 完成**。工具层是标准库了：`content_and_artifact` + Pydantic schema + `ToolError` + 预制 retry/error/limit middleware + `resolve` 层 + `state_schema` 的 `resolved`。
- **解锁 Phase 3**：错误恢复 middleware 直接建在 `ToolError.retryable` 上（`ToolRetryMiddleware` 已在，可加 repair 节点 / `.with_fallbacks`）；`ToolRuntime.store` 给长期记忆；`ToolRuntime.stream_writer` 给 Phase 5 SSE 进度。
- 遗留（非阻塞）：`graph_query` 选 `trend` vs latest 的引用面差异、模型偶尔不内联引用 —— 提示词层面，非工具层。
