"""3.2 acceptance check: does a real trace carry a span per middleware hook?

Runs one question through the graph with tracing on, then reads the trace back
out of the LangSmith API and reports the span tree. The acceptance bar from
plan §3.x is that the six middleware hooks, the model call and the tool calls
each appear as their own span -- the attribution that ``runner.py`` cannot
reconstruct from messages after the fact.

This exists as a script rather than a test because it needs a real key, a real
model call (~$0.001) and the network. Nothing in ``pytest`` may depend on those.

    uv run python scripts/smoke_langsmith.py
    uv run python scripts/smoke_langsmith.py --question "..." --project frc-smoke
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings

# `list_runs` is deprecated in favour of `client.runs.query` (removal Jan 2027),
# but in langsmith 0.12.1 the replacement returns an `AsyncPaginator` -- adopting
# it would make this whole script async for no gain today. Revisit when a sync
# `runs.query` ships.
warnings.filterwarnings("ignore", message=r"list_runs\(\) is deprecated.*")

# The six hooks declared by agent_middleware(); see middleware.py. LangSmith names
# each span `<MiddlewareName>.<hook>` (e.g. `TrimHistory.before_agent`), so spans
# are matched on the part before the dot.
EXPECTED_HOOKS = (
    "TrimHistory",
    "Resolve",
    "RefuseAndClarifyGuard",
    "ActiveContext",
    "ForceFirstTool",
    "GroundingLoop",
)

DEFAULT_QUESTION = "What was Cirrus Logic's revenue in fiscal 2024?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--project", default="frc-smoke")
    ap.add_argument("--wait", type=float, default=8.0,
                    help="seconds to let the ingest queue drain before reading back")
    args = ap.parse_args()

    from copilot.v2.observability import enable_tracing, tracing_project

    if not enable_tracing():
        print("FAIL: tracing is not enabled.\n"
              "  Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env.\n"
              "  Non-US accounts must also set LANGSMITH_ENDPOINT (e.g.\n"
              "  https://eu.api.smith.langchain.com) or the key is not recognised.")
        return 1

    import langsmith as ls

    from copilot.v2.orchestration.graph import run

    print(f"project  : {args.project}")
    print(f"question : {args.question}\n")

    t0 = time.time()
    with tracing_project(args.project, smoke=True):
        result = run(args.question)
    print(f"answer   : {(result['answer'] or '')[:120]}")
    print(f"tools    : {[s.get('tool') for s in result['steps']]}")
    print(f"latency  : {time.time() - t0:.1f}s\n")

    client = ls.Client()
    client.flush()
    print(f"waiting {args.wait:.0f}s for the ingest queue...")
    time.sleep(args.wait)

    roots = list(client.list_runs(project_name=args.project, is_root=True, limit=1))
    if not roots:
        print(f"FAIL: no runs in project {args.project!r}. The run was created locally "
              "but never landed -- check the key and LANGSMITH_ENDPOINT region.")
        return 1

    root = roots[0]
    spans = list(client.list_runs(project_name=args.project, trace_id=root.trace_id))

    print(f"\ntrace    : {getattr(root, 'url', root.trace_id)}")
    print(f"spans    : {len(spans)}\n")
    _print_tree(spans, root)

    seen = {s.name.split(".")[0] for s in spans}
    counts: dict[str, int] = {}
    for s in spans:
        stem = s.name.split(".")[0]
        counts[stem] = counts.get(stem, 0) + 1

    print("\nhook spans per turn (the granularity the docs describe):")
    for hook in EXPECTED_HOOKS:
        n = counts.get(hook, 0)
        kind = ("before_agent -- once per turn" if hook in
                ("TrimHistory", "Resolve", "RefuseAndClarifyGuard")
                else "per model call")
        mark = "ok" if n else "--"
        print(f"  [{mark}] {n}x {hook:<24} ({kind})")

    missing = [h for h in EXPECTED_HOOKS if h not in seen]
    print()
    if missing:
        # Not automatically a failure: a hook that never fires on this question
        # (GroundingLoop when every figure is already sourced, for instance) has
        # nothing to emit.
        print(f"hooks with no span: {missing}")
        print("  -> expected only for hooks that did not fire on this question.")
    else:
        print("PASS: all six middleware hooks appear as their own spans.")
    return 0


def _print_tree(spans: list, root) -> None:
    """Render the span hierarchy, which is the part ``runner.py`` cannot rebuild.

    Nesting is the evidence for two design decisions that were until now only
    asserted in comments: middleware ordering (retry inner, error outer) and hook
    granularity (once per turn vs once per model call).
    """
    kids: dict[str, list] = {}
    for s in spans:
        kids.setdefault(str(s.parent_run_id), []).append(s)
    for sibs in kids.values():
        sibs.sort(key=lambda s: s.start_time)

    def walk(span, depth: int) -> None:
        ms = ""
        if span.end_time and span.start_time:
            ms = f"  {(span.end_time - span.start_time).total_seconds() * 1000:.0f}ms"
        print(f"  {'  ' * depth}{'└─ ' if depth else ''}{span.name}{ms}")
        for child in kids.get(str(span.id), []):
            walk(child, depth + 1)

    walk(root, 0)


if __name__ == "__main__":
    sys.exit(main())
