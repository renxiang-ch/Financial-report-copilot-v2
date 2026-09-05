"""What a follow-up carries forward from the turns before it.

The defect: turn one establishes FY2024, a later turn asks something that names
no year, and the model binds a FY2026 concentration percentage to FY2024
revenue. Every operand is real and fetched, so the groundedness check passes it.

Half of these tests are about NOT inheriting. A carry that fires too eagerly is
worse than none at all -- it overwrites a constraint the user did state, and then
reports having assumed it, which reads as diligence.
"""

from copilot.agent.conversation import (
    active_context_block,
    carried_slots,
    trim_history,
)
from copilot.agent.slots import extract_slots


def _turns(*questions: str) -> list[dict]:
    return [{"question": q, "answer": "(answered)"} for q in questions]


def _follow_up(question: str, *prior: str) -> dict:
    return extract_slots(question, carry=carried_slots(_turns(*prior)))


# ── What is inherited ────────────────────────────────────────────────────────

def test_a_year_survives_a_turn_that_never_mentioned_it():
    """The three-turn shape the defect was reported on: the year is set in turn
    one, turn two is about something else entirely, and turn three still means
    FY2024."""
    slots = _follow_up(
        "What about Cirrus Logic?",
        "What was Apple's revenue in FY2024?",
        "And its gross profit?",
    )
    assert slots["fiscal_year"] == 2024
    assert "fiscal_year" in slots["inherited"]


def test_a_follow_up_naming_a_company_replaces_the_subject():
    """"and Cirrus?" changes who is being asked about. Merging the two would
    answer about both companies when one was asked about."""
    slots = _follow_up("What about Cirrus Logic?", "How dependent is Qorvo on Apple?")
    assert slots["companies"] == ["CRUS"]
    assert "companies" not in slots["inherited"]


def test_a_follow_up_naming_nobody_keeps_the_previous_companies():
    slots = _follow_up("And its gross margin?", "What was Apple's revenue in FY2024?")
    assert slots["companies"] == ["AAPL"]
    assert slots["inherited"] == ["fiscal_year", "companies"]


# ── What is not ──────────────────────────────────────────────────────────────

def test_a_stated_year_is_never_overwritten():
    slots = _follow_up("And in FY2022?", "What was Apple's revenue in FY2024?")
    assert slots["fiscal_year"] == 2022
    assert "fiscal_year" not in slots["inherited"]


def test_a_question_about_change_does_not_inherit_a_single_year():
    """`fiscal_year` is None here because the question is about a series, not
    because nothing was found. Inheriting into it would scope a trend question to
    one year and hide the comparison it asks for."""
    slots = _follow_up("How has it changed since 2020?",
                       "What was Apple's revenue in FY2024?")
    assert slots["is_trend"] is True
    assert slots["fiscal_year"] is None
    assert "fiscal_year" not in slots["inherited"]


def test_a_question_naming_two_years_does_not_inherit_a_third():
    """Also None from `find_fiscal_year`, and for a different reason -- which is
    why the guard tests `years`, not just `fiscal_year is None`."""
    slots = _follow_up("Compare 2022 and 2023.", "What was Apple's revenue in FY2024?")
    assert slots["fiscal_year"] is None
    assert "fiscal_year" not in slots["inherited"]


def test_a_single_shot_question_inherits_nothing():
    """The property the whole frozen eval set depends on: with no history the
    prompt is byte-identical to what it was before any of this existed."""
    slots = extract_slots("What was Apple's revenue in FY2024?", carry=carried_slots([]))
    assert slots["inherited"] == []
    assert active_context_block(slots) is None


def test_only_the_questions_are_read_never_the_answers():
    """An answer states comparative figures, filing dates and ranges the user
    never asked about. Carrying a constraint out of one would let the system
    narrow a later question using something it said itself."""
    history = [{"question": "Which companies supply Apple?",
                "answer": "In FY2019 Qorvo reported 32%, and in FY2024 46%."}]
    carry = carried_slots(history)
    assert carry["fiscal_year"] is None


def test_a_constraint_from_an_evicted_turn_does_not_survive():
    """The carry is built from the turns the model can still see. A year from a
    turn that was trimmed away is one nobody in the conversation can point at."""
    long_thread = _turns("What was Apple's revenue in FY2024?",
                         *[f"Filler question number {i}?" for i in range(8)])
    kept, note = trim_history(long_thread)
    assert note["turns_evicted"] > 0
    assert carried_slots(kept)["fiscal_year"] is None


