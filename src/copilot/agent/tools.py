"""Agent tools — query_financials, list_metrics, compute, retrieve_text, graph_query."""

import ast
import re

from copilot.agent.slots import extract_slots
from copilot.retrieval.hybrid import retrieve_hybrid as _retrieve
from copilot.storage.db import get_conn


import difflib
from functools import lru_cache


@lru_cache(maxsize=1)
def _known_tickers() -> frozenset[str]:
    """Tickers this database actually holds. Cached: it changes only on ingestion."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM companies")
            return frozenset(r["ticker"].upper() for r in cur.fetchall())
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _edge_sides() -> tuple[dict[str, int], dict[str, int]]:
    """How many named edges each ticker has as a supplier, and as a customer.

    Cached like _known_tickers: it changes only on ingestion.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT supplier_ticker AS t, COUNT(*) AS n FROM supply_edges "
                        "WHERE disclosure_status = 'named' GROUP BY 1")
            sup = {r["t"].upper(): r["n"] for r in cur.fetchall()}
            cur.execute("SELECT customer_ticker AS t, COUNT(*) AS n FROM supply_edges "
                        "WHERE disclosure_status = 'named' GROUP BY 1")
            cus = {r["t"].upper(): r["n"] for r in cur.fetchall()}
            return sup, cus
    finally:
        conn.close()


def _no_edges(customer: str | None, supplier: str | None, fy_label: str) -> dict:
    """An empty edge result, and the reason for it when the reason is checkable.

    A bare found:false was read as a fact about the world. Asked how dependent
    Cirrus Logic is on Apple, the model once queried customer='CRUS' -- right
    company, wrong side of the relationship -- got nothing back, and answered
    "I cannot find any supplier exposure data for Cirrus Logic." A parameter
    mistake was reported as an absence in the filings, with no way for the reader
    to tell the two apart.

    Which side a company can appear on is not a matter of opinion: these edges
    come from 10-K concentration disclosures, which are supplier-reported, so a
    company that files them appears as a supplier and the customers it names
    appear as customers. A ticker that has edges only on the other side is
    therefore a recoverable mistake in the call, and saying so costs one query
    against a cached table.

    Only checked when ONE side was asked for. With both given, an empty result
    means that pair has no disclosed edge -- which is a real finding, not an
    error.
    """
    base = {"found": False, "hub": customer or supplier,
            "fiscal_year": fy_label, "edges": []}
    if bool(customer) == bool(supplier):
        return base

    side, ticker = ("customer", customer) if customer else ("supplier", supplier)
    other = "supplier" if side == "customer" else "customer"
    as_supplier, as_customer = _edge_sides()
    reversed_count = (as_supplier if other == "supplier" else as_customer).get(
        (ticker or "").upper(), 0)
    if not reversed_count:
        return {**base, "reason": f"{ticker} has no disclosed edges in either direction"}

    # The explanation has to match the direction it is explaining. One template
    # for both cases said "AAPL discloses who ITS customers are" about a company
    # that files no such disclosure -- a confidently wrong hint is worse than no
    # hint, which is the whole complaint against the bare found:false.
    why = (f"{ticker} files these disclosures, naming the customers it depends on"
           if other == "supplier" else
           f"{ticker} is NAMED as a customer by its suppliers; it does not file "
           f"these disclosures itself")
    return {
        **base,
        "recoverable": True,
        "asked_for": {side: ticker},
        "error": f"{ticker} appears in this table only as a {other}, never as a {side}",
        "hint": (f"These edges come from 10-K customer-concentration disclosures, "
                 f"which are supplier-reported: {why}. Retry with "
                 f"{other}={ticker!r}. This is a wrong argument, not evidence that "
                 f"the data is missing -- do not report {ticker} as absent from "
                 f"the supply-chain data."),
        "edges_if_reversed": reversed_count,
    }


