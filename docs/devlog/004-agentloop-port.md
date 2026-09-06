---
id: 004
phase: 2
title: Agent loop 端口 —— v1 手写循环 → LangChain create_agent
started: 2026-09-06
finished: -
status: active
plan_ref: ../langgraph-migration-plan.md
---

## GOAL

把 v1 手写的 `_ask_openai` 循环换成 LangChain 1.x `create_agent` 实现，在 eval Tier 1-3 上与 v1_loop 行为 parity。增量端口，6 步（见 PLAN）。第一版**只最小 `@tool` 包现有 5 个工具**（正式工具层重构留后）。服务目标 #1（学 `create_agent` + middleware）。

用户已学 https://docs.langchain.com/oss/python/langchain/agents，决定：(1) 用 `create_agent` 而非自写 `StateGraph`；(2) 最小包装工具，后续再调优。

## INSPECT

- 现有 LangChain 代码 = 无（Phase 0a stub 已删）。`tools/__init__.py` 是空占位。
- `langchain` 元包未装（只有 `langchain-core`）→ `create_agent` 在 `langchain.agents` → 加 `langchain==1.4.0` 到 pyproject（要求 `langchain-core>=1.6.0`，与已 pin 的 `1.6.1` 兼容）。
- v1 loop 结构（`agent.py`，之前已通读）：`ask()` → trim_history → carried_slots → route_question(refuse/force_tool/auto) → clarify 早退 → `_ask_openai`（MAX_ROUNDS=10 循环）→ 事后 `build_provenance`/`verify_answer`/`_collect_citations` → `append_turn`。
- v1 工具描述的关键信息在**参数描述**上（`metric` 参数带精确 label 清单 `_metric_description()`），不在顶层 tool description。
- v1 的 `retrieve_text` query 参数由循环强制覆盖成原问题（压缩改写伤 recall），不是模型选的。

## PLAN — 增量端口 6 步

| 步 | 内容 | 验证 |
|---|---|---|
| **1** | `create_agent(model, tools=5 个 @tool 包装, system_prompt=v1.SYSTEM)` + `InMemorySaver`；runner 返回 v1 形状 dict（answer/steps/citations/usage/provenance/verification）| `ab_compare` eval_set.json 与 v1_loop citations/refusal 全匹配 |
| 2 | 事后处理（已在 Step 1 顺带做：复用 v1 `build_provenance`/`verify_answer`/`_collect_citations`）| provenance/verification 字段有值 |
| 3 | 路由 middleware：refuse 短路 + force_tool 首轮 | router eval 集 tool-selection 100% |
| 4 | carried-slots / active-context middleware | multiturn eval 集继承正确 |
| 5 | clarify（`interrupt()` / HumanInTheLoopMiddleware）| 歧义问题触发澄清 |
| 6 | history trim（`SummarizationMiddleware` 或 `before_model` 跑 `trim_messages`）| 长对话不超上下文 |

**怎么验**：每步后 `ab_compare`；Step 1 用 eval_set.json，Step 3 加 router 集，Step 4 加 multiturn 集。v1_loop 全程冻结作 A/B baseline。

## BUILD — Step 1

新增 `src/copilot/orchestration/graph/`：
- `tools.py` —— 5 个 `@tool` 包装 `copilot.agent.tools` 的函数，返回 `json.dumps(...)`（对齐 v1 `_run_tool` 的 JSON 字符串）。描述用 `_desc(name)`：v1 顶层 description + 每个参数描述拼接（因为关键信息在参数上）。`retrieve_text` 的 query 用 `InjectedState` 从 state 里的最后一条 HumanMessage 取。
- `build.py` —— `build_agent(model, checkpointer)`：`ChatOpenAI` 显式从 `settings` 构造（key 在 `.env` 不在环境变量），`timeout=90`/`max_retries=2` 对齐 v1；`create_agent(model, tools=TOOLS, system_prompt=SYSTEM, checkpointer=InMemorySaver())`。
- `runner.py` —— `run(question, history, thread_id, model)`：`agent.invoke({"messages":[user]}, {configurable:{thread_id}})`，从 result messages 重建 v1 形状：`_steps_from_messages`（配对 AIMessage.tool_calls + ToolMessage）、`_usage_from_messages`（累加 `usage_metadata`，含 `input_token_details.cache_read`）、`answer` = 最后一条无 tool_calls 的 AIMessage、citations/provenance/verification 复用 v1 函数。
- `ab_compare.py` —— graph 侧接回真实 `run()`（之前删 stub 时硬编码成 STUB）；加 retired 过滤；graph 侧返回 token。

