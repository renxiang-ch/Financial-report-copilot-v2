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

- **当前里程碑**：Phase 0 — 脚手架与护栏（0a 完成，0b 待做）；**并行轨道 track-1（通用 Agent 基线）本轮完成**（见 [002](002-generic-agent-baseline-run.md) 补记 2/3）
- **上次停止点**：通用 agent 基线 `gpt-5.6-sol` n=1 跑完 + 修完 scorer + judge 打分完成。**Tier1 0.85 (17/20) · Tier2 1.0 · Tier3 0.75 (6/8)**；两个模型（gpt-5.6-sol / 默认）用最终 scorer 逐题零差异。成本按 gpt-5.6-sol 计价 $4.96/全套、$0.131/题（vs v1_loop ~$0.001/题，≈117x）。scorer 迭代了 3 轮（数值抽取 / retrieval 去 verbatim-gate / judge 崩溃 guard），`tests/test_generic_scoring.py` 15 case。`.env` 现有真 `OPENAI_API_KEY`（`DATABASE_URL` 仍缺）。**仓库尚未 git commit**
- **下一步动作**：（a）Phase 0b：`.env` 补 `DATABASE_URL` → seed DB → 跑 harness 记 v1_loop baseline（最好也上 `gpt-5.6-sol` 出 `v1_loop@strong`，隔离模型/架构变量）。（b）确认是否 commit 当前所有改动（一大批未提交）
- **阻塞项**：无

---

## 里程碑表

| ID | 标题 | 状态 | 日志 | 关键结果 |
|----|------|------|------|----------|
| 001 | Phase 0 — 脚手架与护栏 | active | [001](001-phase0-scaffolding.md) | — |
| 002 | Phase 1 — 工具层标准化重构 | planned | — | — |
| 003 | Phase 2 — LangGraph 复刻 v1 循环 | planned | — | — |
| 004 | Phase 3 — 框架能力成熟化（持久化 / 错误恢复 / HITL / 可观测） | planned | — | — |
| 005 | Phase 4 — 能力升级：长难题（指引兑现 / 管理层风格 / 增长模式） | planned | — | — |
| 006 | Phase 5 — 产品化（API / 部署 / 回归闸门） | planned | — | — |

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

### 代码
| 路径 | 说明 |
|------|------|
| `src/copilot/agent/` | v1 原样拷贝，**冻结只读**，两套编排／基线共用的行为真源 |
| `src/copilot/orchestration/v1_loop.py` | 一行 shim，re-export `copilot.agent.agent.ask`，给冻结基线一个干净名字 |
| `src/copilot/orchestration/graph/` | LangGraph 新实现（Phase 0：stub） |
| `src/copilot/tools/` | Phase 1 标准化工具库占位，尚未开始 |
| `src/copilot/eval/ab_compare.py` | v1_loop vs graph 的端到端 A/B 脚本 |
| `src/copilot/eval/generic_scoring.py` | 通用 agent 基线的打分器，按题目 `type`/`scoring` 路由，复用 `harness.py`/`harness_tier3.py` 的判分函数 |
| `scripts/fetch_raw_filings.py` | 抓 58 份真实 10-K 到 `baseline/raw_filings/`（gitignore） |
| `scripts/run_generic_baseline.py` | 逐题起 `codex exec` 跑通用 agent 基线 |
| `scripts/rescore_generic_baseline.py` | 用已保存的答案文本离线重新打分，不用重跑 codex |

### 外部参考
| 资源 | 说明 |
|------|------|
| `renxiang-ch/Financial-Report-Research-Copilot` | v1 源仓库 |
| `renxiang-ch/Financial-copilot-handbook` | v1 配套讲解，pin 在 `v1.0-teaching` |
