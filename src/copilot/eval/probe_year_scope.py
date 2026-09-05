"""Deterministic A/B on fiscal-year scoping of retrieval. No LLM, no cost.

Why a probe and not the harness
    The harness's retrieval metric runs through the agent and an LLM judge and
    has ranged 25%-62.5% on unchanged code. It cannot resolve a change smaller
    than that band, and this change is smaller. Here the agent and the judge are
    removed, so the only thing varying is which filings were searched.

The premise this exists to test
    Every company has around ten filings and a quarter of the corpus is
    boilerplate repeated across them, so an unscoped top-five fills up with the
    same paragraph from four different years. Scoping to one year should fix
    that -- but only one of the seven retrieval questions states a year in its
    text. The other six carry it as dataset metadata the agent never sees. So
    the interesting comparison is not scoped-vs-unscoped, it is which POLICY for
    choosing the year survives that fact.

Four arms:
    none    no scoping                                       (behaviour before)
    asked   scope only when the question text names a year   (the obvious design)
    latest  name a year if the question gives one, else the company's newest
            filing                                           (behaviour after)
    trend   as `latest`, but never scope a question about change over time

Run:
    $env:PYTHONUTF8="1"; uv run --active python -m copilot.eval.probe_year_scope
"""

import argparse
import json
from datetime import datetime, timezone

from copilot.agent.slots import extract_slots
from copilot.agent.tools import _latest_filing_year
from copilot.retrieval.hybrid import retrieve_hybrid

DEPTH = 10          # how far down the list a rank is still recorded
KS = (1, 3, 5)


def _policy_year(policy: str, question: str, ticker: str | None) -> int | None:
    slots = extract_slots(question)
    stated = slots["fiscal_year"]
    if policy == "none":
        return None
    if policy == "asked":
        return stated
    if stated:
        return stated
    if policy == "trend" and (slots["is_trend"] or len(slots["years"]) > 1):
        return None
    return _latest_filing_year(ticker) if ticker else None


def _golden_rank(results: list[dict], phrase: str) -> int | None:
    needle = " ".join(phrase.lower().split())
    for i, r in enumerate(results, start=1):
        if needle in " ".join((r.get("text") or "").lower().split()):
            return i
    return None


def run(dataset: str) -> dict:
    with open(dataset, encoding="utf-8") as fh:
        data = json.load(fh)
    items = [it for it in (data.get("items") or data.get("questions") or [])
             if it.get("type") == "retrieval" and not it.get("retired")]

    arms = {}
    for policy in ("none", "asked", "latest", "trend"):
        rows = []
        for it in items:
            ticker = it.get("ticker")
            year = _policy_year(policy, it["question"], ticker)
            results = retrieve_hybrid(it["question"], ticker=ticker, k=DEPTH,
                                      fiscal_year=year)
            rank = _golden_rank(results, it["golden_citations"][0]["key_phrase"])
            rows.append({"id": it["id"], "scoped_to": year, "rank": rank})
        arms[policy] = {
            "hits": {f"hit@{k}": sum(1 for r in rows if r["rank"] and r["rank"] <= k)
                     for k in KS},
            # Reciprocal rank moves whenever the ranking moves. A hit@5 count at
            # n=7 only moves in steps of 14 points and is blind to any change
            # that reorders without crossing k.
            "mrr": round(sum(1.0 / r["rank"] for r in rows if r["rank"]) / len(rows), 4),
            "results": rows,
        }
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(items),
        "arms": arms,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/datasets/eval_set.json")
    ap.add_argument("--out", default="data/results/probe_year_scope.json")
    args = ap.parse_args()

    rep = run(args.dataset)
    print(f"{rep['n']} retrieval questions\n")
    print(f"{'arm':8s} {'hit@1':>6s} {'hit@3':>6s} {'hit@5':>6s} {'MRR':>7s}")
    for name, arm in rep["arms"].items():
        h = arm["hits"]
        print(f"{name:8s} {h['hit@1']:>6d} {h['hit@3']:>6d} {h['hit@5']:>6d} {arm['mrr']:>7.3f}")

    print(f"\n{'question':38s} " + " ".join(f"{a:>8s}" for a in rep["arms"]))
    for i in range(rep["n"]):
        qid = rep["arms"]["none"]["results"][i]["id"]
        cells = []
        for a in rep["arms"]:
            r = rep["arms"][a]["results"][i]
            cells.append(f"{str(r['rank'] or '-'):>3s}/{str(r['scoped_to'] or '-')[-4:]:>4s}")
        print(f"{qid[:38]:38s} " + " ".join(f"{c:>8s}" for c in cells))
    print("\n(rank / year searched; '-' means not found or not scoped)")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
