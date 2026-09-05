"""When to hand the question back instead of answering it.

Asking is not free and the cost lands on the wrong side. A refusal that fires
wrongly costs the reader everything; a clarification that fires wrongly costs
them a round-trip they did not need, and a system that does it often is worse
than one that guesses well. So this fires on a narrow, checkable condition:
**the question has more than one reading and every reading is answerable.**

That last clause is what keeps this from becoming a general "I'm not sure"
channel. The parser already produces four other signals of unease, and none of
them belong here:

    an unknown ticker (SKWS)        -- one right reading. The model retries.
                                       Asking the reader to confirm their own
                                       typo is handing them the system's work.
    a ticker on the wrong side      -- one right reading. The tool says which.
    a segment or geography qualifier-- NO reading is answerable; this database
                                       holds no segment splits. Offering a menu
                                       there implies the data exists. Measured:
                                       giving the model a way to state
                                       "consolidated" instead of a way to refuse
                                       nearly doubled fabrication.
    no fiscal year stated           -- one reading is much more likely than the
                                       rest, and it is already stated in the
                                       answer ("searched the FY2025 filing").

Two conditions qualify, and both are decided without a model:

    an ambiguous pronoun    "How dependent is it on Apple?" with two companies
                            still live in the thread. Both readings are real
                            questions with real answers.
    an ambiguous metric     "Apple's profit in 2024" -- gross, operating and net
                            are three different figures, all three stored, and
                            nothing in the question chooses.

Every option carries a rewritten question rather than a code, so choosing one is
the same as having asked it that way, and the thread keeps working in ordinary
sentences that a reader can check.
"""

import re

from copilot.agent.slots import (
    _ANAPHOR_RE,
    _POSSESSIVE_ANAPHORS,
    _possessed,
    anaphor_candidates,
    find_metric,
)

# ── Metric families ──────────────────────────────────────────────────────────
#
# A head-word that names a family rather than a member. The third element of
# each option is the stored label it resolves to, which nothing here reads --
# it is there so a test can assert this table against the database and fail when
# a label is renamed. A written-down list that drifts from the data is this
# project's most repeated defect; this one is pinned.
_METRIC_FAMILIES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "profit": (("Gross profit",     "gross profit",     "GrossProfit"),
               ("Operating income", "operating income", "OperatingIncome"),
               ("Net income",       "net income",       "NetIncome")),
    "margin": (("Gross margin",     "gross margin",     "GrossProfit"),
               ("Operating margin", "operating margin", "OperatingIncome"),
               ("Net margin",       "net margin",       "NetIncome")),
    "income": (("Operating income", "operating income", "OperatingIncome"),
               ("Net income",       "net income",       "NetIncome")),
    "eps":    (("Diluted EPS",      "diluted EPS",      "EPS_Diluted"),
               ("Basic EPS",        "basic EPS",        "EPS_Basic")),
}


def _ambiguous_metric(question: str) -> tuple[str, tuple, str] | None:
    """(head-word, options, the matched text) for a question that names a family.

    Two guards, and the second is the one doing the work:

    * `find_metric` must have failed. Every phrase that resolves -- "gross
      margin", "net income", "diluted EPS" -- is already a member, and the
      head-word sitting inside it is not evidence of anything.
    * the family word must be POSSESSED by a company: "Apple's profit", "the
      margin of Cirrus Logic". That is the difference between asking for a
      figure and mentioning one. "What does Skyworks say about profit margins?"
      wants a passage, not a menu of three numbers, and it has no possessor.
    """
    if find_metric(question or ""):
        return None
    for head, options in _METRIC_FAMILIES.items():
        pattern = rf"{head}s?"
        owner, _ = _possessed(question or "", pattern)
        if not owner:
            continue
        match = re.search(pattern, question or "", re.IGNORECASE)
        if match:
            return head, options, match.group(0)
    return None


def _rewrite_metric(question: str, matched: str, replacement: str) -> str:
    """Swap the family word for the member, once, keeping the rest verbatim."""
    return re.sub(re.escape(matched), replacement, question or "", count=1,
                  flags=re.IGNORECASE)


def _rewrite_pronoun(question: str, ticker: str) -> str:
    match = _ANAPHOR_RE.search(question or "")
    if not match:
        return question
    word = match.group(1).lower()
    replacement = f"{ticker}'s" if word in _POSSESSIVE_ANAPHORS else ticker
    return question[:match.start()] + replacement + question[match.end():]


def clarification_for(question: str, slots: dict, carry: dict | None) -> dict | None:
    """The question to put back to the reader, or None to go ahead and answer.

    Returns {"reason", "question", "options": [{"label", "rewrite"}],
             "allow_other": True}.

    One at a time. A question that is ambiguous twice over gets asked about once
    and may come back ambiguous again -- which is honest, and better than a form
    with two dropdowns on it.
    """
    named = slots.get("companies") or []

    # A pronoun with more than one live referent. `slots.resolve_anaphor` already
    # declined to guess here; this is the same computation, kept instead of
    # discarded.
    if not slots.get("resolved_question") and carry:
        candidates = anaphor_candidates(question, carry.get("companies") or [], named)
        if len(candidates) > 1 and _ANAPHOR_RE.search(question or ""):
            return {
                "reason": f"\"{_ANAPHOR_RE.search(question).group(1)}\" could mean "
                          f"{' or '.join(candidates)} -- both are still open in this thread.",
                "question": "Which company do you mean?",
                "options": [{"label": t, "rewrite": _rewrite_pronoun(question, t)}
                            for t in candidates],
                "allow_other": True,
            }

    found = _ambiguous_metric(question)
    if found:
        head, options, matched = found
        return {
            "reason": f"\"{matched}\" names a family, not a figure: "
                      f"{', '.join(o[0].lower() for o in options)} are different numbers.",
            "question": "Which figure do you mean?",
            "options": [{"label": label,
                         "rewrite": _rewrite_metric(question, matched, phrase)}
                        for label, phrase, _ in options],
            "allow_other": True,
        }

    return None


def as_text(clarification: dict) -> str:
    """A plain-text form, for callers with no buttons to render.

    The API is not only consumed by the app, and a client that ignores the
    structured field should still receive something a person can act on rather
    than an empty answer.
    """
    lines = [clarification["reason"], "", clarification["question"]]
    lines += [f"  - {o['label']}: {o['rewrite']}" for o in clarification["options"]]
    lines.append("  - Or rephrase the question yourself.")
    return "\n".join(lines)
