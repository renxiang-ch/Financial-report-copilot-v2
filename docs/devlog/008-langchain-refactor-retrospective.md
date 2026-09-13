---
id: 008
phase: retrospective
title: LangChain 重构复盘 —— 手写 Agent Loop → create_agent，六个维度上到底换来了什么
started: 2026-09-12
finished: 2026-09-12
status: done
covers: Phase 0a / 0b / 2 / 1 / 3 + 评测统一（devlog 001 003 004 005 006 007）
plan_ref: ../langgraph-migration-plan.md
---

## 这份文档是什么

**目标 ①（学框架在状态管理 / 工具调用 / 错误恢复 / 持久化 / 可观测 / 可扩展上的惯用法）的结算单。**

001–007 每个日志回答"这个里程碑做了什么"。这份回答另外三个问题：

1. 六个维度上，框架的惯用法**具体是什么**，v1 手写的做法**差在哪**；
2. 这次重构**换来了什么**（有数字的）；
3. 这次重构**证明不了什么** —— 以及为什么这一条直接决定 Phase 4 的入场条件。

**不做**：复述 diff、复述各 Phase 时间线（看 001–007）、复述决策细节（附录有索引）。

---

## 一页结论

**这次重构买到的是「能力 + 成本」，不是「准确率」—— 而准确率从 Phase 0b 起就没有可买的余量。**

| | 结果 | 证据 |
|---|---|---|
| 准确率 | **持平**（Tier1/Tier2/retrieval 逐项相同）| v1_loop 在 Phase 0b 就已 100%，天花板已到 |
| 成本 | **input token −18%**（208.7K → 170.8K），延迟 3.52s → **2.41s** | `eval-history.md` |
| 能力 | 拿到 4 件 v1 结构上做不到的事 | 「持久化」「可扩展」两节 |
| **缺陷发现能力** | trace 接通后**第一棵**就抓到一个 parity + 绝对分 + 173 个单测**三层全失明**的 bug | 「可观测」「错误恢复」|
| 代价 | 引入一类 v1 不可能有的 bug（`content`/`artifact` 划分错误），踩了 3 次 | devlog 006 Learning |
| 代码量 | 编排层 **573 行**，其中 348 行是可独立测试的 middleware | 「最终数字」（含不可相减的警告）|

**最重要的一条不是成绩，是仪器故障**：现有评测集已饱和，噪声底（judge ±0.14、citation ±4/67）大于 Phase 3 全部改动的可测效应。**连续三次"能力涨了但无法证明"。** 这不是实现问题，是评测集问题 —— 详见「证明不了什么」。

---

## 六个维度

### 1. 状态管理 —— 最大的认知修正

**计划原本想的**：把 `route` / `carried_slots` / `steps` / `citations` / `verification` 全塞进自定义 `state_schema`（plan 原文）。

**实际结论**：**它们是消息序列的纯函数。** checkpointer 已经存了完整 `messages`；middleware 里对回放的 `HumanMessage` 重跑一遍 `extract_slots` 折叠（`carry_from_messages`，等价于 v1 的 `conversation.carried_slots`）就够了。再存一份 slot dict 是**重复状态**，两份会不同步。

**于是形成了一条判据**（三处各不相同，值得单独记）：

| 放哪 | 判据 | 本项目实例 |
|---|---|---|
| **现算**（不进 state）| 能从 `messages` 推出来 | `route` / `carried_slots` / `steps` |
| **`state_schema`** | middleware 每轮加工、要跨节点传、`messages` 里没有 | `resolved`（Phase 1）、`grounding_retries`（Phase 3）|
| **`context_schema`** | 调用方 invoke 时就知道的**静态只读**依赖 | （本项目暂无）|

**踩过的错**：Phase 1 原计划用 `context_schema` 传 resolve 结果 —— `context` 是 invoke 时静态只读，**middleware 写不了**。改 `state_schema` 才对。

**框架机制**：middleware **自带 `state_schema` 可叠加** —— `ResolvedState` 和 `GroundingState` 各自声明，factory 编译时合并，`build.py` 完全不用管（Phase 3 之后 `build.py` 里已经没有 `GraphState` 了）。这个性质是 Phase 4 加 `plan` / `budget` / `evidence_ledger` 的基础。

