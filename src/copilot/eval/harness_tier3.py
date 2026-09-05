"""
Tier-3 eval harness: supply-chain graph reasoning questions.

Scoring types:
  graph_lookup    : graph_query returns all expected edges
  graph_fact      : numeric revenue_pct from graph within tolerance
  graph_trend     : all expected trend edges present across all years
  graph_comparison: LLM judge verifies correct company/ranking identified
  graph_compute   : numeric result (dollar impact) within tolerance
  unanswerable    : agent refuses as expected

Ablation flag:
  --no-graph : strips graph_query from TOOL_SCHEMAS before running.
               Agent falls back to pure text retrieval → baseline naive-RAG score.
               Compare with/without to quantify graph layer contribution.

Usage:
    python -m copilot.eval.harness_tier3
    python -m copilot.eval.harness_tier3 --no-graph --out data/results/eval_results_t3_baseline.json
    python -m copilot.eval.harness_tier3 --out data/results/eval_results_t3_graph.json
"""

import argparse
import json
import time
from pathlib import Path

from openai import OpenAI

from copilot.eval.harness import (
    _extract_number,
    _is_refusal,
    _rates_for,
    _within_tolerance,
)

# ── graph traversal helpers ───────────────────────────────────────────────────

def _collect_graph_edges(steps: list[dict]) -> list[dict]:
    """Return all edges returned by any graph_query call in agent steps."""
    edges = []
    for step in steps:
        if step.get("tool") == "graph_query":
            for e in step.get("output", {}).get("edges", []):
                edges.append(e)
    return edges


def _check_traversal_trace(steps: list[dict], expected_edges: list[dict]) -> tuple[bool, list]:
    """
    Verify graph_query returned all expected edges.
    Returns (all_found: bool, missing: list of unfound expected edges).
    """
    returned = _collect_graph_edges(steps)
    missing = []
    for exp in expected_edges:
        found = any(
            e.get("supplier") == exp["supplier"]
            and e.get("customer") == exp["customer"]
            and e.get("fiscal_year") == exp["fiscal_year"]
            and abs((e.get("revenue_pct") or 0) - exp["revenue_pct"]) <= 0.5
            for e in returned
        )
        if not found:
            missing.append(exp)
    return len(missing) == 0, missing


def _graph_query_called(steps: list[dict]) -> bool:
    return any(s.get("tool") == "graph_query" for s in steps)


def _financials_called(steps: list[dict]) -> bool:
    return any(s.get("tool") == "query_financials" for s in steps)


# ── LLM judge (graph-specific) ────────────────────────────────────────────────

_judge_client: OpenAI | None = None


def _llm_judge_graph(question: str, scoring_notes: str, agent_answer: str) -> dict:
    """
    LLM judge for graph_comparison and graph_compute ranking questions.
    Uses scoring_notes as the reference instead of a golden text answer.
    Returns {"score": 0–3, "reason": str}
    """
    global _judge_client
    from copilot.config import settings
    if not settings.openai_api_key:
        return {"score": -1, "reason": "no API key"}
    if _judge_client is None:
        _judge_client = OpenAI(api_key=settings.openai_api_key)

    prompt = f"""You are evaluating a financial research assistant's answer about supply-chain data.

Question: "{question}"

Correct answer criteria:
{scoring_notes}

Agent's answer:
{agent_answer}

Score 0–3:
3 = Fully correct — identifies the right company/ranking, cites the correct percentages, no fabrications
2 = Mostly correct — right conclusion, minor omission (e.g. missing one caveat or citation)
1 = Partially correct — right direction but wrong company, wrong ranking, or significant omission
0 = Incorrect — wrong answer, fabricated numbers, or refused when it should have answered

Respond with ONLY valid JSON:
{{"score": <0|1|2|3>, "reason": "<one concise sentence>"}}"""

    resp = _judge_client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(resp.choices[0].message.content.strip())
    except Exception:
        return {"score": -1, "reason": "parse error"}


# ── tool trace ────────────────────────────────────────────────────────────────

