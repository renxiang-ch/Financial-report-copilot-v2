"""Drive the ``create_agent`` agent and return a v1-shaped result dict.

Same call shape and return keys as ``copilot.orchestration.v1_loop.ask`` so
``ab_compare`` can diff the two. Step 1 fills ``answer / steps / citations /
usage / provenance / verification``; ``route`` and the multi-turn ``history``
plumbing come with the routing / history middleware in later steps.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from copilot.agent.agent import _collect_citations
from copilot.agent.grounding import verify_answer
from copilot.agent.provenance import build_provenance
from copilot.orchestration.graph.build import DEFAULT_MODEL, build_agent

_AGENT = None


def _agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = build_agent()
    return _AGENT


def _steps_from_messages(messages: list) -> list[dict]:
    """Rebuild v1's ``steps`` -- {tool, input, output} per executed tool call."""
    pending: dict[str, dict] = {}
    steps: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                pending[tc["id"]] = {"tool": tc["name"], "input": tc.get("args", {})}
        elif isinstance(m, ToolMessage):
            base = pending.pop(m.tool_call_id, {"tool": m.name, "input": {}})
            raw = m.content if isinstance(m.content, str) else json.dumps(m.content)
            try:
                out = json.loads(raw)
            except (ValueError, TypeError):
                out = raw
            steps.append({"tool": base["tool"], "input": base["input"], "output": out})
    return steps


def _usage_from_messages(messages: list) -> dict:
    inp = out = cached = 0
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if not um:
            continue
        inp += um.get("input_tokens", 0) or 0
        out += um.get("output_tokens", 0) or 0
        cached += (um.get("input_token_details") or {}).get("cache_read", 0) or 0
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cached_input_tokens": cached,
        "cache_hit_rate": round(cached / inp, 4) if inp else 0.0,
    }


def run(question: str, history: list[dict] | None = None,
        thread_id: str | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    agent = _agent() if model == DEFAULT_MODEL else build_agent(model)
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    result = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)

    messages = result["messages"]
    answer = ""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and not m.tool_calls:
            answer = m.content if isinstance(m.content, str) else str(m.content)
            break

    steps = _steps_from_messages(messages)
    return {
        "answer": answer,
        "steps": steps,
        "citations": _collect_citations(steps, answer),
        "usage": _usage_from_messages(messages),
        "provenance": build_provenance(steps, answer),
        "verification": verify_answer(steps, answer, question),
        "route": {"action": "auto", "category": "default"},
    }
