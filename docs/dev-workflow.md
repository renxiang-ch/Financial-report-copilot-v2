# 开发工作流

> 这个项目是学习 / 研究项目，不是生产软件工程——不上 spec 文件、gate 脚本、hook、CI。
> 工作流就是六步循环，落在现有的 devlog / migration-plan / eval harness 上，尽量轻。
>
> 目的：少重复开发、少开发错误、少需求不匹配、遇到问题能快速迭代。

```
   GOAL  →  INSPECT  →  PLAN  →  BUILD  →  EVAL  →  RECORD
   要解决/    现状/      改什么+   小步改    功能+     结果+
   学什么     复用/影响   怎么验              回归+成本  Learning
                                              │
                                    不过 ─────┘ 回 BUILD
                                    (或发现 PLAN/GOAL 错 → 回上游)
```

---

## 0. 先定这次多大（right-sizing）

| 档 | 什么样 | 跑几步 |
|---|---|---|
| **微改** | 一句话能描述的 diff（typo / 加日志 / 改常量 / 重命名） | GOAL(一句) → BUILD → EVAL(跑相关检查+贴证据) → RECORD(devlog 记一行)。跳过 INSPECT / PLAN |
| **小任务** | 单个文件 / 一个小功能，半天内 | 全六步，轻量，不单独开 devlog 文件（追加到当前里程碑日志） |
| **里程碑（Phase）** | 跨多文件 / 新系统 | 全六步：INSPECT 用只读子 agent，PLAN 用 plan mode，EVAL 跑全量回归，RECORD 出独立 devlog 文件 + eval-history 行 + STOP |

---

## 1. 六步

### ① GOAL — 要解决 / 学什么

- **目的**：说清楚要什么，避免解决错问题。
- **做什么**：一段话写清 —— 要的结果 + 为什么（学框架的哪个点 / 能力提升哪块）+ "完成的样子"是什么。里程碑级直接从 `langgraph-migration-plan.md` 对应 Phase 抄目标 + 退出标准。
- **产出**：devlog 文件的 `GOAL` 段（2–4 行）。

### ② INSPECT — 现状 / 复用 / 影响

- **目的**：防重复开发 + 防踩到别的东西。AI 不会自己停下来查"这个函数是不是已经存在"。
- **做什么**：
  - **复用**：这个功能 v1（`reference/v1/`）或现有 `src/copilot/` 做过没有？有没有能直接 import 的 helper？（例：eval 打分那次复用了 `harness.py` 的 `_within_tolerance` / `_is_refusal` / `_llm_judge`，没重写）
  - **影响**：改动牵动什么？**别碰 `src/copilot/agent/`**（v1 冻结基线）。改共享代码前想清楚谁依赖它。
  - 里程碑级：用 Explore 只读子 agent 做，摘要回来，不占主上下文。
- **产出**：三行 —— "能复用 X / 新写 Y / 会影响 Z"。
- **可跳过**：纯新增、明显没有可复用的、不碰共享代码。

### ③ PLAN — 改什么 + 怎么验

- **目的**：对齐"改什么"，并且**在动手前**定好"怎么证明改对了"。
- **做什么**：
  - **改什么**：逐文件（里程碑级用 plan mode，写完你审一遍再往下）。
  - **怎么验**：这次的 EVAL 具体跑什么 —— 哪个 `pytest`、要不要新写检查、跑哪段 eval、`ab_compare` 对不对 v1、要不要记 token / 延迟。写进 PLAN，不是做完才想。
- **产出**：devlog 的 `PLAN` 段 —— 文件清单 + 验证清单。

### ④ BUILD — 小步改

- **目的**：小步、可回退、复用不重造。
- **做什么**：按 PLAN 的清单一步步来，diff 保持小。用到的现有 schema / 类似代码拉进上下文，照着改不新造。同一个问题纠正超过两次 → `/clear` 重开，带上学到的东西重写 prompt。
- **产出**：代码 + `uv run ruff check` 干净。

### ⑤ EVAL — 功能 + 回归 + 成本