def _build_tool_trace_t3(steps: list[dict]) -> list[str]:
    """Tool trace with dedicated graph_query formatting."""
    trace = []
    for step in steps:
        tool = step.get("tool", "?")
        inp  = step.get("input", {})
        out  = step.get("output", {})

        if tool == "graph_query":
            parts = []
            if inp.get("customer"): parts.append(f"customer={inp['customer']}")
            if inp.get("supplier"): parts.append(f"supplier={inp['supplier']}")
            parts.append(f"fiscal_year={inp.get('fiscal_year', 'latest')}")
            if inp.get("depth", 1) > 1: parts.append(f"depth={inp['depth']}")

            n = out.get("edge_count", 0)
            found = out.get("found", False)
            status = f"{n} edges" if found else "NOT FOUND"
            trace.append(f"graph_query({', '.join(parts)}) → {status}")
            for t in out.get("traversal_trace", []):
                trace.append(f"  edge: {t}")

        elif tool == "query_financials":
            ticker = inp.get("ticker", "?")
            metric = inp.get("metric", "?")
            fy     = inp.get("fiscal_year", "latest")
            if out.get("found"):
                trace.append(f"query_financials({ticker}, {metric}, FY{fy}) → {out.get('value')}")
            else:
                trace.append(f"query_financials({ticker}, {metric}, FY{fy}) → NOT FOUND")

        elif tool == "compute":
            trace.append(f"compute({inp.get('expression', '?')}) → {out.get('result')}")

        elif tool == "retrieve_text":
            results = out.get("results", [])
            trace.append(
                f"retrieve_text('{inp.get('query', '?')}') → {len(results)} chunks"
            )

        else:
            trace.append(f"{tool}({inp}) → {str(out)[:120]}")

    return trace


# ── item scorer ───────────────────────────────────────────────────────────────

def score_item_t3(item: dict, agent_result: dict) -> dict:
    answer_text = agent_result.get("answer", "")
    steps       = agent_result.get("steps", [])
    q_type      = item.get("type", "")

    base = {
        "id":         item["id"],
        "tier":       item["tier"],
        "type":       q_type,
        "answerable": item.get("answerable", True),
    }

    # ── unanswerable ─────────────────────────────────────────────────────────
    if not item.get("answerable", True):
        correct = _is_refusal(answer_text)
        return {**base, "correct": correct, "refusal_detected": correct,
                # Stored whole, not truncated. A 200-character excerpt cuts an
                # accession in half, so a result file could not be re-scored
                # offline -- re-running the grounding check over saved traces read
                # "0000320193-24-0001" as a dollar figure and flagged a correct
                # answer. An artefact you cannot re-examine without paying for the
                # run again is a weak artefact.
                "got_text": answer_text}

    # ── graph_lookup / graph_trend ────────────────────────────────────────────
    # Both check that all expected edges appear in graph_query output.
    if q_type in ("graph_lookup", "graph_trend"):
        expected = item.get("expected_edges", [])
        all_found, missing = _check_traversal_trace(steps, expected)
        graph_called = _graph_query_called(steps)
        correct = all_found and graph_called
        return {
            **base,
            "correct":       correct,
            "graph_called":  graph_called,
            "edges_found":   all_found,
            "missing_edges": missing,
            "got_text":      answer_text,
        }

    # ── graph_fact ────────────────────────────────────────────────────────────
    # Single revenue_pct value — numeric check against expected_value.
    if q_type == "graph_fact":
        expected = item.get("expected_edges", [])
        all_found, missing = _check_traversal_trace(steps, expected)
        got_raw = _extract_number(answer_text)
        correct = (
            all_found
            and got_raw is not None
            and _within_tolerance(got_raw, item["expected_value"], item["tolerance_pct"])
        )
        return {
            **base,
            "correct":      correct,
            "edges_found":  all_found,
            "expected":     item["expected_value"],
            "got_raw":      got_raw,
            "got_text":     answer_text,
        }

    # ── graph_comparison ──────────────────────────────────────────────────────
    # LLM judge checks if agent identified the correct company.
    if q_type == "graph_comparison":
        graph_called = _graph_query_called(steps)
        judge = _llm_judge_graph(
            item["question"],
            item.get("scoring_notes", ""),
            answer_text,
        )
        judge_score = judge.get("score", -1)
        correct = graph_called and judge_score >= 2
        return {
            **base,
            "correct":       correct,
            "graph_called":  graph_called,
            "judge_score":   judge_score,
            "judge_reason":  judge.get("reason", ""),
            "got_text":      answer_text,
        }

    # ── graph_compute ─────────────────────────────────────────────────────────
    # Dollar-impact calculation — requires graph_query + query_financials + compute.
    # Sub-type: numeric (single expected_value) or llm_judge (ranking, expected_values dict).
    if q_type == "graph_compute":
        graph_called      = _graph_query_called(steps)
        financials_called = _financials_called(steps)

        scoring_mode = item.get("scoring", "numeric")

        if scoring_mode == "llm_judge":
            judge = _llm_judge_graph(
                item["question"],
                item.get("scoring_notes", ""),
                answer_text,
            )
            judge_score = judge.get("score", -1)
            correct = graph_called and financials_called and judge_score >= 2
            return {
                **base,
                "correct":           correct,
                "graph_called":      graph_called,
                "financials_called": financials_called,
                "judge_score":       judge_score,
                "judge_reason":      judge.get("reason", ""),
                "got_text":          answer_text,
            }

        # numeric mode
        got_raw = None
        for step in reversed(steps):
            if step.get("tool") == "compute":
                raw = step.get("output", {}).get("result")
                if raw is not None:
                    try:
                        got_raw = float(raw)
                        break
                    except (TypeError, ValueError):
                        pass
        if got_raw is None:
            got_raw = _extract_number(answer_text)

        correct = (
            got_raw is not None
            and graph_called
            and financials_called
            and _within_tolerance(got_raw, item["expected_value"], item["tolerance_pct"])
        )
        return {
            **base,
            "correct":           correct,
            "graph_called":      graph_called,
            "financials_called": financials_called,
            "expected":          item["expected_value"],
            "got_raw":           got_raw,
            "got_text":          answer_text,
        }

    # fallback
    return {**base, "correct": False, "got_text": answer_text,
            "error": f"unknown type: {q_type}"}