def _resolve_ticker(ticker: str | None) -> tuple[str | None, dict | None]:
    """(canonical ticker, error payload) -- exactly one of the two is set.

    "No rows for SKWS" and "SKWS is not a company" are different facts, and
    returning found:false for both let a typo become an authoritative absence.
    Measured: the model called graph_query(supplier='SKWS') -- SWKS with two
    letters swapped -- got found:false, and told the user there is no
    supply-chain data for Skyworks, which there is.

    A company NAME reaches here the same way and needs resolving rather than
    rejecting. `graph_query(supplier="Skyworks Solutions")` produced exactly the
    failure this guard exists to prevent: difflib cannot bridge that to "SWKS",
    so the payload came back with an empty did_you_mean and the model reported
    the absence as a finding. The company index slots.py builds from the
    companies table already resolves the name, so it is asked before giving up.
    Resolution is returned rather than waved through: letting the literal string
    continue into the SQL would find no rows and produce the same wrong answer
    one step later.
    """
    if not ticker:
        return None, None
    t = ticker.upper()
    known = _known_tickers()
    if t in known:
        return t, None

    from copilot.agent.slots import find_companies
    named = [c for c in find_companies(ticker) if c in known]
    if len(named) == 1:
        return named[0], None

    # difflib rather than a prefix match: the mistakes are transpositions and
    # doubled letters (SKWS for SWKS, APPL for AAPL), which a prefix rule answers
    # with a confidently wrong neighbour.
    close = difflib.get_close_matches(t, sorted(known), n=3, cutoff=0.6)
    return None, {
        "found": False,
        "asked_for": ticker,
        "error": f"{ticker!r} is not a company in this database",
        "recoverable": True,
        "did_you_mean": close[:3] or named[:3],
        "hint": "Use one of the known tickers. An unknown ticker is a typo, not "
                "evidence that the data is missing -- do not report it as such.",
    }


# Quarterly figures are stored, and they are not what anyone asking for a quarter
# means. Apple FY2024 holds Q1 119,575M, Q2 210,328M, Q3 296,105M -- year to
# DATE, not the quarter. Worse, there is no argument for choosing a quarter, so
# `period_end DESC LIMIT 1` returns whichever is last: asking for FY2024 with
# form='10-Q' returns 296,105M, which is neither a quarter nor the year, with
# nothing in the result saying what period it covers. The citation string would
# have called it a 10-K as well.
#
# The schema advertised this. list_metrics had already been narrowed to 10-K
# with a comment claiming query_financials "hardcodes form='10-K'", which it did
# not -- three places disagreeing about one capability. Closing the path is the
# honest fix and the fast one; separating discrete quarters from year-to-date is
# work in the ingestion layer, and is not done.
_QUARTERLY_UNSUPPORTED = {
    "found": False,
    "recoverable": False,
    "error": "quarterly figures are not served by this tool",
    "hint": "The 10-Q rows in this database are year-to-date cumulative, not "
            "discrete quarters, and there is no way to select a quarter. Answer "
            "annual (10-K) questions, and say that quarterly figures are not "
            "available rather than deriving one.",
}