- **目的**：三个维度都过才算完。
- **做什么**：
  - **功能**：PLAN 里定的验证跑一遍，**贴命令和输出**，不说"应该好了"。
  - **回归**：`uv run pytest -q` 还是绿的；相关 eval Tier 不掉；里程碑级 `ab_compare` 对 v1 端到端不劣化。
  - **成本**：token / 延迟 / tool 调用数有没有异常涨（`ab_compare` / `run_generic_baseline` 已经记这些）。
  - **可选强化**：拿不准的改动，上换模型审查 —— `git diff` 喂给 `codex exec -m gpt-5.6-sol -s read-only`，让它对着 GOAL 逐条查、只报正确性 / 需求 gap。不是每次都要。
- **产出**：devlog 的 `EVAL` 段 —— 三维度证据；数字进 `eval-history.md`。
- **不过怎么办**：回 ④ BUILD。如果发现是 PLAN 漏了 / GOAL 本身错了 → 回上游改，别在 BUILD 里硬凑。

### ⑥ RECORD — 结果 + Learning

- **目的**：下次不重复踩坑 + 沉淀 learning（这是项目目标 #1）。
- **做什么**：
  - **决策**：选了什么 / 否决了什么 / 为什么。
  - **死胡同**：试过没用的。
  - **Learning**：LangGraph / LangChain 的某机制实际怎么工作、和 v1 自建的差异、意外点 —— 这段是目标 #1 的产出，最后汇编成 handbook 章节。
  - 更新 `devlog/README.md` 的 Current State + 里程碑表；数字进 `eval-history.md`。
  - commit message 引用 devlog 文件（如 `refs docs/devlog/003-...`）。
  - **里程碑结束：STOP**，等你确认再开下一个 Phase。
- **产出**：devlog 文件 `status: done` + README 同步 + 一次 commit。

---

## 1.5 每步的操作细节（runbook）

### 一次会话长什么样

- **微改**：prompt 里直接说目标 → 我改 → 我跑 `uv run ruff check` + 相关 `pytest` 贴输出 → 你点头 → 我 commit + 当前里程碑 devlog 加一行。1 个来回。
- **小任务**：说目标 → 我 INSPECT（grep 现有代码）报三行 → 我给 PLAN（文件 + 怎么验）→ 你 "go" → BUILD → EVAL 贴证据 → RECORD 追加到当前里程碑 devlog + commit。
- **里程碑**：`cp docs/devlog/000-template.md docs/devlog/NNN-slug.md`，逐段填。PLAN 进 plan mode 让你审。EVAL 全量回归。RECORD 独立文件 + eval-history + STOP。

### 启动一个任务

你只要说目标，例如：
> 小任务：给 generic baseline 补 LLM judge 分数。按 dev-workflow 做。

我会自动走六步（memory 里记了这套工作流）。

### ① GOAL — 你说 / 我复述确认
- 模糊的话我用 `AskUserQuestion` 访谈你再落笔。
- 里程碑：从 `langgraph-migration-plan.md` 对应 Phase 抄"目标 + 退出标准"进 devlog `GOAL` 段。

### ② INSPECT — 我执行，报三行
- 复用：`grep -rn "<关键词>" src/copilot/ reference/v1/src/copilot/`，找能 import 的 helper。
- 里程碑级：用只读 `Explore` 子 agent 查"X 现在怎么做的 / Y 有没有 helper / 改 Z 会影响谁"，摘要回主上下文。
- 影响：`grep -rn "from copilot.<模块> import\|copilot\.<模块>\." src/` 找谁 import 了要改的东西。
- 硬检查：路径不在 `src/copilot/agent/`（冻结）。

