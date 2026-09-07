"""Re-score a saved generic-agent baseline report without re-running codex.

The report already has each item's answer text saved -- scoring is a pure
function of (item, answer_text), so fixing a scorer bug or getting an
OPENAI_API_KEY later should never require paying for another 38-item sweep.
See docs/generic-agent-baseline.md and src/copilot/eval/generic_scoring.py.

Usage::

    uv run python scripts/rescore_generic_baseline.py data/results/generic_agent_baseline_<ts>.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from copilot.v2.eval.generic_scoring import score_item  # noqa: E402


def _load_items_by_id() -> dict[str, dict]:
    items: dict[str, dict] = {}
    for fname, dataset in [("eval_set.json", "eval_set"),
                            ("eval_set_tier3.json", "eval_set_tier3")]:
        data = json.loads((REPO_ROOT / "data" / "datasets" / fname).read_text())
        for it in data["items"]:
            items[it["id"]] = {**it, "_dataset": dataset}
    return items


def rescore(report_path: Path) -> dict:
    report = json.loads(report_path.read_text())
    items_by_id = _load_items_by_id()

    new_rows = []
    for row in report["rows"]:
        item = items_by_id.get(row["id"])
        if item is None or row["status"] != "OK":
            new_rows.append(row)
            continue
        scored = score_item(item, row["answer"])
        new_rows.append({**row, **scored})

    report["rows"] = new_rows
    report["summary"] = _summarize(new_rows)
    return report


def _summarize(rows: list[dict]) -> dict:
    from collections import Counter

    by_tier: dict[str, dict] = {}
    for r in rows:
        tier = f"tier{r.get('tier')}" if r.get("tier") else "unanswerable"
        d = by_tier.setdefault(tier, {"n": 0, "correct": 0, "needs_review": 0})
        d["n"] += 1
        if r.get("correct") is True:
            d["correct"] += 1
        elif r.get("needs_review") or r.get("correct") is None:
            d["needs_review"] += 1
    accuracy = {
        t: (round(d["correct"] / (d["n"] - d["needs_review"]), 3)
            if (d["n"] - d["needs_review"]) else None)
        for t, d in by_tier.items()
    }
    statuses = Counter(r["status"] for r in rows)
    return {"status_counts": dict(statuses), "by_tier": by_tier, "accuracy_excl_pending": accuracy}


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    report = rescore(path)
    out_path = path.with_name(path.stem + "_rescored.json")
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print(f"\nwrote {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
