"""Run the generic-agent baseline: Codex CLI (``codex exec``) over the raw
10-K filings in ``baseline/raw_filings/``, no domain tooling, scored with the
same tolerance bands / refusal detector as v1_loop.

Each question gets a fresh ``codex exec`` process -- pinned model, read-only
sandbox, web search left off, working root pinned to ``baseline/raw_filings/``
so the process never sees v2's code or prompts. No memory carries between
questions. See docs/generic-agent-baseline.md.

Usage::

    uv run python scripts/run_generic_baseline.py                    # 1 run, all 38 items
    uv run python scripts/run_generic_baseline.py --runs 3           # 3 runs, for variance
    uv run python scripts/run_generic_baseline.py --limit 3          # smoke test
    uv run python scripts/run_generic_baseline.py --model gpt-5-codex
    uv run python scripts/run_generic_baseline.py --dry-run          # list items only
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_FILINGS_DIR = REPO_ROOT / "baseline" / "raw_filings"
RESULTS_DIR = REPO_ROOT / "data" / "results"
RAW_LOG_DIR = RESULTS_DIR / "generic_agent_raw"

sys.path.insert(0, str(REPO_ROOT / "src"))
from copilot.v2.eval.generic_scoring import score_item  # noqa: E402

DEFAULT_MODEL = "gpt-5.6-sol"
CODEX_TIMEOUT_S = 300

PROMPT_TEMPLATE = (
    "You are a financial analyst. This directory contains SEC 10-K filings for "
    "several companies, organized in subfolders by ticker.\n\n"
    "Question: {question}\n\n"
    "Answer clearly and directly. If you consult a filing, name which file you used."
)

_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
               "output_tokens", "reasoning_output_tokens")


def _load_items(limit: int | None) -> list[dict]:
    items: list[dict] = []
    for fname, dataset in [("eval_set.json", "eval_set"),
                            ("eval_set_tier3.json", "eval_set_tier3")]:
        data = json.loads((REPO_ROOT / "data" / "datasets" / fname).read_text())
        for it in data["items"]:
            if it.get("retired"):
                continue
            items.append({**it, "_dataset": dataset})
    if limit:
        items = items[:limit]
    return items


def _run_codex(item_id: str, question: str, model: str, run_idx: int) -> dict:
    prompt = PROMPT_TEMPLATE.format(question=question)
    answer_file = RAW_LOG_DIR / f"r{run_idx}_{item_id}_answer.txt"

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            ["codex", "exec",
             "-m", model,
             "-C", str(RAW_FILINGS_DIR),
             "-s", "read-only",
             "--json",
             "-o", str(answer_file),
             prompt],
            capture_output=True, text=True, timeout=CODEX_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "answer": "", "latency_s": CODEX_TIMEOUT_S,
                "usage": {}, "tool_calls": None}

    latency_s = round(time.perf_counter() - t0, 1)
    (RAW_LOG_DIR / f"r{run_idx}_{item_id}_events.jsonl").write_text(proc.stdout)

    if proc.returncode != 0:
        return {"status": "ERROR", "answer": "", "latency_s": latency_s,
                "usage": {}, "tool_calls": None, "reason": proc.stderr[-2000:]}

    usage: dict = {}
    tool_calls = 0
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn.completed":
            usage = ev.get("usage", {})
        elif ev.get("type") == "item.started":
            if (ev.get("item") or {}).get("type") == "command_execution":
                tool_calls += 1

    answer = answer_file.read_text().strip() if answer_file.exists() else ""
    return {"status": "OK", "answer": answer, "latency_s": latency_s,
            "usage": usage, "tool_calls": tool_calls}


def _run_once(items: list[dict], model: str, run_idx: int) -> list[dict]:
    rows = []
    for i, item in enumerate(items, 1):
        r = _run_codex(item["id"], item["question"], model, run_idx)
        scored = (score_item(item, r["answer"]) if r["status"] == "OK"
                  else {"id": item.get("id"), "tier": item.get("tier"),
                        "correct": False, "type": "unscored"})
        rows.append({"run": run_idx, "dataset": item["_dataset"], **r, **scored})
        verdict = ("OK" if scored.get("correct") is True
                   else "??" if scored.get("correct") is None else "--")
        print(f"  [run {run_idx}] [{i}/{len(items)}] {verdict} {item.get('id')} "
              f"({r['status']}, {r['latency_s']}s, {r.get('tool_calls')} calls)")
    return rows


def _summarize(all_rows: list[dict], n_runs: int) -> dict:
    ok = [r for r in all_rows if r["status"] == "OK"]

    by_tier: dict[str, dict] = {}
    for r in all_rows:
        tier = f"tier{r.get('tier')}" if r.get("tier") else "no_tier"
        d = by_tier.setdefault(tier, {"n": 0, "correct": 0, "needs_review": 0})
        d["n"] += 1
        if r.get("correct") is True:
            d["correct"] += 1
        elif r.get("needs_review") or r.get("correct") is None:
            d["needs_review"] += 1

    unans = [r for r in all_rows if r.get("type") == "unanswerable"]
    unans_by_reason: dict[str, dict] = {}
    for r in unans:
        d = unans_by_reason.setdefault(r.get("unanswerable_reason", "?"),
                                       {"n": 0, "refused": 0})
        d["n"] += 1
        d["refused"] += int(bool(r.get("refusal_detected")))

    latencies = sorted(r["latency_s"] for r in ok)
    tok = {k: sum((r.get("usage") or {}).get(k, 0) or 0 for r in ok) for k in _USAGE_KEYS}
    tool_calls = [r["tool_calls"] for r in ok if r.get("tool_calls") is not None]

    def pct(p: float) -> float | None:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else None

    return {
        "n_runs": n_runs,
        "n_rows": len(all_rows),
        "status_counts": _counter(r["status"] for r in all_rows),
        "by_tier": by_tier,
        "accuracy_excl_pending": {
            t: (round(d["correct"] / (d["n"] - d["needs_review"]), 3)
                if (d["n"] - d["needs_review"]) else None)
            for t, d in by_tier.items()
        },
        "unanswerable_by_reason": unans_by_reason,
        "latency_s": {"p50": pct(0.50), "p95": pct(0.95),
                      "max": max(latencies) if latencies else None,
                      "mean": round(statistics.mean(latencies), 1) if latencies else None},
        "tokens_total": tok,
        "tokens_mean_per_q": {k: round(v / len(ok)) for k, v in tok.items()} if ok else {},
        "tool_calls": {"mean": round(statistics.mean(tool_calls), 1) if tool_calls else None,
                       "max": max(tool_calls) if tool_calls else None},
    }


def _counter(it) -> dict:
    from collections import Counter
    return dict(Counter(it))


def _consistency(all_rows: list[dict], n_runs: int) -> list[dict]:
    """Per-item: how often did it come out `correct` across runs?"""
    if n_runs < 2:
        return []
    by_id: dict[str, list] = {}
    for r in all_rows:
        by_id.setdefault(r["id"], []).append(r.get("correct"))
    out = []
    for iid, verdicts in by_id.items():
        trues = sum(1 for v in verdicts if v is True)
        out.append({"id": iid, "correct_runs": trues, "n": len(verdicts),
                    "flaky": 0 < trues < len(verdicts)})
    return sorted(out, key=lambda x: (not x["flaky"], x["id"]))


def _markdown(report: dict) -> str:
    s = report["summary"]
    unans = ", ".join(f"{k} {v['refused']}/{v['n']}"
                      for k, v in s["unanswerable_by_reason"].items())
    out = [
        f"# Generic-agent baseline — {report['generated_at']}",
        "",
        f"- model: `{report['model']}`  ·  runs: {report['n_runs']}"
        f"  ·  items/run: {report['n_items']}",
        f"- status: {s['status_counts']}",
        f"- accuracy (excl. pending): {s['accuracy_excl_pending']}",
        f"- unanswerable by reason (refused/n): {unans}",
        f"- latency s: {s['latency_s']}",
        f"- tokens/q (mean): {s['tokens_mean_per_q']}",
        f"- tool calls: {s['tool_calls']}",
    ]
    if report.get("consistency"):
        flaky = [c["id"] for c in report["consistency"] if c["flaky"]]
        out.append(f"- flaky across runs ({len(flaky)}): {', '.join(flaky) or 'none'}")
    out += ["", "| run | id | tier | status | correct | latency(s) | tool calls |",
            "|-----|----|------|--------|---------|------------|------------|"]
    for r in report["rows"]:
        c = {True: "✓", False: "✗", None: "?"}.get(r.get("correct"), "?")
        out.append(f"| {r['run']} | {r['id']} | {r.get('tier','')} | {r['status']} "
                   f"| {c} | {r['latency_s']} | {r.get('tool_calls','')} |")
    return "\n".join(out) + "\n"


def run(model: str, runs: int, limit: int | None, dry_run: bool) -> dict | None:
    items = _load_items(limit)
    print(f"{len(items)} items x {runs} run(s) via `codex exec -m {model}`")
    if dry_run:
        for it in items:
            print(f"  {it['_dataset']:>16}  {it['id']:<32} {it['question'][:70]}")
        return None

    RAW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    for run_idx in range(1, runs + 1):
        all_rows += _run_once(items, model, run_idx)

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": ts, "model": model, "n_runs": runs, "n_items": len(items),
        "summary": _summarize(all_rows, runs),
        "consistency": _consistency(all_rows, runs),
        "rows": all_rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"generic_agent_baseline_{ts}"
    (RESULTS_DIR / f"{stem}.json").write_text(json.dumps(report, indent=2))
    (RESULTS_DIR / f"{stem}.md").write_text(_markdown(report))
    print(f"\nwrote data/results/{stem}.{{json,md}}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    report = run(args.model, args.runs, args.limit, args.dry_run)
    if report:
        print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
