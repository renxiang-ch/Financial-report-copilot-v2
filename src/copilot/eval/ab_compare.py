"""A/B harness: same questions through the frozen v1 loop and the LangGraph impl.

Phase 0: skeleton. There is no graph implementation yet (the Phase 0a stub was
removed 2026-09-05), so the graph side is hard-coded to ``STUB``. Phase 2 wires
in ``copilot.orchestration.graph`` and this becomes the parity gate -- run it on
every orchestration change and diff answer / citations / refusal / cost.

Comparison is end-to-end only (answer text, citation set, refusal, latency,
tokens). Tool internals are deliberately not compared: after Phase 1 the two
sides no longer share a tool implementation.

Usage::

    uv run python -m copilot.eval.ab_compare --limit 5
    uv run python -m copilot.eval.ab_compare --dataset data/datasets/eval_set_tier3.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESULTS_DIR = _REPO_ROOT / "data" / "results"


def _refused(answer: str) -> bool:
    a = (answer or "").lower()
    return "cannot determine" in a or "cannot find" in a or "unanswerable" in a


def _run_v1(question: str) -> dict[str, Any]:
    if not os.environ.get("OPENAI_API_KEY") and not _dotenv_has_key():
        return {"status": "SKIP", "reason": "no OPENAI_API_KEY"}
    from copilot.orchestration.v1_loop import ask

    t0 = time.perf_counter()
    try:
        r = ask(question)
    except Exception as e:  # noqa: BLE001 -- record, don't crash the sweep
        return {"status": "ERROR", "reason": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=3)}
    dt = round((time.perf_counter() - t0) * 1000)
    usage = r.get("usage") or {}
    return {
        "status": "OK",
        "answer": r.get("answer", ""),
        "citations": sorted(set(r.get("citations") or [])),
        "refused": _refused(r.get("answer", "")),
        "n_steps": len(r.get("steps") or []),
        "latency_ms": dt,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def _run_graph(question: str) -> dict[str, Any]:
    # No graph implementation yet -- Phase 2 wires in copilot.orchestration.graph
    # and this dispatches to it. Until then every row's graph side is STUB.
    return {"status": "STUB"}


def _dotenv_has_key() -> bool:
    env = _REPO_ROOT / ".env"
    if not env.exists():
        return False
    for line in env.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY=") and line.split("=", 1)[1].strip():
            return True
    return False


def _diff(v1: dict, graph: dict) -> dict[str, Any]:
    if v1.get("status") != "OK" or graph.get("status") != "OK":
        return {"comparable": False,
                "v1_status": v1.get("status"), "graph_status": graph.get("status")}
    return {
        "comparable": True,
        "citations_match": v1["citations"] == graph["citations"],
        "refusal_match": v1["refused"] == graph["refused"],
        "answer_len_delta": len(graph["answer"]) - len(v1["answer"]),
    }


def compare(dataset: str, limit: int | None = None) -> dict[str, Any]:
    data = json.loads(Path(dataset).read_text())
    items = data["items"] if isinstance(data, dict) else data
    if limit:
        items = items[:limit]

    rows = []
    for it in items:
        q = it["question"]
        v1 = _run_v1(q)
        graph = _run_graph(q)
        rows.append({"id": it.get("id"), "tier": it.get("tier"), "question": q,
                     "v1": v1, "graph": graph, "diff": _diff(v1, graph)})

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": ts,
        "dataset": dataset,
        "n": len(rows),
        "summary": _summary(rows),
        "rows": rows,
    }
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (_RESULTS_DIR / f"ab_{ts}.json").write_text(json.dumps(report, indent=2))
    (_RESULTS_DIR / f"ab_{ts}.md").write_text(_markdown(report))
    return report


def _summary(rows: list[dict]) -> dict[str, Any]:
    from collections import Counter

    v1_status = Counter(r["v1"]["status"] for r in rows)
    graph_status = Counter(r["graph"]["status"] for r in rows)
    comparable = [r for r in rows if r["diff"].get("comparable")]
    return {
        "v1_status": dict(v1_status),
        "graph_status": dict(graph_status),
        "comparable": len(comparable),
        "citations_match": sum(r["diff"]["citations_match"] for r in comparable),
        "refusal_match": sum(r["diff"]["refusal_match"] for r in comparable),
    }


def _markdown(report: dict) -> str:
    s = report["summary"]
    out = [
        f"# A/B compare — {report['generated_at']}",
        "",
        f"- dataset: `{report['dataset']}`  ·  n = {report['n']}",
        f"- v1 status: {s['v1_status']}",
        f"- graph status: {s['graph_status']}",
        f"- comparable: {s['comparable']}  ·  citations match: {s['citations_match']}"
        f"  ·  refusal match: {s['refusal_match']}",
        "",
        "| id | tier | v1 | graph | cites | refusal |",
        "|----|------|----|-------|-------|---------|",
    ]
    for r in report["rows"]:
        d = r["diff"]
        cm = "—" if not d.get("comparable") else ("✓" if d["citations_match"] else "✗")
        rm = "—" if not d.get("comparable") else ("✓" if d["refusal_match"] else "✗")
        out.append(f"| {r['id']} | {r.get('tier','')} | {r['v1']['status']} "
                   f"| {r['graph']['status']} | {cm} | {rm} |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="data/datasets/eval_set.json")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    report = compare(args.dataset, args.limit)
    print(json.dumps(report["summary"], indent=2))
    print(f"\nwrote data/results/ab_{report['generated_at']}.{{json,md}}")


if __name__ == "__main__":
    main()
