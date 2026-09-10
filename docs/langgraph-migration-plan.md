# LangGraph 重构规划：Financial Report Copilot v2

> 目标：在 v1（自建 ReAct Loop）架构基础上，用 **LangChain + LangGraph** 重写 Agent 编排层与工具层；
> 同时把分析能力从"单跳事实问答"升级到"公司级深度分析"，能回答长难问题。
>
> 存储 / 检索算法 / 图遍历 / 摄取管线保留；工具的领域逻辑（SQL、RRF 融合、递归 CTE、AST 沙箱）保留，接口与横切层重写。

---

## 0. 现状盘点（v1 baseline）

来源：`renxiang-ch/Financial-Report-Research-Copilot`

### 编排层（要被替换的部分）
| 模块 | 作用 |
|---|---|
| `agent/agent.py::_ask_openai()` | 手写多轮 tool-calling 循环，`MAX_ROUNDS=10`，`LLM_TIMEOUT_S=90` |
| `agent/model_router.py` | 问题预路由：`refuse` / `force_tool` / `auto`，并选模型 |
| `agent/conversation.py` | append-only 的 Q/A 历史，丢弃中间 tool 结果，trim 到 6 轮 / ~3000 tokens |
| `agent/slots.py` | 槽位继承：从追问文本重新推导 fiscal year 等约束，作为"可被推翻的假设"注入 |
| `agent/clarify.py` | 追问前的澄清预处理 |
| `agent/grounding.py` / `provenance.py` / `authority.py` | 事后 grounding、来源追溯、权限判断 |
| `_run_tool` + `ThreadPoolExecutor` | 手动并行 tool 调用；错误对象带 `recoverable` 标志 |
| 事后 `build_provenance` / `verify_answer` / `_collect_citations` | 追溯每个数字来源、校验、收集真正被引用的 citation |

### 工具层（**整体重构**；v1 实现作参考规格，不直接复用）
| 工具 | 签名 | 说明 | 重构时 |
|---|---|---|---|
| `query_financials` | `(ticker, metric, fiscal_year=None, form="10-K")` | 从 Postgres 取 XBRL 数字 | 逻辑保留，接口/信封重做 |
| `list_metrics` | `(ticker, form="10-K")` | 枚举可用指标及年份 | 可能并入 `financials` 工具 |
| `retrieve_text` | `(query, ticker=None, k=5, fiscal_year=None)` | 混合检索 BM25 + pgvector，RRF 融合 | 融合逻辑保留，包成标准检索工具 |
| `graph_query` | `(customer=None, supplier=None, fiscal_year="latest", depth=1)` | 供应链图，递归 CTE | 逻辑保留，接口重做 |
| `compute` | `(expression, variables)` | AST 白名单沙箱算术，LLM 永不自己算数 | **沙箱实现保留（安全关键）**，仅接口重做 |

v1 工具的不标准之处（重构要解决）：返回 shape 不统一（`{"found":...}` vs `{"ok":...}`）、错误契约临时（手塞 `recoverable` dict）、无分页/限流、docstring 不是写给 LLM 的、ticker 解析等横切逻辑散落各处。

### 基础设施（保留）
- 存储：PostgreSQL + pgvector 0.8.2
- Embedding：BAAI/bge-small-en-v1.5（本地，384 维）
- API：FastAPI；前端：Streamlit
- 可观测性：已依赖 `langfuse>=2.0`
- Eval：`eval/harness.py` `harness_router.py` `harness_tier3.py` + `probe_*`（router / retrieval / clarify / truncation / year-scope）
- 数据快照：15 公司、148 filings、38,966 XBRL facts、16,342 text chunks、128 供应链边，FY2014–FY2026

### v1 的结构性短板（决定了升级方向）
1. **扁平循环**：无显式 plan，10 轮上限对多跳分析题不够。
2. **历史丢中间结果**：追问时无法复用上一轮证据，只能重查。
3. **状态是隐式的**：`messages` list + 返回 dict 混在一起，难以 checkpoint / 回放 / 恢复。
4. **无持久化**：进程重启即丢会话；无法断点续跑。
5. **扩展靠改循环体**：加一个新分析能力要动 `_ask_openai` 主干。

---

## 1. 总体策略

**编排层和工具层都重写；存储/检索/图算法保留**：

```
┌─────────────────────────────────────────────┐
│  编排层  orchestration/                       │  ← 重写
│  ┌───────────────┐   ┌──────────────────┐    │
│  │ v1_loop/ 冻结  │   │ graph/ LangGraph │    │
│  │ (行为基线)     │   │ (新实现)         │    │
│  │ 自带旧工具副本 │   │ 用新工具库       │    │
│  └───────────────┘   └──────────────────┘    │
├─────────────────────────────────────────────┤
│  工具层  tools/  ← 标准化重构（统一信封/错误/  │  ← 重写
│                   schema/provenance/限流）    │
├─────────────────────────────────────────────┤
│  存储 / 检索算法 / 图遍历 / 摄取  ← 保留        │
│  （SQL、RRF 融合、递归 CTE、AST 沙箱不动）      │
└─────────────────────────────────────────────┘
```

三条铁律：
- **先对齐再增强**：LangGraph 版必须在 Tier 1–3 上打平 v1，才允许上多智能体。
- **Eval 先行**：把 v1 的 eval harness 移植好、加 A/B 对比脚本，作为每个 Phase 的验收闸门。
- **A/B 只比端到端**：工具层重构后 v1_loop 与 graph 不共用工具实现，比较只看答案/引用/拒答行为/数字，不比工具内部。v1_loop 作为"行为基线"，容忍其旧实现。

---

## 2. 里程碑与执行步骤

> 时间以"相对里程碑"计，单人节奏每段约 1–2 周。今天 2026-09-03。

### Phase 0 — 脚手架与护栏（先做）

