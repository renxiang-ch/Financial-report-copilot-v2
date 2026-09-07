"""Middleware for the ``create_agent`` port -- v1's pre-loop policy, as hooks.

v1 (``copilot.agent.agent.ask``) runs a fixed sequence of pure classifiers
before the agent loop and acts on their verdicts. Each piece maps to one hook:

  ==========================  ==================  ============================
  v1 step                     hook                what it does
  ==========================  ==================  ============================
  trim_history                _trim               drop oldest whole turns past
                              (@before_model)     MAX_TURNS / token budget
  route_question -> refuse    _guard              inject canned refusal, end
  clarification_for           _guard              inject clarify text, end
                              (@before_model,
                               can_jump_to=end)
  active_context_block        _active_context     insert an assumption
                              (@wrap_model_call)  SystemMessage before the Q
  route_question ->           _force_first_tool   pin round-0 tool_choice
    force_tool                (@wrap_model_call)
  ==========================  ==================  ============================

"First model call of the turn" is ``no AIMessage in state`` here, where v1 uses
``round_idx == 0``. The slot classifiers are pure and take a ``carry`` folded
from the earlier turns' questions -- multi-turn state is the checkpointer's
message history, re-parsed, not a stored slot dict (``conversation.carried_slots``
does the same fold over its kept ``history``). ``runner.py`` recomputes the
route/slots from the returned messages for the result dict rather than threading
them through a custom state schema.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from copilot.agent.agent import _PROCUREMENT_REFUSAL_TEXT, route_question
from copilot.agent.clarify import as_text, clarification_for
from copilot.agent.conversation import (
    HISTORY_TOKEN_BUDGET,
    MAX_TURNS,
    _approx_tokens,
    active_context_block,
)
from copilot.agent.slots import extract_slots


def _text(m) -> str:
    return m.content if isinstance(m.content, str) else str(m.content)


def _human_texts(messages: list) -> list[str]:
    return [_text(m) for m in messages if isinstance(m, HumanMessage)]


def latest_question(messages: list) -> str:
    qs = _human_texts(messages)
    return qs[-1] if qs else ""


def carry_from_messages(messages: list) -> dict | None:
    """Fold extract_slots over every prior turn's question (not the current one).

    Mirror of ``copilot.agent.conversation.carried_slots``, reading the
    checkpointer's message history instead of a ``history`` list.
    """
    carry = None
    for q in _human_texts(messages)[:-1]:
        if q.strip():
            carry = extract_slots(q, carry=carry)
    return carry


def slots_from_messages(messages: list) -> dict:
    return extract_slots(latest_question(messages), carry=carry_from_messages(messages))


def route_from_messages(messages: list) -> dict:
    return route_question(latest_question(messages), carry=carry_from_messages(messages))


def _last_human_idx(messages: list) -> int:
    return max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)),
               default=0)


def _before_first_model_call(messages: list) -> bool:
    """True at the start of the current turn -- no AIMessage since the last human.

    v1's ``round_idx == 0``. Looking at the whole list instead would make this
    fire only on the very first turn of a thread; the checkpointer replays every
    earlier turn's AIMessages into state.
    """
    tail = messages[_last_human_idx(messages):]
    return not any(isinstance(m, AIMessage) for m in tail)


def _turns(messages: list) -> list[list]:
    """Group a message list into turns, each starting at a HumanMessage."""
    turns: list[list] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            turns.append([m])
        elif turns:
            turns[-1].append(m)
    return turns


def agent_middleware() -> list:
    """v1's pre-loop policy as [trim, guard, active-context, force-first-tool]."""
    from langchain.agents.middleware import before_model, wrap_model_call

    @before_model(name="TrimHistory")
    def _trim(state, runtime):
        msgs = state["messages"]
        if not _before_first_model_call(msgs):
            return None
        turns = _turns(msgs)
        if len(turns) <= 1:
            return None
        kept: list[list] = []
        used = 0
        for turn in reversed(turns):
            cost = sum(_approx_tokens(_text(m)) for m in turn)
            if len(kept) >= MAX_TURNS or (kept and used + cost > HISTORY_TOKEN_BUDGET):
                break
            kept.append(turn)
            used += cost
        keep_ids = {id(m) for t in kept for m in t}
        drop = [m for m in msgs if id(m) not in keep_ids and m.id is not None]
        if not drop:
            return None
        return {"messages": [RemoveMessage(id=m.id) for m in drop]}

    @before_model(can_jump_to=["end"], name="RefuseAndClarifyGuard")
    def _guard(state, runtime):
        msgs = state["messages"]
        if not _before_first_model_call(msgs):
            return None
        q = latest_question(msgs)
        carry = carry_from_messages(msgs)
        route = route_question(q, carry=carry)
        if route["action"] == "refuse":
            return {"messages": [AIMessage(content=_PROCUREMENT_REFUSAL_TEXT)],
                    "jump_to": "end"}
        clar = clarification_for(q, extract_slots(q, carry=carry), carry)
        if clar:
            return {"messages": [AIMessage(content=as_text(clar))], "jump_to": "end"}
        return None

    @wrap_model_call(name="ActiveContext")
    def _active_context(request, handler):
        msgs = request.state["messages"]
        if _before_first_model_call(msgs):
            block = active_context_block(slots_from_messages(msgs))
            if block:
                out = list(request.messages)
                out.insert(max(len(out) - 1, 0), SystemMessage(content=block))
                request = request.override(messages=out)
        return handler(request)

    @wrap_model_call(name="ForceFirstTool")
    def _force_first_tool(request, handler):
        msgs = request.state["messages"]
        if _before_first_model_call(msgs):
            route = route_from_messages(msgs)
            if route["action"] == "force_tool":
                request = request.override(tool_choice=route["tool"])
        return handler(request)

    return [_trim, _guard, _active_context, _force_first_tool]
