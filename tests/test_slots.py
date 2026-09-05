"""Unit tests for question slot extraction.

Every case here is one the extractor got wrong at some point during the two
hours it took to write, kept so that getting it right again is not accidental.
The negative cases matter more than the positive ones: an extractor that claims
a supply-chain relationship in every question would pass all the positive tests
while making routing worse than the regex it replaces.

These touch the database for the company index, which is the point -- a
hardcoded company list is the defect this module exists to avoid repeating.
"""

from copilot.agent.slots import extract_slots, find_companies, find_fiscal_year, relation


def side(question: str) -> str | None:
    return relation(question)["side"]


# ── Companies ────────────────────────────────────────────────────────────────

def test_informal_names_resolve_to_tickers():
    assert find_companies("How dependent is Cirrus Logic on Apple?") == ["CRUS", "AAPL"]


def test_order_of_appearance_is_preserved():
    """Direction rules read positionally, so order is load-bearing, not cosmetic."""
    assert find_companies("Apple and Qorvo") == ["AAPL", "QRVO"]
    assert find_companies("Qorvo and Apple") == ["QRVO", "AAPL"]


def test_the_word_on_is_not_the_ticker_ON():
    """ON is a real company in this database and also the commonest preposition
    in these questions. Matching it as a word would put a spurious second party
    into half the dependency questions."""
    assert find_companies("How dependent is Cirrus Logic on Apple?").count("ON") == 0


# ── Fiscal year ──────────────────────────────────────────────────────────────

def test_a_stated_year_is_found_in_every_notation():
    for phrasing in ("in fiscal year 2024", "in FY2024", "in fiscal 2024", "in 2024"):
        assert find_fiscal_year(f"What was Apple's revenue {phrasing}?") == 2024


def test_a_trend_question_yields_no_year_even_when_one_is_named():
    """Scoping retrieval to a single year would hide the comparison the question
    asks for, so None is the correct parse, not a failed one."""
    assert find_fiscal_year("How has Cirrus Logic's revenue grown since 2020?") is None
    assert find_fiscal_year("Apple's revenue trend in 2024 and after") is None


def test_two_years_yield_no_single_year():
    assert find_fiscal_year("Apple revenue from 2022 to 2024") is None


# ── Relationship direction: the slot the router never had ────────────────────

def test_a_share_of_a_suppliers_revenue_is_supplier_side():
    r = relation("What percentage of Qorvo's revenue came from Apple in fiscal 2024?")
    assert (r["side"], r["supplier"], r["customer"]) == ("supplier", "QRVO", "AAPL")


def test_a_share_of_a_customers_spend_is_customer_side():
    r = relation("What fraction of Apple's total supplier spend is Qorvo?")
    assert (r["side"], r["customer"], r["supplier"]) == ("customer", "AAPL", "QRVO")


def test_a_buying_verb_settles_the_direction_without_a_denominator():
    """No share word, no percentage -- but no filing reports it either way."""
    assert side("How much does Apple buy from Qorvo?") == "customer"


def test_cost_of_goods_sold_is_not_a_selling_verb():
    """'sold' inside a line item read as an act of selling, which flipped a
    procurement question into a supply question."""
    assert side("What share of Apple's cost of goods sold is attributable to "
                "Cirrus Logic?") == "customer"


def test_role_nouns_settle_the_direction_outright():
    assert side("Is Apple a significant customer of Corning?") == "supplier"
    assert side("Which of Apple's suppliers is most at risk?") == "supplier"


def test_the_word_purchasing_does_not_make_a_question_customer_side():
    """Three of the regex router's four false refusals were this: a supplier-side
    question containing a procurement word. The direction is set by whose
    denominator is named, not by which words are present."""
    assert side("How much of Jabil's revenue comes from its purchasing "
                "customers?") == "supplier"
    assert side("Which of Apple's suppliers is most at risk if procurement "
                "spending goes to competitors?") == "supplier"


# ── Relationships that must NOT be claimed ───────────────────────────────────

def test_a_plain_lookup_is_not_a_relationship():
    assert side("What was Apple's revenue in fiscal year 2024?") is None


def test_a_share_of_revenue_from_a_non_company_is_not_a_relationship():
    """This one is in the frozen refusal set. It names a company, a revenue
    noun, a share word and the word 'from' -- everything a dependency question
    has except a counterparty -- and routing it to the graph would answer a
    geography question out of a supply-chain table."""
    assert side("What percentage of Apple's revenue came from China in 2023?") is None


