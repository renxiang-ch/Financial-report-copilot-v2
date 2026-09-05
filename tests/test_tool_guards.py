"""What a tool says when the model called it wrong.

Both guards here answer the same question: is this empty result a fact about the
filings, or a mistake in the call? Getting that wrong produces the worst output
this system can make -- a confident finding of absence, in the system's own
honest-refusal register, from a typo or a swapped argument.

Each exists because it happened. Neither had a test until now, which for a guard
whose whole job is to be there on a rare path is most of the problem.
"""

from copilot.agent.tools import graph_query, query_financials

# ── A ticker that does not exist ─────────────────────────────────────────────

def test_a_misspelt_ticker_is_a_typo_not_an_absence():
    """The model wrote SKWS, got nothing back, and reported that the database
    holds no supply-chain data for Skyworks."""
    result = graph_query(supplier="SKWS")
    assert result["found"] is False
    assert result["recoverable"] is True
    assert "SWKS" in result["did_you_mean"]


def test_a_company_name_resolves_rather_than_being_refused():
    """difflib cannot get from "Skyworks Solutions" to SWKS; the company index
    can. Resolving to the ticker matters more than passing the string through --
    a literal name reaches SQL and fails one step later, where the error is
    harder to read."""
    result = graph_query(supplier="Skyworks Solutions")
    assert result["found"] is True
    assert all(e["supplier"] == "SWKS" for e in result["edges"])


def test_a_ticker_that_really_is_absent_gets_no_suggestion():
    result = query_financials("NVDA", "Revenue", 2024)
    assert result["found"] is False
    assert not result.get("did_you_mean")


# ── A ticker on the wrong side of the relationship ───────────────────────────

def test_the_wrong_side_of_an_edge_is_reported_as_the_wrong_side():
    """Asked how dependent Cirrus Logic is on Apple, the model queried
    customer='CRUS' -- right company, wrong side -- and answered "I cannot find
    any supplier exposure data for Cirrus Logic." One run in five."""
    result = graph_query(customer="CRUS")
    assert result["found"] is False
    assert result["recoverable"] is True
    assert result["edges_if_reversed"] > 0
    assert "supplier='CRUS'" in result["hint"]


def test_the_explanation_matches_the_direction_it_explains():
    """One template served both directions and told Apple -- which files no
    concentration disclosure naming its own customers -- that it "discloses who
    ITS customers are". A confidently wrong hint is worse than the bare
    found:false it replaced."""
    reversed_to_customer = graph_query(supplier="AAPL")
    assert "does not file these disclosures itself" in reversed_to_customer["hint"]
    assert "customer='AAPL'" in reversed_to_customer["hint"]

    reversed_to_supplier = graph_query(customer="CRUS")
    assert "files these disclosures" in reversed_to_supplier["hint"]


def test_a_pair_with_no_edge_is_a_finding_not_an_error():
    """With both sides given, empty means that pair has no disclosed edge. That
    is a real answer, and dressing it as a recoverable mistake would push the
    model to retry a query that is already correct."""
    result = graph_query(customer="AAPL", supplier="GLW")
    assert result["found"] is False
    assert "recoverable" not in result
    assert "hint" not in result


# ── A capability the schema advertised and could not deliver ─────────────────

def test_a_quarterly_request_is_declined_rather_than_answered_wrongly():
    """The 10-Q rows are year-to-date, not discrete quarters, and nothing
    selected a quarter -- so asking for Apple FY2024 quarterly revenue returned
    296,105M, which is neither a quarter nor the year, with no field saying what
    period it covered."""
    result = query_financials("AAPL", "Revenue", 2024, form="10-Q")
    assert result["found"] is False
    assert result["recoverable"] is False
    assert "year-to-date" in result["hint"]


def test_the_annual_path_is_untouched():
    assert query_financials("AAPL", "Revenue", 2024)["value"] == 391035000000.0


def test_the_schema_no_longer_offers_what_the_tool_declines():
    """The drift this closes: list_metrics had been narrowed to 10-K with a
    comment claiming query_financials "hardcodes form='10-K'", which it did not,
    while the schema still offered 10-Q. Three places, one capability, three
    answers."""
    from copilot.agent.agent import TOOL_SCHEMAS

    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "query_financials")
    assert "form" not in schema["function"]["parameters"]["properties"]