**另一个深坑（花了最多时间）**：**"首轮"必须 turn-scoped。**
`round_idx == 0` 在图里对应「最后一条 `HumanMessage` 之后没有 `AIMessage`」。查「整个 `messages` 无 `AIMessage`」只在**线程第一轮**为真 —— 因为 checkpointer 把每轮的 `AIMessage` 都回放进 state。这个 bug 让 t2 及之后**所有 hook 静默失效**，表现是年份继承失灵（CRUS 答成 FY2026 的 91% 而不是 FY2024 的 87%）。

> **教训**：手写循环里"第几轮"是显式变量；图里它是**要从消息历史推导的判断**，而 checkpointer 会让最直觉的推导方式恰好是错的。

---

### 2. 工具调用

**变的**：信封、错误契约、schema、横切层。
**没变的**：SQL / RRF 融合 / 递归 CTE / AST 沙箱 —— **逐字**从 `copilot.agent.tools` 拷过来。A/B 只比端到端，就是为了让"接口变了、行为没变"可验证。

**框架惯用法**：

- **`@tool(response_format="content_and_artifact")`** 返回 `(text, dict)` → `ToolMessage(content=text, artifact=dict)`。`content` 进模型上下文，`artifact` 只给程序（`runner.py` 读它重建 v1 形状的 `steps`）。**这是 token 从 ~5.5–7.8K/问 降到 ~4.4K/问 的直接原因。**
- **`ToolRuntime`** —— 保留参数名，对模型隐藏，给 `.state` / `.context` / `.store` / `.tool_call_id`。取代散装的 `InjectedState` / `InjectedStore` / `InjectedToolCallId`。即使传了 `args_schema` 它仍被识别（`retrieve_text.args` 里不含 `runtime`，运行时能拿到 state）。
- **参数描述的位置**：`@tool` 的 args schema 从签名+注解自动生成；参数描述要么写在 Pydantic `Field(description=...)`，要么塞进 tool 的顶层 description。v1 的关键提示（精确 label 清单）在参数描述上，所以 Phase 2 先拼进顶层 description，Phase 1 才改成正式 `args_schema`。
- **`bind_tools` vs `ToolNode`**：`bind_tools` 是**模型侧**（绑 schema 让模型能*发起*调用），`ToolNode` 是**图侧**（*执行*：分发 / 并行 / 错误）。用 `create_agent` 时两者都在内部 —— 我们的暴露面只有 `TOOLS` 列表 + 几个预制 middleware。裸 `ToolNode` 留到 Phase 4 手搭子图。

**横切逻辑该放哪**（Phase 1 定的界）：

- 跨轮的（年份继承，需要完整历史）→ **middleware**，每轮一次
- 单次调用的（"没给年 → 用最新 filing"）→ **工具内**

> 别把需要历史的塞进工具。工具看不到历史，只能看到 `runtime.state`。

**这次重构引入的新 bug 类别（v1 不可能有）**：
模型看 `content`、程序看 `artifact`，**这个划分可以搞错，而且静默**。本轮踩了三次，全是同一个形状 ——「`content` 没有无损携带模型完成任务所需的东西」：

1. 段落只进 artifact（模型答"I cannot find"）
2. EPS `:,.0f` 把 6.08 显示成 6（artifact 里是精确值，模型只读 content）
3. 段落 `[:700]` 截断（丢 76% 文本）

**v1 只 dump 一个 dict，不存在这个划分。** 这是重构的真实代价，不是实施失误 —— 划分标准是「模型完成任务所需 vs 仅供程序追溯」，**不是**「摘要 vs 全量」，而且**每个工具要单独判**。

---

### 3. 错误恢复 —— 也是唯一被证伪的一条结论

**原本的结论是"不用手写，用预制件"。** 两处（工具错误、循环熔断）第一版都打算手写 `wrap_tool_call`，查文档才知有预制件，且文档写明组合顺序。

| 需求 | v1 | 框架 |
|---|---|---|
| 错误变模型可见消息 | 手拼错误字符串 | `ToolErrorMiddleware(on_error=…)` |
| `MAX_ROUNDS=10` | 手写计数器 | `ToolCallLimitMiddleware(run_limit=12, thread_limit=40)` |
| 模型异常降级 | `model_router` 手写 | `ModelFallbackMiddleware` |
| ~~工具报错重试~~ | ~~循环体 try/except + 计数~~ | **移除了**，见下 |

**我们自己只写了两样**：`ToolError` 异常类型（`kind` / `hint` / `model_correctable` / `data`）+ 15 行 `on_tool_error` 披露策略。**错误的分类学是领域知识** —— 这半条成立。

#### `ToolRetryMiddleware` 的移除（2026-09-12，由 LangSmith span 树抓到）