def query_financials(ticker: str, metric: str, fiscal_year: int | None = None, form: str = "10-K") -> dict:
    """
    Query financial facts from the database.

    Returns the most recent matching row, or all rows for the fiscal_year if specified.
    Always reads from SQL — never from LLM.

    Annual only. See _QUARTERLY_UNSUPPORTED for what the stored 10-Q rows
    actually contain and why serving them would return a wrong number rather than
    no number.
    """
    if (form or "10-K").upper() != "10-K":
        return {**_QUARTERLY_UNSUPPORTED, "ticker": ticker, "metric": metric,
                "asked_for_form": form}
    ticker, bad = _resolve_ticker(ticker)
    if bad:
        return {**bad, "ticker": bad["asked_for"], "metric": metric}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if fiscal_year:
                cur.execute(
                    """
                    SELECT f.ticker, f.label, f.value, f.unit, f.period_end,
                           f.fiscal_year, f.form, f.accn,
                           fi.doc_url
                    FROM financial_facts f
                    LEFT JOIN filings fi ON fi.accn = f.accn
                    WHERE f.ticker = %s
                      AND f.label  = %s
                      AND f.fiscal_year = %s
                      AND f.form = %s
                    ORDER BY f.period_end DESC
                    LIMIT 1
                    """,
                    (ticker.upper(), metric, fiscal_year, form),
                )
            else:
                cur.execute(
                    """
                    SELECT f.ticker, f.label, f.value, f.unit, f.period_end,
                           f.fiscal_year, f.form, f.accn,
                           fi.doc_url
                    FROM financial_facts f
                    LEFT JOIN filings fi ON fi.accn = f.accn
                    WHERE f.ticker = %s
                      AND f.label  = %s
                      AND f.form = %s
                    ORDER BY f.period_end DESC
                    LIMIT 1
                    """,
                    (ticker.upper(), metric, form),
                )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return {"found": False, "ticker": ticker, "metric": metric, "fiscal_year": fiscal_year}

    return {
        "found": True,
        "ticker": row["ticker"],
        "metric": row["label"],
        "value": float(row["value"]),
        "unit": row["unit"],
        "period_end": str(row["period_end"]),
        "fiscal_year": row["fiscal_year"],
        "form": row["form"],
        "citation": f"SEC 10-K filing accession {row['accn']} "
                    f"({row['doc_url'] or 'https://www.sec.gov/Archives/edgar/data/' + row['accn'].split('-')[0].lstrip('0') + '/' + row['accn'].replace('-','') + '/'})",
    }


def list_metrics(ticker: str, form: str = "10-K") -> dict:
    """
    Return all available metrics and fiscal years for a company.
    Call this first when unsure what data exists, or before a multi-step question
    to confirm which years are available.

    Advertises EXACTLY what query_financials() can serve, no more. Two earlier
    mismatches, both measured before this was written:

    1. No form filter. query_financials hardcodes form='10-K', but this returned
       10-Q rows too, so 5 (ticker, label) pairs and 319 (ticker, label, year)
       combinations were advertised that query_financials can never return
       (e.g. AVGO D&A exists only in 10-Q).
    2. `years_available` was a MIN-MAX range string. 17.1% of 10-K entries have
       gaps inside that range -- 125 advertised years do not exist (AAPL
       TotalDebt reported "2013-2025" for 5 actual years). The years are now
       enumerated, so the range can no longer imply coverage it lacks.

    Both caused the same failure: the model is told a fact exists, queries it,
    gets found:false, and burns tool rounds. `form` is a parameter rather than a
    hardcoded literal for symmetry with query_financials(); no tool schema
    exposes it, so callers always get the 10-K set.
    """
    ticker, bad = _resolve_ticker(ticker)
    if bad:
        return {**bad, "ticker": bad["asked_for"]}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT label,
                       array_agg(DISTINCT fiscal_year ORDER BY fiscal_year) AS years
                FROM financial_facts
                WHERE ticker = %s AND fiscal_year IS NOT NULL AND form = %s
                GROUP BY label
                ORDER BY label
                """,
                (ticker.upper(), form),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"found": False, "ticker": ticker}

    return {
        "found": True,
        "ticker": ticker.upper(),
        "form": form,
        "metrics": [
            {
                "metric": row["label"],
                "years_available": row["years"],
                "n_years": len(row["years"]),
            }
            for row in rows
        ],
    }


@lru_cache(maxsize=64)
def _latest_filing_year(ticker: str) -> int | None:
    """Newest fiscal year on record for a company. Cached; changes on ingestion."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(fiscal_year) AS m FROM filings WHERE ticker = %s",
                        (ticker.upper(),))
            row = cur.fetchone()
            return row["m"] if row else None
    finally:
        conn.close()