**目标**：新仓库能同时跑 v1 和 LangGraph 两套编排，eval 能对比。

1. 初始化仓库：`git init`，`uv init`，Python 3.11+，复制 v1 的 `pyproject.toml` 依赖。
2. 新增依赖：
   ```
   langgraph
   langchain
   langchain-openai
   langgraph-checkpoint-postgres
   langsmith
   ```
   保留 `openai` `pgvector` `psycopg2-binary` `rank-bm25` `sentence-transformers` `langfuse`。全部 **pin 精确版本**（LangGraph API 变化快）。
3. 目标目录结构（**实际 Phase 0 与此有出入**，见下方注）：
   ```
   src/copilot/
     tools/            # Phase 1 标准化重构；Phase 0 先放 v1 原样拷贝
     orchestration/
       v1_loop/        # v1 agent.py 原样拷贝并冻结，仅作行为基线
       graph/
         state.py      # 类型化 State
         router.py     # 预路由节点
         nodes/        # agent / tool / verify / provenance / clarify / synthesize
         subgraphs/    # simple_qa / deep_research / 各 specialist
         checkpointer.py
         build.py      # 组装 StateGraph
     ingestion/
     eval/             # 移植 v1 harness + A/B + 新 Tier
   ```
   > **实际（2026-09-05）**：v1 代码原样拷成 `copilot.agent`（不是 `_v1_frozen/` 子目录），
   > `orchestration/v1_loop.py` 是一行 shim。`orchestration/graph/` 的 Phase 0a stub 已删
   > —— 用户要先学 LangGraph 再用，Phase 2 从零重建。deps 仍 pin。
4. 移植 eval harness（Tier 1–3、router/retrieval probes）到新仓库，**跑通 v1_loop**（用 `_v1_frozen` 工具），记录 baseline 分数与 latency/token。
5. 写 `eval/ab_compare.py`：同一批问题分别过 v1_loop 和 graph，输出端到端 diff（答案、引用、拒答、耗时、token）。

**退出标准**：`uv run eval` 能对 v1_loop 复现 v1 的 Tier 1–3 分数；A/B 脚本能跑（graph 侧暂时空实现）。

---

### Phase 1 — 工具层标准化重构   （devlog 005；2026-09-07 定案，2 轮用 docs+reference MCP 核对过 API）

**目标**：把现在的最小 `@tool` 包装（`copilot/v2/orchestration/graph/tools.py`，直接调 `copilot.agent.tools` 冻结函数）换成一套标准工具库 `copilot/v2/tools/`。领域逻辑（SQL / BM25+pgvector RRF / 递归 CTE / AST 沙箱）**保留**，接口 / 信封 / 横切层重写。直接收益：关掉 Phase 2 A/B 里 citation 集波动的两个根因（`retrieve_text` 年份没锁、`graph_query` 漏传 `supplier`）。

**范围裁剪（按当前进度）**：`create_agent` 端口已完成、跑在最小包装上、5 个 eval 集 refusal 全绿。Phase 1 只做"让工具本身成为像样的 API"，不碰编排；不为 Phase 4 的假想工具提前抽维度。每条设计点标了 **【本次】** / **【推迟】**。

#### 定案

