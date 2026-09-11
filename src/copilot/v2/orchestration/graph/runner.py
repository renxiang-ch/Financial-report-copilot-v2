"""Drive the ``create_agent`` agent and return a v1-shaped result dict.

Same call shape and return keys as ``copilot.v2.orchestration.v1_loop.ask`` so
``ab_compare`` can diff the two. Fills ``answer / steps / citations / usage /
provenance / verification / route``.

Multi-turn: pass a stable ``thread_id`` and the checkpointer replays the prior
turns' messages. ``steps`` / ``usage`` / ``answer`` are scoped to the CURRENT
turn (messages from the last HumanMessage on); ``route`` sees the whole thread
so its ``carry`` fold works, matching v1's ``route_question(q, carry=...)``.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from copilot.agent.agent import _collect_citations
from copilot.agent.grounding import verify_answer
from copilot.agent.provenance import build_provenance
from copilot.v2.orchestration.graph.build import DEFAULT_MODEL, build_agent
from copilot.v2.orchestration.graph.middleware import (
    route_from_messages,
    steps_from_messages,
)

_AGENT = None


def _agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = build_agent()
    return _AGENT


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


def run(question: str, thread_id: str | None = None,
        model: str = DEFAULT_MODEL) -> dict[str, Any]:
    agent = _agent() if model == DEFAULT_MODEL else build_agent(model)
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    result = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)

    messages = result["messages"]
    # Scope steps/usage/answer to this turn; route needs the whole thread.
    last_human = max((i for i, m in enumerate(messages)
                      if isinstance(m, HumanMessage)), default=0)
    turn = messages[last_human:]

    answer = ""
    for m in reversed(turn):
        if isinstance(m, AIMessage) and not m.tool_calls:
            answer = m.content if isinstance(m.content, str) else str(m.content)
            break

    steps = steps_from_messages(turn)
    return {
        "answer": answer,
        "steps": steps,
        "citations": _collect_citations(steps, answer),
        "usage": _usage_from_messages(turn),
        "provenance": build_provenance(steps, answer),
        "verification": verify_answer(steps, answer, question),
        "route": route_from_messages(messages),
    }
