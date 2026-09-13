"""Middleware for the ``create_agent`` port -- v1's pre-loop policy, as hooks.

v1 (``copilot.agent.agent.ask``) runs a fixed sequence of pure classifiers
before the agent loop and acts on their verdicts. Each piece maps to one hook,
chosen by the granularity the logic actually needs:

  ==========================  ===================  ===========================
  v1 step                     hook                 what it does
  ==========================  ===================  ===========================
  trim_history                _trim                drop oldest whole turns past
                              (@before_agent)      MAX_TURNS / token budget
  (slots for the turn)        _resolve             write state["resolved"]
                              (@before_agent)      -- question + carried year
  route_question -> refuse    _guard               inject canned refusal, end
  clarification_for           _guard               inject clarify text, end
                              (@before_agent,
                               can_jump_to=end)
  active_context_block        _active_context      insert an assumption
                              (@wrap_model_call)   SystemMessage before the Q
  route_question ->           _force_first_tool    pin round-0 tool_choice
    force_tool                (@wrap_model_call)
  ==========================  ===================  ===========================

The first three are ``@before_agent`` because they are **once per turn**, which
is what that hook means (one ``agent.invoke()`` = one turn here). They used to be
``@before_model`` -- which fires before *every* model call -- with a hand-written
"is this the first call of the turn?" guard. That guard was already the source of
one silent bug: checking "no AIMessage anywhere in state" is only true on a
thread's *first* turn, because the checkpointer replays every earlier turn's
AIMessages, so every hook quietly no-opped from turn 2 on. ``before_agent``
removes the need to hand-roll the semantics.

The two ``@wrap_model_call`` hooks still need ``_before_first_model_call``: they
modify the ``ModelRequest``, so there is no once-per-turn hook for them.

The slot classifiers are pure and take a ``carry`` folded from the earlier turns'
questions -- multi-turn state is the checkpointer's message history, re-parsed,
not a stored slot dict (``conversation.carried_slots`` does the same fold over
its kept ``history``). ``runner.py`` recomputes the route from the returned
messages for the result dict rather than threading it through state.
"""

from __future__ import annotations

import json
import os
from typing import NotRequired

from langchain.agents.middleware import AgentState
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from copilot.agent.agent import _PROCUREMENT_REFUSAL_TEXT, route_question
from copilot.agent.clarify import as_text, clarification_for
from copilot.agent.conversation import (
    HISTORY_TOKEN_BUDGET,
    MAX_TURNS,
    _approx_tokens,
    active_context_block,
)
from copilot.agent.grounding import verify_answer
from copilot.agent.slots import extract_slots

# One correction attempt. More risks a loop and doubles cost for an answer the
# model already failed to source once.
MAX_GROUNDING_RETRIES = 1


class ResolvedState(AgentState):
    """The one field this module adds to agent state.

    Declared here, next to the hook that writes it, rather than on
    ``create_agent(state_schema=...)`` -- middleware carries its own schema and
    the factory merges it at compile time. Keeps a field and its writer together,
    which matters once Phase 4 adds ``plan`` / ``sub_results`` / ``evidence_ledger``.

    Why state and not context: ``resolved`` is derived per turn by ``_resolve``
    from the message history, so the caller cannot supply it at invoke time and
    ``context`` (static, read-only) cannot hold it.
    """

    resolved: NotRequired[dict]


class GroundingState(AgentState):
    """Correction attempts spent on the current turn, for ``_grounding_loop``."""

    grounding_retries: NotRequired[int]


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


def steps_from_messages(messages: list) -> list[dict]:
    """Rebuild v1's ``steps`` -- {tool, input, output} per executed tool call.

    ``output`` is the tool's ``ToolMessage.artifact`` (the full structured dict
    from ``content_and_artifact``); v1's ``_collect_citations`` /
    ``build_provenance`` / ``verify_answer`` read the same keys. An error
    ``ToolMessage`` has no artifact -- fall back to its text.

    ``output`` is ALWAYS a dict, matching v1's ``_run_tool`` (whose error paths
    also returned dicts). Every v1 consumer does ``step["output"].get(...)``
    unguarded, so a bare error string here raises ``AttributeError`` deep inside
    frozen code.

    Lives here rather than in ``runner`` so the grounding hook can call it
    without a circular import.
    """
    pending: dict[str, dict] = {}
    steps: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                pending[tc["id"]] = {"tool": tc["name"], "input": tc.get("args", {})}
        elif isinstance(m, ToolMessage):
            base = pending.pop(m.tool_call_id, {"tool": m.name, "input": {}})
            if getattr(m, "artifact", None) is not None:
                out = m.artifact
            else:
                raw = m.content if isinstance(m.content, str) else json.dumps(m.content)
                try:
                    out = json.loads(raw)
                except (ValueError, TypeError):
                    out = raw
            if not isinstance(out, dict):
                out = {"found": False, "error": str(out)}
            steps.append({"tool": base["tool"], "input": base["input"], "output": out})
    return steps