def _year_scope(query: str, ticker: str | None,
                fiscal_year: int | None) -> tuple[int | None, str]:
    """Which filing year to search, and why -- the second half is not optional.

    Every company here has around ten filings and a quarter of the corpus is
    boilerplate repeated across them, so an unscoped top-five fills with the same
    paragraph from four different years. Scoping fixes that, and the deterministic
    probe puts the retrieval set at hit@5 3/7 -> 5/7, MRR 0.286 -> 0.548.

    The awkward part is that almost nobody says which year. Only one of the seven
    retrieval questions in the frozen set states one in its text; the others carry
    it as dataset metadata the agent never sees. So scoping only when a year is
    stated would fire once in seven and buy nothing, and the fallback to the
    newest filing is what makes the change worth making.

    A fallback is a narrowing the user did not ask for, which is exactly the shape
    of failure this system has measured elsewhere -- a qualifier dropped at a tool
    boundary, and a confident answer to the reduced question. So the reason is
    returned with the year and travels into the result, the trace and the
    provenance. Narrowing is allowed; narrowing silently is not.
    """
    if fiscal_year:
        return fiscal_year, "caller specified this fiscal year"
    s = extract_slots(query or "")
    if s["fiscal_year"]:
        return s["fiscal_year"], "the query names this fiscal year"
    if s["is_trend"] or len(s["years"]) > 1:
        return None, "the query spans more than one year, so it is not scoped"
    if not ticker:
        return None, "no company named, so there is no 'most recent filing' to pick"
    latest = _latest_filing_year(ticker)
    if not latest:
        return None, f"no filings on record for {ticker}"
    return latest, (f"no year given; searched {ticker}'s most recent filing (FY{latest}) "
                    f"-- say so if the answer depends on it")


def retrieve_text(query: str, ticker: str | None = None, k: int = 5,
                  fiscal_year: int | None = None) -> dict:
    """
    Search 10-K text chunks using hybrid BM25 + dense retrieval (RRF fusion).
    Use for qualitative questions (why, how, risk factors, MD&A commentary).
    Never use for numeric data — use query_financials instead.

    Financial-statement tables are not searched; see retrieve_hybrid for the
    measurements behind that. The flag that controls it is deliberately absent
    from the tool schema: whether this corpus contains readable tables is a
    property of the deployment, not a choice to make per question, and a model
    offered the switch would flip it precisely when it wanted a number it should
    have fetched from SQL.

    `fiscal_year` IS in the schema, because which year to read is a property of
    the question rather than of the deployment. When it is omitted the search is
    scoped anyway -- see `_year_scope` -- and the returned record says to what
    and why.
    """
    ticker, bad = _resolve_ticker(ticker)
    if bad:
        return {**bad, "query": query}

    year, why = _year_scope(query, ticker, fiscal_year)
    results = _retrieve(query, ticker=ticker, k=k, fiscal_year=year)

    # A year the caller never asked for must not be able to turn a hit into a
    # miss. If scoping emptied the result, the unscoped search runs and the
    # record says the scope was abandoned.
    if not results and year is not None and fiscal_year is None:
        results = _retrieve(query, ticker=ticker, k=k)
        year, why = None, ("scoping to the most recent filing found nothing, "
                           "so all years were searched")

    if not results:
        return {"found": False, "query": query, "ticker": ticker,
                "scoped_to_fiscal_year": year, "scope_reason": why}
    # `id` exists to give a chunk an identity while the two ranked lists are
    # fused (see retrieval/hybrid.py) and has no meaning past that point. Dropped
    # here so the model never sees a key it could mistake for a citation: the
    # only citable handle in this system is the SEC accession.
    return {
        "found": True,
        "query": query,
        "ticker": ticker,
        "scoped_to_fiscal_year": year,
        "scope_reason": why,
        "results": [{k_: v for k_, v in r.items() if k_ != "id"} for r in results],
    }


