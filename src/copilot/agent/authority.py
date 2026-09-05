"""Where did this formula come from, and can the system vouch for it.

The failure
    Asked for Apple's book value per share, the model computed
    `total_equity / (total_assets - total_debt)` and reported 0.2122. Asked for
    days sales outstanding, which needs accounts receivable that this database
    does not hold, it computed `(current_assets - inventory) / revenue * 365` --
    five runs, four different invented formulas. Every operand in every one of
    them was genuinely fetched, so the grounding check reported verified:true.
    An invented formula over real inputs is fully sourced.

Why not check whether the formula is correct
    That requires knowing the metric's definition, which means enumerating
    definitions, which means the system is blind to every metric nobody wrote
    down. An earlier attempt did exactly that -- a table of metrics whose inputs
    are missing -- and it was reverted: it fixed days sales outstanding and did
    nothing at all for book value per share, because the set of derived figures
    nobody listed has no bound.

    `compute` takes an expression. An expression carries no name and no meaning,
    so there is nothing for a checker to compare it against. This is structural,
    not a gap in the implementation.

What is decidable instead: where the formula came from
    Not "is this correct" but "does this have a source". Three sources, and the
    third is the complement of the first two rather than a list:

        question  -- the operation is stated in the question ("cut orders by
                     20%"), so the questioner supplied the formula
        registry  -- the combination of stored labels is one this system vouches
                     for
        none      -- everything else, unbounded and never enumerated

    That inversion is the whole point. Enumerating capability is finite and
    honest; enumerating incapacity is not. And the degradation is safe: an empty
    registry means every derivation is reported as unsourced -- noisy, but never
    silent. The reverted design failed the other way, where an incomplete table
    meant nothing was checked.

Why the registry is keyed on labels rather than on expression text
    The model writes one formula many ways -- `gross_profit / revenue * 100`,
    `(operating_income / revenue) * 100`, `OperatingIncome / Revenue * 100`,
    and inlined as `4276000000 / 13118000000 * 100`. Matching text would need
    every variant. The set of stored labels feeding the calculation is the same
    in all of them, and it separates the observed cases exactly:

        {GrossProfit, Revenue}                     94 runs   gross margin
        {NetIncome, Revenue}                       25 runs   net margin
        {OperatingIncome, Revenue}                 22 runs   operating margin
        {CapEx, OperatingCashFlow}                  2 runs   free cash flow
        {TotalDebt, TotalEquity}                    2 runs   debt to equity
        ---------------------------------------------------------------
        {CurrentAssets, Revenue}                    3 runs   INVENTED (DSO)
        {CurrentAssets, Inventory, Revenue}         2 runs   INVENTED (DSO)
        {COGS, NetIncome, OperatingIncome, R&D}     2 runs   INVENTED (FCF)

    Every entry below is one of the first group. They were observed, not
    imagined, which is the difference between this table and the one that was
    reverted.

What this does NOT do
    It does not stop the model computing, and it does not decide the figure is
    wrong. `total_equity / (total_assets - total_debt)` is arithmetically fine;
    what it is not is a definition of book value per share, and only a reader
    can say that. So the finding is recorded and shown, with the expression and
    the labels it used, and the judgement stays with the person reading it.
"""

import ast

# (labels, name). Seeded from formulas observed working across the saved traces;
# the counts are in the module docstring. Adding an entry is a claim that this
# system vouches for that combination, so it should follow evidence rather than
# precede it.
REGISTRY: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"GrossProfit", "Revenue"}),          "gross margin"),
    (frozenset({"OperatingIncome", "Revenue"}),      "operating margin"),
    (frozenset({"NetIncome", "Revenue"}),            "net margin"),
    (frozenset({"OperatingCashFlow", "CapEx"}),      "free cash flow"),
    (frozenset({"TotalDebt", "TotalEquity"}),        "debt to equity"),
    (frozenset({"NetIncome", "TotalAssets"}),        "return on assets"),
    (frozenset({"NetIncome", "TotalEquity"}),        "return on equity"),
    (frozenset({"CurrentAssets", "CurrentLiabilities"}), "current ratio"),
    (frozenset({"COGS", "Inventory"}),               "inventory turnover"),
    (frozenset({"R&D", "Revenue"}),                  "R&D intensity"),
)

_BY_LABELS = {labels: name for labels, name in REGISTRY}

# Constants that convert units rather than assert a fact. A formula is not made
# unsourced by turning a fraction into a percentage.
_UNIT_CONSTANTS = {100.0, 1.0, 0.0, 1000.0, 1e6, 1e9}


