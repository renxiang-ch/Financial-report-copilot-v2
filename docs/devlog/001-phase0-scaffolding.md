---
id: 001
phase: 0
title: 脚手架与护栏
started: 2026-09-03
finished: -
status: active
plan_ref: ../langgraph-migration-plan.md#phase-0--脚手架与护栏先做
---

## 目标

新仓库能同时跑「v1_loop（冻结基线）」和「LangGraph（Phase 0 为 stub）」两套编排；eval harness 移植到位；A/B 对比脚本骨架能跑；拿到 v1_loop 的 Tier 1–3 baseline 分数。

退出标准：
- `from copilot.orchestration.v1_loop import ask` 和 `graph.build_graph()` 都能 import
- `ab_compare` 端到端能跑（graph 侧 stub）
- v1_loop 复现 v1 的 Tier 1–3 分数，记入 `docs/eval-history.md`

## 拆解

分两批：**0a 离线**（无需 DB / API key，现在可做）、**0b 需凭据**（Postgres + pgvector + `OPENAI_API_KEY` + 网络）。

### 0a — 离线

| # | 任务 | 产出 / 验证 |
|---|------|-------------|
| 0.1 | 仓库初始化：`git init` + `.gitignore` + `.env.example`（抄 v1）。`pyproject.toml`：v1 运行时依赖 + `langgraph` / `langchain` / `langchain-openai` / `langgraph-checkpoint-postgres` / `langsmith`；dev 依赖 `pytest` `pytest-asyncio` `ruff` `mypy`；src 布局；`python>=3.11`。`uv sync` | `uv run python -c "import langgraph, openai, sentence_transformers"` 通过 |
| 0.2 | **原样移植 v1**：`rm -rf reference/v1/.git`，`.gitignore` 掉 `reference/`。拷 `reference/v1/src/copilot/{agent,retrieval,storage,pipeline,eval,config.py,api.py,dashboard.py,__init__.py}` → `src/copilot/`（**不改 import**，路径全部保持 `copilot.*`）。拷 `reference/v1/{data,tests}` → 仓库根。可选拷 `.streamlit` `frontend.py` `_dash_common.py` | `uv run python -c "import copilot.agent.agent"` 干净 import（有 lru_cache fallback，不需要 DB） |
| 0.3 | orchestration 包 + v1_loop shim：`src/copilot/orchestration/v1_loop.py` 仅 re-export `copilot.agent.agent.ask`，docstring 声明「冻结，勿改 `copilot.agent`」。`src/copilot/tools/__init__.py` 空占位（Phase 1 填） | `from copilot.orchestration.v1_loop import ask` 成功 |
| 0.4 | LangGraph stub：`orchestration/graph/{__init__,state,build,runner}.py`。`state.py` = `AgentState` 类型骨架；`build.py` = 单节点 `StateGraph`，节点体 `raise NotImplementedError("Phase 2")`；`runner.py` = `run(question, thread_id=None)` 包装 | `uv run python -c "from copilot.orchestration.graph.build import build_graph; build_graph()"` 能编译出图对象 |
| 0.5 | 离线测试基线：`uv run pytest` 全跑一遍，分类哪些无 DB 可过、哪些需 DB/网络（**不改 v1 测试文件**，只记录）。`ruff check` 新代码 | 绿色子集清单记入本文件；新代码 ruff 干净 |
| 0.6 | A/B 骨架：`src/copilot/eval/ab_compare.py`。`compare(eval_set_path, limit)`：逐题跑 v1 `ask()` 与 graph `run()`（`NotImplementedError` → 标 `graph="STUB"`；无 `OPENAI_API_KEY` → v1 侧也标 `SKIP`，只验管道）。收集 `{q, *_answer, *_citations, *_refused, *_latency_ms, *_tokens}`，diff，写 `data/results/ab_<ts>.{json,md}` | `uv run python -m copilot.eval.ab_compare --limit 2` 跑通 |
| 0.7 | 文档：更新 `eval-history.md` 占位行、devlog README（Current State + 里程碑表），补本文件「决策 / 学到什么」。**提交等用户确认** | devlog 一致 |

### 0b — 需凭据

| # | 任务 | 产出 / 验证 |
|---|------|-------------|
| 0.8 | 起 DB + 灌种子：`createdb` + `CREATE EXTENSION vector` + `uv run python -m copilot.pipeline.seed --load` + `uv run python -m copilot.pipeline.embed_chunks`（首次下 bge-small ~130MB） | `query_financials("AAPL","Revenue",2024)` 返回数值 |
| 0.9 | v1_loop baseline：跑 `harness`（`eval_set.json`）+ `harness_tier3` + `harness_router` + probes。结果存 `data/results/`，分数/延迟/token 抄进 `docs/eval-history.md`（impl=`v1_loop`） | eval-history 有真实基线；本里程碑 → `done` |

## 进度

**0a 离线 — 全部完成**（2026-09-03）