3.2 接通后第一棵 trace 就显示 `ToolRetryMiddleware` 包在 `ToolErrorMiddleware` **外面**，而 `build.py` 的注释和 plan 都写着"retry inner / error outer"。实测两种顺序：

| 顺序 | 工具体执行次数 |
|---|---|
| 当前 `[retry, error]`（retry 外）| **1x** —— 内层先把异常转成 ToolMessage，**retry 永不触发** |
| 交换 `[error, retry]`（retry 内）| **3x** = 1 + 2 retries |

**它是死代码，跑过整个 Phase 1/2/3。** 但**交换顺序是错的修法** —— `_MODEL_CORRECTABLE` 里三个 kind（`UNKNOWN_TICKER` / `WRONG_RELATION_SIDE` / `BAD_ARGUMENT`）**全是确定性参数错误**，而 `ToolRetryMiddleware` 用**同一组参数**重跑：打错的 ticker 重试 3 次就是失败 3 次，然后才披露。把它"修好"会让系统严格变差。

**根因是范畴错误**：`retryable` 这个名字把「**模型**换参数再调一次能修」接到了「**机械**重试同参数」的机制上。字段已改名 `model_correctable`，机制已移除。**系统此前是靠错误的顺序碰巧正确的。**

> 修正后的结论：**预制件不等于合适的预制件。** 预制件解决的是"要不要自己写重试循环"，不解决"这个错误该不该重试"。后者是领域判断，而且**只有把机制真跑起来看一眼才会发现自己搞错了** —— 这正是可观测性的价值，不是附加功能。

真正需要重试中间件的时机是 Phase 4 打外部 HTTP 源（EDGAR / 8-K），那里才有真瞬时失败。

**`ModelFallbackMiddleware` 接模型实例**，不限于 `"provider:model"` 字符串 —— key 不在环境变量时**必须**这样传（见「反复踩的那个坑」）。

---

### 4. 持久化

**`PostgresSaver` 给到了 v1 结构上做不到的事：跨进程续答。**

```
进程 1 (COPILOT_CHECKPOINTER=postgres, thread=pg_smoke_1)
  Q: "What was Cirrus Logic's revenue in fiscal 2024?"  → $1,788,890,000
进程 2 （全新 Python 进程，同 thread_id）
  Q: "How dependent is it on Apple?"        ← 只有这一句
  → graph_query(customer=AAPL, supplier=CRUS)
  → "In fiscal year 2024, Cirrus Logic derived 87.0% ..."
```

"it" 解析成 CRUS、年份继承 FY2024（87.0% 是 FY2024 值，不是 FY2026 的 91%）—— **全靠 checkpointer 回放的 messages，进程里没有任何内存状态。**

**框架机制 / 坑**：

- **`from_conn_string` 是上下文管理器**（`Iterator[PostgresSaver]`），生命周期和模块级 agent 单例对不上 → 改持有 `psycopg_pool.ConnectionPool`（`autocommit=True` + `row_factory=dict_row`：saver 自己管事务、按名读行），模块级变量保活
- **两个驱动一个库**：`storage/db.py` 是 psycopg2（领域查询，冻结不动），checkpointer 需要 psycopg3（`langgraph-checkpoint-postgres` 自带）。**不必统一** —— 各管各的关注点
- `setup()` 幂等，建 4 张表：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`
- **默认留 `memory`**：postgres 会写 checkpoint 行并给 eval 引入 DB 状态依赖，破坏可复现性

**还没处理的两个文档坑**：durability 模式要显式选；checkpoint 会**无限增长**（`DeltaChannel` 是解法）。Phase 4 长对话之前要面对。

---

### 5. 可观测 —— 六个维度里唯一**当场收回成本**的一个

**状态（2026-09-13）：✅ 通过。** `scripts/smoke_langsmith.py` 跑真题、从 API 把 trace 读回、核对 span 树 —— **6 个 middleware hook 各自出 span**：

```
LangGraph  2167ms
  └─ TrimHistory.before_agent  0ms            ← 1x   before_agent = 每轮一次
  └─ Resolve.before_agent  37ms               ← 1x
  └─ RefuseAndClarifyGuard.before_agent  1ms  ← 1x
  └─ model  1294ms
    └─ ActiveContext.wrap_model_call  1292ms  ← 2x   wrap_model_call = 每次调用
      └─ ForceFirstTool.wrap_model_call
        └─ ChatOpenAI  1287ms
  └─ GroundingLoop.after_model  0ms           ← 2x
  └─ tools  66ms
    └─ ToolRetryMiddleware.wrap_tool_call     ← 🐛 见下
      └─ ToolErrorMiddleware.wrap_tool_call
        └─ query_financials  64ms
  └─ model  729ms  …（第二次调用，整组 per-call hook 重来）
