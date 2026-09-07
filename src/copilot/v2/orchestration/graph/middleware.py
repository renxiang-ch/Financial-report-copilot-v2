"""Middleware for the ``create_agent`` port -- Step 3: routing.

The ``create_agent`` equivalent of v1's ``route_question`` (``copilot.agent.agent``).
v1 runs that pure classifier BEFORE the agent loop and acts on its verdict twice:

  * ``action == "refuse"`` -> short-circuit to a deterministic refusal, no LLM
    call at all (category ``procurement_share``: a customer-side procurement-share
    question is structurally undisclosed in supplier-reported 10-K data).
  * ``action == "force_tool"`` -> pin the FIRST model call's ``tool_choice`` to
    the named tool (category ``dependency`` -> ``graph_query``); rounds 1+ choose
    freely.

Here those become two hooks:

  * ``_refuse_guard``    -- ``@before_model``, jumps to ``end`` with the canned
    refusal text before the first model call.
  * ``_force_first_tool`` -- ``@wrap_model_call``, overrides ``tool_choice`` on
    the round-0 request.

"First round" is ``no AIMessage in state yet`` here, where v1 uses
``round_idx == 0``. ``route_question`` is pure; ``runner.py`` calls it again for
the result dict's ``route`` field rather than threading it through state -- the
same tradeoff v1 documents at its ``slots = extract_slots(...)`` re-call.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from copilot.agent.agent import _PROCUREMENT_REFUSAL_TEXT, route_question


def _latest_question(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _before_first_model_call(messages: list) -> bool:
    return not any(isinstance(m, AIMessage) for m in messages)


def routing_middleware() -> list:
    """v1 route_question as [refuse-guard, force-first-tool] middleware."""
    from langchain.agents.middleware import before_model, wrap_model_call

    @before_model(can_jump_to=["end"], name="RefuseGuard")
    def _refuse_guard(state, runtime):
        msgs = state["messages"]
        if not _before_first_model_call(msgs):
            return None
        if route_question(_latest_question(msgs))["action"] == "refuse":
            return {"messages": [AIMessage(content=_PROCUREMENT_REFUSAL_TEXT)],
                    "jump_to": "end"}
        return None

    @wrap_model_call(name="ForceFirstTool")
    def _force_first_tool(request, handler):
        msgs = request.state["messages"]
        if _before_first_model_call(msgs):
            route = route_question(_latest_question(msgs))
            if route["action"] == "force_tool":
                request = request.override(tool_choice=route["tool"])
        return handler(request)

    return [_refuse_guard, _force_first_tool]
