---
id: NNN
phase: <0-5 或 track>
title: <里程碑标题>
started: YYYY-MM-DD
finished: -
status: active   # active / done / blocked
plan_ref: ../langgraph-migration-plan.md#<章节>
---

<!-- 段落对齐 docs/dev-workflow.md 的六步。微改/小任务不必开独立文件。 -->

## GOAL

<2–4 行。要解决 / 学什么 + 为什么（学框架哪个点 / 能力提升哪块）+ "完成的样子"。里程碑级抄 plan 对应章节的目标 + 退出标准。>

## INSPECT

<现状 / 复用 / 影响，三行：>

- **能复用**：<v1 或现有 src/copilot 里已有的，能 import 的 helper>
- **新写**：<确实要新建的>
- **会影响**：<改动牵动什么；确认不碰 src/copilot/agent/>

## PLAN

- **改什么**：<逐文件清单>
- **怎么验**：<这次 EVAL 具体跑什么：哪个 pytest / 新检查 / 哪段 eval / ab_compare / 记不记 token·延迟>

## BUILD

<简洁列表，细节交给 git diff。>

- 

## EVAL

<三维度，贴命令和输出，不写"应该好了"。数字同步到 docs/eval-history.md。>

- **功能**：<PLAN 里定的验证，实际输出>
- **回归**：<pytest 绿 / 相关 eval Tier 不掉 / ab_compare 对 v1 不劣化>
- **成本**：<token / 延迟 / tool 调用数有无异常>

| 指标 | 值 | 对比 baseline |
|------|-----|--------------|
| Tier 1 / 2 / 3 / 4 | | |
| p95 延迟 (s) | | |
| 平均 token / 问 | | |

## RECORD

### 决策

### D1. <决策标题>
- **背景** / **选择** / **否决的方案** / **理由·代价**

### 死胡同 / 坑

<失败的尝试、放弃的路径。省未来最多时间。>

- 

### Learning（框架机制）

<服务目标 1：LangGraph / LangChain 的某机制实际怎么工作、和 v1 自建的差异、意外点。汇编成 handbook。>

- 

### Retro（可选）

<这次 EVAL 抓到 / BUILD 漏 / 返工的，要不要反馈进 dev-workflow.md 或 memory。>

## 下一步 / 解锁了什么

- 
