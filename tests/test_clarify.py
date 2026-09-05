"""When the question goes back to the reader.

The condition is narrow on purpose: more than one reading, and every reading
answerable. Most of these tests are about the second clause, because that is
what separates a useful question from a system that makes the reader do its
work. Three of this project's other "I'm not sure" signals are represented here
as cases that must NOT ask, each for a different reason.
"""

from copilot.agent.clarify import clarification_for
from copilot.agent.conversation import carried_slots
from copilot.agent.slots import extract_slots


def _clarify(question: str, *prior: str) -> dict | None:
    carry = carried_slots([{"question": q, "answer": "(answered)"} for q in prior])
    return clarification_for(question, extract_slots(question, carry=carry), carry)


# ── Asks ─────────────────────────────────────────────────────────────────────

def test_a_metric_family_offers_its_members():
    """"Profit" is three different stored figures. The system currently picks one
    without saying it picked."""
    got = _clarify("What was Apple's profit in fiscal 2024?")
    assert [o["label"] for o in got["options"]] == \
        ["Gross profit", "Operating income", "Net income"]


def test_every_option_is_the_question_asked_a_different_way():
    """Options carry sentences, not codes. Choosing one is indistinguishable from
    having asked it that way, so the thread stays readable and the rewrite can be
    checked by the person who picked it."""
    got = _clarify("What was Apple's profit in fiscal 2024?")
    assert got["options"][0]["rewrite"] == "What was Apple's gross profit in fiscal 2024?"
    assert got["options"][2]["rewrite"] == "What was Apple's net income in fiscal 2024?"


def test_an_ambiguous_pronoun_offers_the_companies_still_open():
    got = _clarify("How dependent is it on Apple?",
                   "Compare Cirrus Logic and Qorvo in fiscal 2024.")
    assert [o["label"] for o in got["options"]] == ["CRUS", "QRVO"]
    assert got["options"][0]["rewrite"] == "How dependent is CRUS on Apple?"


def test_there_is_always_a_way_out_of_the_menu():
    """Two readings the system thought of do not exhaust what the reader meant.
    Without this the menu is a smaller cage than the question was."""
    assert _clarify("What was Apple's profit in fiscal 2024?")["allow_other"] is True


# ── Stays quiet ──────────────────────────────────────────────────────────────

def test_a_family_word_inside_a_resolved_phrase_is_not_ambiguous():
    for question in ("What was Apple's gross profit in fiscal 2024?",
                     "What was Apple's net margin in fiscal 2024?",
                     "What was Apple's diluted EPS in 2024?"):
        assert _clarify(question) is None


def test_a_family_word_nobody_owns_is_a_mention_not_a_request():
    """Wants a passage, not a menu of three numbers. The guard is structural --
    no company possesses the word -- rather than a list of qualitative verbs."""
    assert _clarify("What does Skyworks say about profit margins?") is None


def test_a_qualifier_with_no_answerable_reading_is_not_asked_about():
    """This database holds no segment splits, so every option would be a lie
    about what exists. Measured elsewhere in this project: giving the model a way
    to state "consolidated" instead of a way to refuse nearly doubled
    fabrication. A menu is a stronger version of that same mistake."""
    assert _clarify("What was Apple's Americas segment revenue in 2024?") is None


def test_a_typo_is_not_the_readers_problem():
    """One right reading, and the tool already hands the model the correction.
    Asking the reader to confirm their own typo is handing them the system's
    work."""
    assert _clarify("What was Skyworks' SKWS revenue in 2024?") is None


def test_a_pronoun_with_one_candidate_resolves_instead_of_asking():
    assert _clarify("How dependent is it on Apple?",
                    "What was Cirrus Logic's revenue in fiscal 2024?") is None


def test_an_unambiguous_question_is_never_interrupted():
    for question in ("What was Apple's revenue in fiscal 2024?",
                     "Which companies supply Apple in FY2024?",
                     "How dependent is Cirrus Logic on Apple?"):
        assert _clarify(question) is None


# ── The round trip ───────────────────────────────────────────────────────────

def test_a_handed_back_question_costs_nothing_and_is_not_a_turn():
    """No model is called, so no tokens. And nothing was answered, so the thread
    does not record a turn -- the rewrite arrives as a fresh question against the
    same history, which is why the options carry sentences."""
    from copilot.agent.agent import ask

    result = ask("What was Apple's profit in fiscal 2024?")
    assert result["needs_clarification"] is True
    assert result["usage"]["input_tokens"] == 0
    assert result["history"] == []
    assert result["answer"]          # a readable form for clients with no buttons


def test_an_unanswerable_question_is_refused_not_offered_a_menu():
    """Refusal comes first. A question no reading can answer is answered by
    saying so, not by a menu of readings that all fail."""
    from copilot.agent.agent import ask

    result = ask("What share of Apple's procurement spend goes to Qorvo?")
    assert result.get("needs_clarification") is not True
    assert "cannot determine" in result["answer"].lower()