### ③ PLAN — 我起草，你审
- 里程碑：`Shift+Tab` 进 plan mode，我出"逐文件改动 + 验证清单"，你 `Ctrl+G` 改后 approve。
- 小任务：我在 chat 给短 plan + 验证清单，你说 go。
- 验证清单从这里选（写进 PLAN，不是做完才想）：

  | 目的 | 命令 |
  |---|---|
  | 回归（必跑） | `uv run pytest -q` |
  | 针对性功能 | `uv run pytest tests/test_<x>.py -q` |
  | lint | `uv run ruff check <files>` |
  | 编排改动 | 跑 eval harness（参数见 `src/copilot/eval/harness.py::main`）或某 tier 子集 |
  | graph vs v1 | `uv run python -m copilot.v2.eval.ab_compare --limit N` |
  | 通用 agent 基线 | `uv run python scripts/run_generic_baseline.py --limit N` |
  | 成本 | 看上面几个 summary 里的 token / 延迟字段，对比 `eval-history.md` 上一行 |
  | 覆盖不到的验收标准 | 现写一个一次性检查脚本 |

### ④ BUILD — 我改
- 按 PLAN 清单从上往下，一次一个文件 / 一个逻辑改动。
- 每改完一块：`uv run ruff check <file>` + `git diff` 自己扫一眼。
- 同一个问题纠正两次不成 → 我 `/clear`，带上学到的重开。

### ⑤ EVAL — 我跑，贴命令和输出
- 逐条跑 PLAN 的验证清单，**每条贴命令 + 实际输出**，不说"应该好了"。
- 三维度：功能（目标定的检查过没过）/ 回归（`pytest` 还是 `N passed`、相关 eval Tier 没掉、`ab_compare` 对 v1 不劣化）/ 成本（token·延迟·tool 调用数对比上一行）。
- 拿不准的改动，换模型审：
  ```bash
  git diff > /tmp/d.txt
  codex exec -m gpt-5.6-sol -s read-only --json -o /tmp/r.txt \
    "审 /tmp/d.txt，对照目标：<一句话>。只报正确性 / 需求 gap。"
  cat /tmp/r.txt
  ```
- 不过 → 回 ④ BUILD；发现是 PLAN 漏了 / GOAL 错了 → 回上游改。

### ⑥ RECORD — 我写，你审，我 commit
- 填 devlog `RECORD` 段：决策（D1/D2）、死胡同、Learning（框架机制那段）。
- frontmatter：`status: done`、`finished: <日期>`。
- 更新 `devlog/README.md`：覆盖 Current State、更新里程碑表行。
- 数字变了 → 追加 `eval-history.md` 一行。
- `git add <具体文件>` + `git commit`，message 末尾 `refs docs/devlog/NNN-slug.md` + `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`。
- 里程碑：STOP，报完等你说"下一个"。
- Retro（一行，可选）：这次 EVAL 抓到 BUILD 漏的，值不值得改 workflow / memory。

---

## 2. 和现有东西的对应

| 步 | 落在哪 |
|---|---|
| GOAL | devlog 文件 `目标` 段；里程碑级抄 `langgraph-migration-plan.md` |
| INSPECT | 无专门文件，动作落在 `reference/v1/` + `src/copilot/` 搜索；里程碑级 Explore 子 agent |
| PLAN | devlog 文件 `PLAN` 段；里程碑级 plan mode |
| BUILD | 代码 + `ruff` |
| EVAL | `pytest` + `src/copilot/eval/harness*`（v1 移植）+ `src/copilot/v2/eval/{ab_compare,generic_scoring}` + `eval-history.md` |
| RECORD | devlog `决策 / 死胡同 / Learning` + `README` Current State + commit |

**唯一要改的一处**：`docs/devlog/000-template.md` 的段落对齐成这六步（见下），这样"跑工作流"和"写记录"就是同一件事。

---

## 3. 以后再长出来的（retro 驱动，现在不做）

第 ⑥ 步顺带做个小 retro：这次 EVAL 抓到的、BUILD 漏的、返工的，反馈进工作流本身。触发条件到了才加：

- INSPECT 老是漏掉可复用的 → 在 memory 里加一条 "可复用 helper 清单"
- EVAL 的回归检查老是手动跑同一串命令 → 做成 `scripts/gate.py`
- "换模型审查"每次都有用 → 从"可选"提成 EVAL 的固定一步
- 某段流程反复手动重复 → 做成一个 `.claude/skills/` skill

别一次性全搭，用得着了再加。
