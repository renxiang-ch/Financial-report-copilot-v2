"""Pure-logic tests for the create_agent port's middleware helpers.

No LLM / no agent invoke -- just the message-inspection functions that decide
what the hooks do. End-to-end behavior is covered by ab_compare.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from copilot.v2.orchestration.graph.middleware import (
    _before_first_model_call,
    _turns,
    agent_middleware,
    carry_from_messages,
    latest_question,
)


def _h(text, i):
    return HumanMessage(content=text, id=f"h{i}")


def _a(text, i, tool_calls=None):
    return AIMessage(content=text, id=f"a{i}", tool_calls=tool_calls or [])


def test_latest_question_takes_last_human():
    msgs = [_h("first", 1), _a("ans", 1), _h("second", 2)]
    assert latest_question(msgs) == "second"


def test_before_first_model_call_is_turn_scoped():
    # start of turn 2: prior turn's AIMessage present, but none since last human
    msgs = [_h("q1", 1), _a("a1", 1), _h("q2", 2)]
    assert _before_first_model_call(msgs) is True
    # after the model answered in turn 2
    assert _before_first_model_call([*msgs, _a("a2", 2)]) is False


def test_carry_folds_prior_questions_only():
    msgs = [
        _h("What was Cirrus Logic's revenue in fiscal 2024?", 1),
        _a("$1.79B", 1),
        _h("How dependent is it on Apple?", 2),
    ]
    carry = carry_from_messages(msgs)
    # the FY2024 constraint from turn 1 is what a follow-up inherits
    assert carry["fiscal_year"] == 2024
    # current-turn question is NOT folded in
    assert carry_from_messages(msgs[:1]) is None


def test_turns_group_at_human_boundaries():
    msgs = [_h("q1", 1), _a("", 1, [{"id": "t", "name": "x", "args": {}}]),
            ToolMessage(content="{}", tool_call_id="t", id="tm1"), _a("a1", 1),
            _h("q2", 2), _a("a2", 2)]
    turns = _turns(msgs)
    assert len(turns) == 2
    assert len(turns[0]) == 4 and len(turns[1]) == 2


def test_trim_keeps_recent_turns_and_drops_old(monkeypatch):
    import copilot.v2.orchestration.graph.middleware as mw

    monkeypatch.setattr(mw, "MAX_TURNS", 2)
    mids = {m.name: m for m in agent_middleware()}
    trim = mids["TrimHistory"]

    # 3 completed turns + turn 4 just starting (last msg is the new HumanMessage,
    # model not yet called -- this is when before_model fires)
    msgs = []
    for i in range(1, 4):
        msgs += [_h(f"q{i}", i), _a(f"a{i}", i)]
    msgs.append(_h("q4", 4))
    out = trim.before_model({"messages": msgs}, None)

    removed = {m.id for m in out["messages"] if isinstance(m, RemoveMessage)}
    assert removed == {"h1", "a1", "h2", "a2"}  # keep turns 3-4, drop 1-2


def test_trim_noop_for_single_turn():
    mids = {m.name: m for m in agent_middleware()}
    out = mids["TrimHistory"].before_model(
        {"messages": [_h("only", 1), _a("x", 1)]}, None)
    assert out is None