# ── ablation: disable graph_query ─────────────────────────────────────────────

def _strip_graph_tool() -> list:
    """Remove graph_query from agent TOOL_SCHEMAS. Returns original list."""
    import copilot.agent.agent as _agent
    original = _agent.TOOL_SCHEMAS[:]
    _agent.TOOL_SCHEMAS = [
        s for s in _agent.TOOL_SCHEMAS
        if s["function"]["name"] != "graph_query"
    ]
    return original


def _restore_graph_tool(original: list) -> None:
    import copilot.agent.agent as _agent
    _agent.TOOL_SCHEMAS = original


# ── main harness ──────────────────────────────────────────────────────────────

def run_eval_t3(dataset_path: Path, no_graph: bool = False,
                limit: int | None = None, agent_model: str | None = None) -> dict:
    from copilot.agent.agent import ask

    with open(dataset_path) as f:
        data = json.load(f)

    items = data["items"]
    if limit:
        items = items[:limit]

    original_schemas = None
    if no_graph:
        original_schemas = _strip_graph_tool()
        print(">> BASELINE MODE: graph_query tool disabled\n")

    results = []
    total_input  = 0
    total_output = 0
    total_latency = 0.0

    mode = "baseline (no-graph)" if no_graph else "graph-augmented"
    print(f"Tier-3 eval — {mode} — {len(items)} questions\n")

    try:
        for idx, item in enumerate(items, 1):
            print(f"[{idx:02d}/{len(items)}] {item['id']}")
            print(f"       Q: {item['question']}")

            t0 = time.time()
            try:
                result = ask(item["question"], model=agent_model)
            except Exception as e:
                result = {"answer": f"ERROR: {e}", "steps": [], "citations": [],
                          "usage": {}}
            elapsed = time.time() - t0
            total_latency += elapsed

            total_input  += result.get("usage", {}).get("input_tokens",  0)
            total_output += result.get("usage", {}).get("output_tokens", 0)

            scored              = score_item_t3(item, result)
            scored["latency_s"] = round(elapsed, 2)
            scored["tool_trace"] = _build_tool_trace_t3(result.get("steps", []))
            # Recorded, never scored -- see the same note in harness.py. Tier-3
            # answers state the most derived figures, so they are where an
            # untraceable number is most likely to hide.
            scored["verification"] = result.get("verification") or {}
            # Saved for the same reason the full answer is: without the trace, a
            # result file cannot be re-scored offline, and re-running the grounding
            # check against an empty trace reports every figure as unsourced.
            scored["steps"] = result.get("steps") or []
            results.append(scored)

            status = "PASS" if scored["correct"] else "FAIL"
            q_type = item.get("type", "")

            if not item.get("answerable", True):
                print(f"       {status}  refusal={'yes' if scored.get('refusal_detected') else 'NO'}  ({elapsed:.1f}s)")
            elif q_type in ("graph_lookup", "graph_trend"):
                missing = scored.get("missing_edges", [])
                miss_str = f"  missing={[e['supplier'] for e in missing]}" if missing else ""
                print(f"       {status}  graph_called={scored.get('graph_called')}  edges={'ok' if scored.get('edges_found') else 'MISS'}{miss_str}  ({elapsed:.1f}s)")
            elif q_type == "graph_fact":
                print(f"       {status}  expected={item['expected_value']}  got={scored.get('got_raw')}  ({elapsed:.1f}s)")
            elif q_type == "graph_comparison":
                print(f"       {status}  graph_called={scored.get('graph_called')}  judge={scored.get('judge_score')}/3  ({elapsed:.1f}s)")
            elif q_type == "graph_compute":
                if item.get("scoring") == "llm_judge":
                    print(f"       {status}  graph_called={scored.get('graph_called')}  financials={scored.get('financials_called')}  judge={scored.get('judge_score')}/3  ({elapsed:.1f}s)")
                else:
                    print(f"       {status}  expected={item['expected_value']:,.0f}  got={scored.get('got_raw')}  "
                          f"graph={scored.get('graph_called')}  financials={scored.get('financials_called')}  ({elapsed:.1f}s)")

            for line in scored["tool_trace"]:
                print(f"         > {line}")
            print()

    finally:
        if original_schemas is not None:
            _restore_graph_tool(original_schemas)

    # ── aggregate ─────────────────────────────────────────────────────────────

    def _acc(lst: list) -> float | None:
        return round(sum(r["correct"] for r in lst) / len(lst) * 100, 1) if lst else None

    by_type = lambda t: [r for r in results if r.get("type") == t]

    unans       = [r for r in results if not r["answerable"]]

    # Priced at the rates of the model that actually answered, not at a constant.
    # A run on gpt-4o costed at gpt-4o-mini rates understates by 16x, which is the
    # whole quantity a model comparison exists to measure.
    (_rate_in, _rate_out), _cost_basis = _rates_for(agent_model)
    cost_usd = total_input / 1e6 * _rate_in + total_output / 1e6 * _rate_out

    _flagged = [r for r in results
                if r.get("verification") and not r["verification"].get("verified", True)]
    _unsourced = [r["id"] for r in results
                  if (r.get("verification") or {}).get("unsourced_formulas")]
    _checked = sum(r.get("verification", {}).get("numbers_checked", 0) for r in results)

    summary = {
        "dataset":              str(dataset_path),
        "dataset_version":      data.get("version"),
        "mode":                 mode,
        "n_total":              len(results),
        "agent_model":          agent_model or "default (gpt-4o-mini)",
        "cost_basis":           _cost_basis,
        "graph_lookup_accuracy":     _acc(by_type("graph_lookup")),
        "graph_fact_accuracy":       _acc(by_type("graph_fact")),
        "graph_trend_accuracy":      _acc(by_type("graph_trend")),
        "graph_comparison_accuracy": _acc(by_type("graph_comparison")),
        "graph_compute_accuracy":    _acc(by_type("graph_compute")),
        "refusal_accuracy":          _acc(unans),
        "overall_accuracy":          _acc(results),
        "unverified_answers":        len(_flagged),
        "unsourced_formula_ids":     _unsourced,
        "unverified_ids":            [r["id"] for r in _flagged],
        "figures_checked":           _checked,
        "avg_latency_s":    round(total_latency / len(results), 2) if results else 0,
        "total_latency_s":  round(total_latency, 1),
        "input_tokens":     total_input,
        "output_tokens":    total_output,
        "estimated_cost_usd": round(cost_usd, 4),
        "results":          results,
    }

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset",  default="data/datasets/eval_set_tier3.json")
    parser.add_argument("--no-graph", action="store_true",
                        help="Disable graph_query (baseline naive-RAG ablation)")
    parser.add_argument("--limit",    type=int, default=None)
    parser.add_argument("--out",      default=None, help="Save results JSON")
    parser.add_argument("--model",    default=None,
                        help="Model id for the agent (default: agent's own default). "
                             "The judge stays on gpt-4o-mini so a model comparison "
                             "does not also vary the grader.")
    args = parser.parse_args()

    summary = run_eval_t3(
        Path(args.dataset),
        no_graph=args.no_graph,
        limit=args.limit,
        agent_model=args.model,
    )

    print("=" * 60)
    print(f"TIER-3 EVAL SUMMARY  [{summary['mode'].upper()}]")
    print("=" * 60)
    print(f"  Total questions       : {summary['n_total']}")
    print("  ── Graph scoring ───────────────────────")
    print(f"  graph_lookup          : {summary['graph_lookup_accuracy']}%")
    print(f"  graph_fact            : {summary['graph_fact_accuracy']}%")
    print(f"  graph_trend           : {summary['graph_trend_accuracy']}%")
    print(f"  graph_comparison      : {summary['graph_comparison_accuracy']}%")
    print(f"  graph_compute         : {summary['graph_compute_accuracy']}%")
    print("  ── Other ───────────────────────────────")
    print(f"  Refusal accuracy      : {summary['refusal_accuracy']}%")
    print(f"  Overall accuracy      : {summary['overall_accuracy']}%")
    print(f"  Grounding check       : {summary['figures_checked']} figures checked, "
          f"{summary['unverified_answers']} answer(s) flagged"
          + (f" -> {', '.join(summary['unverified_ids'])}" if summary["unverified_ids"] else ""))
    print("  ── Cost & Latency ──────────────────────")
    print(f"  Avg latency           : {summary['avg_latency_s']}s / question")
    print(f"  Estimated cost        : ${summary['estimated_cost_usd']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
