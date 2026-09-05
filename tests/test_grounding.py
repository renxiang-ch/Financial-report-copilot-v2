"""Unit tests for the grounding check — pure Python, no LLM, no database.

The interesting cases are the negative ones. A verifier that never flags anything
passes every positive test while being worthless, so the deliberately fabricated
figures below are the tests that actually establish it works.
"""

from copilot.agent.grounding import verify_answer

ACC = "0000320193-24-000123"


def _fetch(value, ticker="CRUS", fy=2024):
    return {"tool": "query_financials", "input": {},
            "output": {"found": True, "value": value, "ticker": ticker,
                       "fiscal_year": fy, "accn": ACC}}


def _compute(expression, variables, result):
    return {"tool": "compute",
            "input": {"expression": expression, "variables": variables},
            "output": {"ok": True, "result": result, "expression": expression}}


def _edge(pct, supplier="CRUS", fy=2024):
    return {"tool": "graph_query", "input": {},
            "output": {"found": True, "edges": [{
                "supplier": supplier, "customer": "AAPL", "fiscal_year": fy,
                "revenue_pct": pct, "citation": f"SEC filing accession {ACC}"}]}}


def _passage(text):
    return {"tool": "retrieve_text", "input": {},
            "output": {"found": True, "results": [
                {"text": text, "citation": f"SEC filing accession {ACC}"}]}}


# ── Answers that should pass ──────────────────────────────────────────────────

def test_fetched_value_verifies():
    r = verify_answer([_fetch(118254000000.0)],
                      f"FY2024 operating cash flow was $118,254 million (accession {ACC}).")
    assert r["verified"]
    assert r["numbers_checked"] == 1


def test_rounded_prose_form_of_a_computed_value_verifies():
    steps = [_fetch(4178000000.0), _edge(87.0),
             _compute("r*p/100*0.2", {"r": 4178000000.0, "p": 87.0}, 311266860.0)]
    r = verify_answer(steps, "A 20% cut costs Cirrus Logic about $311.3 million.",
                      "If Apple cuts orders by 20%, what does Cirrus Logic lose?")
    assert r["verified"], r


def test_years_and_small_counts_are_not_treated_as_figures():
    r = verify_answer([_edge(46.0, supplier="QRVO")],
                      "In FY2024, 4 suppliers disclosed Apple concentration; "
                      "Qorvo was the largest at 46%.")
    assert r["verified"], r


def test_figure_quoted_from_a_retrieved_passage_is_sourced():
    r = verify_answer(
        [_passage("Apple, Inc. represented approximately 87 percent of total sales.")],
        "Cirrus Logic disclosed that Apple represented approximately 87 percent of total sales.")
    assert r["verified"], r


def test_a_number_the_user_supplied_is_not_a_fabrication():
    """Echoing the questioner's own premise back is a different failure from
    inventing a figure, and only the second one belongs to this check."""
    r = verify_answer([], "A 20% cut would reduce revenue proportionally.",
                      "What happens if Apple cuts orders by 20%?")
    assert r["verified"], r


# ── Answers that must be flagged ──────────────────────────────────────────────

def test_fabricated_figure_is_flagged():
    r = verify_answer([_fetch(118254000000.0)],
                      "Operating cash flow was $118,254 million and free cash flow "
                      "was $99,999 million.")
    assert not r["verified"]
    assert 99999.0 in r["unverified_numbers"]


def test_invented_percentage_is_flagged_despite_being_small():
    r = verify_answer([_edge(46.0, supplier="QRVO")],
                      "Qorvo derived 46% of revenue from Apple, and Skyworks derived 69%.")
    assert not r["verified"]
    assert 69.0 in r["unverified_numbers"]


def test_fabricated_number_laundered_through_compute_is_flagged():
    """The failure mode the check exists for: an invented figure acquires the
    authority of a tool result by being passed into one."""
    steps = [_edge(46.0, supplier="QRVO"),
             _compute("r*p/100", {"r": 3765000000.0, "p": 46.0}, 1731900000.0)]
    r = verify_answer(steps, "Apple-attributable revenue is about $1,731.9 million.")
    assert not r["verified"]
    assert any(v.startswith("r=") for v in r["unsourced_inputs"]), r


def test_fabricated_accession_is_flagged():
    r = verify_answer([_fetch(118254000000.0)],
                      "Operating cash flow was $118,254 million "
                      "(accession 9999999999-99-999999).")
    assert not r["verified"]
    assert r["unverified_citations"] == ["9999999999-99-999999"]