# ── The block itself ─────────────────────────────────────────────────────────

def test_the_block_names_only_what_was_actually_inherited():
    slots = _follow_up("What about Cirrus Logic?", "What was Apple's revenue in FY2024?")
    block = active_context_block(slots)
    assert "fiscal year: 2024" in block
    assert "Cirrus" not in block and "CRUS" not in block   # stated, not assumed


def test_the_block_invites_contradiction_rather_than_just_asserting():
    """A carried constraint's failure mode is being carried into a turn that
    meant something else. The only defence is making it arguable."""
    slots = _follow_up("And Cirrus?", "What was Apple's revenue in FY2024?")
    block = active_context_block(slots)
    assert "say that instead" in block
    assert "which of these you assumed" in block


# ── Routing stays out of it ──────────────────────────────────────────────────

def test_inheritance_cannot_cause_a_refusal():
    """Refusal is the router's one irreversible action, so it reads the question
    and nothing else. This follow-up would name two companies if the carry were
    merged in, and a share-of-spend refusal would then fire on a question whose
    second party the user never mentioned in it."""
    from copilot.agent.agent import route_question

    route = route_question("How much of Apple's procurement spend was that?")
    assert route["action"] != "refuse"


# ── The instrument ───────────────────────────────────────────────────────────
#
# Three of these pin decisions the first two runs of the conversation set forced,
# where the checker was wrong and the answer was right. A scoring rule that has
# produced a false negative once and has no test is a scoring rule that will
# produce it again after someone tidies it.

def test_asking_for_all_years_is_not_a_year_violation():
    """A trend query returns the required year along with the others, so nothing
    is bound to the wrong period by making it. This harness has already failed
    correct answers once for using a better tool path."""
    from copilot.eval.harness import _check_tool_years

    got = _check_tool_years(
        [{"tool": "graph_query", "input": {"fiscal_year": "trend"}}], [2024])
    assert got["ok"] is True
    assert got["supersets"] and not got["violations"]


def test_falling_back_to_latest_is_a_year_violation():
    """"Latest" for Cirrus Logic is FY2026. In a FY2024 thread that returns a
    real figure from the wrong year, which is the defect itself."""
    from copilot.eval.harness import _check_tool_years

    for dropped in ({"fiscal_year": "latest"}, {}):
        got = _check_tool_years([{"tool": "query_financials", "input": dropped}], [2024])
        assert got["ok"] is False and got["violations"]


def test_only_the_number_fetching_tools_are_checked():
    from copilot.eval.harness import _check_tool_years

    steps = [{"tool": "retrieve_text", "input": {"fiscal_year": 2019}},
             {"tool": "query_financials", "input": {"fiscal_year": 2024}}]
    assert _check_tool_years(steps, [2024])["ok"] is True


def test_context_from_other_years_is_not_fabrication():
    """The false negative that cost a correct answer its mark: an answer led with
    FY2024 and added the neighbouring years, all real, all cited, all from the
    same tool result."""
    from copilot.eval.harness import _score_grounded

    item = {"id": "x", "tier": 3, "golden_fact_keywords": [["87%"]],
            "allowed_pct_values": [83, 87, 89, 91], "leading_pct": 87,
            "require_citation": False}
    answer = ("Cirrus Logic was 87% dependent on Apple in FY2024. For context: "
              "FY2025 89%, FY2026 91%, FY2023 83%.")
    assert _score_grounded(item, answer)["correct"] is True


def test_leading_with_the_wrong_year_is_caught():
    """The hole widening allowed_pct_values would otherwise open: every figure
    real, the right one present, and the answer still about the wrong year."""
    from copilot.eval.harness import _score_grounded

    item = {"id": "x", "tier": 3, "golden_fact_keywords": [["87%"]],
            "allowed_pct_values": [83, 87, 89, 91], "leading_pct": 87,
            "require_citation": False}
    answer = ("Cirrus Logic was 91% dependent on Apple in FY2026, up from 87% "
              "in FY2024.")
    scored = _score_grounded(item, answer)
    assert scored["correct"] is False
    assert scored["facts_ok"] is True and scored["pct_ok"] is True   # both blind to it
    assert scored["lead_ok"] is False