| 设计点 | 决定 | 依据 |
|---|---|---|
| **返回信封** 【本次】 | `@tool(response_format="content_and_artifact")` —— 返回二元组 `(给模型的紧凑文本, 完整 dict)` → 框架建成 `ToolMessage(content=…, artifact=…)`。**不**自造 `{ok,data,error}` 包装 | reference 核对：`response_format: Literal['content','content_and_artifact']`。`runner.py` 读 `ToolMessage.artifact` 重建 v1 形状 `steps`；docs 的 retrieval-metadata 例子就是这个用法（content=模型引用的段落，artifact=accession/分数/年份）|
| **错误** 【本次，改用预制件】 | 定义 `ToolError(kind: Enum, retryable: bool, hint: str)` 当异常类型（这是我们的）。**管道用两个预制 middleware**：`ToolRetryMiddleware`（inner，`retry_on=lambda e: isinstance(e, ToolError) and e.retryable`，`on_failure="error"`，指数退避+jitter）+ `ToolErrorMiddleware`（outer，`on_error(exc, req)`：`ToolError` 非 retryable → 返回 `hint` 字符串给模型；其它异常 → 返回 `None` 传播）。取代 v1 手塞的 `recoverable` dict | reference：`ToolErrorMiddleware`（需 `langchain>=1.3.14`，当前 pin `1.4.0` ✓）、`ToolRetryMiddleware`。docs 明确 retry 放 inner + `on_failure="error"` 让异常穿到 error middleware。**修正**：不再手写 `@wrap_tool_call` |
| **循环护栏** 【本次，新增】 | `ToolCallLimitMiddleware(run_limit=…, thread_limit=…, exit_behavior="continue")` —— 单工具 / 全局调用次数上限。框架版的 v1 `MAX_ROUNDS=10`，也是 Phase 4 fan-out 预算的种子 | reference：`ToolCallLimitMiddleware`。廉价安全网，顺手加 |
| **args schema** 【本次】 | 每工具一个 Pydantic v2 `args_schema`，字段级 `Field(description=…)`；`metric` 用 `Literal[…]`（label 清单从 DB 读，同 v1 `advertised_metrics()`）。干掉现在 `_desc()` 拼字符串的 hack。简单签名的（`compute` / `list_metrics`）可退而用 `@tool(parse_docstring=True)` 解析 Google 风格 docstring | docs "Advanced schema definition"；reference `tool(parse_docstring=…)` |
| **resolve 层** 【本次】 | `copilot/v2/tools/resolve.py`：搬 v1 `_resolve_ticker`（ticker + typo 建议）、`_year_scope` / `_latest_filing_year`（fiscal-year scoping）、单位归一。工具内部调用。**外加**：一个 `@before_model` middleware 每轮 resolve 一次，结果写进**自定义 `state_schema`** 字段（如 `state["resolved"] = {"question","fiscal_year","tickers"}`），`retrieve_text` / `graph_query` 用 `runtime.state["resolved"]` 读 | **修正**：`context` 是 invoke 时传入的**静态只读**数据（docs/runtime：user id、db 连接），middleware **写不了**它。resolve 结果是"middleware 每轮算一次、tool 读"的派生值 → 必须走 `state_schema=`。这是整个端口第一次真正需要自定义 state（理由正当）。`context_schema` 留给静态 per-run 依赖（如调用方传的 `as_of_date`）|
| **`ToolRuntime` 迁移** 【本次】 | `retrieve_text` 的 `Annotated[dict, InjectedState]` → `runtime: ToolRuntime`（`runtime` 保留名，模型看不到）。读 `runtime.state["resolved"]` 优先、原始 `runtime.state["messages"]` 兜底 | docs/reference 明确 `InjectedState`/`InjectedToolCallId`/`get_runtime()` 已过时，1.x 统一走 `ToolRuntime`（给 `.state`/`.context`/`.store`/`.stream_writer`/`.tool_call_id`/`.execution_info`）|
| **粒度** 【本次保持 / 合并推迟】 | 5 个工具维持 1:1，不合并 `query_financials`+`list_metrics`。`timeseries` / `get_segment_financials` 等是 Phase 4 新增 | 合并会动 A/B 基线，收益不明；先稳 |
| **compute 沙箱** 【本次】 | AST 白名单沙箱**逐字**从 v1 拷（安全关键），只换签名 + 信封 | 安全代码不重写 |
| **只读缓存** 【本次，轻量】 | `query_financials` / `list_metrics` / `graph_query` 稳定 cache key 做进程内缓存（`functools` / `cachetools`）；`compute` memoize | 纯函数、DB 快照期内不变。**MCP 核对：LangChain 没有原生 tool-result 缓存中间件**，手做即可 |
| **注册表** 【本次】 | `copilot/v2/tools/registry.py` 单一 `TOOLS`，`orchestration/graph` import 它 | — |
| **v1_loop 兼容** 【本次：不做 adapter】 | v1_loop 继续冻结在 `copilot.agent.tools`，**不**迁到新层。A/B 只比端到端（铁律 3），不需要 adapter | 保基线不动 |
| **`bind_tools` / `ToolNode`** 【本次只碰 TOOLS + 预制 middleware】 | `create_agent` 内部自己 `bind_tools` + 跑内建 ToolNode；我们的暴露面只有 `TOOLS` 列表 + 上面那几个预制 middleware。手搭 `ToolNode(handle_tool_errors=…)` / `Command` 写 state 是 Phase 4 子图的事 | 见"关注点 1" |
| **限流 `RateLimiter`** 【推迟 Phase 4】 | 当前数据路径全是本地 Postgres，无外部 API。等 Phase 4 `extract_guidance` 打 transcript / SEC 源再加 | 无假想需求 |
| **同步 / 异步工具** 【推迟 Phase 5】 | 工具保持同步。`create_agent` 会把同步工具丢线程池，不阻塞事件循环 | 见"关注点 3" |

#### 关注点回应（用户看完 tools 文档提的三点）

1. **`bind_tools` vs `ToolNode`**：`bind_tools` 是**模型侧**（把 schema 绑上去让模型能*发起*调用）；`ToolNode` 是**图侧**（*执行*调用：分发 / 并行 / 错误）。用 `create_agent` 时两者都在内部，我们碰不到也不用碰 —— 只交付一个好的 `TOOLS` 列表 + 预制的 `ToolRetryMiddleware` / `ToolErrorMiddleware` / `ToolCallLimitMiddleware`。裸 `ToolNode`（`handle_tool_errors=`、`Command` 写 state）留到 Phase 4 手搭 deep_research 子图时再学。**本次涉及**：`TOOLS` + 3 个预制 middleware。
2. **tool runtime 访问上下文**：`ToolRuntime` 参数（`runtime: ToolRuntime`，保留名、对模型隐藏）给 `.state` / `.context` / `.store` / `.stream_writer` / `.tool_call_id` / `.execution_info`。**关键区分**（MCP 核对）：`context` = invoke 时传的**静态只读**依赖（user id、db 连接、`as_of_date`），middleware 不能改；`state` = 本轮可变数据（messages + 自定义字段），middleware 能写。resolve 结果是 middleware 每轮算的派生值 → 走 **自定义 `state_schema`**，不是 `context_schema`。**本次涉及**：`retrieve_text` 从 `InjectedState` 迁到 `ToolRuntime`；加 `state_schema` 的 `resolved` 字段。`.store`（长期记忆）留 Phase 3，`.stream_writer`（进度流）留 Phase 5 SSE。
3. **同步 / 异步工具**：docs 说同步工具会被丢到线程池执行，原生 async 省掉线程开销。我们的工具全是同步 `psycopg2` 阻塞调用，转 async 要动 `copilot.storage.db`（共享基础设施）换 async 连接池 —— 收益（并发吞吐）只在 Phase 5 产品化 + FastAPI `ainvoke` 时才兑现。**本次不做**，标为 Phase 5 前置项：`tools/*` 加 `a*` 变体 + async DB pool。

#### 构建顺序（devlog 005）

