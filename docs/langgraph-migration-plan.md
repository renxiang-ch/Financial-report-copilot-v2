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

### Phase 1 — 工具层标准化重构

**目标**：把 5 个风格不一的工具重做成一套标准化、可扩展、带 provenance 的工具库。v1 实现作参考规格，逻辑（SQL / RRF / 递归 CTE / AST 沙箱）保留，接口与横切层重写。

**待确认的设计点**（`到时候再定`，此处只列清单）：
- **统一返回信封**：自定义 `{ok, data, error, provenance, meta}`，还是直接用 LangChain 的 `response_format="content_and_artifact"`（模型看摘要，artifact 存完整数据 + 来源）。
- **统一错误**：`ToolError(kind: Enum, retryable: bool, hint: str)`，取代手塞的 `recoverable` dict。
- **参数校验**：Pydantic v2 args schema，字段级约束 + 写给 LLM 的 description。
- **命名 / 粒度**：`query_financials` + `list_metrics` + 新 `timeseries` 是否合并成一个 `financials` 工具带 `mode`，还是保持细粒度。
- **横切层抽出**：ticker 解析 / typo 建议、fiscal-year scoping、单位归一 → 独立 `resolve` 层，工具内部调用，不再各写各的。
- **只读工具缓存**：稳定 cache key；`compute` 等纯函数可 memoize。
- **限流**：外部 API（SEC / transcript 源）挂 `RateLimiter`。
- **compute**：AST 白名单沙箱实现**原样保留**（安全关键），只换签名与信封。
- **注册表**：`tools/registry.py` 单一 `TOOLS`，schema 从 Pydantic 自动生成；若 v1_loop 仍要新工具，写一个 adapter 转 openai `TOOL_SCHEMAS`（否则 v1_loop 继续用 `_v1_frozen`）。

**产出**：`tools/<name>.py` 每工具一模块 + `tools/base.py`（信封 / 错误 / 装饰器）+ `tools/resolve.py` + `tools/registry.py` + 每个工具的单测（含错误路径）。

**退出标准**：所有工具统一信封 + 类型化错误 + Pydantic schema + 单测通过；一个最小 `create_react_agent` smoke 能用新工具库跑通若干 Tier 1 问题；v1_loop 仍可运行（走 `_v1_frozen` 或 adapter），baseline 不失效。

---

### Phase 2 — 直译：LangGraph 复刻 v1 循环

**目标**：功能等价，不追求更强，只把"手写循环"翻译成"图"。

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

---

### Phase 3 — 用框架能力做"成熟化"升级

**目标**：拿到 v1 手写循环给不了的东西。逐项替换，每项单独 A/B。

| 关注点 | v1 做法 | LangGraph 做法 | 本阶段动作 |
|---|---|---|---|
| 状态管理 | ad-hoc list + 返回 dict | 类型化 `StateGraph` + reducer | 已在 Phase 2 |
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

**退出标准**：新 Tier 4（见下）rubric 平均分达标；每条结论都有 `evidence_ledger` 支撑；预算护栏生效（无失控 fan-out）。

---

### Phase 5 — 产品化

1. FastAPI 换成调用 graph（`ainvoke` / `astream`），SSE 流式中间步骤。
2. Streamlit 展示：研究计划、子结论、证据台账、rubric 得分。
3. 部署：`PostgresSaver` 生产库；LangSmith 项目分 dev/prod。
4. 回归闸门：CI 跑 Tier 1–3（硬闸）+ Tier 4（软闸，记录趋势）+ A/B 对 v1。
5. 成本/延迟预算：每问 token、tool 调用数、p95 延迟设阈值，超阈告警。

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
| 状态管理 | list + dict 混合 | `StateGraph` + `Annotated` reducer | 状态即 schema，可校验/可合并 |
| 工具设计 | 5 个 shape 不一的函数 + 手塞错误 dict | `@tool` + Pydantic schema + 统一信封 + `content_and_artifact` | 工具是 LLM 的 API，契约要像对外 API 一样严 |
| 工具调用 | 手写 dispatch + 线程池 | `bind_tools` + `ToolNode`（内建并行） | 调用/结果/错误统一成消息 |
| 错误恢复 | `recoverable` 标志 | `handle_tool_errors` + `RetryPolicy` + fallback | 分层：工具级 / 节点级 / 模型级 |
| 持久化 | 无 | Checkpointer（thread）+ Store（跨会话） | 断点续跑、时间旅行、崩溃恢复 |
| 可观测性 | Langfuse callback | LangSmith 原生 span/节点 | 按节点归因耗时与失败 |
| 可扩展性 | 改循环主干 | 加节点/子图/supervisor | 编排即图，增量不侵入 |
| 人在环 | 前置澄清 pass | `interrupt()` 任意点暂停 | 澄清从"预判"变"按需" |
| 多轮记忆 | append-only + trim | checkpointer + `trim_messages` | 记忆策略与编排解耦 |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 分部 / guidance 数据获取难 | Phase 4 前先做 1–2 天 spike，拿不到就缩小 C/A 范围到 XBRL 可覆盖部分 |
| LangGraph API 变动 | pin 版本；升级单独开分支跑全量 eval |
| 过度设计（没对齐就上 supervisor） | Phase 2 未达退出标准不进 Phase 4 |
| LLM-as-judge 不稳 | Tier 4 保留人工锚定集，judge 与人工一致性 < 0.8 则重写 rubric |
| 深度研究 fan-out 成本失控 | state 里带 `budget`，planner 限子问题数，超预算降级为浅层回答并标注 |
| 工具层重构引入行为回归 | v1_loop 用 `_v1_frozen` 旧工具作行为基线；新工具库每个工具带错误路径单测；A/B 只比端到端，回归立即可见 |
| 工具层过度设计（信封/抽象层做太多） | Phase 1 设计点"到时候再定"，先满足 5 个现有工具 + 4.2 节新工具，不为假想需求加维度 |

---

## 6. 立即可做的第一步（Phase 0）

1. `git init` + `uv init`，建 Phase 0 目录骨架。
2. 从 v1 拷工具到 `tools/_v1_frozen/` + `eval/` harness，跑通 v1_loop baseline，存分数到 `docs/eval-history.md`。
3. 写 `eval/ab_compare.py` 空壳（端到端 diff）。
4. 整理 Phase 1 工具层"待确认设计点"清单，逐条定方案（信封 / 错误 / 粒度 / resolve 层）。