def graph_query(
    customer: str | None = None,
    supplier: str | None = None,
    fiscal_year: int | str | None = "latest",
    depth: int = 1,
) -> dict:
    """
    Query the supply-chain graph stored in supply_edges.

    Use this for Tier-3 questions about supply-chain dependencies:
    - "Who are Apple's direct suppliers?" → graph_query(customer="AAPL")
    - "Who does QRVO sell to?"           → graph_query(supplier="QRVO")
    - Multi-hop (depth=2)                → returns suppliers-of-suppliers

    Always include traversal_trace in your final answer for citation traceability.
    """
    customer, bad = _resolve_ticker(customer)
    if not bad:
        supplier, bad = _resolve_ticker(supplier)
    if bad:
        return {**bad, "customer": customer, "supplier": supplier, "edges": []}
    import re as _re

    conn = get_conn()
    try:
        with conn.cursor() as cur:

            # ── Resolve fiscal_year into a SQL WHERE fragment + params ──────
            fy_clause = ""   # extra WHERE condition for fiscal year
            fy_params: list = []
            fy_label  = str(fiscal_year)

            if fiscal_year == "latest" or fiscal_year is None:
                # Resolve per-company: latest year THIS company has data, not global max
                # When both given, resolve from supplier (the filing company)
                ticker_col = "supplier_ticker" if supplier else "customer_ticker"
                ticker_val = (supplier or customer or "").upper()
                cur.execute(
                    f"SELECT MAX(fiscal_year) FROM supply_edges "
                    f"WHERE {ticker_col} = %s AND disclosure_status='named'",
                    (ticker_val,),
                )
                row_fy = cur.fetchone()["max"]
                if row_fy is None:
                    # Nothing on the side that was asked about. Whether that is an
                    # absence or a wrong argument is decidable -- see _no_edges.
                    conn.close()
                    return _no_edges(customer, supplier, "latest")
                fy_single = row_fy
                fy_clause = "AND e.fiscal_year = %s"
                fy_params = [fy_single]
                fy_label  = str(fy_single)

            elif fiscal_year == "trend":
                # No year filter — return all years
                fy_clause = ""
                fy_params = []
                fy_label  = "all years"

            elif isinstance(fiscal_year, str) and _re.match(r"\d{4}-\d{4}", fiscal_year):
                # Range like "2022-2025"
                y_start, y_end = fiscal_year.split("-")
                fy_clause = "AND e.fiscal_year BETWEEN %s AND %s"
                fy_params = [int(y_start), int(y_end)]
                fy_label  = fiscal_year

            else:
                fy_single = int(fiscal_year)
                fy_clause = "AND e.fiscal_year = %s"
                fy_params = [fy_single]
                fy_label  = str(fy_single)

            # ── Build single-hop query (depth=1) ────────────────────────────
            if depth == 1:
                if customer and supplier:
                    # Both specified — filter to this exact supplier→customer relationship
                    ticker_clause = "e.customer_ticker = %s AND e.supplier_ticker = %s"
                    ticker_param  = [customer.upper(), supplier.upper()]
                elif customer:
                    ticker_clause = "e.customer_ticker = %s"
                    ticker_param  = [customer.upper()]
                elif supplier:
                    ticker_clause = "e.supplier_ticker = %s"
                    ticker_param  = [supplier.upper()]
                else:
                    return {"found": False, "error": "Provide customer or supplier ticker"}

                cur.execute(
                    f"""
                    SELECT e.supplier_ticker, e.customer_ticker,
                           e.revenue_pct, e.threshold_only, e.fiscal_year,
                           e.disclosure_status, e.accn,
                           e.source_text, f.doc_url
                    FROM supply_edges e
                    LEFT JOIN filings f ON f.accn = e.accn
                    WHERE {ticker_clause}
                      AND e.disclosure_status = 'named'
                      {fy_clause}
                    ORDER BY e.fiscal_year DESC, e.revenue_pct DESC NULLS LAST
                    """,
                    ticker_param + fy_params,
                )
                rows = cur.fetchall()

            else:
                # ── Multi-hop: recursive CTE (single year only) ─────────────
                if not fy_params:
                    # trend/range not supported for multi-hop; fall back to latest
                    cur.execute(
                        "SELECT MAX(fiscal_year) FROM supply_edges WHERE disclosure_status='named'"
                    )
                    fy_params = [cur.fetchone()["max"]]

                anchor    = customer or supplier
                direction = "customer_ticker" if customer else "supplier_ticker"
                follow    = "supplier_ticker" if customer else "customer_ticker"
                cur.execute(
                    f"""
                    WITH RECURSIVE chain AS (
                        SELECT supplier_ticker, customer_ticker,
                               revenue_pct, fiscal_year, disclosure_status, accn, 1 AS depth
                        FROM supply_edges
                        WHERE {direction} = %s
                          AND fiscal_year = %s
                          AND disclosure_status = 'named'
                        UNION ALL
                        SELECT e.supplier_ticker, e.customer_ticker,
                               e.revenue_pct, e.fiscal_year, e.disclosure_status, e.accn,
                               c.depth + 1
                        FROM supply_edges e
                        JOIN chain c ON e.{direction} = c.{follow}
                        WHERE c.depth < %s AND e.disclosure_status = 'named'
                    )
                    SELECT DISTINCT supplier_ticker, customer_ticker,
                                    revenue_pct, fiscal_year, disclosure_status, accn, depth
                    FROM chain
                    ORDER BY depth, fiscal_year DESC, revenue_pct DESC NULLS LAST
                    """,
                    (anchor.upper(), fy_params[0], depth),
                )
                rows = cur.fetchall()

    finally:
        conn.close()

    if not rows:
        return _no_edges(customer, supplier, fy_label)

    edges = []
    trace = []
    for row in rows:
        doc_url = row.get("doc_url", "")
        accn    = row["accn"] or ""
        citation = f"SEC 10-K accession {accn} ({doc_url})" if accn else "accession not available"
        is_threshold = bool(row.get("threshold_only", False))
        # Displaying the percentage and computing with it are separate concerns,
        # and every edge must satisfy both. A threshold_only edge is honestly
        # reported as ">10%" but still has to supply a number when a question
        # needs arithmetic -- revenue_pct holds the disclosed floor. Keeping the
        # two in one field made the string form the only one visible in
        # traversal_trace, and an agent reading the trace found no figure to
        # multiply and silently dropped the term instead of using the floor,
        # overstating a 20% order-cut exposure by exactly 10x. So revenue_pct is
        # always numeric, pct_display is always the string to print, and the
        # trace below carries both.
        pct_display = ">10%" if is_threshold else f"{row['revenue_pct']}%"
        edge = {
            "supplier":          row["supplier_ticker"],
            "customer":          row["customer_ticker"],
            "revenue_pct":       row["revenue_pct"],
            "pct_display":       pct_display,
            "pct_is_floor":      is_threshold,
            "threshold_only":    is_threshold,
            "fiscal_year":       row["fiscal_year"],
            "disclosure_status": row["disclosure_status"],
            "citation":          citation,
            "source_text":       (row.get("source_text") or "")[:400],
        }
        edges.append(edge)
        pct_str = (f">10% (floor; revenue_pct={row['revenue_pct']} is the disclosed "
                   f"lower bound -- exact % not stated in the 10-K)"
                   if is_threshold else pct_display)
        trace.append(
            f"{row['supplier_ticker']}->{row['customer_ticker']} "
            f"{pct_str} FY{row['fiscal_year']} [{accn}]"
        )

    return {
        "found":           True,
        "hub":             customer or supplier,
        "fiscal_year":     fy_label,
        "edge_count":      len(edges),
        "edges":           edges,
        "traversal_trace": trace,
    }