1. `tools/base.py` —— `content_and_artifact` 打包 helper（紧凑文本 + 完整 dict）、`ToolError` + `ToolErrorKind` 枚举、`@financial_tool` 装饰器（统一名字 / 描述 / 进程内缓存）。
2. `tools/resolve.py` —— 搬 v1 `_resolve_ticker` + `_year_scope` + `_latest_filing_year` + 单位归一，带单测。
3. `tools/schemas.py` —— 每工具 Pydantic `args_schema`；`metric` 的 `Literal` 从 DB 生成。
4. `tools/{financials,retrieval,graph,compute}.py` —— 一工具一模块，调 `copilot.retrieval` / `copilot.storage`（共享，不改）；compute 沙箱逐字拷；`retrieve_text` 加 `runtime: ToolRuntime`。
5. `tools/registry.py` —— `TOOLS`。
6. `orchestration/graph/` —— `tools.py` 最小包装 → `from copilot.v2.tools.registry import TOOLS`；`build.py` 加 `ToolRetryMiddleware` + `ToolErrorMiddleware` + `ToolCallLimitMiddleware` + `state_schema`（`resolved` 字段）；`middleware.py` 加 `_resolve` `@before_model` hook 写 `state["resolved"]`；`runner.py` 改读 `ToolMessage.artifact` 拿 `steps`。
7. 每工具单测（含 `ToolError` retryable / 非 retryable 两条路径）+ resolve 层单测 + `_resolve` middleware 单测。
8. EVAL：`ab_compare` 5 集全跑 —— 目标是 citation 集差异（年份 scope 那几处）测得到地收窄，其余不劣化；对比 input token（artifact 分流后应降）。

**产出**：`copilot/v2/tools/{base,resolve,schemas,financials,retrieval,graph,compute,registry}.py` + `tests/test_tools_*.py`；`orchestration/graph` 里 `state_schema` + 3 个预制 middleware + `_resolve` hook。

**退出标准**：所有工具统一 `content_and_artifact` 信封 + `ToolError` 类型化错误（`ToolRetryMiddleware`+`ToolErrorMiddleware` 接管）+ Pydantic schema + 单测（含 retryable/非 retryable 路径）通过；`ab_compare` 5 集不劣于 Phase 2，且年份 scope 的 citation 差异测得到地收窄，input token/问 不升；v1_loop 冻结不受影响；`pytest` 全绿。

---

### Phase 2 — 直译：LangGraph 复刻 v1 循环   ✅ done（2026-09-07，devlog 004）

**目标**：功能等价，不追求更强，只把"手写循环"翻译成"图"。

> **实际实现路径（2026-09-07，devlog 004）—— 与下面 1-5 的手搭方案有实质出入，以此为准**
>
> **A. 用 `create_agent` 预制 harness，不手搭 `StateGraph`。**
> 下面 1-5 描述的是手写 `state.py` / `router.py` 节点 / `nodes/`（agent/tool/verify/provenance/clarify）/ `should_continue` 条件边 / `verify` 回边节点。实际用了 LangChain 1.x 的 `create_agent(model, tools, system_prompt, middleware, checkpointer)`，它内部就是 `model ⇄ tools` 循环的图版本。用户已学 `create_agent` 文档并明确选它。
> - v1 的 `route_question`（refuse / force_tool）、`clarification_for`、`trim_history`、`active_context_block` 全部搬成 **4 个 middleware hook**（`src/copilot/v2/orchestration/graph/middleware.py`）：
>   - `_trim` `@before_model` —— 按整轮删旧历史（`RemoveMessage`）
>   - `_guard` `@before_model(can_jump_to=["end"])` —— refuse 短路 + clarify 短路（refuse 优先），注入文本并跳 END
>   - `_active_context` `@wrap_model_call` —— 首轮把假设块作为 `SystemMessage` 插进 `request.messages`
>   - `_force_first_tool` `@wrap_model_call` —— 首轮 `request.override(tool_choice=...)`
> - `verify` / `provenance` / `citations` **不做成图节点**。`runner.py` 事后复用 v1 的 `verify_answer` / `build_provenance` / `_collect_citations`，把 `create_agent` 返回的 `messages` 链重建成 v1 形状 dict（`_steps_from_messages` 按 `tool_call_id` 配对 `AIMessage.tool_calls` + `ToolMessage`）。
> - 手搭 `StateGraph` 的能力（非线性流程、自定义路由、`verify` 回边补证据）留到真需要时 —— Phase 4 的 supervisor / deep_research 子图。
>
> **B. 不加自定义 `state_schema=`（步骤 1 的 `AgentState` 扩展字段没做）。**
> 计划里 `route` / `carried_slots` / `steps` / `citations` / `verification` 进 state。实际结论：这些是**问题序列的纯函数**，checkpointer 已经存了完整 `messages`，middleware 里对回放的 `HumanMessage` 重跑 `extract_slots` 折叠（`carry_from_messages`，等价 `conversation.carried_slots`）即可，再存一份 slot dict 是重复状态。`state_schema=` 留给"框架无法从 messages 推导、又要跨节点/跨轮传"的东西 —— Phase 4 的 `plan` / `evidence_ledger` / `budget`。
> - 多轮：走 `thread_id` + `InMemorySaver`，不穿 `history` list。
> - 坑：middleware 判"首轮"必须 turn-scoped（"最后一条 `HumanMessage` 之后无 `AIMessage`" = v1 的 `round_idx == 0`）；查"整个 messages 无 `AIMessage`"只在线程第一轮为真，会让 t2+ 所有 hook 静默失效。
>
> **C. `MAX_ROUNDS` circuit breaker**：`create_agent` 自带递归上限，没另做 guard 节点。
>
> **D. 工具**：用的是 Phase 1 之前的**最小 `@tool` 包装**（`tools.py`，直接调 `copilot.agent.tools` 的冻结函数），不是计划里假设的 `tools/registry.py` 标准库。Phase 1 顺序后置。

1. **State 定义**（`state.py`）— 用 `TypedDict` + reducer：
   ```python
   class AgentState(TypedDict):
       messages: Annotated[list[AnyMessage], add_messages]
       route: dict            # 预路由结果
       carried_slots: dict    # 槽位继承
       steps: list[ToolStep]  # 证据台账（provenance 用）
       citations: list[str]
       verification: dict | None
   ```
