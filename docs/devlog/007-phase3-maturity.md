---
id: 007
phase: 3
title: 框架能力成熟化 —— 持久化 / 可观测 / 模型降级 / grounding 回边 / 按需澄清
started: 2026-09-11
finished: -
status: active
plan_ref: ../langgraph-migration-plan.md#3x-执行顺序与出处
---

## GOAL

拿到 v1 手写循环给不了的东西，**逐项单独验证**：会话跨进程重启续跑、按节点可观测、模型异常降级、grounding 不达标回边补证据、澄清从"前置预判"改成"按需中断"。

服务目标 #1（学框架在持久化 / 错误恢复 / HITL / 可观测上的惯用法）。工具级错误恢复 Phase 1 已完成，不重做。

**完成的样子**：plan §3.x 表里 3.1–3.5 各有 demo + 测试；`score` + `ab_compare` 不回归。

## INSPECT

- **能复用**：
  - `langgraph-checkpoint-postgres` **3.1.2 已装**（`pyproject` 里就有），`DATABASE_URL` 在 `.env`，复用现有 `financial_copilot` 库 —— 3.1 不用加依赖
  - `ModelFallbackMiddleware` 在 `langchain.agents.middleware` 导出列表里（已验证）—— 3.3 是预制件
  - `copilot.agent.grounding.verify_answer` —— 3.4 的判据直接用它，不重写
  - `copilot.agent.clarify.{clarification_for, as_text}` —— 3.5 复用判定，只换"怎么把它交给用户"
  - `ToolRuntime.store` 口子 Phase 1 已留 —— 3.6 若做不用改工具签名
- **新写**：`build.py` 的 checkpointer 选择；`middleware.py` 加 `_grounding_loop`(`@after_model`)、改 `_guard` 的 clarify 分支；测试
- **会影响**：`build.py` / `middleware.py` / 可能 `runner.py`（interrupt 的返回形状）。**不碰** `src/copilot/agent/`（v1 冻结）、`src/copilot/{storage,retrieval}`。v1_loop 不动。
- **读文档发现的三处修正**（已写进 plan §3.x）：durability 模式要显式选；checkpoint 无限增长有专门解法；**`HumanInTheLoopMiddleware` 不适用于澄清场景**，要用裸 `interrupt()`。

## PLAN

按独立性排序，前三项 smoke、后两项完整 A/B（见 plan §3.x 表）：

1. **3.1 PostgresSaver** —— `build.py` 的 `checkpointer=` 可配置；`setup()` 建表；定 durability 模式
2. **3.2 LangSmith** —— 环境变量挂 trace
3. **3.3 ModelFallbackMiddleware** —— `wrap_model_call` 层加主/备
4. **3.4 grounding 回边** —— `@after_model(can_jump_to=["model"])` + 重试上限
5. **3.5 interrupt 澄清** —— `_guard` 的 clarify 分支改 `interrupt()`，依赖 3.1

**怎么验**：每项见上表。**编排改动一律同时跑 `score --impl graph`（绝对分）和 `ab_compare`（parity）** —— devlog 006 的教训，parity 全绿不代表答案对。

## BUILD

**3.1 PostgresSaver** —— `build.py`：
- `_checkpointer(kind=None)` 读 `COPILOT_CHECKPOINTER=memory|postgres`（**默认 memory**，eval 不写 checkpoint 行、不依赖 DB 状态）
- `PostgresSaver.from_conn_string` 是**上下文管理器**，不适合模块级 agent 单例 → 改持有 `psycopg_pool.ConnectionPool`（`autocommit=True` + `row_factory=dict_row`，saver 自己管事务、按名读行），模块级 `_pg_pool` 让池活过单次调用
- 两个驱动一个库：`copilot.storage.db` 是 psycopg2（领域查询，共享/冻结不动），checkpointer 是 psycopg3（`langgraph-checkpoint-postgres` 自带，已装）
- `saver.setup()` 幂等建表

