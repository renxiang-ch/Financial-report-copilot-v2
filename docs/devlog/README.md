# Devlog — 开发记录索引

> 本文件是**实时状态**的唯一真源。工作流在 [`../dev-workflow.md`](../dev-workflow.md)，路线图在 [`../langgraph-migration-plan.md`](../langgraph-migration-plan.md)，分数历史在 [`../eval-history.md`](../eval-history.md)。
>
> 规则：
> - 开发按 [`../dev-workflow.md`](../dev-workflow.md) 的六步：GOAL → INSPECT → PLAN → BUILD → EVAL → RECORD。
> - 一个里程碑一个日志文件 `NNN-slug.md`，用 [`000-template.md`](000-template.md)（段落 = 六步）。
> - 小于半天的活不单独开文件，作为 bullet 追加到当前 active 里程碑的日志。
> - 日志记 git 记不下的东西：**决策 / 否决方案 / Learning / 死胡同 / 数字**。不复述 diff。
> - 完成一个里程碑：改该日志 `status: done` + 更新下面「里程碑表」+ 追加 `eval-history.md` + 覆盖「Current State」+ STOP 等确认。同一次提交里做完。

---

## Current State

> 每次开发结束覆盖这一块。

- **当前里程碑**：**Phase 0 完成**（0a=001, 0b=003）；**Phase 2 完成**（004，agent-loop → `create_agent` + 6 步 middleware）；**Phase 1 完成**（005，工具层标准库）；track-1（通用 Agent 基线）完成（002 补记 2/3）
- **上次停止点**：Phase 0b 做完 —— DB 灌全量 v1 快照 + embed 16342 chunks；`pytest` **143/0**（8 个 DB 失败转绿）；**v1_loop baseline 三 harness 全 100%**（Tier1/2/3、retrieval、refusal、router 全 100%，grounding 0 flagged），精确复现 v1 发布结果，成本 $0.055。三方对比第一版见 003 Learning。第一个 commit `e4f5d88` 已推到 `github.com/renxiang-ch/Financial-report-copilot-v2`；Phase 0b 的改动**未 commit**
- **2026-09-06**：用户学完 `create_agent` 文档，开始 agent loop 端口（devlog 004）。**Step 1 完成**：`orchestration/graph/` 用 `create_agent` + 5 个最小 `@tool` 包装 + v1 SYSTEM prompt + `InMemorySaver` 重建，runner 返回 v1 形状 dict。`ab_compare` eval_set.json（30 题）与 v1_loop **citations/refusal 30/30 匹配、0 语义分歧、延迟/步数一致**。加了 `langchain==1.4.0` 到 deps。pytest 143/0
- **2026-09-07**：v1/v2 代码分开 —— v2 新写的全部移进 `src/copilot/v2/`（`orchestration/` `tools/` `eval/{ab_compare,generic_scoring}`）。规则：`copilot.v2.*` = 新，其它 = v1/共享。`ab_compare` 的 `_REPO_ROOT` 深度改 `parents[4]`。pytest 143/0，`ab_compare` 正常
- **2026-09-07**：devlog 004 **Step 3 完成** —— 路由 middleware（`@before_model` refuse 短路 + `@wrap_model_call` force_tool）
- **2026-09-07**：devlog 004 **Step 4-6 完成，Phase 2（agent-loop 端口）收尾**。`middleware.py` 重构：模块级纯 helper + `agent_middleware()` 返回 4 hook（`_trim` 历史裁剪 / `_guard` refuse+clarify 短路 / `_active_context` 假设块注入 / `_force_first_tool`）。多轮走 `thread_id`+checkpointer，`carry_from_messages` 折叠回放的历史问题，**不加 `state_schema=`**（slot 是问题序列纯函数）。踩坑：`_before_first_model_call` 要 turn-scoped（"最后一条 Human 之后无 AIMessage"），否则 checkpointer 回放让 t2+ 所有 hook 失效。`ab_compare._refused()` 改用 v1 冻结的 `looks_like_refusal`。加 `compare_multiturn`（v1 喂 history / graph 喂 thread_id）。**5 eval 集 A/B 67 组：refusal 0 不匹配、0 行为回归**；eval_set 30/30+30/30，router 7/12cit+12/12ref，multiturn 10-11/11cit+11/11ref，tier3 6/8+8/8，defects 5/6+6/6（citation 集差异全部是 `retrieve_text` 广度 / `graph_query` 非确定性，非回归，数处 graph 更紧）。`pytest` 149/0（+6）
- **2026-09-07**：Phase 1（工具层）定案，见 plan「Phase 1」表 + [devlog 005](005-tool-layer-rebuild.md)。2 轮 MCP 核对 API。改用预制 `ToolRetryMiddleware`/`ToolErrorMiddleware`/`ToolCallLimitMiddleware`（不手写 `wrap_tool_call`）；resolve 结果走 `state_schema` 不是 `context`（context 只读）。加 `.mcp.json`
- **2026-09-08**：**Phase 1 完成**（devlog 005）。新增 `copilot/v2/tools/{base,resolve,schemas,financials,retrieval,graph,compute,registry}.py` —— `content_and_artifact` 信封 + Pydantic `args_schema`（`metric` enum+validator）+ `ToolError` + `resolve` 层。`graph/`：`GraphState` 加 `resolved`、`_resolve` `@before_model` hook、3 个预制 tool middleware、`runner` 读 `ToolMessage.artifact`、删 `graph/tools.py`。踩坑：`retrieve_text` 第一版把段落塞 artifact → 模型读不到 → eval_set 退化到 24/30；`content` 改带段落全文后恢复。**A/B 5 集**：refusal 66/67、0 回归；**tier3 引用 6→8、multiturn 引用变满（年份 scope 修复达标）**；input token/问 ~4.4K（Phase 2 ~5.5–7.8K）。`pytest` 167/0（+18）
- **下一步动作**：Phase 3（持久化 PostgresSaver / 错误恢复 —— `ToolRetryMiddleware` 已在，加 repair 节点 + `.with_fallbacks` / HITL `interrupt()` / LangSmith 可观测）
- **阻塞项**：无

