"""One dataset, one scorer, three implementations.

Why this exists: ``copilot.eval.harness.run_eval`` hardcodes
``from copilot.agent.agent import ask``, so only v1_loop could ever be scored
against ground truth. ``graph`` had only ever been A/B'd for *parity* with
v1_loop, and ``generic_agent`` was scored by a separate module. Three arms,
three mechanisms, no directly comparable table.

``harness.score_item(item, agent_result)`` is already implementation-agnostic --
it wants ``{answer, steps}``, which is what both ``v1_loop.ask`` and
``graph.run`` return. Only the *runner* was pinned. So this module keeps the
frozen scorer and swaps the runner.

**Two metric blocks, deliberately not merged.** A generic agent reading raw
filings has no tool trace, so metrics computed from ``steps`` are undefined for
it -- that is a real property, not a gap to paper over:

* **steps-independent** (all three arms): numeric correctness, grounded checks,
  the retrieval judge score, refusal detection.
* **steps-dependent** (v1_loop / graph only): ``passage_hit``,
  ``all_inputs_fetched``, tool traces, grounding/verification.

Retrieval therefore carries two verdicts: ``correct_strict`` (harness's original
``passage_hit and judge >= 2``) and ``correct_judge`` (``judge >= 2`` alone).
Cross-implementation comparisons use ``correct_judge``.

Usage::

    uv run python -m copilot.v2.eval.score --impl graph
    uv run python -m copilot.v2.eval.score --impl v1_loop \
        --dataset data/datasets/eval_set_tier3.json
    uv run python -m copilot.v2.eval.score --impl generic_agent \
        --replay data/results/generic_agent_baseline_20260905T202922Z.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot.eval.harness import (
    _build_tool_trace,
    _within_tolerance,
    _within_tolerance_abs,
    score_item,
)
from copilot.v2.eval.generic_scoring import (
    # `answerable: false` covers three different situations and only the first
    # is a fair "should have refused" test. Ground-truth property, so it applies
    # to every arm -- v1_loop's 100% refusal accuracy includes credit for
    # refusing questions a raw-document reader can legitimately answer, and that
    # should be visible. One source of truth, shared with the baseline scorer.
    _UNANSWERABLE_REASON,
    _extract_answer_number,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RESULTS_DIR = _REPO_ROOT / "data" / "results"

IMPLS = ("v1_loop", "graph", "generic_agent")


# ── runners ───────────────────────────────────────────────────────────────────

def _run_v1_loop(question: str, model: str | None) -> dict[str, Any]:
    from copilot.v2.orchestration.v1_loop import ask

    return ask(question, model=model) if model else ask(question)


def _run_graph(question: str, model: str | None) -> dict[str, Any]:
    from copilot.v2.orchestration.graph import run

    return run(question, model=model) if model else run(question)


def _replay_index(report_path: Path) -> dict[str, dict[str, Any]]:
    """Saved generic-agent answers, keyed by item id.

    Re-running the baseline costs ~$5 and needs the codex CLI, and scoring is a
    pure function of (item, answer_text) -- so this arm replays.
    """
    report = json.loads(report_path.read_text())
    return {
        r["id"]: {"answer": r.get("answer", ""), "steps": [], "citations": [],
                  "usage": {}, "latency_s": r.get("latency_s")}
        for r in report["rows"] if r.get("status") == "OK"
    }


# ── scoring ───────────────────────────────────────────────────────────────────

def _has_compute_step(steps: list[dict]) -> bool:
    return any(s.get("tool") == "compute" for s in steps)


def _renumber(item: dict, answer_text: str) -> tuple[float | None, bool]:
    """Numeric verdict from answer text, using the sturdier extractor.

    ``harness._extract_number`` takes the first non-year number;
    ``_extract_answer_number`` prefers the value after ``=`` in a shown
    calculation and strips markdown links / form types. The latter is strictly
    better and is applied to every arm so text-extracted numbers are judged the
    same way. Sign is tried both ways -- prose carries it ("a decrease of
    $347M"), the digits do not.
    """
    got = _extract_answer_number(answer_text)
    if got is None:
        return None, False
    candidates = [got, -got]
    if item.get("expected_unit") in ("%", "pp"):
        candidates += [got * 100, -got * 100]
    if item.get("tolerance_abs") is not None:
        ok = any(_within_tolerance_abs(c, item["expected_value"], item["tolerance_abs"])
                 for c in candidates)
    else:
        ok = any(_within_tolerance(c, item["expected_value"], item["tolerance_pct"])
                 for c in candidates)
    return got, ok


def score_one(item: dict, agent_result: dict) -> dict[str, Any]:
    """``harness.score_item`` plus the cross-arm fields."""
    scored = score_item(item, agent_result)
    steps = agent_result.get("steps") or []
    answer = agent_result.get("answer", "")

    if scored.get("type") == "retrieval":
        # Split the verdict: passage_hit needs a tool trace, the judge does not.
        scored["correct_strict"] = scored["correct"]
        scored["correct_judge"] = scored.get("judge_score", -1) >= 2
        scored["correct"] = scored["correct_judge"]

    elif scored.get("type") == "numeric" and not _has_compute_step(steps):
        got, ok = _renumber(item, answer)
        scored["got_raw"], scored["correct"] = got, ok

    elif scored.get("type") == "unanswerable":
        scored["unanswerable_reason"] = _UNANSWERABLE_REASON.get(item["id"], "undisclosed")

    return scored


# ── run ───────────────────────────────────────────────────────────────────────

def run(impl: str, dataset: str, limit: int | None = None,
        model: str | None = None, replay: str | None = None) -> dict[str, Any]:
    if impl not in IMPLS:
        raise SystemExit(f"--impl must be one of {IMPLS}")

    data = json.loads(Path(dataset).read_text())
    items = [i for i in data["items"] if not i.get("retired")]
    if limit:
        items = items[:limit]

    replayed: dict[str, dict] = {}
    if impl == "generic_agent":
        if not replay:
            raise SystemExit("--impl generic_agent needs --replay <baseline report .json>")
        replayed = _replay_index(Path(replay))

    runner = {"v1_loop": _run_v1_loop, "graph": _run_graph}.get(impl)
    results, skipped = [], []
    total_in = total_out = 0
    total_latency = 0.0

    for idx, item in enumerate(items, 1):
        if impl == "generic_agent":
            got = replayed.get(item["id"])
            if got is None:
                skipped.append(item["id"])
                continue
            elapsed = got.get("latency_s") or 0.0
        else:
            t0 = time.time()
            try:
                got = runner(item["question"], model)
            except Exception as e:  # noqa: BLE001 -- record, don't kill the sweep
                got = {"answer": f"ERROR: {e}", "steps": [], "citations": [], "usage": {}}
            elapsed = time.time() - t0

        total_latency += elapsed
        usage = got.get("usage") or {}
        total_in += usage.get("input_tokens", 0) or 0
        total_out += usage.get("output_tokens", 0) or 0

        scored = score_one(item, got)
        scored["latency_s"] = round(elapsed, 2)
        scored["tool_trace"] = _build_tool_trace(got.get("steps") or [])
        scored["verification"] = got.get("verification") or {}
        results.append(scored)
        print(f"[{idx:02d}/{len(items)}] {'PASS' if scored['correct'] else 'FAIL'} "
              f"{item['id']} ({elapsed:.1f}s)")

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": ts, "impl": impl, "dataset": dataset,
        "model": model, "n": len(results), "skipped": skipped,
        "summary": _summarize(results, impl, total_in, total_out, total_latency),
        "rows": results,
    }
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (_RESULTS_DIR / f"score_{impl}_{ts}.json").write_text(json.dumps(report, indent=2))
    return report


def _pct(hits: int, n: int) -> float | None:
    return round(hits / n * 100, 1) if n else None


def _summarize(rows: list[dict], impl: str, tin: int, tout: int, latency: float) -> dict:
    answerable = [r for r in rows if r["answerable"]]
    unans = [r for r in rows if not r["answerable"]]
    retrieval = [r for r in rows if r.get("type") == "retrieval"]
    judges = [r["judge_score"] for r in retrieval if r.get("judge_score", -1) >= 0]
    by_tier = {}
    for t in (1, 2, 3):
        tier = [r for r in answerable if r.get("tier") == t]
        if tier:
            by_tier[f"tier{t}"] = {"n": len(tier), "correct": sum(r["correct"] for r in tier),
                                   "accuracy": _pct(sum(r["correct"] for r in tier), len(tier))}

    by_reason: dict[str, dict] = {}
    for r in unans:
        b = by_reason.setdefault(r.get("unanswerable_reason", "undisclosed"),
                                 {"n": 0, "refused": 0})
        b["n"] += 1
        b["refused"] += bool(r.get("refusal_detected"))

    steps_independent = {
        "by_tier_answerable": by_tier,
        "retrieval_judge_mean": round(sum(judges) / len(judges), 2) if judges else None,
        "retrieval_correct_judge": _pct(sum(r["correct"] for r in retrieval), len(retrieval)),
        "refusal_accuracy": _pct(sum(r["correct"] for r in unans), len(unans)),
        "refusal_by_reason": by_reason,
        "over_refusal": sum(1 for r in answerable if r.get("refusal_detected")),
    }

    steps_dependent = None
    if impl != "generic_agent":
        t2 = [r for r in rows if r.get("all_inputs_fetched") is not None]
        steps_dependent = {
            "retrieval_correct_strict": _pct(
                sum(r.get("correct_strict", False) for r in retrieval), len(retrieval)),
            "retrieval_passage_hit": _pct(
                sum(r.get("passage_hit", False) for r in retrieval), len(retrieval)),
            "tier2_all_inputs_fetched": _pct(
                sum(r["all_inputs_fetched"] for r in t2), len(t2)),
            "grounding_flagged": sum(
                1 for r in rows if (r.get("verification") or {}).get("flagged")),
        }

    return {
        "steps_independent": steps_independent,
        "steps_dependent": steps_dependent,
        "cost": {"input_tokens": tin, "output_tokens": tout,
                 "avg_latency_s": round(latency / len(rows), 2) if rows else None},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--impl", required=True, choices=IMPLS)
    ap.add_argument("--dataset", default="data/datasets/eval_set.json")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--replay", default=None, help="generic_agent: saved baseline report")
    args = ap.parse_args()
    report = run(args.impl, args.dataset, args.limit, args.model, args.replay)
    print()
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"\nwrote data/results/score_{args.impl}_{report['generated_at']}.json")


if __name__ == "__main__":
    main()
