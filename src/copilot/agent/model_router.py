"""Which model answers a question.

Kept apart from the question router in agent.py because they answer different
questions: that one picks a *tool*, this one picks a *model*.

Why there is no automatic model tiering
---------------------------------------
An earlier version routed by question category to a cheap or a strong tier, and
escalated to the strong tier when the cheap one left evidence of having failed.
Both halves were removed, for measured reasons rather than taste.

*Tiering* was removed because the measurement did not support it. Running the
33-question frozen set on gpt-4o and gpt-4o-mini gave identical scores on every
deterministic metric -- Tier-1, Tier-2, input fetch and refusal all 100% on both
-- while gpt-4o cost $0.4875 against $0.0304, sixteen times more for no change in
any number. The honest reading is narrower than "the models are equivalent": this
eval set is saturated, so it has no power to resolve a difference. Either way it
gives no basis for deciding per question which tier to spend on, and shipping a
tiering table that all categories happen to fall through is dead configuration --
the same drift-from-data defect this codebase keeps finding elsewhere.

*Escalation* was removed after checking what agent frameworks actually do at this
step. LangChain's ModelFallbackMiddleware, LangGraph's tool retry, LiteLLM's
fallbacks and the OpenAI Agents SDK all switch models on **exceptions** -- 429s,
5xx, timeouts, context-window overruns -- and never on answer quality. Quality
problems get a different response everywhere: Pydantic AI feeds the validation
failure back to the *same* model via ModelRetry, and the OpenAI Agents SDK trips
a guardrail and halts. LangChain's rubric middleware even runs the grader on a
*cheaper* model than the one being graded, on the reasoning that judging is
easier than producing. Nobody maps "this answer looks wrong" to "pay more".

That consensus is well-founded. A stronger model is a fix for a model that could
not do the task; it is not a fix for a fabricated figure, because a stronger
model fabricates more fluently. What this system does instead is verify: see
grounding.py, which checks every stated number against the tool trace and labels
what it cannot source. Detection is the part worth building; escalation was a
guess dressed as a policy.

What remains is the part with a real user behind it: let the caller choose.
"""

# Any model id the configured OpenAI-compatible endpoint serves. Kept as a
# constant rather than read per call so a deployment pointed at a different
# aggregator has exactly one place to change.
DEFAULT_MODEL = "gpt-4o-mini"


def select_model(requested: str | None = None) -> tuple[str, str]:
    """Return (model_id, human-readable reason).

    An explicit choice is an instruction, not a hint: nothing overrides it.
    """
    if requested:
        return requested, "caller specified this model explicitly"
    return DEFAULT_MODEL, "default model"