**中途修的 2 处**（EVAL 打回 BUILD）：
1. tool 描述最初只取 `TOOL_SCHEMAS` 顶层 `description`（276 字），漏了参数描述 → 模型不知道 `EPS_Diluted` / `R&D` 的精确 label，diluted EPS 和 Broadcom R&D 两题误拒。改 `_desc()` 拼上参数描述（921 字）后两题都对。
2. `ab_compare` 没过滤 retired，`ret_swks_apple_concentration_2024`（已废弃，golden 短语在 FY2024 filing 里不存在）造成假不匹配。加过滤。

## EVAL — Step 1 通过

`ab_compare` eval_set.json（30 非 retired），v1_loop vs create_agent：

| 指标 | v1_loop | create_agent |
|---|---|---|
| status OK | 30/30 | 30/30 |
| citations 匹配 | — | **30/30** |
| refusal 匹配 | — | **30/30** |
| 语义分歧 | — | 0（2 行仅措辞详略差异）|
| 平均延迟 | 2.6s | 2.3s |
| 平均步数 | 1.7 | 1.7 |
| 总 input / output token | 206K / 4.2K | 同量级 |

`pytest` 143/0。

## RECORD

### 决策

### D1. `create_agent` 的 model 用显式 `ChatOpenAI(api_key=settings...)`，不用 `"openai:gpt-4o-mini"` 字符串
- **背景**：`init_chat_model("openai:...")` 读环境变量 `OPENAI_API_KEY`；我们的 key 在 `.env`，由 pydantic-settings 加载，没导出到环境。
- **选择**：`create_agent(model=_model("gpt-4o-mini"))`，`_model` 显式传 `api_key` / `base_url` / `timeout` / `max_retries`（后两个对齐 v1）。
- **否决**：在某处 `os.environ["OPENAI_API_KEY"]=...` —— 隐式副作用，不如显式构造。

### D2. tool 描述把参数描述拼进顶层 description，不建 per-tool Pydantic args_schema
- **背景**：v1 的关键提示（精确 label 清单等）在参数描述上。最小 wrapper 没带 args schema。
- **选择**：`_desc(name)` = v1 顶层 description + 每个有描述的参数 `- name: desc` 拼接。
- **否决**：为每个工具建 Pydantic `Field(description=...)` 的 args_schema —— 那是正式工具层重构（后面的阶段）的事，Step 1 保持"最小"。

### Learning（框架机制）

- **`create_agent` 的 model 参数**：接字符串（`"provider:model"`，走 `init_chat_model`，读环境）或已初始化的模型实例。想控 key/timeout/base_url 就传实例。
- **`@tool` 的描述来源优先级**：`@tool("name", description=...)` kwarg > docstring。args schema 从函数签名 + 类型注解自动生成（`.args` 里能看到），参数描述要么写在 args_schema 的 `Field`，要么塞进 tool description。
- **`InjectedState`**（`langgraph.prebuilt`）：给 `@tool` 的参数加 `Annotated[dict, InjectedState]`，模型看不到这个参数，运行时框架注入当前 graph state。这是"工具需要 state 但不该让模型控制"的标准做法（对应 v1 手动 `inp["query"] = question`）。
- **`create_agent` 返回**：`result["messages"]`（完整历史）。没有 v1 那种结构化 `steps` —— 要自己从 messages 里配对 `AIMessage.tool_calls` + `ToolMessage`（按 `tool_call_id`）重建。
- **usage**：每条 `AIMessage` 带 `usage_metadata`（`input_tokens` / `output_tokens` / `input_token_details.cache_read`）。多轮要自己累加。
- **意外**：Step 1 没写任何路由/routing 逻辑就在 eval_set.json 上 100% 匹配 v1 的拒答和工具选择行为。v1 的 `route_question`（refuse/force_tool）是针对实测失败加的护栏，但 SYSTEM prompt 本身对 gpt-4o-mini 已经够 —— 说明那层护栏的价值要在 router/tier3 eval 集上才显现（Step 3 验证）。

### 死胡同 / 坑

- `create_agent` / `init_chat_model` 静默要求环境变量里有 key，`.env` 里的读不到 → `OpenAIError: Missing credentials`。显式构造 `ChatOpenAI` 解决。

## 下一步

- Step 3：路由 middleware（refuse 短路 + force_tool），跑 router eval 集验证。
- Step 4-6：carried-slots / clarify / history-trim middleware。
- 之后：graph 过 tier3 + defects + multiturn eval 集，全绿再算 Phase 2 完成。