```

**这棵树本身就是两条结论的证据**，而在此之前它们只是注释里的断言：

1. **hook 粒度**：`before_agent` 1 次 / `wrap_model_call` 2 次 —— 「可扩展」那节的核心 Learning 从推断变成实测。
2. **middleware 嵌套顺序** —— 而它当场证明代码**装反了**。

#### 立刻抓到一个藏了三个 Phase 的 bug

`ToolRetryMiddleware` 包在 `ToolErrorMiddleware` **外面**，与 `build.py` 注释和 plan 记的相反 → 内层先把 `ToolError` 转成 ToolMessage → **retry 永不触发，是死代码**。完整分析见「3. 错误恢复」。

**为什么只有 trace 看得见**：

| 闸门 | 为什么失明 |
|---|---|
| `ab_compare`（parity）| 只比引用 + 拒答。两种顺序**最终都披露**，输出一致 |
| `score.py`（绝对分）| 同理 —— 答案正确，分数满分 |
| 173 个单测 | **没有一个**验证 retry 行为，只断言 `.retryable` 的**属性值** |
| LangSmith span 树 | **一眼看出嵌套是反的** |

> **这是整个重构里可观测性唯一一次、也是最有说服力的一次投资回报**：接通后的第一棵 trace 就找到了一个三层闸门都测不到的缺陷。
> 更重要的是它属于**一整类**问题 —— 「配置写对了、注释写对了、但运行时结构和你以为的不一样」。这类问题**不可能**用输入输出断言发现，因为输入输出是对的。

#### 但"零代码"在本项目是错的

文档说 `create_agent` 自动出 trace、只需两个环境变量。前提是 SDK 看得见那两个变量，**而本项目它看不见**。两条都**静默**失败（不抛异常，trace 就是不出现）：

1. `config.py` 用 pydantic-settings 读 `.env`，**不导出到 `os.environ`**，而 `langsmith` SDK 直接读 `os.environ`。**这和 Phase 2 D1（`init_chat_model("openai:…")` 读不到 key）是同一个根因，隔了两个 Phase 又出现一次**，只是这次不报错
2. **`langsmith.utils.get_env_var` 是 `@lru_cache` 的** —— 任何在桥接前问过一次"tracing 开没开"的代码，把"没开"缓存到进程结束。**光桥接不够，必须 `cache_clear()`**

→ `enable_tracing()` 同时解这两条（幂等，`.env` 只读一次，shell `export` 优先）；`tracing_project()` 给 eval 分项目并给每题盖 item id。

**调试技巧（无 key 也能验链路）**：把 `LANGSMITH_ENDPOINT` 指向死端口 `http://127.0.0.1:9`，开 tracing 跑一次工具调用 → 报错信息里会看到 tracer 确实创建了 run 并去 `POST http://127.0.0.1:9/runs/multipart`。**五段（`.env` → `os.environ` → 清缓存 → SDK 认账 → langchain 发 run）逐段确认，不出一次外网请求。**

#### 它补上的是 `runner.py` 结构上拿不到的东西

`create_agent` 不返回 v1 那种结构化 `steps`，所以 `runner.py` 是**从 messages 反推**（按 `tool_call_id` 配对 `AIMessage.tool_calls` + `ToolMessage`）。反推够打分，但**不含归因**：

| 问题 | 反推 | trace |
|---|---|---|
| 答案对不对 | ✅ | ✅ |
| 调了哪些工具 | ✅ | ✅ |
| **哪个 hook 耗了多久** | ❌ | ✅（`Resolve` 37ms / `TrimHistory` 0ms）|
| **middleware 实际嵌套成什么样** | ❌ | ✅ **← 抓到 bug 的就是这条** |
| 哪次重试发生在哪 | ❌ | ✅ |
| token 花在哪个环节 | ❌ | ✅ |

**Phase 4 会让这件事从"便利"变成"阻塞"**：supervisor + 子 agent fan-out 之后，"哪个子 agent 烧了 70% 预算"只有 trace 能答，而那正是 4.5「按角色分模型」要 A/B 数据支撑的东西。