def on_tool_error(exc: Exception, request) -> str | None:
    """``ToolErrorMiddleware`` handler: disclose our ``ToolError``s, propagate the rest.

    A ``ToolError`` carries a hint written for the model -- surface it so the model can correct
    the call or change approach. Anything unexpected returns ``None`` and
    propagates, halting the run rather than being hidden.
    """
    from copilot.v2.tools.base import ToolError

    if not isinstance(exc, ToolError):
        return None
    name = request.tool_call["name"]
    msg = f"{name} error ({exc.kind}): {exc.hint}"
    dym = exc.data.get("did_you_mean")
    if dym:
        msg += f"  did you mean: {', '.join(map(str, dym))}"
    return msg


def _num(n: float) -> str:
    """Readable figure -- ``:,g`` turns 391035000000 into ``3.91035e+11``, which
    the model cannot match against what it wrote."""
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,}"


def _grounding_complaint(v: dict) -> str:
    """What to tell the model about a failed grounding check."""
    parts = []
    if v["unverified_numbers"]:
        nums = ", ".join(_num(n) for n in v["unverified_numbers"][:6])
        parts.append(f"these figures are not traceable to any tool result: {nums}")
    if v["unsourced_inputs"]:
        parts.append(f"these compute inputs were never fetched: {v['unsourced_inputs'][:6]}")
    if v["unverified_citations"]:
        parts.append(f"these accessions were cited but never returned by a tool: "
                     f"{v['unverified_citations'][:4]}")
    if v["misbound_inputs"]:
        parts.append(f"these figures are bound to the wrong company or year: "
                     f"{v['misbound_inputs'][:4]}")
    return (
        "Grounding check failed on your answer -- " + "; ".join(parts) + ". "
        "Either fetch each one with a tool and cite its accession, or remove it "
        "and say what could not be sourced. Do not restate an unsourced number."
    )


def agent_middleware() -> list:
    """v1's pre-loop policy as [trim, resolve, guard, active-context, force-tool],
    plus the Phase 3 grounding loop-back."""
    from langchain.agents.middleware import after_model, before_agent, wrap_model_call

    @before_agent(name="TrimHistory")
    def _trim(state, runtime):
        msgs = state["messages"]
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

    @before_agent(state_schema=ResolvedState, name="Resolve")
    def _resolve(state, runtime):
        # Once per turn: the raw question + the fiscal year the slots inherit
        # from earlier turns (needs message history, so it can't ride the
        # immutable context). Tools read runtime.state["resolved"].
        msgs = state["messages"]
        slots = slots_from_messages(msgs)
        return {"resolved": {"question": latest_question(msgs),
                             "fiscal_year": slots.get("fiscal_year")}}

    @before_agent(can_jump_to=["end"], name="RefuseAndClarifyGuard")
    def _guard(state, runtime):
        msgs = state["messages"]
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

    @after_model(state_schema=GroundingState, can_jump_to=["model"],
                 name="GroundingLoop")
    def _grounding_loop(state, runtime):
        """Send a final answer back to the model when its figures aren't sourced.

        v1 runs ``verify_answer`` *after* the loop -- it can only label a bad
        answer, never fix it. ``after_model`` + ``can_jump_to=["model"]`` turns
        detection into correction, which is the thing a hand-written loop could
        not do without restructuring itself.

        Fires only on a FINAL answer (an AIMessage with no tool calls); a
        mid-loop message on its way to the tools node is not an answer yet.
        Capped at ``MAX_GROUNDING_RETRIES`` per turn.
        """
        if os.getenv("COPILOT_GROUNDING_LOOP", "1") == "0":
            return None
        msgs = state["messages"]
        last = msgs[-1] if msgs else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        if state.get("grounding_retries", 0) >= MAX_GROUNDING_RETRIES:
            return None

        turn = msgs[_last_human_idx(msgs):]
        v = verify_answer(steps_from_messages(turn), _text(last), latest_question(msgs))
        # `numbers_checked == 0` is "stated no checkable figure", which is not
        # the same as passing -- but there is nothing to send back either.
        if v["verified"] or not v["numbers_checked"]:
            return None

        return {
            "grounding_retries": state.get("grounding_retries", 0) + 1,
            "messages": [SystemMessage(content=_grounding_complaint(v))],
            "jump_to": "model",
        }

    return [_trim, _resolve, _guard, _active_context, _force_first_tool,
            _grounding_loop]