---

## 里程碑表

| ID | 标题 | 状态 | 日志 | 关键结果 |
|----|------|------|------|----------|
| 001 | Phase 0a — 脚手架与护栏 | done | [001](001-phase0-scaffolding.md) | v1 移植冻结 + LangGraph stub + ab_compare 骨架 + pytest 120/8 |
| 003 | Phase 0b — 灌库 + v1_loop baseline | done | [003](003-phase0b-v1loop-baseline.md) | DB 全量 + embed；v1_loop 三 harness 全 100%；pytest 143/0 |
| 004 | Phase 2 — Agent loop 端口（→ `create_agent` + 6 步 middleware） | done | [004](004-agentloop-port.md) | 5 eval 集 A/B 67 组：refusal 0 不匹配、0 行为回归；多轮年份继承生效；pytest 149/0 |
| 005 | Phase 1 — 工具层标准化重构（→ `copilot/v2/tools/`） | done | [005](005-tool-layer-rebuild.md) | `content_and_artifact` + Pydantic schema + `ToolError` + 预制 retry/error/limit middleware + resolve 层 + `state_schema.resolved`。A/B 5 集 refusal 66/67、0 回归、tier3 引用 6→8 |
| — | Phase 3 — 框架能力成熟化（持久化 / 错误恢复 / HITL / 可观测） | planned | — | — |
| — | Phase 4 — 能力升级：长难题（指引兑现 / 管理层风格 / 增长模式） | planned | — | — |
| — | Phase 5 — 产品化（API / 部署 / 回归闸门） | planned | — | — |

状态取值：`planned` / `active` / `done` / `blocked`

### 并行轨道（不占 Phase 序号）

| ID | 标题 | 状态 | 日志 |
|----|------|------|------|
| track-1 | 通用 Agent 基线（Codex CLI vs v1_loop vs graph） | done（首次跑分；9 题待 judge 复核） | [002](002-generic-agent-baseline-run.md) |

---

## 文件索引

### 文档
| 路径 | 说明 |
|------|------|
| `docs/dev-workflow.md` | 开发工作流：GOAL→INSPECT→PLAN→BUILD→EVAL→RECORD 六步循环，right-sizing 三档 |
| `docs/langgraph-migration-plan.md` | 路线图，Phase 级，稳定。改动需谨慎 |
| `docs/generic-agent-baseline.md` | 第三条基线：零定制通用 agent + 原始 10-K 文档，导师建议，独立于 Phase 0-5 的并行评测轨道 |
| `docs/devlog/README.md` | 本文件，实时状态 + 里程碑表 + 文件索引 |
| `docs/devlog/000-template.md` | 日志模板 |
| `docs/devlog/NNN-*.md` | 各里程碑日志 |
| `docs/eval-history.md` | 可追加的 Eval 分数 / 延迟 / 成本历史 |

### 代码 —— `copilot.v2.*` = 新写的，其它全是 v1/共享（2026-09-07 拆分）
| 路径 | 类别 | 说明 |
|------|------|------|
| `src/copilot/agent/` `api.py` `dashboard.py` | **v1 冻结** | v1 原样拷贝，只读，行为真源 |
| `src/copilot/{config,storage,retrieval,pipeline}` | **共享** | v1 拷来的基础设施，v2 import 使用不重写 |
| `src/copilot/eval/{harness*,probe*}.py` | **v1 移植** | Tier 1-3 / router / probe 评测 |
| `src/copilot/v2/orchestration/v1_loop.py` | v2 | 一行 shim，re-export `copilot.agent.agent.ask` |
| `src/copilot/v2/orchestration/graph/` | v2 | LangChain 重制版：`build.py`（`create_agent` + `GraphState` + middleware 栈）/ `middleware.py`（5 hook：trim / resolve / refuse+clarify / active-context / force-tool + `on_tool_error`）/ `runner.py`（驱动+重建 v1 形状，读 `ToolMessage.artifact`）|
| `src/copilot/v2/tools/` | v2 | Phase 1 工具层标准库：`base`（`ToolError`/`pack`/`financial_tool`）/ `resolve`（ticker·年份·relation-side）/ `schemas`（Pydantic）/ `financials`·`retrieval`·`graph`·`compute` / `registry`（`TOOLS`）。领域逻辑逐字搬自 `copilot.agent.tools` |
| `.mcp.json` | 配置 | 两个 LangChain 文档 MCP（`docs-langchain` / `reference-langchain`），项目级 |
| `src/copilot/v2/eval/ab_compare.py` | v2 | v1_loop vs graph 端到端 A/B（`python -m copilot.v2.eval.ab_compare`）|
| `src/copilot/v2/eval/generic_scoring.py` | v2 | 通用 agent 基线打分器，复用 `harness.py` 判分函数 |
| `scripts/fetch_raw_filings.py` | v2 | 抓 58 份真实 10-K 到 `baseline/raw_filings/`（gitignore） |
| `scripts/run_generic_baseline.py` | 逐题起 `codex exec` 跑通用 agent 基线 |
| `scripts/rescore_generic_baseline.py` | 用已保存的答案文本离线重新打分，不用重跑 codex |

### 外部参考
| 资源 | 说明 |
|------|------|
| `renxiang-ch/Financial-Report-Research-Copilot` | v1 源仓库 |
| `renxiang-ch/Financial-copilot-handbook` | v1 配套讲解，pin 在 `v1.0-teaching` |