**顺带纠正一处 plan 错误**：plan 第 238 行写「保留 Langfuse callback，双挂对比」—— 但 `config.py:22-24` 只有三个 Langfuse **配置字段**，全仓库**没有任何 Langfuse 客户端或 callback 代码**。v1 从没真接过 Langfuse，**没有"双挂"可做**。已改 plan。

**测试侧还踩了一个值得单记的坑**：`monkeypatch` **撤销不了被测代码自己写进 `os.environ` 的值**。`delenv(raising=False)` 在键本不存在时什么都没记录，于是 `enable_tracing()` 写的 `LANGSMITH_TRACING=true` 漏给了后面的测试文件，`test_v2_tools` 的工具调用**真被 POST 到 LangSmith**（403，用的是测试里的假 key）。**单跑那个测试文件不复现，只在全量套件里出现** —— 又一个"闸门的分辨率决定能不能看见"的例子。

---

### 6. 可扩展 —— "改循环体" → "加一个装饰函数"

这是六个维度里**结构变化最大**的一个。v1 的 pre-loop policy（route → refuse 短路 / force_tool / 假设块注入 / 历史裁剪）原本全在循环体里；现在是 6 个挂在图上的 hook：

| hook | 装饰器 | 干什么 | 为什么是这个粒度 |
|---|---|---|---|
| `TrimHistory` | `@before_agent` | 历史裁剪 | 每轮一次 |
| `Resolve` | `@before_agent(state_schema=ResolvedState)` | ticker / 年份 / relation-side 归一 | 每轮一次，结果要跨节点传 |
| `RefuseAndClarifyGuard` | `@before_agent(can_jump_to=["end"])` | 拒答 / 澄清短路 | 每轮一次，且要能跳出图 |
| `ActiveContext` | `@wrap_model_call` | 假设块注入 | **每次调用现拼，不写回历史** |
| `ForceFirstTool` | `@wrap_model_call` | 首轮 pin 工具 | 要改 `tool_choice` |
| `GroundingLoop` | `@after_model(can_jump_to=["model"])` | 不达标退回重做 | 每次模型响应后 |

**最重要的一条框架机制**：

> **`before_agent` / `after_agent` 是"每轮一次"；`before_model` / `after_model` 是"每次模型调用"。**
> 按逻辑需要的**粒度**选 hook，不按方便选。

这条是**吃了两次亏**才学到的：前三个 hook 原本挂 `@before_model` + 手写"首轮守卫"，归位到 `@before_agent` 后**手写守卫从 5 处降到 2 处** —— 框架原生就有这个语义，手写等于重新实现一遍并且会写错（见「状态管理」里的 turn-scoped bug）。

**其余机制**：

- `@wrap_model_call(request, handler)` 包住每次 model 调用，`handler(request)` 才是真调用 —— retry / fallback / 改 `tool_choice` 都在这层。`ModelRequest.override(...)` 返回改过的副本
- **改这次 prompt 但不写回历史**：在 `wrap_model_call` 里 `request.override(messages=…)` 只影响这一次调用；在 `before_model` 里返回 `{"messages": […]}` 会**持久化进 state、下一轮还在**。假设块属前者
- **删消息要 `RemoveMessage(id=…)`**：`messages` 是 add-reducer，正常返回只能追加
- `can_jump_to=[…]` **必须声明在装饰器上**，否则返回 `jump_to` 无效

**`after_model` + 回边把"检测"变成"纠正" —— 这是本维度最干净的收获。**
v1 的 `verify_answer` 只能在循环**之后**给答案贴标签（"这几个数字无来源"）。**同一个纯函数**挂到 `@after_model(can_jump_to=["model"])` 上、配一条回边，模型就能被退回重做。这是手写循环不重构自身**做不到**的事。

⚠️ **但它在评测上是 no-op** —— 见下一节。

---

## 最终数字

### 三方同尺子（`eval_set.json` 30 题，`copilot.v2.eval.score`）

| | generic_agent | v1_loop | **graph** |
|---|---|---|---|
| Tier1 可答 (17) | **100%** | **100%** | **100%** |
| Tier2 (10) | **100%** | **100%** | **100%** |
| retrieval judge 均分 | **2.57** | **2.57** | **2.57** |
| refusal_accuracy | 0% (0/3) | **100%** | **100%** |
| steps-dep（strict/hit/inputs/flagged）| — *(无定义)* | 100/100/100/0 | 100/100/100/0 |
| input tokens | — | 208,695 | **170,748（−18%）** |
| 平均延迟 | 31.3s | 3.52s | **2.36s** |
| 成本/题 | ~$0.131 | ~$0.001 | ~$0.001 |