def _literals(expression) -> list[float]:
    if not isinstance(expression, str) or not expression.strip():
        return []
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    return [float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)]


def classify(step: dict, steps: list[dict], question: str = "",
             question_values: set[float] | None = None) -> tuple[str | None, dict]:
    """(authority, detail) for one compute step. None means no source.

    Operands are read from BOTH the variables dict and the literals written into
    the expression. The model inlines its numbers about a third of the time --
    `3769506000 * 0.46 * 0.20` is the same calculation as
    `revenue * pct / 100 * cut` -- and a classifier that only looked at variables
    reported the inlined form as unsourced.

    The trace is read through grounding.index_trace. This module used to build
    its own index and it was the third one over the same data; see index_trace
    for what that cost.
    """
    from copilot.agent.grounding import index_trace, facts_for

    inp = step.get("input") or {}
    variables = inp.get("variables") or {}
    expression = inp.get("expression", "")

    facts = index_trace(steps)
    qvals = question_values or set()

    operands: list[float] = []
    for v in variables.values():
        try:
            operands.append(float(v))
        except (TypeError, ValueError):
            pass
    operands += _literals(expression)

    labels: set[str] = set()
    years: set = set()
    tickers: set[str] = set()
    has_edge = False
    loose: list[float] = []          # operands nothing accounts for
    for value in operands:
        hits = [f for f in facts_for(value, facts) if f.kind in ("metric", "edge_pct")]
        if hits:
            for f in hits:
                if f.kind == "metric":
                    labels.add(f.label); years.add(f.fiscal_year); tickers.add(f.ticker)
                else:
                    has_edge = True
            continue
        # Below a financial magnitude a bare number is an exponent, a divisor, a
        # day count or a rate -- not a figure that could have been invented as a
        # FACT. Counting them as unsourced flagged every CAGR, whose exponent is
        # the number of years spanned, while catching nothing: what makes the
        # days-sales-outstanding formulas unsourced is the labels they draw on,
        # not the 365 in them.
        if value in _UNIT_CONSTANTS or abs(value) < 1000.0:
            continue
        if any(abs(value - q) <= 1e-9 * max(abs(q), 1.0) for q in qvals):
            continue                  # the questioner supplied this number
        loose.append(value)

    stored = frozenset(labels)
    detail = {"expression": expression,
              "labels": sorted(stored),
              "fiscal_years": sorted(y for y in years if y is not None),
              "unsourced_operands": [round(x, 4) for x in loose]}

    # 1. The system vouches for this combination of stored figures.
    if stored in _BY_LABELS and not loose:
        return f"registry:{_BY_LABELS[stored]}", detail

    # 2. One figure across two periods is a rate of change. The periods, not a
    #    remembered formula, are what make it one -- covers year-over-year and
    #    CAGR without naming either.
    if len(stored) == 1 and len({y for y in years if y is not None}) >= 2 and not loose:
        return "period comparison", detail

    # 3. One figure across two companies is an aggregation, not a definition.
    #    "Qorvo's revenue plus Cirrus Logic's" invents nothing.
    if len(stored) == 1 and len({t for t in tickers if t}) >= 2 and not loose:
        return "aggregation", detail

    # 4. The questioner supplied the operation. An edge percentage times a
    #    revenue times a cut the question named is the question's own
    #    arithmetic, not a metric definition recalled from training.
    if has_edge and not loose:
        return "question", detail

    # 5. Composed from results this check already accepted.
    computed = {f.value for f in facts if f.kind == "computed"}
    this_result = None
    out = step.get("output")
    if isinstance(out, dict) and out.get("ok"):
        try:
            this_result = float(out["result"])
        except (KeyError, TypeError, ValueError):
            pass
    others = {c for c in computed if this_result is None or abs(c - this_result) > 1e-9}
    if operands and all(any(abs(v - c) <= 1e-9 * max(abs(c), 1.0) for c in others)
                        or v in _UNIT_CONSTANTS for v in operands):
        return "composed", detail

    return None, detail


def describe(detail: dict) -> str:
    """One line a reader can judge: what was computed, out of what."""
    parts = [f"`{detail.get('expression', '?')}`"]
    if detail.get("labels"):
        parts.append("from " + ", ".join(detail["labels"]))
    if detail.get("fiscal_years"):
        parts.append("FY" + "/".join(str(y) for y in detail["fiscal_years"]))
    return " ".join(parts)
