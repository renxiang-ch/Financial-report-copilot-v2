"""Typed graph state.

Phase 0 skeleton -- the fields the Phase 2 parity graph needs, mapped from what
the v1 loop threads through by hand (``copilot.agent.agent._ask_openai`` locals
plus the ``ask()`` return dict). Phase 4 adds the deep-research fields
(``plan``, ``sub_results``, ``evidence_ledger``, ``budget``).
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def _extend(left: list | None, right: list | None) -> list:
    """Reducer: append tool steps instead of overwriting."""
    return (left or []) + (right or [])


class ToolStep(TypedDict):
    """One executed tool call -- mirrors v1's ``steps[]`` entries."""

    tool: str
    input: dict[str, Any]
    output: Any


class AgentState(TypedDict, total=False):
    # Conversation. ``add_messages`` reducer appends / merges by id.
    messages: Annotated[list[AnyMessage], add_messages]

    # The current turn's question, kept verbatim (v1 re-injects it into
    # retrieve_text rather than trusting a model-written query string).
    question: str

    # Pre-routing decision: {"category", "action": auto|force_tool|refuse, ...}.
    route: dict[str, Any]

    # Constraints a follow-up inherited from earlier turns (v1: slots.py).
    carried_slots: dict[str, Any]
    active_context: str | None

    # Evidence ledger -- accumulated, feeds provenance / citation collection.
    steps: Annotated[list[ToolStep], _extend]
    citations: list[str]

    # Post-answer checks (v1: grounding.verify_answer / provenance.build_provenance).
    provenance: dict[str, Any] | None
    verification: dict[str, Any] | None

    # Loop guard, replaces MAX_ROUNDS=10.
    round: int