def test_year_over_year_growth_is_not_a_relationship():
    assert side("What was Apple's revenue growth percentage from 2023 to 2024?") is None


def test_one_company_asking_about_its_own_purchasing_is_not_a_share_question():
    assert side("Does Corning disclose where its raw material purchasing "
                "comes from?") is None


# ── Carrying constraints between turns ───────────────────────────────────────

def test_a_follow_up_inherits_the_year_it_did_not_restate():
    first = extract_slots("What was Cirrus Logic's revenue in fiscal year 2024?")
    second = extract_slots("And Qorvo?", carry=first)
    assert second["fiscal_year"] == 2024
    assert "fiscal_year" in second["inherited"]
    assert second["companies"] == ["QRVO"]


def test_a_restated_year_is_not_overwritten_by_the_carried_one():
    first = extract_slots("What was Cirrus Logic's revenue in fiscal year 2024?")
    second = extract_slots("What about 2022?", carry=first)
    assert second["fiscal_year"] == 2022
    assert "fiscal_year" not in second["inherited"]


def test_what_was_inherited_is_reported():
    """A carried constraint the user never restated has to be visible, or the
    answer silently assumes a year and the questioner cannot tell which."""
    first = extract_slots("Apple's net income in fiscal 2024?")
    second = extract_slots("And Qorvo?", carry=first)
    assert set(second["inherited"]) == {"fiscal_year", "metric"}
    assert second["metric"] == "NetIncome"


# ── The rejection channel ────────────────────────────────────────────────────
#
# A fixed set of slots is not the danger; a fixed set of slots that cannot
# report what fell outside them is. Measured on this project's own tool
# boundary: `query_financials` has no argument for a segment or geography, the
# qualifier reached the tool 0.0% of the time, and the reduced query came back
# confidently answered. Adding an argument that could REJECT fixed most of it;
# adding one that merely made the model state "consolidated" nearly doubled
# fabrications. The rejection channel is the mechanism, not the extra field.

def test_a_scope_qualifier_is_reported_not_swallowed():
    """The possessive pattern steps over up to two words to survive padding,
    and a scope qualifier hides in exactly that gap."""
    assert extract_slots("What fraction of Apple's Americas segment supplier "
                         "spend went to Qorvo?")["unaccounted"] == ["americas", "segment"]
    assert extract_slots("What percentage of Cirrus Logic's automotive segment "
                         "revenue came from Apple?")["unaccounted"] == ["automotive",
                                                                        "segment"]


def test_padding_is_not_residue():
    """'total' and 'net' do not narrow what is asked for. Crying wolf on them
    would push every consumer onto the permissive path and the channel would
    stop meaning anything."""
    assert extract_slots("What fraction of Apple's total supplier spend went to "
                         "Qorvo in fiscal year 2024?")["unaccounted"] == []
    assert extract_slots("What percentage of Qorvo's net revenue came from "
                         "Apple?")["unaccounted"] == []


def test_something_another_slot_holds_is_not_residue():
    """The year in "Qorvo's FY2024 revenue" sits in the gap but is not lost --
    fiscal_year carries it. Residue means noticed-and-unrepresented."""
    s = extract_slots("If Apple reduced orders by 20% in fiscal year 2024, what "
                      "would be the estimated dollar revenue impact on Qorvo? "
                      "Use Qorvo's FY2024 revenue and its disclosed Apple "
                      "concentration from its 10-K.")
    assert s["fiscal_year"] == 2024
    assert s["unaccounted"] == []


def test_a_different_quantity_wearing_a_known_noun_is_reported():
    """"days sales outstanding" is not sales. The revenue pattern matches the
    word in the middle of it, and the residue is what says so."""
    assert extract_slots("What was Apple's days sales outstanding in fiscal "
                         "year 2024?")["unaccounted"] == ["days"]


def test_the_frozen_sets_raise_no_false_alarms():
    """Measured: 1 flag across the 87 questions in five sets and the router
    probe, and it is the true positive above. A channel that fired on ordinary
    questions would make backing off the default, which is the same as having
    no channel."""
    for q in ("What was Apple's revenue in fiscal year 2024?",
              "What percentage of Apple's procurement budget goes to Qorvo?",
              "What share of Apple's supply chain spending goes to Skyworks?",
              "How dependent is Cirrus Logic on Apple?"):
        assert extract_slots(q)["unaccounted"] == [], q
