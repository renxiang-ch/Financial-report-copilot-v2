"""Conversation history policy for multi-turn /ask.

Three decisions are made here rather than left implicit in the agent loop,
because each trades something real away and the trade should be visible.

**1. A turn stores the question and the final answer, not the tool traffic.**
An answered question can carry ten tool results, and retrieve_text alone returns
five passages of ~500 tokens. Replaying that into every later turn would spend
the whole context budget on evidence the model already summarised. What is kept
is the answer text, which the system prompt requires to carry its figures and
accession numbers -- so the numbers survive even though the raw rows do not.
The cost is real: a follow-up needing a figure the previous answer did not print
cannot recover it and must re-query. That is the intended failure mode, and it
fails loudly (a fresh tool call) rather than quietly (a hallucinated recall).

**2. History is append-only. Nothing is summarised or rewritten.**
Prompt caching matches on a prefix. Rewriting turn 2 while composing turn 5
invalidates everything after it, so a summarisation policy quietly pays a cache
miss on every turn it touches. Appending keeps the prefix stable and growing,
which is the case caching is best at.

**3. Eviction is oldest-first, and it is the cache-invalidating event.**
When the budget is exceeded the oldest turns are dropped. This does move the
prefix -- the static system-plus-tools block still matches, but every history
token after it shifts. Eviction is therefore reported in the policy note so the
cost shows up in measurement instead of hiding. The alternative, never evicting,
just relocates the problem to an unbounded and increasingly expensive prompt.

**4. Constraints a follow-up leaves unstated are re-derived, not remembered.**
The previous turns' questions are re-parsed on every request rather than stored,
so the API keeps no session state and a client can replay a thread against a
fresh process and get the same reading. Only the QUESTIONS are parsed. An answer
is full of numbers and years the user never asked for -- a comparative figure, a
filing date, a range in a caveat -- and folding those into a constraint carried
into later turns would let the system narrow a question using something it said
itself rather than something it was told.

The carry is derived from the turns that SURVIVED trimming. A constraint from an
evicted turn would be one the model cannot see the origin of and the user cannot
scroll back to, which is the same silent narrowing this file exists to avoid.
"""

MAX_TURNS = 6
HISTORY_TOKEN_BUDGET = 3000
_CHARS_PER_TOKEN = 4  # rough; only used for budgeting, never for billing


def _approx_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def trim_history(history: list[dict] | None) -> tuple[list[dict], dict]:
    """Apply the policy above. Returns (kept turns, note describing what happened)."""
    turns = [
        {"question": t.get("question", ""), "answer": t.get("answer", "")}
        for t in (history or [])
        if t.get("question") and t.get("answer")
    ]
    received = len(turns)

    kept: list[dict] = []
    used = 0
    # Walk newest-first so the budget is spent on the turns a follow-up is most
    # likely to refer to, then restore chronological order.
    for turn in reversed(turns):
        cost = _approx_tokens(turn["question"]) + _approx_tokens(turn["answer"])
        if len(kept) >= MAX_TURNS or (kept and used + cost > HISTORY_TOKEN_BUDGET):
            break
        kept.append(turn)
        used += cost
    kept.reverse()

    evicted = received - len(kept)
    return kept, {
        "turns_received":     received,
        "turns_kept":         len(kept),
        "turns_evicted":      evicted,
        "history_tokens_est": used,
        # True whenever the prefix moved, which is what a cache-hit rate should be
        # read against -- a drop on an eviction turn is expected, not a regression.
        "prefix_invalidated": evicted > 0,
    }


def to_openai_messages(history: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    for turn in history:
        msgs.append({"role": "user", "content": turn["question"]})
        msgs.append({"role": "assistant", "content": turn["answer"]})
    return msgs


def append_turn(history: list[dict] | None, question: str, answer: str) -> list[dict]:
    return list(history or []) + [{"question": question, "answer": answer}]


# ── What a follow-up inherits ────────────────────────────────────────────────
#
# The defect this closes, measured: turn one establishes FY2024, a later turn
# asks a follow-up that names no year, and the model binds a FY2026
# concentration percentage to FY2024 revenue. Every operand is real and fetched,
# so the groundedness check passes it; the binding check added the day before
# flags it, which is detection, not prevention. This is the prevention.
#
# Nothing here reaches the model as a free-text hint. It is a stated assumption
# with an instruction to contradict it if it is wrong -- because the failure mode
# of a carried constraint is that it is carried into a turn that meant something
# else, and the only defence against that is making it visible enough to argue
# with.


def carried_slots(history: list[dict] | None) -> dict | None:
    """Fold the kept turns' questions into the constraints they leave standing.

    Folded rather than read off the last turn alone, so a constraint set in turn
    one survives a turn two that did not mention it: "Apple's FY2024 revenue" ->
    "and its gross profit?" -> "what about Cirrus?" still means FY2024.
    """
    from copilot.agent.slots import extract_slots

    carry = None
    for turn in history or []:
        question = (turn.get("question") or "").strip()
        if not question:
            continue
        carry = extract_slots(question, carry=carry)
    return carry


_FIELD_LABELS = {
    "fiscal_year": "fiscal year",
    "metric":      "metric",
    "companies":   "companies",
}


def active_context_block(slots: dict) -> str | None:
    """The assumption block for a turn that inherited something, or None.

    None for anything that inherited nothing -- which is every single-shot
    question, so the prompt sent for the entire frozen eval set is byte-identical
    to what it was before any of this existed. That is deliberate: a change that
    cannot move a regression number does not need a regression argument.

    It is appended AFTER the history and before the current question, so the
    cacheable prefix (system prompt, tool schemas, every prior turn) is untouched
    and the block lands in the part of the prompt that was going to be new anyway.
    """
    inherited = slots.get("inherited") or []
    if not inherited:
        return None

    lines = []
    for field in inherited:
        value = slots.get(field)
        if field == "companies":
            value = ", ".join(value or [])
        lines.append(f"- {_FIELD_LABELS.get(field, field)}: {value}")

    return (
        "Active context carried from earlier turns. This question did not "
        "restate the following, so they are assumed to still hold:\n"
        + "\n".join(lines)
        + "\n\nPass these exact values to every tool call that needs them -- do "
          "not substitute a different year or company, and do not fall back to "
          "the latest available one. Say in your answer which of these you "
          "assumed. If the question actually means something else, say that "
          "instead of answering for one of these."
    )