# ── Binding: whose fact is it, and from which year ────────────────────────────
#
# These four are the cases where every operand is sourced and the answer is
# still wrong. Three are now caught. The fourth is the bound that remains.

def test_a_percentage_bound_to_the_customers_revenue_is_flagged():
    """The 94x error, from the trace that actually produced it.

    Asked for the impact on Skyworks, the model fetched Apple's revenue and
    multiplied it by the Skyworks->Apple concentration. Every operand came from
    a tool, so the grounding check passed it and the answer read as sourced.
    A revenue_pct is a share of the SUPPLIER's revenue; binding it to the
    customer's is wrong without needing to know what was asked.
    """
    steps = [_fetch(4178000000.0, "SWKS"), _fetch(391035000000.0, "AAPL"),
             _edge(10.0, supplier="SWKS"),
             _compute("(391035000000 * 0.20) * 0.1",
                      {"revenue_aapl": 391035000000, "pct_cut": 0.2, "pct_swks": 0.1},
                      7820700000.0)]
    r = verify_answer(steps, "The impact would be approximately $7,820,700,000.",
                      "If Apple cut orders by 20% in FY2024, what is the impact "
                      "on Skyworks Solutions?")
    assert not r["verified"], r
    assert r["misbound_inputs"], r
    assert "SWKS" in r["misbound_inputs"][0] and "AAPL" in r["misbound_inputs"][0]


def test_a_percentage_from_the_wrong_year_is_flagged():
    """The multi-turn failure: a fiscal year established in turn one stops being
    carried, and FY2026's concentration lands on FY2024's revenue."""
    steps = [_fetch(4178000000.0, "CRUS", 2024), _edge(91.0, fy=2026),
             _compute("r*p/100*0.2", {"r": 4178000000.0, "p": 91.0}, 760396000.0)]
    r = verify_answer(steps, "A 20% cut costs about $760.4 million.",
                      "If Apple cuts orders by 20% what does Cirrus Logic lose in FY2024?")
    assert not r["verified"], r
    assert "FY2026" in r["misbound_inputs"][0], r


def test_a_year_over_year_calculation_is_not_a_mismatch():
    """The false positive this check must never produce.

    Every correct year-over-year question in the frozen set mixes two fiscal
    years on purpose. A blanket 'inputs came from different years' rule would
    flag all of them, and a red flag that lands on correct answers is worth less
    than no flag at all. The rule is anchored to supply edges, and there is no
    edge here.
    """
    steps = [_fetch(391035000000.0, "AAPL", 2024), _fetch(383285000000.0, "AAPL", 2023),
             _compute("(a-b)/b*100", {"a": 391035000000.0, "b": 383285000000.0}, 2.0219)]
    r = verify_answer(steps, "Revenue grew 2.02% year over year.")
    assert r["verified"], r


def test_the_questioners_own_percentage_is_not_read_as_an_edge():
    """20% is the user's premise here, not a disclosed concentration, even
    though an edge in the trace happens to carry the same number."""
    steps = [_fetch(391035000000.0, "AAPL"), _edge(20.0, supplier="AVGO"),
             _compute("r*0.20", {"r": 391035000000.0}, 78207000000.0)]
    r = verify_answer(steps, "A 20% cut removes about $78.2 billion of revenue.",
                      "What if Apple's revenue fell by 20%?")
    assert r["verified"], r


# ── The documented bound ──────────────────────────────────────────────────────

def test_the_wrong_metric_is_still_not_caught():
    """Every figure came from a tool and no supply edge is involved, so nothing
    here can tell that operating income was used where gross profit was asked
    for. Answering that needs the question's intent, which is the judgement this
    module refuses to hand to a model. Pinned so a future change claiming to fix
    it has to change this test on purpose rather than by accident.
    """
    steps = [_fetch(123216000000.0, "AAPL"), _fetch(391035000000.0, "AAPL"),
             _compute("a/b*100", {"a": 123216000000.0, "b": 391035000000.0}, 31.51)]
    r = verify_answer(steps, "Gross margin was 31.5% in FY2024.",
                      "What was Apple's gross margin in FY2024?")
    assert r["verified"]


# ── Citations written as EDGAR links ──────────────────────────────────────────

# The same accession as ACC, written the way EDGAR writes it in a path.
_URL = ("https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019324000123/aapl-20240928.htm")