**3.3 ModelFallbackMiddleware** —— `build.py::_model_fallback()`：
- 读 `COPILOT_FALLBACK_MODELS`（逗号分隔，**默认空 = 不启用**）
- 传**模型实例**而非 `"openai:..."` 字符串（同 `_model` 的理由：key 在 `.env` 不在环境变量）
- 只在**异常**上触发，不在答案质量上 —— 沿用 v1 `model_router` 的结论

**3.4 grounding 回边** —— `middleware.py`：
- `_grounding_loop` `@after_model(state_schema=GroundingState, can_jump_to=["model"])`
- 只在**最终答案**（无 `tool_calls` 的 AIMessage）上触发；中途消息不干预
- `verify_answer` 不过 → 注入 `SystemMessage` 说明哪些数字无来源 → `jump_to: "model"`
- `MAX_GROUNDING_RETRIES = 1`（更多会绕圈且翻倍成本）；`COPILOT_GROUNDING_LOOP=0` 可关
- `numbers_checked == 0` 不触发（"没有可核查的数字"≠"通过"，但也没东西可退回）
- 投诉文本用 `_num()` 而非 `:,g` —— 后者把 391035000000 印成 `3.91035e+11`，模型无法与自己写的对应
- 顺带：`_steps_from_messages` 从 `runner` 移到 `middleware`（改名 `steps_from_messages`），避免 `middleware → runner` 循环 import

**3.2 LangSmith** —— 纯环境变量零代码，`.env.example` 记了 `LANGSMITH_TRACING` / `LANGSMITH_API_KEY`。**无 key，未能 smoke**。

## EVAL

**3.1 —— 跨进程续答 ✅（这是 `InMemorySaver` 做不到的）**

