"""Thin call wrapper around the compiled graph.

Gives the graph the same call shape as ``copilot.orchestration.v1_loop.ask`` so
``ab_compare`` can drive both sides through one interface. Phase 2 fleshes out
the input/output mapping (history <-> checkpointer thread, state <-> v1's return
dict).
"""

from __future__ import annotations

from typing import Any

from copilot.orchestration.graph.build import build_graph

_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run(question: str, history: list[dict] | None = None,
        thread_id: str | None = None) -> dict[str, Any]:
    """Run one turn. Mirrors ``v1_loop.ask`` return keys where they exist yet."""
    state = _graph().invoke({"question": question, "messages": []})
    return {
        "answer": "",
        "steps": state.get("steps", []),
        "citations": state.get("citations", []),
        "route": state.get("route", {}),
    }