def test_a_citation_given_only_as_a_link_verifies():
    """EDGAR writes the accession undashed in the URL path. Before this was
    handled, the digits parsed as a colossal dollar figure and flagged five
    correct answers; the link itself was never checked at all."""
    r = verify_answer([_edge(87.0)], f"CRUS supplied **87.0%** of revenue. Source: {_URL}")
    assert r["verified"], r
    assert r["citations_checked"] == 1


def test_a_link_to_a_filing_no_tool_returned_is_flagged():
    bad = "https://www.sec.gov/Archives/edgar/data/999999/000099999999999999/x.htm"
    r = verify_answer([_edge(87.0)], f"CRUS supplied **87.0%** of revenue. Source: {bad}")
    assert not r["verified"]
    assert r["unverified_citations"] == ["000099999999999999"]


def test_direction_carried_by_a_word_is_not_a_missing_source():
    """"Revenue decreased by 2.80%" against a computed -2.8004 is one fact stated
    two ways. Whether the sign is right is the harness's numeric check to make."""
    steps = [_compute("(a-b)/b*100", {}, -2.8004605303199366)]
    r = verify_answer(steps, "Apple's revenue decreased by approximately 2.80%.")
    assert r["verified"], r


# ── Literals written into the expression are inputs too ───────────────────────

def test_fabrication_written_as_a_literal_is_flagged():
    """The laundering check once read only the variables dict, so the identical
    fabrication passed by writing the number into the expression string instead.
    The agent really does write that form -- five expressions in the saved traces
    inline their operands."""
    steps = [{"tool": "compute",
              "input": {"expression": "9500000000 - 1000000", "variables": {}},
              "output": {"ok": True, "result": 9499000000.0}}]
    r = verify_answer(steps, "The result is $9,499,000,000.")
    assert not r["verified"]
    assert any("9,500,000,000" in v for v in r["unsourced_inputs"]), r


def test_inline_literals_that_were_fetched_are_fine():
    """Inlining is not itself the offence -- sourcing is. Both operands here came
    from query_financials, so writing them into the expression changes nothing."""
    steps = [_fetch(4178000000.0), _fetch(4772400000.0),
             {"tool": "compute",
              "input": {"expression": "(4178000000 - 4772400000) / 4772400000 * 100",
                        "variables": {}},
              "output": {"ok": True, "result": -12.4508}}]
    r = verify_answer(steps, "Revenue declined 12.45%.")
    assert r["verified"], r


def test_small_inline_constants_are_not_treated_as_facts():
    """100 and 0.2 are question-derived arithmetic, not fetched figures."""
    steps = [_fetch(4178000000.0, "SWKS"), _edge(10.0, supplier="SWKS"),
             {"tool": "compute",
              "input": {"expression": "revenue * pct / 100 * 0.2",
                        "variables": {"revenue": 4178000000.0, "pct": 10.0}},
              "output": {"ok": True, "result": 83560000.0}}]
    r = verify_answer(steps, "A 20% cut costs about $83.6 million.",
                      "If Apple cuts orders by 20%, what does Skyworks lose?")
    assert r["verified"], r


def test_a_percentage_in_prose_matches_a_fraction_from_compute():
    """The model puts the *100 inside the expression about half the time and
    outside it the rest, so compute returns -0.004184 or -0.4184 on a coin
    flip while the answer says "-0.42%" either way. This check flagged the
    correct answer whenever the coin landed the other way -- a red flag on a
    right answer, which is the one thing it must never do.
    """
    steps = [_fetch(394328000000.0, "AAPL", 2022), _fetch(391035000000.0, "AAPL", 2024),
             _compute("(a/b)**(1/2)-1", {"a": 391035000000.0, "b": 394328000000.0},
                      -0.004184211808589633)]
    r = verify_answer(steps, "Apple's revenue CAGR was approximately -0.42%.")
    assert r["verified"], r


def test_the_percentage_bridge_does_not_excuse_a_fabricated_one():
    """Bridging by 100 only applies to figures written as percentages, and only
    downward. Otherwise an invented 46 would pass against a real 0.46."""
    steps = [_fetch(394328000000.0, "AAPL", 2022), _fetch(391035000000.0, "AAPL", 2024),
             _compute("(a/b)**(1/2)-1", {"a": 391035000000.0, "b": 394328000000.0},
                      -0.004184211808589633)]
    r = verify_answer(steps, "Apple's revenue CAGR was approximately -46%.")
    assert not r["verified"]
    assert -46.0 in r["unverified_numbers"]
