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

    # 3 completed turns + turn 4 just starting -- the state `before_agent` sees
    # at the top of a turn (checkpointer replayed history + the new question)
    msgs = []
    for i in range(1, 4):
        msgs += [_h(f"q{i}", i), _a(f"a{i}", i)]
    msgs.append(_h("q4", 4))
    out = trim.before_agent({"messages": msgs}, None)

    removed = {m.id for m in out["messages"] if isinstance(m, RemoveMessage)}
    assert removed == {"h1", "a1", "h2", "a2"}  # keep turns 3-4, drop 1-2


def test_trim_noop_for_single_turn():
    mids = {m.name: m for m in agent_middleware()}
    out = mids["TrimHistory"].before_agent(
        {"messages": [_h("only", 1), _a("x", 1)]}, None)
    assert out is None


def test_once_per_turn_hooks_are_before_agent():
    """The three once-per-turn hooks must be before_agent, not before_model.

    before_model fires on every model call, which is why these used to carry a
    hand-written "first call of the turn?" guard -- and that guard was the source
    of a silent bug (see middleware.py's module docstring).
    """
    mids = {m.name: m for m in agent_middleware()}
    for name in ("TrimHistory", "Resolve", "RefuseAndClarifyGuard"):
        m = mids[name]
        assert type(m).before_agent is not None
        # no before_model override -- the base class's is a no-op passthrough
        assert "before_model" not in type(m).__dict__, f"{name} still on before_model"


def _grounding():
    return {m.name: m for m in agent_middleware()}["GroundingLoop"]


def test_grounding_loop_sends_unsourced_answer_back():
    st = {"messages": [_h("What was Apple's revenue in FY2024?", 1),
                       _a("Revenue was $391,035,000,000 and margin 46.2%.", 1)]}
    out = _grounding().after_model(st, None)
    assert out["jump_to"] == "model"
    assert out["grounding_retries"] == 1
    # figures must be readable, not 3.91035e+11 -- the model has to match them
    assert "391,035,000,000" in out["messages"][0].content


def test_grounding_loop_respects_retry_cap():
    st = {"messages": [_h("q", 1), _a("Revenue was $391,035,000,000.", 1)],
          "grounding_retries": 1}
    assert _grounding().after_model(st, None) is None


def test_grounding_loop_ignores_mid_loop_and_figureless_answers():
    g = _grounding()
    # an AIMessage carrying tool calls is not an answer yet
    mid = {"messages": [_h("q", 1),
                        _a("", 1, [{"id": "t", "name": "query_financials", "args": {}}])]}
    assert g.after_model(mid, None) is None
    # nothing checkable to send back
    assert g.after_model({"messages": [_h("q", 1),
                                       _a("I cannot determine this.", 1)]}, None) is None


def test_grounding_loop_can_be_disabled(monkeypatch):
    monkeypatch.setenv("COPILOT_GROUNDING_LOOP", "0")
    st = {"messages": [_h("q", 1), _a("Revenue was $391,035,000,000.", 1)]}
    assert _grounding().after_model(st, None) is None


def test_resolve_writes_state_and_carries_its_own_schema():
    mids = {m.name: m for m in agent_middleware()}
    resolve = mids["Resolve"]
    assert "resolved" in resolve.state_schema.__annotations__

    msgs = [_h("What was Cirrus Logic's revenue in fiscal 2024?", 1), _a("$1.79B", 1),
            _h("How dependent is it on Apple?", 2)]
    out = resolve.before_agent({"messages": msgs}, None)
    assert out["resolved"]["question"] == "How dependent is it on Apple?"
    assert out["resolved"]["fiscal_year"] == 2024      # inherited from turn 1