generic_agent 的 refusal 0/3 不是能力问题：那 3 道全是「原始 10-K 能答、v1 schema 没有」，判错是 v1 口径所致。

### 工程指标轨迹

| 里程碑 | pytest | token/问 | A/B refusal |
|---|---|---|---|
| Phase 0a | 120/8 | — | — |
| Phase 0b | 143/0 | ~7K (v1_loop) | baseline |
| Phase 2（agent loop 端口）| 149/0 | ~5.5–7.8K | 67/67 |
| Phase 1（工具层）| 167/0 | **~4.4K** | 66/67 |
| 评测统一 | 169/0 | 同 | — |
| Phase 3 | **173/0** | 同 | **67/67** |

### 代码量

| | 行数 |
|---|---|
| v1 `copilot/agent/`（全部）| 3,450（`agent.py` 787）|
| v2 `orchestration/graph/` | **573**（middleware 348 + build 142 + runner 83）|
| v2 `tools/` | 800 |

⚠️ **不能直接相减**：v2 仍 import v1 的 `slots` / `grounding` / `clarify` / `provenance`（那是领域逻辑，故意不重写），且领域 SQL 是逐字拷的。这组数字只说明一件事 —— **编排层本身**在 573 行里，且其中 348 行是可独立测试的 middleware。

---

## 踩坑清单

按"为什么没被早点发现"排序 —— 这是最有教学价值的一列。

| # | 坑 | 后果 | 为什么闸门没抓到 |
|---|---|---|---|
| 1 | `_before_first_model_call` 查全局 `AIMessage` 而非 turn-scoped | t2+ **所有 hook 静默失效**，年份继承失灵 | 单轮测试全过；只有多轮集能暴露 |
| 2 | 段落只进 `artifact`，`content` 放 160 字预览 | 6 道 `ret_*` 答"I cannot find"，eval_set 掉到 24/30 | A/B 抓到了（这次运气好）|
| 3 | `retrieve_text` `body[:700]` | 丢 **76%** 检索文本，judge 3→1 | **A/B 结构上看不见** —— 两边引用同一 accession、都没拒答 = "匹配" |
| 4 | `financials` `:,.0f` 把 EPS 6.08 印成 6 | 答 $6.00，golden 6.08 → 失败 | 同上。**答案数字错了，parity 测不到** |
| 5 | `runner` 错误路径返回 str 而非 dict | `harness._build_tool_trace` 崩；同一坑埋在 `_collect_citations` / `build_provenance` / `verify_answer` 底下（**8+ 处**无保护 `.get()`）| 5 轮 A/B 没触发，只因为那些跑里没有工具报错 |
| 6 | `context_schema` 传 resolve 结果 | 设计错误（`context` 只读，middleware 写不了）| 文档核对时发现，没进代码 |
| 7 | `init_chat_model("openai:…")` 读不到 `.env` 的 key | `Missing credentials` | 立即失败，便宜。**但同一根因在 3.2 会静默失败** |
| 8 | `:,g` 把 391035000000 印成 `3.91035e+11` | grounding 投诉文本里模型对不上自己写的数 | 单测抓到 |
| 9 | `middleware → runner` 循环 import | — | 立即失败 |
| 10 | `ToolErrorKind(str, enum.Enum)` | ruff `UP042`（Py3.11+ 要 `enum.StrEnum`）| lint |
| 11 | `langsmith.utils.get_env_var` 是 `@lru_cache` 的 | 桥接后 tracing 仍是 off —— **静默** | 需专门写回归测试（现已有）|
| 12 | `monkeypatch` 撤销不了**被测代码**写的 `os.environ` | `LANGSMITH_TRACING=true` 漏给后面的测试，工具调用真被 POST 出去（403）| **单跑该文件不复现**，只在全量套件出现 |
| **13** | **`ToolRetryMiddleware` 顺序装反 → 从未触发，是死代码** | 跑过整个 Phase 1/2/3。而且"修好"它会让系统更差（见「错误恢复」）| **两道闸门同时失明**：A/B 只比引用+拒答；173 个测试里**没有一个**验证 retry 行为（只断言 `.retryable` 的属性值）。**最后是 LangSmith 的 span 树看出来的** |

### 从 3 / 4 / 5 抽出的通用教训

**`ab_compare`（parity）作为唯一闸门跑了整个 Phase 1 和 Phase 2。**
它只比 citations + refusal，因此对两类失败**结构上失明**：

