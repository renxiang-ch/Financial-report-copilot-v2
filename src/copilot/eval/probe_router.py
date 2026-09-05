"""Deterministic probe for question routing. No LLM, no database, no cost.

Why this exists separately from `harness_router.py`
    That harness reports 100% tool-selection accuracy, and the number is not
    trustworthy: its twelve questions were written by reading the regex
    patterns, so every "procurement share" item contains one of the exact words
    the patterns match ("procurement budget", "sourcing", "supply chain
    spending", "purchasing"). A test drawn from an implementation measures
    whether the implementation is itself, which it always is.

    The cases below are drawn from the failure instead. Each one is a phrasing a
    person would plausibly use, chosen to sit near a decision boundary rather
    than in the middle of a pattern. The regex router scores 38.9% here against
    100% on that harness, and the gap between those two numbers is the
    measurement problem, not a regression.

What the categories mean
    procurement_share  Structurally undisclosed. 10-K concentration disclosures
                       are supplier-reported -- they state what share of the
                       SUPPLIER's revenue a named customer accounted for. No
                       filing states the customer's share of its own spend, so a
                       question asking for one must be refused.
    dependency         Answerable from supply_edges; force graph_query.
    default            Everything else; let the agent choose.

The asymmetry that matters
    A false `procurement_share` is a hard refusal delivered with zero tokens and
    no path for the model to disagree -- the user is told nothing exists when it
    does. A false `dependency` costs one wasted tool call. These are not
    equally bad, and a replacement router should be readier to fall back to
    `default` than to refuse.

Run:  uv run --active python -m copilot.eval.probe_router [--out FILE]
"""

import argparse
import json
from datetime import datetime, timezone

from copilot.agent.agent import route_question

# (question, expected category, note)
#
# `note` says what the case is FOR. A case with no reason to exist is a case
# nobody can tell is broken.
CASES: list[tuple[str, str, str]] = [
    # ── supplier-side dependency: answerable, graph_query ────────────────────
    ("How dependent is Cirrus Logic on Apple?",
     "dependency", "canonical phrasing; the pattern was written for this"),
    ("What percentage of Qorvo's revenue came from Apple in fiscal 2024?",
     "dependency", "canonical phrasing with an explicit year"),
    ("What share of Cirrus Logic's sales does Apple account for?",
     "dependency", "synonym stress: 'sales' where the pattern says 'revenue'"),
    ("How much of Skyworks' top line is Apple?",
     "dependency", "synonym stress: financial idiom"),
    ("Is Apple a significant customer of Corning?",
     "dependency", "asks the same fact as a yes/no rather than a share"),
    ("What percentage of Skyworks' revenue comes from purchasing agreements?",
     "default", "false-positive probe: supplier-side, but says 'purchasing'. "
                "Expected `dependency` when first written; revised to `default` "
                "because 'purchasing agreements' is not a counterparty and there "
                "is no principled reason to force graph_query. What this case "
                "tests is that it is not REFUSED, and `default` satisfies that "
                "while `procurement_share` -- what the regex returns -- does not."),
    ("How much of Jabil's revenue comes from its purchasing customers?",
     "dependency", "false-positive probe: supplier-side, but says 'purchasing'"),
    ("Which of Apple's suppliers is most at risk if procurement spending "
     "goes to competitors?",
     "dependency", "false-positive probe: supplier-side, but says 'procurement'"),

    # ── customer-side spend: structurally undisclosed, must refuse ───────────
    ("What percentage of Apple's procurement budget goes to Qorvo?",
     "procurement_share", "canonical phrasing; the pattern was written for this"),
    ("How much does Apple buy from Qorvo?",
     "procurement_share", "false-negative probe: plain verb, no pattern word"),
    ("What fraction of Apple's total supplier spend is Qorvo?",
     "procurement_share", "false-negative probe: 'supplier spend' unmatched"),
    ("Of everything Apple pays its component vendors, how much goes to Skyworks?",
     "procurement_share", "false-negative probe: paraphrased entirely"),
    ("What share of Apple's cost of goods sold is attributable to Cirrus Logic?",
     "procurement_share", "false-negative probe: dressed as an XBRL metric"),
    ("How much of Apple's component sourcing comes from Cirrus Logic?",
     "procurement_share", "ADDED after this probe missed it: the slot router "
     "let it through because 'sourcing' was only a prefix in the spend-noun "
     "pattern and never a head, so 'component sourcing' matched nothing. "
     "harness_router's twelve older questions caught it and these eighteen did "
     "not -- and it had passed that afternoon only because the model happened "
     "to refuse on its own, which is not the same as being routed correctly."),
    ("What share of Apple's procurement is with Broadcom?",
     "procurement_share", "the same gap with no qualifier in front of the noun"),

    # ── everything else: let the agent choose ────────────────────────────────
    ("Does Corning disclose where its raw material purchasing comes from?",
     "default", "false-positive probe: 'purchasing' but not a share question"),
    ("What was Apple's revenue in fiscal year 2024?",
     "default", "plain lookup; must not be captured by either pattern"),
    ("What risks does Skyworks disclose about customer concentration?",
     "default", "qualitative; belongs to retrieve_text, not graph_query"),
    ("How did Apple's gross margin change between 2023 and 2024?",
     "default", "multi-step numeric; no supply-chain involvement"),
    ("Which suppliers disclosed Apple as a customer in fiscal 2024?",
     "default", "set question -- graph_query is right, but forcing it is not "
                "required for correctness, so `default` is acceptable here"),
]


def run() -> dict:
    rows = []
    for question, expected, note in CASES:
        route = route_question(question)
        got = route.get("category", "default")
        rows.append({
            "question": question,
            "expected": expected,
            "got": got,
            "action": route.get("action"),
            "ok": got == expected,
            "note": note,
        })

    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["expected"], {"n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += bool(r["ok"])

    # The two failure directions are not symmetric, so they are counted apart.
    false_refusals = [r for r in rows
                      if r["got"] == "procurement_share" != r["expected"]]
    missed_refusals = [r for r in rows
                       if r["expected"] == "procurement_share" != r["got"]]

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(rows),
        "correct": sum(r["ok"] for r in rows),
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 4),
        "by_expected_category": {
            k: {**v, "accuracy": round(v["ok"] / v["n"], 4)} for k, v in by_cat.items()
        },
        "false_refusals": [r["question"] for r in false_refusals],
        "missed_refusals": [r["question"] for r in missed_refusals],
        "results": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/results/probe_router.json")
    args = ap.parse_args()

    rep = run()
    width = max(len(r["question"]) for r in rep["results"])
    print(f"{'':2s} {'expected':18s} {'got':18s} question")
    for r in rep["results"]:
        print(f"{'ok' if r['ok'] else 'XX':2s} {r['expected']:18s} "
              f"{r['got']:18s} {r['question'][:width]}")
    print()
    print(f"router accuracy {rep['correct']}/{rep['n']} = {rep['accuracy']:.1%}")
    for cat, v in sorted(rep["by_expected_category"].items()):
        print(f"  {cat:18s} {v['ok']}/{v['n']}")
    print(f"  false refusals  {len(rep['false_refusals'])}  "
          f"(answerable questions hard-refused with zero tokens)")
    print(f"  missed refusals {len(rep['missed_refusals'])}  "
          f"(undisclosed quantities allowed through to the model)")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
