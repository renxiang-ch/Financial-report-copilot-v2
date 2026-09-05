"""Where a formula came from, and what happens when it came from nowhere.

Asked for days sales outstanding -- which needs accounts receivable, and this
database has no such label -- the model computed
`(current_assets - inventory) / revenue * 365`, five runs and four different
invented formulas. Asked for book value per share it computed
`total_equity / (total_assets - total_debt)`. Both drew every operand from the
database, so the grounding check reported verified:true for all of them.

An earlier attempt listed the metrics whose inputs are missing and refused those.
It was reverted: it fixed days sales outstanding and did nothing for book value
per share, because the set of derived figures nobody listed has no bound. These
tests pin the inversion that replaced it -- enumerate what the system vouches
for, and treat everything else as the complement.

Swept over every saved trace, this classifier reports 7 of 299 calculations as
unsourced, and all 7 are the known fabrications.
"""

from copilot.agent.authority import classify
from copilot.agent.grounding import verify_answer


def _fact(value, metric, ticker="AAPL", fy=2024):
    return {"tool": "query_financials", "input": {},
            "output": {"found": True, "value": value, "metric": metric,
                       "ticker": ticker, "fiscal_year": fy}}


def _edge(pct, supplier="QRVO", fy=2024):
    return {"tool": "graph_query", "input": {},
            "output": {"found": True, "edges": [
                {"supplier": supplier, "customer": "AAPL", "revenue_pct": pct,
                 "fiscal_year": fy}]}}


def _compute(expression, variables, result):
    return {"tool": "compute",
            "input": {"expression": expression, "variables": variables},
            "output": {"ok": True, "result": result}}


def _auth(steps, question="", qvals=None):
    step = next(s for s in steps if s["tool"] == "compute")
    return classify(step, steps, question, qvals or set())[0]


# ── Sources the system can point at ──────────────────────────────────────────

def test_a_vouched_combination_of_labels_is_sourced():
    steps = [_fact(180683000000.0, "GrossProfit"), _fact(391035000000.0, "Revenue"),
             _compute("gross_profit / revenue * 100",
                      {"gross_profit": 180683000000.0, "revenue": 391035000000.0}, 46.21)]
    assert _auth(steps) == "registry:gross margin"


def test_the_same_formula_written_differently_is_still_sourced():
    """The model writes one formula many ways and inlines its numbers about a
    third of the time. Matching expression text would need every variant; the
    labels feeding the calculation are the same in all of them."""
    steps = [_fact(180683000000.0, "GrossProfit"), _fact(391035000000.0, "Revenue"),
             _compute("180683000000 / 391035000000 * 100", {}, 46.21)]
    assert _auth(steps) == "registry:gross margin"


def test_one_figure_across_two_periods_is_a_rate_of_change():
    """Year-over-year and CAGR are recognised by the periods involved, without
    either being named anywhere."""
    steps = [_fact(391035000000.0, "Revenue", fy=2024),
             _fact(394328000000.0, "Revenue", fy=2022),
             _compute("(a / b) ** (1 / 2) - 1",
                      {"a": 391035000000.0, "b": 394328000000.0}, -0.0042)]
    assert _auth(steps) == "period comparison"


def test_one_figure_across_two_companies_is_an_aggregation():
    steps = [_fact(3769506000.0, "Revenue", ticker="QRVO"),
             _fact(1788890000.0, "Revenue", ticker="CRUS"),
             _compute("a + b", {"a": 3769506000.0, "b": 1788890000.0}, 5558396000.0)]
    assert _auth(steps) == "aggregation"


def test_an_operation_the_question_stated_is_sourced_to_the_question():
    steps = [_fact(3769506000.0, "Revenue", ticker="QRVO"), _edge(46.0),
             _compute("revenue * pct / 100 * cut",
                      {"revenue": 3769506000.0, "pct": 46.0, "cut": 0.2}, 346794552.0)]
    assert _auth(steps, "If Apple cuts orders 20%, what does Qorvo lose?",
                 {20.0, 0.2}) == "question"


# ── The complement ───────────────────────────────────────────────────────────

def test_an_invented_ratio_has_no_source():
    """Days sales outstanding out of current assets. Every operand was fetched;
    the combination is not one this system vouches for."""
    steps = [_fact(152987000000.0, "CurrentAssets"), _fact(391035000000.0, "Revenue"),
             _fact(6331000000.0, "Inventory"),
             _compute("(current_assets - inventory) / revenue * 365",
                      {"current_assets": 152987000000.0, "inventory": 6331000000.0,
                       "revenue": 391035000000.0}, 136.9)]
    assert _auth(steps) is None


def test_an_invented_cash_flow_has_no_source():
    steps = [_fact(93736000000.0, "NetIncome"), _fact(123216000000.0, "OperatingIncome"),
             _fact(31370000000.0, "R&D"), _fact(210352000000.0, "COGS"),
             _compute("net_income + operating_income - rd - cogs",
                      {"net_income": 93736000000.0, "operating_income": 123216000000.0,
                       "rd": 31370000000.0, "cogs": 210352000000.0}, -24770000000.0)]
    assert _auth(steps) is None


def test_book_value_per_share_out_of_a_balance_sheet_has_no_source():
    """The metric the reverted table did nothing for, because nobody had listed
    it. Nothing here names it either -- it falls out as the complement."""
    steps = [_fact(56950000000.0, "TotalEquity"), _fact(364980000000.0, "TotalAssets"),
             _fact(96660000000.0, "TotalDebt"),
             _compute("total_equity / (total_assets - total_debt)",
                      {"total_equity": 56950000000.0, "total_assets": 364980000000.0,
                       "total_debt": 96660000000.0}, 0.2122)]
    assert _auth(steps) is None


# ── How it is reported ───────────────────────────────────────────────────────

def test_an_unsourced_formula_is_recorded_but_does_not_fail_verification():
    """Recording it as a failure would put a red flag on every legitimate novel
    calculation; recording nothing would leave a reader unable to tell one from
    the other. The arithmetic really is fine and every input really was fetched
    -- what is missing is a source for the formula, and that is the reader's
    call to make."""
    steps = [_fact(152987000000.0, "CurrentAssets"), _fact(391035000000.0, "Revenue"),
             _compute("(current_assets / revenue) * 365",
                      {"current_assets": 152987000000.0, "revenue": 391035000000.0}, 142.8)]
    r = verify_answer(steps, "Apple's days sales outstanding was about 142.8 days.",
                      "What was Apple's days sales outstanding in fiscal 2024?")
    assert r["verified"] is True                     # every figure has a source
    assert r["unsourced_formulas"]                   # the formula does not
    assert "current_assets" in r["unsourced_formulas"][0]


def test_a_calculation_the_answer_never_states_is_not_reported():
    """An intermediate the model computed and discarded is not a claim."""
    steps = [_fact(152987000000.0, "CurrentAssets"), _fact(391035000000.0, "Revenue"),
             _compute("(current_assets / revenue) * 365",
                      {"current_assets": 152987000000.0, "revenue": 391035000000.0}, 142.8)]
    r = verify_answer(steps, "I cannot determine days sales outstanding.", "")
    assert r["unsourced_formulas"] == []


def test_a_sourced_calculation_is_not_reported():
    steps = [_fact(180683000000.0, "GrossProfit"), _fact(391035000000.0, "Revenue"),
             _compute("gross_profit / revenue * 100",
                      {"gross_profit": 180683000000.0, "revenue": 391035000000.0}, 46.21)]
    r = verify_answer(steps, "Apple's gross margin was 46.21%.", "")
    assert r["unsourced_formulas"] == []