1. **两边一起错**
2. **内容错但引用对**

坑 3 和坑 4 是**同时**带在身上跑过 5 轮全绿 A/B 的。修复办法不是改 parity 阈值，而是**再加一个不同类型的闸门** —— `score.py`（绝对分）。两个都要，不是二选一。已写进 `docs/dev-workflow.md` 的验证清单。

**另一条**：坑 3 的 `700` 是**没量就拍的数**。当时的动机是压 input token（Phase 1 被考核的指标），而手上的工具**结构上测不到质量代价**。修复后实测：无损全文只多 **5.2%** token（仍比 v1_loop 低 18%）。那次"优化"从头到尾是净损失。

> **指标驱动优化的陷阱**：当只有一个指标能测、而代价落在测不到的维度上时，优化会**稳定地**往错方向走。

---

## 这次重构证明不了什么

**这一节比成绩单重要 —— 它是 Phase 4 入场条件的全部依据。**

### 仪器已经饱和

`eval_set.json` 上**三方在可答题上完全打平**（Tier1 / Tier2 / retrieval judge **逐项**相同）。这个集**无分辨力**。唯一差异在 refusal，而那 3 道**没有一道是真 `undisclosed` 陷阱**（全是 `v1_schema_gap` / `out_of_scope`）。

### 噪声底大于信号

| 指标 | 逐次波动 | 一次最小可分辨变化 | Phase 3 可测效应 |
|---|---|---|---|
| retrieval judge 均分 | 2.57 ↔ 2.43 | **±0.14**（7 题、整数 judge，一题动 1 分 = 1/7）| **0** |
| citation 匹配（67 组）| 62 ↔ 58 | **±4** | **0** |

观测到的 judge 波动（0.14）**恰好等于**仪器的最小刻度 —— 即"一道题的 judge 从 3 掉到 2"。`correct_judge` 仍 100%（7/7），所以它是噪声不是回归（Phase 3 没动检索路径）。

**但结论很硬：噪声 ≥ 信号。** Phase 3 全部改动的可测效应是 0，而仪器抖动是 ±0.14 / ±4。

### 连续三次"做了但证明不了"

1. **检索改进**（去截断）—— 靠 `score.py` 才测到，`ab_compare` 全程失明
2. **grounding 回边**（3.4）—— 代码 + 4 个单测通过，**5 个集上全部确认 no-op**：`grounding_flagged` 在 v1_loop 和 graph 上**本来就都是 0**，没有可抓的目标
3. **3.4 的开关 A/B** —— 开 vs 关**逐项相同**

**能力在涨，但没有任何指标能证明。**

> 这不是实现问题，是**评测集**问题。Phase 4 的规模比前面所有 Phase 加起来还大（supervisor + 3 个专家子图 + 新工具 + 新摄取 + rubric + 按角色分模型）。**在没有尺子的情况下盲建，是把已经发生三次的问题放大十倍。**

---

## Phase 4 入场条件（交接）

plan 的风险表自己写着两条闸门：**「过度设计（没对齐就上 supervisor）」** 和 **「分部 / guidance 数据获取难 → Phase 4 前先做 1–2 天 spike」**。两条都还没满足。

| # | 入场条件 | 现状 | 为什么是硬条件 |
|---|---|---|---|
| **1** | **≥10 道 Tier 4 题**，带人工锚定答案 + rubric | **0 道** | Phase 4 的退出标准是"Tier 4 rubric 均分达标"。**无法通过一个不存在的标准** |
| **2** | 扩 `undisclosed` 真陷阱题 1 → 8~10 道 | 1 道（n=1）| 三方唯一的真差异维度，现在样本量是 1 |
| **3** | 数据 spike：8-K Item 2.02（指引）+ XBRL 维度成员（分部）| 未做 | 语料是 **148 份纯 10-K**，而 **10-K 里基本没有前瞻性指引** → 三类目标问题里「指引兑现度」**数据为零** |
| ~~4~~ | ~~3.2 LangSmith 打通~~ | ✅ **2026-09-13 完成** | 已解除。supervisor fan-out 的预算归因现在有工具了 |

**执行顺序（重要）**：**先写题，再按题抓数据。**
10 道题写完会自己指出缺哪些数据 —— 这比抽象地问"数据够不够"有界得多。而且它**同时**解掉评测饱和。