```
进程 1 (COPILOT_CHECKPOINTER=postgres, thread=pg_smoke_1)
  Q: "What was Cirrus Logic's revenue in fiscal 2024?"
  → $1,788,890,000
进程 2 （全新 Python 进程，同 thread_id）
  Q: "How dependent is it on Apple?"        ← 只有这一句
  → graph_query(customer=AAPL, supplier=CRUS)
  → "In fiscal year 2024, Cirrus Logic derived 87.0% ..."
```
"it" 解析成 CRUS、年份继承 FY2024（87.0% 是 FY2024 值，不是 FY2026 的 91%）。建表确认：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`，smoke 线程已清理。

**3.3 —— 降级 ✅**

| 配置 | 结果 |
|---|---|
| 坏主模型，无 fallback | `OpenAIModelNotFoundError`（如期失败）|
| 坏主模型 + `COPILOT_FALLBACK_MODELS=gpt-4o-mini` | 切换成功，正确答出 Apple FY2024 营收并带 accession |

**3.4 —— 单测通过，但评测上是 no-op ⚠️**

4 个单测（`pytest` **173/0**）：无来源 → 回边 + 投诉含可读数字；重试上限；中途 `tool_calls` 消息不干预；无可核查数字不干预；可关闭。

**完整 A/B（开 vs 关）**：defects 集两种配置**逐项相同**（4/6 cit + 6/6 ref）。

**结论：现有评测集上它从不触发** —— `grounding_flagged` 在 eval_set 上 v1_loop 和 graph **本来就都是 0**，没有可抓的目标。能力有了、单测证明了，**但评测价值为零**。和检索改进撞同一堵墙：集已饱和。

**回归（默认配置）**

| 检查 | 结果 |
|---|---|
| `pytest` | **173/0**（+4）|
| `score --impl graph` | Tier1 **17/17** · Tier2 **10/10** · refusal **100%** · grounding_flagged **0** · tokens 170,724 · 2.52s |
| `ab_compare` eval_set | **30/30 cit + 30/30 ref**（完美 parity）|
| `ab_compare` defects | 4/6 cit + 6/6 ref（两处为代词范围/引用宽窄的模型非确定性，与回边无关，已 A/B 证明）|

retrieval judge 2.57 → 2.43，但 `correct_judge` 仍 **100%（7/7）** —— 某题 judge 从 3 降到 2，仍算过，属 judge 噪声。

## RECORD

### 决策

### D1. 新能力一律**默认关**（除 grounding 回边），走环境变量
`COPILOT_CHECKPOINTER=memory`（默认）/ `COPILOT_FALLBACK_MODELS=`（空）。理由：eval 要可复现 —— postgres checkpointer 会写 checkpoint 行并引入 DB 状态依赖，fallback 会掩盖真实失败。grounding 回边例外（默认开），因为它是质量特性、不改可复现性，且 A/B 证明是 no-op。

### D2. checkpointer 用 `ConnectionPool` 而非 `from_conn_string`
`from_conn_string` 返回上下文管理器（`Iterator[PostgresSaver]`），和模块级 `_AGENT` 单例的生命周期对不上。改持有池，模块级变量保活。

### D3. 不动 `copilot/config.py` 加 setting，改用环境变量
`config.py` 在共享/冻结清单里。三个新开关都走 `os.getenv`，记在 `.env.example`。

### 死胡同 / 坑

- **`PostgresSaver` 需要 psycopg 3**，而项目 `storage/db.py` 是 psycopg2。不必统一 —— 两个驱动连同一个库、各管各的（领域查询 vs checkpoint）。`langgraph-checkpoint-postgres` 已把 psycopg3 带进来了。
- **`:,g` 格式化把大数印成科学计数法**（`3.91035e+11`），投诉文本里模型对不上自己写的数字。
- **`middleware → runner` 会循环 import**：grounding 钩子要 `_steps_from_messages`，而 `runner` 已经 import `middleware`。把 helper 移到 `middleware`，`runner` 反向 import。

### Learning（框架机制）

- **`before_agent` / `after_agent` 是"每轮一次"，`before_model` / `after_model` 是"每次模型调用"** —— 选 hook 按逻辑需要的粒度，不按方便。devlog 006 已经因为手写"首轮守卫"吃过一次亏。
- **`after_model` + `can_jump_to=["model"]` 把检测变成纠正**。v1 的 `verify_answer` 只能在循环**之后**给答案贴标签；同一个纯函数挂到 `after_model` 上、配一条回边，就能让模型重做 —— 这是手写循环不重构自身就做不到的事。**这是 Phase 3 "拿到 v1 给不了的东西"最干净的一个例子。**
- **middleware 自带 `state_schema` 可叠加**：`ResolvedState`（`_resolve`）和 `GroundingState`（`_grounding_loop`）各自声明，factory 编译时合并，`build.py` 完全不用管。Phase 4 加 `plan`/`budget`/`evidence_ledger` 时这个性质是关键。
- **`ModelFallbackMiddleware` 接模型实例**，不限于 `"provider:model"` 字符串 —— key 不在环境变量时必须这样传。
- **checkpoint 表是 4 张**（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`），`setup()` 幂等。文档另有两个坑待处理：durability 模式要显式选、checkpoint 会无限增长（`DeltaChannel` 是解法）。

### Retro

**连续第三次撞"评测集饱和"**：检索改进（2/3/4）测不出、grounding 回边测不出、3.4 的 A/B 是 no-op。**能力在涨，但没有任何指标能证明。** 这不是实现问题，是评测集问题 —— 优先级应该真正上移到"扩 `undisclosed` 陷阱题 + 建 Tier 4"，否则 Phase 3/4 都会是"做了但证明不了"。

## 下一步 / 解锁了什么

- **3.5（interrupt 澄清）有个设计问题要先定**：改成真中断后，`runner.run()` 对歧义问题不再返回答案而是返回中断，`score` / `ab_compare` 都会当失败。而 UI 在 Phase 5.F（已 park）。**没有消费端的中断，价值是零而破坏是实的** —— 需要先决定是做兼容层（中断时把澄清文本当 answer 返回 + 标志位）还是整体推到 Phase 5。
- 3.2 待 `LANGSMITH_API_KEY`。
- 3.6 `store` 已判定推 Phase 5；3.7 replay 可选。