def _alias_non_identifier_vars(expression: str, allowed: dict) -> tuple[str, dict]:
    """
    Rewrite variable names that are not valid Python identifiers into ones that are.

    The expression is evaluated by `eval`, so every variable name in it must
    parse as a Python identifier. Four canonical metric labels do not: `R&D`,
    `PP&E`, `D&A` and `D&A_Component`. Python reads `&` as bitwise-AND, so
    `R&D` parses as `R & D` and raises `NameError: name 'R' is not defined`
    even though the value was supplied correctly under the key "R&D".

    Measured before this fix, on 2,160 agent observations: 23 of 225 compute
    calls failed this way. The model's workaround was worse than the error --
    it re-issued the call with the raw numbers inlined
    (`(2096387000 / 21345260000) * 100`, variables `{}`), which returns the
    right number while destroying the link between each figure and the query
    that produced it. That link is the entire reason this tool exists.

    Substitution is longest-key-first and boundary-guarded, so `D&A` is not
    matched inside `D&A_Component`. The alias is internal: the returned dict
    still reports the caller's original expression and variables.
    """
    namespace: dict = {}
    rewritten = expression
    for i, key in enumerate(sorted(allowed, key=len, reverse=True)):
        if key.isidentifier():
            namespace[key] = allowed[key]
            continue
        alias = f"_v{i}_"
        pattern = rf"(?<![\w&]){re.escape(key)}(?![\w&])"
        new_text, n = re.subn(pattern, alias, rewritten)
        if n:
            rewritten = new_text
            namespace[alias] = allowed[key]
        else:
            namespace[key] = allowed[key]  # not referenced; harmless
    return rewritten, namespace