**已知可切的垂直切片**：三类目标问题里，**「管理层风格」的 3/4 子面用现有 10-K 语料就能答**（口径一致性、谈风险的方式、资本配置措辞；只有"是否惯性保守"需要指引数据）。所以 Phase 4 可以先用这一类把 supervisor → 子图骨架跑通，A 类和 C 类之后是**加子图 + 加数据源**，不是重做。

### Phase 3 的 carve-out

| 项 | 处置 | 理由 |
|---|---|---|
| 3.2 LangSmith | ✅ **完成**（2026-09-13）| `observability.py` 桥接 + `scripts/smoke_langsmith.py` 验收。6 hook 各自出 span；抓到 retry middleware 装反 |
| 3.5 interrupt 澄清 | **推 Phase 5.F** | 真 `interrupt()` 后 `run()` 对歧义题不返回答案，`score` / `ab_compare` 都判失败，而消费它的 UI 本就在 5.F。**没有消费端的中断，价值是零而破坏是实的**。不做兼容层 —— 为了让 eval 好看而包一层，是给评测演戏 |
| 3.6 `store` | 推 Phase 5 | 已判定 |
| 3.7 replay | **可选，优先级已降** | 3.2 通了之后，"那次到底发生了什么"大部分能从 trace 看，不必重放 |

---

## 方法论层面的三条

这三条不是框架知识，是这次重构里关于**怎么干活**的结论，已同步进 `docs/dev-workflow.md`。

**① parity 闸门和绝对分闸门是两件事，都要。**
`ab_compare` 回答"改动有没有引起行为变化"，`score.py` 回答"答案对不对"。前者对"两边一起错"失明。Phase 3 起两个都进验证清单。

**② EVAL 段落必须列出验证清单的每一项并标状态，而不是只列跑过的。**
Phase 3 一度只跑了 5 个 A/B 集里的 2 个却写成"回归"，用户追问才发现。漏掉的恰恰是**风险最高**的两个 —— tier3（多步计算最多，3.4 最可能触发）和 multiturn（对 state 变化最敏感，本轮刚加 `GroundingState`）。
**一份只列已跑项的报告，读起来和跑全了一样。** 改进：每项标 ✅ / ❌未跑 / ⏸阻塞。

**③ 先查预制件，再动手写 —— 但装上之后必须验证它真的在跑。**
错误处理、循环熔断两处第一版都打算手写 middleware；查 MCP 文档才知道有 `ToolRetryMiddleware` / `ToolErrorMiddleware` / `ToolCallLimitMiddleware`，**且文档写明了组合顺序**。`HumanInTheLoopMiddleware` 反过来 —— 查了才知道它**不适用**于澄清场景（它的 `interrupt_on` 按工具名触发，而我们的澄清是答前判断），要用裸 `interrupt()`。**两个方向都靠文档核对，不靠猜。**

**但这条有个后半段，是坑 13 教的**：预制件装上、文档读对、顺序写进注释，它**依然可能根本没在跑**。`ToolRetryMiddleware` 的注释准确描述了预期顺序，代码里的列表顺序是反的，三个 Phase 无人发现。
**规则**：引入一个改变运行时行为的预制件时，同时补一条**断言它确实生效**的测试（"工具体执行了 N 次"，而不是"配置对象长这样"）。否则它是一行昂贵的注释。

---

## 附录：决策索引

| 决策 | 出处 |
|---|---|
| model 传实例不传 `"openai:…"` 字符串 | [004](004-agentloop-port.md) D1 |
| 参数描述拼进顶层 description（Phase 2 权宜）| [004](004-agentloop-port.md) D2 |
| `retrieve_text` 的 `content` 必须带段落全文 | [005](005-tool-layer-rebuild.md) D1 |
| 评测分 steps-independent / steps-dependent 两块 | [006](006-unified-eval.md) D1 |
| 恢复「`step["output"]` 永远是 dict」不变式 | [006](006-unified-eval.md) D2 |
| 数值格式化不能损失精度（`_fmt`）| [006](006-unified-eval.md) D3 |
| `retrieve_text` 段落不截断 | [006](006-unified-eval.md) D4 |
| 新能力一律默认关，走环境变量 | [007](007-phase3-maturity.md) D1 |
| checkpointer 用 `ConnectionPool` 而非 `from_conn_string` | [007](007-phase3-maturity.md) D2 |
| 不动 `config.py`，新开关走 `os.getenv` | [007](007-phase3-maturity.md) D3 |

**分数历史**：`docs/eval-history.md`
**路线图**：`docs/langgraph-migration-plan.md`
**实时状态**：`docs/devlog/README.md`
