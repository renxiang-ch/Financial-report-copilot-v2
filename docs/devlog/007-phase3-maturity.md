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

**3.2 LangSmith** —— **"纯环境变量零代码"是错的**（本条 2026-09-12 重做，见下）。

### 3.2（补做，2026-09-12）—— 零代码的前提不成立

文档说 `create_agent` 自动出 trace、只需两个环境变量。**前提是 SDK 看得见那两个变量，而本项目它看不见。** 两条都**静默**失败 —— 不抛异常，trace 就是不出现：

1. `config.py` 用 pydantic-settings 读 `.env`，**不导出到 `os.environ`**；`langsmith` SDK 直接读 `os.environ`。和 devlog 004 D1（`init_chat_model("openai:…")` 读不到 key）**同一个根因**。而且 `LANGSMITH_*` 连 `Settings` 字段都没有（`config.py` 在冻结清单，D3），所以 `settings` 也供不出来。
2. **`langsmith.utils.get_env_var` 是 `@lru_cache` 的。** 任何在桥接之前问过一次"tracing 开没开"的代码，会把"没开"缓存到进程结束 —— **光桥接不够，必须清缓存**。实测：设完 env 不清缓存仍是 `False`，清完变 `True`。

**新增 `src/copilot/v2/observability.py`（~110 行）**：

- `enable_tracing()` —— 从 `.env` 把 4 个 `LANGSMITH_*` 键拷进 `os.environ`（**shell `export` 优先**），再 `get_env_var.cache_clear()`。幂等，文件只读一次。**`TRACING=true` 但无 key 时返回 `False` 并告警** —— 那个组合正是本模块要防的静默失败
- `tracing_project(project, **metadata)` —— context manager，tracing 关时是 no-op（调用方无条件包）。**每道 eval 题一条 trace，带 item id**
- 只桥接 `LANGSMITH_*`。整个 `.env` 灌进环境会顺带改变别的库怎么解析凭据
- 复用 `config._ENV_FILE` 而不是重算 `__file__` 深度 —— `ab_compare._REPO_ROOT` 就是这么坏过一次

**接线**：`build_agent()` 调一次 `enable_tracing()`（显式调用，不用 import 副作用 —— 同 004 D1 的理由）；`score.run()` 每题包 `tracing_project(f"frc-eval-{impl}", item=…, tier=…, type=…)`；`ab_compare._run_graph` 包 `frc-eval-ab`。

**`.env` / `.env.example`** 各补 `LANGSMITH_PROJECT`（默认 `frc-dev`）和 **`LANGSMITH_ENDPOINT`** —— 后者非 US 区账号**必填**，否则 key 认不出、同样静默失败。

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

**3.2 —— 链路验证通过 ✅，真 smoke 仍缺 key ⏸**

没有 key 也能验"桥接是否真的通到 tracer"：把 `LANGSMITH_ENDPOINT` 指向死端口 `http://127.0.0.1:9`，开 tracing 跑一次工具调用 ——

```
enable_tracing() -> True
tool -> 1+1 = 2.0
Failed to multipart ingest runs: ... POST http://127.0.0.1:9/runs/multipart
```

**tracer 确实创建了 run，并按桥接进去的 `LANGSMITH_ENDPOINT` 去投递。** 即：`.env` → `os.environ` → 清缓存 → SDK 认账 → langchain 发 run，五段全通，**只差真 key**。

**8 个单测**（`pytest` **181/0**，+8）：默认关 / `.env` 桥接生效 / **stale `lru_cache` 被清**（那个坑的回归测试）/ shell 优先于文件 / 开但无 key 时告警且返回 `False` / 关时 `tracing_project` 是 no-op / 开时 project+metadata 正确且丢掉 `None` / `.env` 只读一次。

**坑（测试侧）**：`monkeypatch` **撤销不了被测代码自己写进 `os.environ` 的值** —— `delenv(raising=False)` 在键本不存在时什么都没记录，于是 `enable_tracing()` 写的 `LANGSMITH_TRACING=true` 漏给了后面的测试，`test_v2_tools` 的工具调用**真被 POST 到 LangSmith**（403，用的是测试里的假 key）。单跑该文件不复现，只在全量套件里出现。改成手工快照/恢复 4 个键。

**3.2 的副产品 —— span 树抓到 `ToolRetryMiddleware` 是死代码 🐛**

接通后第一棵 trace 就显示 retry 包在 error **外面**，与 `build.py` 注释和 plan 记的 "retry inner / error outer" 相反。实测：

| 顺序 | 工具体执行次数 |
|---|---|
| 当前 `[retry, error]`（retry 外）| **1x** —— 内层先转 ToolMessage，**retry 永不触发** |
| 交换 `[error, retry]`（retry 内）| **3x** = 1 + 2 retries |

**但交换是错的修法**：`_RETRYABLE` 三个 kind（`UNKNOWN_TICKER` / `WRONG_RELATION_SIDE` / `BAD_ARGUMENT`）**全是确定性参数错误**，机械重试同参数只会失败 3 次再披露 —— 把它"修好"会让系统严格变差。

**根因是范畴错误**：`retryable` 把「**模型**换参数再调能修」接到了「**机械**重试同参数」的机制上。处置：**移除 `ToolRetryMiddleware`**；字段改名 **`model_correctable`**；新增 `tests/test_tool_error_recovery.py`（5 个端到端行为测试，用 fake model、零成本）。**验过这些测试"有牙"** —— 把 retry 以生效顺序加回去，2 个测试立刻转红。