2. **节点**：
   - `router` 节点：包 `model_router.py` 逻辑，返回 `Command(goto="refuse" | "agent")`，`force_tool` 时在 state 里塞 `tool_choice`。
   - `agent` 节点：`ChatOpenAI(model=...).bind_tools(TOOLS)`；带 `.with_fallbacks([cheaper_model])` 处理 upstream 失败。
   - `tools` 节点：`ToolNode(TOOLS, handle_tool_errors=<映射 recoverable 标志>)`，天然并行，替代 `ThreadPoolExecutor`。
   - `should_continue` 条件边：有 tool_calls → `tools`；否则 → `verify`。
   - `verify` 节点：包 `verify_answer` + `build_provenance` + `_collect_citations`，可回边到 `agent` 要求补证据（最多 N 次）。
3. **工具接入**：直接用 Phase 1 的 `tools/registry.py::TOOLS`（已是 `@tool` + Pydantic）；`ToolNode` 的 `handle_tool_errors` 映射 Phase 1 的 `ToolError.retryable`。
4. **循环上限**：用 `graph.compile(...)` 的 `recursion_limit` + 一个显式 guard 节点，复刻 `MAX_ROUNDS=10` 的 circuit breaker。
5. **历史/记忆**：本阶段先用 `MemorySaver` checkpointer，`thread_id = session_id`；在 `agent` 节点前挂 `trim_messages` 复刻 `conversation.py` 的 6 轮 / 3000 token 裁剪；`carried_slots` 逻辑搬进 `router` 节点。

**退出标准**：A/B 脚本上，graph 在 Tier 1–3 与 v1_loop 差异 ≤ 容忍阈值（答案语义一致、引用集合一致、拒答行为一致）。latency/token 不显著劣化。

> **实际达标情况（devlog 004）**：5 个 eval 集（eval_set / router / multiturn / tier3 / defects）`ab_compare` 对 v1_loop 共 67 组 —— **拒答 0 处不匹配、0 处行为回归**，延迟/步数持平。引用集合非 100% 精确相等（eval_set 30/30，其余 6-11 处里各差 1-3），差异逐条归因为最小 `@tool` 包装的 `retrieve_text` 引用面偏宽 + `graph_query` 参数非确定性（Phase 1 工具层 resolve 收口），数处是 graph 比 v1 更紧。多轮年份继承生效（`mt_year_carries` t3 compute = $311,266,860，与 eval 集 `verification` 逐位一致）。`pytest` 149/0。**判定：达标**。

---

### Phase 3 — 用框架能力做"成熟化"升级

**目标**：拿到 v1 手写循环给不了的东西。逐项替换，每项单独 A/B。

| 关注点 | v1 做法 | LangGraph 做法 | 本阶段动作 |
|---|---|---|---|
| 状态管理 | ad-hoc list + 返回 dict | `create_agent` 的 `messages` + checkpointer；纯函数状态（route/slots）现算不存 | 已在 Phase 2（未加自定义 `state_schema=`，见 Phase 2 实际注 B）|
| 持久化 | 无，进程重启即丢 | `PostgresSaver`（复用现有 PG），按 `thread_id` 自动加载 | 切 `MemorySaver` → `PostgresSaver`，会话跨重启可续 |
| 多轮记忆 | 传入传出 `history` | checkpointer thread + `trim_messages` 预钩子 + 长期记忆 store | 加 `store` 存"跨会话事实"（如用户常问的公司） |
| 错误恢复 | `recoverable` 标志，模型重试 | `ToolNode(handle_tool_errors)` + 节点 `RetryPolicy` + 独立 repair 节点 | 加 `RetryPolicy(max_attempts=3, ...)`；不可恢复错误进 repair 节点决定降级/拒答 |
| 上游 LLM 失败 | try/except → 用户消息 | 节点重试 + `.with_fallbacks([model_b])` | 配主/备模型 |
| 中断/澄清 | `clarify.py` 前置 pass | 图中 `interrupt()` 停下等分析师确认 | 把澄清从"前置"改成"按需中断"：ticker 歧义、fiscal year 假设、深度分析前确认范围 |
| Provenance | 事后 `build_provenance` | ToolNode 包装器把证据按 reducer 累加进 state | 证据台账实时累积，深度分析时全树可追溯 |
| 可观测性 | Langfuse callback | LangSmith 原生 trace（每节点）+ 保留 Langfuse callback | 双挂，对比；按节点看耗时/token/重试 |
| 可扩展性 | 改循环体 | 加节点 / 加 subgraph / 加 tool | 为 Phase 4 的 supervisor 留接口 |
| 流式 | 无 | `graph.stream(stream_mode="messages"/"updates")` | FastAPI 接 SSE，前端流式显示中间步骤 |
| 回放/调试 | 无 | checkpointer 时间旅行 `get_state_history` | 加 `eval/replay.py` 从任意 checkpoint 重跑 |

**退出标准**：持久化、HITL、错误恢复、LangSmith trace 四项均有 demo + 测试；Tier 1–3 不回归。

---

### Phase 4 — 能力升级：回答长难问题

**目标**：能答三类公司级分析问题：
- **A. 管理层指引的兑现程度** — 提取 T 期 guidance（区间/口径）→ 对 T+1 期实际值 → 打分 + 归因偏差。
- **B. 管理层风格 / 模式判断** — 跨多年文本纵向分析：是否惯性保守（sandbag guidance）、口径一致性、谈失败/风险的方式、资本配置措辞。
- **C. 公司增长模式分析** — 拆解收入增长（有机 vs 并购、量 vs 价、FX、分部结构）、再投资（capex/R&D）vs 回报（回购/分红）、利润率轨迹。