def test_an_item_without_leading_pct_is_unaffected():
    """Every frozen item is one of these. The check has to be invisible to them."""
    from copilot.eval.harness import _score_grounded

    item = {"id": "x", "tier": 3, "golden_fact_keywords": [["87%"]],
            "require_citation": False}
    assert _score_grounded(item, "Somewhere around 91%, and 87% before that.")["correct"] is True


# ── Filling a party the sentence points at but does not name ─────────────────
#
# "How dependent is it on Apple?" needs two parties for any direction rule to
# fire and names one. Merging the carried list in is the obvious move and the
# wrong one: every rule in `relation` is positional, so appending CRUS after
# AAPL reads as Apple depending on Cirrus Logic. The pronoun's POSITION is the
# information, so the company goes into that position and the sentence is parsed
# again.

def test_a_pronoun_takes_the_carried_company_and_keeps_the_direction():
    slots = _follow_up("How dependent is it on Apple?",
                       "What was Cirrus Logic's revenue in fiscal 2024?")
    assert slots["resolved_question"] == "How dependent is CRUS on Apple?"
    assert slots["relation_supplier"] == "CRUS"      # not AAPL
    assert slots["relation_customer"] == "AAPL"
    assert "companies" in slots["inherited"]


def test_a_possessive_pronoun_is_substituted_as_a_possessive():
    slots = _follow_up("What share of its revenue comes from Apple?",
                       "How dependent is Cirrus Logic on Samsung?")
    assert slots["resolved_question"] == "What share of CRUS's revenue comes from Apple?"
    assert slots["relation_supplier"] == "CRUS"


def test_a_pronoun_that_does_not_stand_for_a_company_is_discarded_whole():
    """"it" here is the revenue. Substituting a company produces no relation
    either way, so the rewrite is thrown away rather than left behind as a parse
    of a sentence nobody wrote."""
    slots = _follow_up("How much of it comes from Apple?",
                       "What was Cirrus Logic's revenue in fiscal 2024?")
    assert slots["resolved_question"] is None
    assert slots["relation_side"] is None


def test_a_sentence_that_already_parses_is_never_rewritten():
    slots = _follow_up("How dependent is Qorvo on Apple?",
                       "What was Cirrus Logic's revenue in fiscal 2024?")
    assert slots["resolved_question"] is None
    assert slots["relation_supplier"] == "QRVO"


def test_an_ambiguous_pronoun_is_resolved_by_not_resolving_it():
    """Two carried companies are still in play, so which one "it" means is a
    guess. Guessing here would put a company the reader never mentioned into a
    routing decision.

    The counterparty has to be a company this database knows, or the follow-up
    names nobody and takes the whole carried set through the other branch --
    which is what the first version of this test did, passing without ever
    reaching the code it names. Samsung is a customer in supply_edges and not a
    row in `companies`, so it is invisible to find_companies.
    """
    carry = carried_slots(_turns("Compare Cirrus Logic and Qorvo in fiscal 2024."))
    assert carry["companies"] == ["CRUS", "QRVO"]

    slots = extract_slots("How dependent is it on Apple?", carry=carry)
    assert slots["companies"] == ["AAPL"]          # the pronoun branch was reached
    assert slots["resolved_question"] is None      # and declined to guess
    assert "companies" not in slots["inherited"]


def test_a_carried_company_can_force_a_tool_but_never_a_refusal():
    """The same sentence refuses when both parties are named in it. Supplied by
    an earlier turn, it falls back to the permissive path -- a wrong forced call
    costs a round the model recovers from, a wrong refusal returns nothing and
    the reader cannot tell which earlier turn caused it."""
    from copilot.agent.agent import route_question

    carry = carried_slots(_turns("How dependent is Qorvo on Apple in fiscal 2024?"))
    with_pronoun = "What share of Apple's procurement spend does it represent?"
    both_named = "What share of Apple's procurement spend does Qorvo represent?"

    assert route_question(with_pronoun, carry=carry)["action"] == "auto"
    assert route_question(both_named)["action"] == "refuse"


def test_routing_a_single_shot_question_is_unchanged_by_the_carry_parameter():
    """Every frozen item runs this way. carry=None must reach byte-identical
    decisions or the whole zero-regression argument is a hope."""
    from copilot.agent.agent import route_question

    for q in ("How dependent is Cirrus Logic on Apple?",
              "What share of Apple's procurement spend goes to Qorvo?",
              "What was Apple's revenue in FY2024?"):
        assert route_question(q) == route_question(q, carry=None)