# Node types an arithmetic expression needs, and nothing else. Emptying
# __builtins__ already blocks __import__ and open, but it does not stop
# 9**9**9, which needs no name at all and will exhaust memory before it
# returns. A whitelist says what arithmetic IS rather than enumerating the
# ways it can be abused.
_SAFE_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)
# Big enough for any ratio, root or CAGR this domain produces; small enough that
# the worst case is instant. A 20-year CAGR needs 1/20, not 10**9.
_MAX_EXPONENT = 64


def _reject_unsafe(expression: str, namespace: dict) -> str | None:
    """Return a reason to refuse, or None if the expression is plain arithmetic."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return f"could not parse expression: {e}"
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            return (f"{type(node).__name__} is not allowed here; compute evaluates "
                    f"arithmetic over the supplied variables only")
    # Exponents are checked deepest-first so an inner power is proven bounded
    # before an outer one is evaluated. Only the exponent subtree is ever
    # evaluated, never the whole expression, and each one is already known to be
    # whitelisted arithmetic -- so this cannot itself be the thing that hangs.
    powers = [(_depth(tree, n), n) for n in ast.walk(tree)
              if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow)]
    for _, node in sorted(powers, key=lambda p: -p[0]):
        try:
            value = eval(compile(ast.Expression(node.right), "<exp>", "eval"),  # noqa: S307
                         {"__builtins__": {}}, namespace)
        except Exception as e:                   # noqa: BLE001
            return f"exponent could not be evaluated: {e}"
        if not isinstance(value, (int, float)) or abs(value) > _MAX_EXPONENT:
            return (f"exponent {value} exceeds the limit of {_MAX_EXPONENT}; a "
                    f"financial ratio, root or CAGR does not need one that large")
    return None


def _depth(tree: ast.AST, target: ast.AST) -> int:
    """How deep `target` sits under `tree`. Used only to order the Pow checks."""
    for depth, level in enumerate(_levels(tree)):
        if any(n is target for n in level):
            return depth
    return 0


def _levels(tree: ast.AST):
    level = [tree]
    while level:
        yield level
        level = [c for n in level for c in ast.iter_child_nodes(n)]


def compute(expression: str, variables: dict) -> dict:
    """
    Evaluate a simple arithmetic expression with named variables.
    Numbers come from query_financials — never computed by the LLM.

    Example:
        compute("gross_profit / revenue * 100", {"gross_profit": 180683e9, "revenue": 391035e9})

    Variable names that are not valid Python identifiers (`R&D`, `PP&E`, `D&A`,
    `D&A_Component`) are aliased internally — see _alias_non_identifier_vars.
    """
    allowed = {k: v for k, v in variables.items() if isinstance(v, (int, float))}
    evaluated, namespace = _alias_non_identifier_vars(expression, allowed)
    rejected = _reject_unsafe(evaluated, namespace)
    if rejected:
        return {"ok": False, "error": rejected, "expression": expression}
    try:
        result = eval(evaluated, {"__builtins__": {}}, namespace)  # noqa: S307
        return {"ok": True, "result": float(result), "expression": expression, "variables": variables}
    except Exception as e:
        return {"ok": False, "error": str(e), "expression": expression}