三类的共同形态：**多步研究任务**（planner → 并行子查询 → 带 rubric 的综合 → 校验）。这正是 LangGraph 强、手写循环弱的地方。

#### 4.1 架构：Supervisor + 专家子图

```
supervisor（判断复杂度）
├─ 简单事实题 ──▶ simple_qa 子图（= Phase 2 的图）
└─ 深度分析题 ──▶ deep_research 子图
                   ├─ planner：写研究计划（子问题列表 + 每个子问题指定专家/工具）
                   ├─ map：并行跑子问题（每个是 simple_qa 或某专家子图）
                   ├─ synthesize：按 domain rubric 汇总（A/B/C 各一套评分标准）
                   └─ verify：证据覆盖率 + 数字可追溯性检查，不达标回 planner 补
专家子图：guidance_analyst / management_profiler / growth_analyst
         各自封装 domain prompt + 偏好工具 + 输出 schema
```

State 增加：`plan`、`sub_results`、`evidence_ledger`（全树 provenance）、`budget`（token/tool 调用预算，防 fan-out 爆炸）。

#### 4.2 需要新增的工具（沿用 Phase 1 的标准信封）

| 工具 | 用途 | 数据来源 |
|---|---|---|
| `timeseries(ticker, metric, start, end)` | 一次拉多年，减少轮数 | 现有 XBRL 表 |
| `get_segment_financials(ticker, year)` | 分部收入/经营利润 | XBRL 带 segment axis 的维度数据（需扩摄取） |
| `list_filings(ticker, form, date_range)` / `get_filing_section(accession, item)` | 纵向文本分析取指定 Item | 现有 filings + 章节切分 |
| `extract_guidance(ticker, as_of_period)` | 结构化 guidance：metric / low / high / basis / source | earnings call transcript / MD&A outlook，LLM 抽取 + 校验 |
| `peer_set(ticker)` | 同业对照，用于"模式"相对判断 | 手工 mapping 或 SIC/行业表 |

#### 4.3 需要新增的摄取（ingestion/）

- **Earnings call transcripts**：分块，标注 speaker role（CEO/CFO/analyst）、日期。
- **Guidance 表**：仿 v1 供应链边的做法 — regex 预筛 → schema-guided LLM 抽取（Instructor + Pydantic）→ 对已披露实际值/第三方做校验。
- **Segment facts**：从 companyfacts 的维度成员抽 `us-gaap` 分部数据（本 Phase 最大不确定性，**先做 spike 验证可行性**）。

#### 4.4 Prompt / rubric 资产

每类问题一套评分 rubric（放 `graph/subgraphs/<name>/rubric.md`）：
- A：guidance 覆盖了几个指标 / 实际落在区间内比例 / 偏差方向是否一贯 / 是否给了归因。
- B：至少 N 年样本 / 是否量化保守度（guidance vs actual 系统性差）/ 是否举原文证据。
- C：增长拆解是否 MECE / 是否区分有机与并购 / 是否含利润率与再投资视角 / 是否有同业对照。

#### 4.5 模型编排：按角色分模型（不是按难度分层）

> 加这节的动机：长难问题拆成多个子任务后，不同子任务对模型的要求确实不同（planner 要强推理、extractor 要便宜稳定、judge 反而要更便宜）。用 LangChain 的多模型能力给每个角色配一个模型。

**先明确不做什么。** v1 `agent/model_router.py` 已经论证过并**否决**了"按问题难度自动分层 / 按答案质量升级模型"：
- 冻结 eval 集已饱和 —— gpt-4o vs gpt-4o-mini 每个确定性指标同分、成本 16x，分层表是死配置（drift-from-data 缺陷）。
- 所有主流框架只在**异常**（429 / 5xx / 超时 / 超上下文）上自动切模型，从不在"答案看着不对"上切。更强的模型只会更流畅地编造 —— 对"错答案"的正解是**验证**（`grounding.py`），不是花更多钱。

**这条结论 Phase 4 继续有效，不推翻。** Phase 4 不同的地方是：deep_research 子图把一个长问题拆成**结构上不同的子任务**，不是同一个扁平 Q&A 循环跑十遍。按角色（role）分模型，不是按难度（tier）分层：

| 角色 | 子任务 | 模型取向 | 依据 |
|---|---|---|---|
| `planner` | 写研究计划、拆子问题、指派专家 | 强指令遵循 + 推理 | 计划错，整棵树错 |
| `extractor` | 从 transcript / MD&A 抽 guidance 成 schema（`extract_guidance`，跑很多次）| 便宜 + 稳定 JSON | 高频、结构化、可校验 |
| `specialist`（guidance / profiler / growth）| 单个子问题的检索 + 判断 | 中档，够用即可（≈ 现在的 simple_qa）| Phase 2 已证 gpt-4o-mini 够 |
| `synthesizer` | 按 rubric 汇总多个子结论 | 强 | 综合是这类问题真正难的一步 |
| `verifier` / judge | 证据覆盖率、数字可追溯性、rubric 打分 | 比被评的**更便宜**一档 | LangChain rubric middleware 就这么做：判断比生产容易 |

算术不在此表 —— v1 铁律"LLM 永不自己算数"保留，`compute` 沙箱工具照旧。

**LangChain 机制**（已用 docs MCP 核对 `oss/python/langchain/models` "Dynamic model selection"）：
- **子图 / subagent 各带 `model=`**：`create_agent(model=synth_model, tools=..., system_prompt=...)`；deepagents 直接 `subagents=[{..., "model": "openai:..."}]`。
- **一个 agent 内按 state 动态换**：`@wrap_model_call` + `request.override(model=chosen)`（文档原样模式，按 `len(request.state["messages"])` 或自定义 context 选）。
- **运行时可配**：`init_chat_model(configurable_fields=("model",))` + `config={"configurable": {"model": ...}}`。
- **异常降级**：`ModelFallbackMiddleware` / `.with_fallbacks([...])` —— v1 已认可的唯一"自动切模型"场景。
- 模型都从 `copilot.config.settings` 的 OpenAI 兼容端点构造（同 Phase 2 `build.py::_model`，key 在 `.env`）。

