"""
Tool-selection eval harness: measures whether the agent reaches for the
correct tool, not whether its final answer is numerically correct.

Runs data/datasets/eval_set_router.json — a 12-question mini-benchmark isolating three
tool-displacement failure patterns (see docs/case-study-tool-router.md):
  dependency        : supplier-on-customer revenue concentration -> graph_query
  qualitative       : risk-factor / business-description narrative -> retrieve_text
  procurement_share : customer-side spend share (structurally unanswerable) -> zero tool calls, refuse

Ablation flag:
  --no-router : disables copilot.agent.agent.ROUTER_ENABLED before running,
                so every question falls through to the original "auto"
                tool-choice behavior. Compare with/without to quantify the
                router's contribution — same pattern as harness_tier3.py's
                --no-graph flag.

Usage:
    python -m copilot.eval.harness_router --out data/results/eval_results_router_after.json
    python -m copilot.eval.harness_router --no-router --out data/results/eval_results_router_before.json
"""

import argparse
import json
import time
from pathlib import Path

from copilot.eval.harness import _is_refusal, _rates_for

_EXPECTED_TOOL = {
    "graph_query":    "graph_query",
    "retrieve_text":  "retrieve_text",
}


def _tool_selection_correct(expected: str, steps: list[dict]) -> bool:
    if expected == "refuse_no_tool":
        return len(steps) == 0
    return any(s.get("tool") == _EXPECTED_TOOL[expected] for s in steps)


def score_item_router(item: dict, agent_result: dict) -> dict:
    steps       = agent_result.get("steps", [])
    answer_text = agent_result.get("answer", "")
    expected    = item["expected_tool_category"]

    tool_ok = _tool_selection_correct(expected, steps)

    scored = {
        "id":                    item["id"],
        "category":              item["category"],
        "expected_tool_category": expected,
        "tools_called":          [s.get("tool") for s in steps],
        "n_tool_calls":          len(steps),
        "tool_selection_correct": tool_ok,
    }

    # Extra diagnostic for the procurement_share category: separate "did it
    # skip the wasted tool call" from "did it still land on a correct refusal
    # in the final text" — these are two different things the router improves.
    if expected == "refuse_no_tool":
        scored["refused_correctly"] = _is_refusal(answer_text)
        scored["correct"] = tool_ok and scored["refused_correctly"]
    else:
        scored["correct"] = tool_ok

    return scored


def run_eval_router(dataset_path: Path, no_router: bool = False,
                    limit: int | None = None) -> dict:
    import copilot.agent.agent as _agent
    from copilot.agent.agent import ask

    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]
    if limit:
        items = items[:limit]

    original_router_enabled = _agent.ROUTER_ENABLED
    _agent.ROUTER_ENABLED = not no_router
    mode = "baseline (router disabled)" if no_router else "router-enabled"
    print(f"Tool-selection eval — {mode} — {len(items)} questions\n")

    results = []
    total_input  = 0
    total_output = 0
    total_latency = 0.0

    try:
        for idx, item in enumerate(items, 1):
            print(f"[{idx:02d}/{len(items)}] {item['id']}  ({item['category']})")
            print(f"       Q: {item['question']}")

            t0 = time.time()
            try:
                result = ask(item["question"])
            except Exception as e:
                result = {"answer": f"ERROR: {e}", "steps": [], "citations": [], "usage": {}}
            elapsed = time.time() - t0
            total_latency += elapsed

            total_input  += result.get("usage", {}).get("input_tokens",  0)
            total_output += result.get("usage", {}).get("output_tokens", 0)

            scored = score_item_router(item, result)
            scored["latency_s"] = round(elapsed, 2)
            scored["route"] = result.get("route")
            results.append(scored)

            status = "PASS" if scored["correct"] else "FAIL"
            print(f"       {status}  expected={scored['expected_tool_category']}  "
                  f"called={scored['tools_called']}  ({elapsed:.2f}s)")
            print()
    finally:
        _agent.ROUTER_ENABLED = original_router_enabled

    def _acc(lst: list) -> float | None:
        return round(sum(r["correct"] for r in lst) / len(lst) * 100, 1) if lst else None

    by_cat = lambda c: [r for r in results if r["category"] == c]

    cost_usd = (
        total_input  / 1e6 * _rates_for(None)[0][0] +
        total_output / 1e6 * _rates_for(None)[0][1]
    )

    summary = {
        "dataset":         str(dataset_path),
        "dataset_version": data.get("version"),
        "mode":            mode,
        "n_total":         len(results),
        "tool_selection_accuracy":          _acc(results),
        "dependency_accuracy":              _acc(by_cat("dependency")),
        "qualitative_accuracy":             _acc(by_cat("qualitative")),
        "procurement_share_accuracy":       _acc(by_cat("procurement_share")),
        "avg_tool_calls_procurement_share": round(
            sum(r["n_tool_calls"] for r in by_cat("procurement_share")) / len(by_cat("procurement_share")), 2
        ) if by_cat("procurement_share") else None,
        "avg_latency_s":     round(total_latency / len(results), 2) if results else 0,
        "total_latency_s":   round(total_latency, 2),
        "input_tokens":      total_input,
        "output_tokens":     total_output,
        "estimated_cost_usd": round(cost_usd, 5),
        "results":           results,
    }

    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset",   default="data/datasets/eval_set_router.json")
    parser.add_argument("--no-router", action="store_true",
                        help="Disable the router (baseline: original auto tool-choice behavior)")
    parser.add_argument("--limit",     type=int, default=None)
    parser.add_argument("--out",       default=None)
    args = parser.parse_args()

    summary = run_eval_router(Path(args.dataset), no_router=args.no_router, limit=args.limit)

    print("=" * 60)
    print(f"TOOL-SELECTION EVAL SUMMARY  [{summary['mode'].upper()}]")
    print("=" * 60)
    print(f"  Total questions              : {summary['n_total']}")
    print(f"  Overall tool-selection acc.  : {summary['tool_selection_accuracy']}%")
    print("  ── By category ─────────────────────────")
    print(f"  dependency  (-> graph_query) : {summary['dependency_accuracy']}%")
    print(f"  qualitative (-> retrieve_text): {summary['qualitative_accuracy']}%")
    print(f"  procurement_share (-> refuse): {summary['procurement_share_accuracy']}%")
    print(f"  avg tool calls (procurement) : {summary['avg_tool_calls_procurement_share']}")
    print("  ── Cost & Latency ──────────────────────")
    print(f"  Avg latency                  : {summary['avg_latency_s']}s / question")
    print(f"  Estimated cost                : ${summary['estimated_cost_usd']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