- 0.1 ✅ `git init`；`.gitignore`（+`reference/`）；`.env.example` 抄自 v1；`pyproject.toml`（setuptools，src 布局，py≥3.11，pytest markers `db`/`llm`）；`.python-version`=3.12。`uv sync --extra dev` 通过，venv = CPython 3.12.13。
- 0.2 ✅ v1 原样移植：`src/copilot/{agent,retrieval,storage,pipeline,eval,config.py,api.py,dashboard.py}` + `data/` + `tests/` + `.streamlit/` + `frontend.py`/`_dash_common.py`/`start.ps1`。import 路径零改动。`import copilot.agent.agent` 干净。
- 0.3 ✅ `orchestration/__init__.py` + `orchestration/v1_loop.py`（re-export `copilot.agent.agent.ask`）+ `tools/__init__.py`（Phase 1 占位）。
- 0.4 ✅ `orchestration/graph/{__init__,state,build,runner}.py`。`build_graph()` → `CompiledStateGraph`；stub 节点 `raise NotImplementedError`；`run()` 正确透传该异常。
- 0.5 ✅ `uv run pytest`：**120 passed / 8 failed**，8 个全部因 DB 未灌种（`relation "supply_edges" does not exist` / `companies` 表）→ 0.8 之后应转绿。清单：`test_constants_match_data`×3、`test_slots::test_the_word_purchasing...`×1（`slots.py` 读 `companies` 表，设计如此）、`test_tool_guards`×4。`ruff check` 新代码全绿。
- 0.6 ✅ `src/copilot/eval/ab_compare.py`：`--limit 2` 跑通，v1=`SKIP`（无 `.env` key）/ graph=`STUB`，产出 `data/results/ab_<ts>.{json,md}`（smoke 产物已删）。
- 0.7 ✅ 本文件 + README Current State 更新。**未提交，等确认。**

**0b 需凭据 — 待做**：需先 `cp .env.example .env` 填 `OPENAI_API_KEY` + `DATABASE_URL`。

## 决策

### D1. 冻结基线用「原样拷贝 `copilot.agent` + 薄 shim」，不做目录重命名
- **背景**：plan 原写 `orchestration/v1_loop/` 目录。真实 v1 的 `agent/` 有 10 个文件互相 import，`eval/` 也 import `copilot.agent`。
- **选择**：`src/copilot/agent/` 保持 v1 原样、零改动；`orchestration/v1_loop.py` 一行 re-export 提供干净命名。
- **否决的方案**：物理搬到 `orchestration/v1_loop/` 并 sed 重写所有 `copilot.agent.*` import —— 每次编辑都是行为偏离风险，且难以从上游 re-sync。
- **理由 / 代价**：基线的唯一价值是「行为和 v1 完全一致」。代价是命名上 `copilot.agent` 这个名字留给了旧代码；可接受。

### D2. `reference/` 加进 `.gitignore`，不纳入 v2 仓库历史
- **背景**：v1 是完整克隆，自带 `.git`。
- **选择**：删其 `.git`，`.gitignore` 掉整个 `reference/`，仅作本地参照。需要的文件显式拷进 `src/`。
- **否决**：作为 git submodule / vendor 进历史 —— 徒增体积与耦合，我们只需要它的少数文件。

### D3. LangGraph 栈 pin 精确版本；venv 用 Python 3.12
- **背景**：系统 Python 是 3.14（sentence-transformers/torch wheel 覆盖不稳）。LangGraph 已到 1.x（`langgraph 1.2.11` / `langchain-core 1.6.1` / `langchain-openai 1.6.0` / `langgraph-checkpoint-postgres 3.1.2` / `langsmith 0.12.1`）。
- **选择**：`.python-version`=3.12（uv 自动下载托管版本）；上述 5 个包 pin `==`。`langchain-core` 初始误 pin `1.4.0`，与 `langchain-openai 1.6.0`（要求 `>=1.6.0`）冲突 → 改 `1.6.1`。
- **理由**：plan 明确「pin 精确版本，LangGraph API 变化快」；升级走单独分支跑全量 eval。

## 学到什么（框架机制）

- **LangGraph 1.x 最小图**：`from langgraph.graph import START, END, StateGraph`；`StateGraph(AgentState)` → `add_node(name, fn)` / `add_edge(START, name)` / `add_edge(name, END)` → `.compile(checkpointer=None)` 得 `CompiledStateGraph`，`.invoke(dict)` 执行。节点签名 `fn(state) -> partial_state_dict`。
- **State schema**：`TypedDict`，字段用 `Annotated[T, reducer]` 声明合并语义。`messages` 用官方 `add_messages`（`langgraph.graph.message`）按 id append/merge；自定义 reducer（如 append 列表）就是个 `(left, right) -> merged` 函数。`total=False` 允许节点只返回部分字段。
- **`from __future__ import annotations` + 自定义 reducer**：reducer 函数要在模块内先定义再被 `Annotated[...]` 引用（LangGraph compile 时经 `get_type_hints` 解析），否则 NameError。
- 对照 v1：v1 的 `messages` list / `steps` / `round` 计数器 / 返回 dict，在 LangGraph 里全部收敛成一个带 reducer 的 `AgentState`。

## Eval / 指标

<0.9 完成后填，并同步 docs/eval-history.md。>

| 指标 | 值 | 对比 baseline |
|------|-----|--------------|
| Tier 1 | | (= v1 自身) |
| Tier 2 | | |
| Tier 3 | | |
| p95 延迟 (s) | | |
| 平均 token / 问 | | |

## 死胡同 / 坑

- `langchain-core==1.4.0` 与 `langchain-openai==1.6.0` 冲突（后者要 `>=1.6.0`）→ 改 `1.6.1`。教训：langchain 生态内部版本耦合紧，pin 时以最上层包（`langchain-openai`）的下界为准，或干脆只 pin 顶层让 uv 解 core。
- 8 个测试失败全是 DB 依赖，不是回归。`slots.py` 故意读 `companies` 表（v1 设计：不写死公司名单）。灌种子后重跑确认转绿。
- 系统 Python 3.14，torch/sentence-transformers wheel 未必齐 → venv 强制 3.12。

## 下一步 / 解锁了什么

- 0a 完成 → 可开 Phase 1（工具层重构），Phase 1 不依赖 DB 也能起草设计
- 0b 完成 → A/B 有真实 v1 侧数字，Phase 2 对齐才有意义