**怎么验（否则又是一张死配置表）**：
- Tier 4 跑两遍 —— (A) 全程单一模型；(B) 按上表分角色 —— 比 rubric 均分、证据覆盖率、总 token 成本、p95 延迟。
- 分角色只在"某指标真变好"或"同分但成本显著降"时保留；否则退回单模型 + 一句注释。
- 每个角色的模型 id 进 `graph/subgraphs/<name>/` 的配置，单一改动点（照 `model_router.DEFAULT_MODEL` 的先例）。

**退出标准**：新 Tier 4（见下）rubric 平均分达标；每条结论都有 `evidence_ledger` 支撑；预算护栏生效（无失控 fan-out）；**4.5 的分角色配置有 A/B 数据支撑，不是"感觉强的地方用强模型"**。

---

### Phase 5 — 产品化

1. FastAPI 换成调用 graph（`ainvoke` / `astream_events`），SSE 流式中间步骤 —— 细节见下「Phase 5.F 前端 / 交互层」。
2. 前端展示：研究计划、子结论、证据台账、rubric 得分 —— 见 5.F。
3. 部署：`PostgresSaver` 生产库；LangSmith 项目分 dev/prod。
4. 回归闸门：CI 跑 Tier 1–3（硬闸）+ Tier 4（软闸，记录趋势）+ A/B 对 v1。
5. 成本/延迟预算：每问 token、tool 调用数、p95 延迟设阈值，超阈告警。

---

### Phase 5.F — 前端 / 交互层（累积式规划，非阻塞）

> **现在没有展示机会**，编排层唯一的消费者是 `ab_compare`（批量评测，`agent.invoke()` 阻塞返回终态即对）。这一节是**前端相关学习的存放处** —— 边学 LangChain 的流式 / UI 部分边往里记，等 Phase 5 真做 UI 时落地。不阻塞任何 Phase。`run()` 的批量路径全程保持 `invoke()`，流式是加法不是替换。

#### F.1 传输：用 `stream_events(version="v3")`，不是裸 `stream_mode`

`docs/oss/python/langchain/event-streaming` 明说：应用 / 前端场景用 **Event Streaming**（`agent.stream_events(input, version="v3")`），它返回一个 run 对象带**分类投影**，每种独立消费；`graph.stream(stream_mode="messages"/"updates")` 是底层 Pregel API，前端不直接用。

- FastAPI：一个 `/ask` 的 SSE 端点，`async for` 消费投影 → 转 SSE event。
- 同步场景 `stream.interleave("messages","tool_calls","values")`；异步 `astream_events` + `asyncio.gather`。

#### F.2 投影 → 这个项目的 UI 需求

| 投影 | UI 显示 | 何时 |
|---|---|---|
| `stream.messages` 的 `.text` | 答案逐 token 出 | Phase 5 |
| `stream.tool_calls`（`.tool_name` / `.input` / `.output_deltas` / `.error`）| 活动条："查 AAPL Revenue FY2024" / "检索 CRUS 10-K（scope FY2026）" / 工具报错 | Phase 5 |
| 自定义 transformer（`AgentMiddleware.transformers` 注册）| `retrieve_text` 的 scope 决策、unscoped 回退、命中数；`graph_query` 的 traversal 展开 | Phase 5，只在有 UI 消费时加 |
| `stream.values` on `plan` / `sub_results` / `evidence_ledger` / `budget` | 研究计划、子结论、证据台账**逐步填充** | Phase 4 起 |
| `stream.subagents`（`.name` + `.cause`）| 每个专家子 agent（`guidance_analyst` …）一个面板，标明由哪次工具调用派发 | Phase 4 |
| `stream.output` | 收尾：最终答案 + provenance / verification 徽章 | Phase 5 |

#### F.3 组件清单（Streamlit 或换框架后一样适用）

- **答案区**：流式 markdown；citation 渲染成可点的 SEC EDGAR 链接。
- **活动区**：工具调用时间线（名字 + 关键参数 + 耗时 + 成功/错误）。数据来自 `stream.tool_calls`。
- **证据台账**：每个数字 → accession → 章节，可展开。数据来自 `evidence_ledger`（Phase 4）/ 现在的 `provenance`。
- **校验徽章**：`verify_answer` 的结果（每个数字过没过 grounding：grounded / flagged）。
- **研究计划树**（Phase 4）：planner 的子问题 + 每个状态（pending / running / done），`stream.values` 驱动。
- **澄清交互**（Phase 3 HITL）：`interrupt()` 抛出的澄清选项渲染成按钮，点击 → `Command(resume=…)` 继续。

#### F.4 工具里的进度：`runtime.stream_writer`

`retrieve_text` 是多步（BM25 + embedding + RRF + 可能的 unscoped 回退）。加 `runtime.stream_writer("scoping to FY2024…")` 发进度，经 middleware 注册的自定义 transformer 投影成 `stream.extensions["retrieval_activity"]`。**只在有 UI 消费时加**，否则是噪声 —— 现在不加。

#### F.5 v1 `dashboard.py` 的迁移

v1 的 Streamlit dashboard（`copilot/dashboard.py`，共享 / 冻结）直接调 v1 函数。Phase 5：新写 `copilot/v2/ui/`，调 FastAPI SSE 端点（或 `astream_events` 直连 graph）。不动 v1 的。

#### F.6 不要提前做

- Phase 2 / 3 稳定前不碰 UI。
- eval 全程 `invoke()`，不依赖流式；A/B 报告不需要流式。
- 前端选型（保留 Streamlit vs 换 React）**等 Phase 5 再定**；这一节只记"需要显示什么、数据从哪个投影来"，不定"用什么框架"。