**为什么藏了三个 Phase**：`ab_compare` 只比引用 + 拒答（两种顺序都最终披露，输出一致）；173 个测试里**没有一个**验证 retry 行为，只断言 `.retryable` 的**属性值**。
**规则（已进 008 方法论 ③）**：引入改变运行时行为的预制件时，同时补一条断言**它确实生效**的测试（"工具体执行了 N 次"，而不是"配置对象长这样"），否则它是一行昂贵的注释。

**回归（2026-09-13，移除 retry 后）—— 零行为变化**

| 闸门 | 结果 |
|---|---|
| `pytest` | **186/0**（+5）|
| `score --impl graph` | Tier1 **17/17** · Tier2 **10/10** · judge **2.57**（2.43 回弹）· refusal **100%** · flagged 0 · 170,758 tok · 2.41s |
| A/B 5 集 | **59/67 cit · 67/67 ref**（eval_set 29/30 · router 7/12 · tier3 **8/8** · defects 5/6 · multiturn 10/11）|

全部落在已记录波动带内。**judge 第三次观测回到 2.57，坐实 ±0.14 是噪声底**（见 `eval-history.md` 新增小节）。

**回归（默认配置）—— 5 个 A/B 集全跑**

| 检查 | 结果 |
|---|---|
| `pytest` | **173/0**（+4）|
| `score --impl graph` | Tier1 **17/17** · Tier2 **10/10** · refusal **100%** · grounding_flagged **0** · tokens 170,724 · 2.52s |

| A/B 集 | citations | refusal | 对比 devlog 005 |
|---|---|---|---|
| eval_set (30) | **30/30** | **30/30** | cit 29→30 ↑ |
| router (12) | 7/12 | **12/12** | 9→7（已知 7–9 波动带）|
| multiturn (11轮) | 10/11 | **11/11** | 11→10（已知 10–11）|
| tier3 (8) | 7/8 | **8/8** | 8→7（已知 7–8）|
| defects (6) | 4/6 | **6/6** | 5→4（已知 4–5）|
| **合计** | 58/67 | **67/67** | 62→58，全在波动带内 |

**refusal 67/67，零不匹配。** citation 集逐条查过，全部落在已记录的三类：`retrieve_text` 引用宽窄、`graph_query` 参数非确定性、代词范围。例如 tier3 唯一分歧 `t3_crus_dollar_impact_2024` —— 两边**算出同一个正确数** `$311,266,860`，只是 v1 带了 5 个 accession、graph 只带真用到的 2 个（**graph 更紧**）。

**citation 匹配在整套上有 ±4 的逐次波动**（62→58），这个分辨率下它不是回归信号 —— 又一次"仪器分辨不出想测的东西"。

retrieval judge 2.57 → 2.43，但 `correct_judge` 仍 **100%（7/7）** —— 某题 judge 从 3 降到 2，仍算过，属 judge 噪声。

**3.4 在 5 个集上全部确认 no-op。** 除 defects 的开关 A/B 外，还单独验了 tier3 那道分歧题：`verified: True`、4 个数字全可溯源、`unverified_numbers` 为空 —— 回边从未触发。

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

**① 连续第三次撞"评测集饱和"**：检索改进（2/3/4）测不出、grounding 回边测不出、3.4 的 A/B 是 no-op。**能力在涨，但没有任何指标能证明。** 这不是实现问题，是评测集问题 —— 优先级应该真正上移到"扩 `undisclosed` 陷阱题 + 建 Tier 4"，否则 Phase 3/4 都会是"做了但证明不了"。

**② 本轮 EVAL 一度只跑了 5 个 A/B 集里的 2 个，却写成了"回归"。**
用户追问"跑过测试了吗"才发现漏了 router / tier3 / multiturn。漏掉的恰恰是**风险最高**的两个 —— tier3 多步计算最多（3.4 最可能触发），multiturn 对 state 变化最敏感（本轮给 schema 加了 `GroundingState`）。
dev-workflow 要求 EVAL "贴命令和输出，不说『应该好了』"，我贴了输出，**但没贴「没跑的清单」** —— 一份只列已跑项的报告，读起来和跑全了一样。
**改进**：EVAL 段落必须把验证清单里的每一项都列出并标状态（✅ / ❌未跑 / ⏸阻塞），而不是只列做过的。

## 下一步 / 解锁了什么

- **3.5（interrupt 澄清）有个设计问题要先定**：改成真中断后，`runner.run()` 对歧义问题不再返回答案而是返回中断，`score` / `ab_compare` 都会当失败。而 UI 在 Phase 5.F（已 park）。**没有消费端的中断，价值是零而破坏是实的** —— 需要先决定是做兼容层（中断时把澄清文本当 answer 返回 + 标志位）还是整体推到 Phase 5。
- **3.2 基础设施已完成**（`observability.py` + 接线 + 8 单测 + 链路验证），**只剩真 smoke 待 `LANGSMITH_API_KEY`**：拿到 key 后 `.env` 里 `LANGSMITH_TRACING=true`，跑一次 `run()`，验收标准是 trace 里能看到 **6 个 hook + model + tool 各自独立的 span**。⚠️ 建 key 时**确认账号 region** —— 非 US 必须同时设 `LANGSMITH_ENDPOINT`。
- **顺带发现 plan 的一处错误**：plan 第 238 行写「保留 Langfuse callback，双挂对比」，但 `config.py` 只有 3 个 Langfuse **配置字段**，全仓库**零** Langfuse 客户端/callback 代码 —— v1 从没真接过，**没有"双挂"可做**。已改 plan。
- 3.6 `store` 已判定推 Phase 5；3.7 replay 可选。