---

## 3. Eval 升级

| Tier | 内容 | 评分 | 闸门 |
|---|---|---|---|
| 1–3 | 沿用 v1（二元正确性、推理深度、检索精度、拒答恰当性） | 确定性 | 硬闸：graph 不得回归 v1 |
| 4（新） | 10–30 道 A/B/C 类分析题，人工标注锚定答案 | LLM-as-judge（rubric）+ 人工抽检 | 软闸：跟踪均分趋势 |

通用指标：证据覆盖率（每个论断有引用）、数字可追溯率、rubric 分、latency、token 成本、tool 调用数。

`eval/ab_compare.py` 每次改编排都跑，产出 v1 vs graph 的 diff 报告。

---

## 4. 目标 1 的学习对照表（交付物之一）

把 v1 每个机制映射到框架惯用法，边做边记，最终整理成一篇 handbook 章节：

| 主题 | v1 自建 | LangGraph 对应 | 学到什么 |
|---|---|---|---|
| 状态管理 | list + dict 混合 | `create_agent` 的 `messages` + checkpointer；`state_schema=` 只加真正需要跨节点传的派生不出的东西 | 别把纯函数结果（route/slots）也塞进 state —— checkpointer 已存 messages，现算即可。状态即 schema，但最小化 |
| 工具设计 | 5 个 shape 不一的函数 + 手塞 `recoverable` dict | `@tool(response_format="content_and_artifact")` + Pydantic `args_schema`（`Literal` 枚举）+ `raise ToolError` → 预制 `ToolRetryMiddleware`+`ToolErrorMiddleware`+`ToolCallLimitMiddleware` | 工具是 LLM 的 API：schema 严、错误类型化、给模型的文本和完整数据分开（artifact 不吃上下文）。错误/重试/限流有预制件，别手写 |
| 工具访问上下文 | 循环里手动 `inp["query"]=question` / 散落的 ticker·年份解析 | `runtime: ToolRuntime`（`.state`/`.context`/`.store`）；派生值走 `state_schema`，静态依赖走 `context_schema` | `context` 是 invoke 时传的静态只读，middleware 改不了；middleware 每轮算的 resolve 结果必须走 state。`InjectedState` 已过时 |
| 工具调用 | 手写 dispatch + 线程池 | `bind_tools` + `ToolNode`（内建并行） | 调用/结果/错误统一成消息 |
| 错误恢复 | `recoverable` 标志 | `handle_tool_errors` + `RetryPolicy` + fallback | 分层：工具级 / 节点级 / 模型级 |
| 持久化 | 无 | Checkpointer（thread）+ Store（跨会话） | 断点续跑、时间旅行、崩溃恢复 |
| 可观测性 | Langfuse callback | LangSmith 原生 span/节点 | 按节点归因耗时与失败 |
| 可扩展性 | 改循环主干 | 加节点/子图/supervisor | 编排即图，增量不侵入 |
| 人在环 | 前置澄清 pass | `interrupt()` 任意点暂停 | 澄清从"预判"变"按需" |
| 多轮记忆 | append-only + trim | checkpointer + `trim_messages` | 记忆策略与编排解耦 |
| 模型选择 | `model_router.py`：只让调用方显式选，否决自动分层/升级 | 子图各带 `model=` + `@wrap_model_call` 动态换 + `ModelFallbackMiddleware` 异常降级 | 按**角色**分模型（planner/extractor/synth/judge 要求不同）是真的；按**难度**分层要 A/B 数据，否则是死配置。自动切模型只在异常上，不在"答案看着不对"上 |
| 流式 / 前端 | 无（v1 dashboard 直调函数）| `stream_events(version="v3")` 分类投影（`messages`/`tool_calls`/`values`/`subagents`）| 应用层用 v3 事件流、不用裸 `stream_mode`；子 agent 走 `stream.subagents`。批量评测保持 `invoke()`，流式是加法（见 Phase 5.F）|

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 分部 / guidance 数据获取难 | Phase 4 前先做 1–2 天 spike，拿不到就缩小 C/A 范围到 XBRL 可覆盖部分 |
| LangGraph API 变动 | pin 版本；升级单独开分支跑全量 eval |
| 过度设计（没对齐就上 supervisor） | Phase 2 未达退出标准不进 Phase 4 |
| LLM-as-judge 不稳 | Tier 4 保留人工锚定集，judge 与人工一致性 < 0.8 则重写 rubric |
| 深度研究 fan-out 成本失控 | state 里带 `budget`，planner 限子问题数，超预算降级为浅层回答并标注 |
| 4.5 按角色分模型退化成按难度分层 | 沿用 v1 `model_router` 结论：自动切模型只在异常（429/5xx/超时/超上下文）上。分角色配置必须有 Tier 4 A/B（rubric / 覆盖率 / 成本 / 延迟）支撑，无提升即退回单模型 |
| 工具层重构引入行为回归 | v1_loop 用 `_v1_frozen` 旧工具作行为基线；新工具库每个工具带错误路径单测；A/B 只比端到端，回归立即可见 |
| 工具层过度设计（信封/抽象层做太多） | Phase 1 设计点"到时候再定"，先满足 5 个现有工具 + 4.2 节新工具，不为假想需求加维度 |

---

## 6. 立即可做的第一步（Phase 0）

1. `git init` + `uv init`，建 Phase 0 目录骨架。
2. 从 v1 拷工具到 `tools/_v1_frozen/` + `eval/` harness，跑通 v1_loop baseline，存分数到 `docs/eval-history.md`。
3. 写 `eval/ab_compare.py` 空壳（端到端 diff）。
4. 整理 Phase 1 工具层"待确认设计点"清单，逐条定方案（信封 / 错误 / 粒度 / resolve 层）。
