"""Cross-cutting resolution: tickers, fiscal-year scope, relation side.

v1 had this logic (``_resolve_ticker``, ``_year_scope``, ``_no_edges``) copied
into three tool modules. Here it lives once. Behaviour and the measured reasoning
are carried over from ``copilot.agent.tools``; the difference is that failures
``raise ToolError`` instead of returning ``{"found": false, "recoverable": ...}``.
"""

from __future__ import annotations

import difflib
from functools import lru_cache

from copilot.storage.db import get_conn
from copilot.v2.tools.base import ToolError, ToolErrorKind


@lru_cache(maxsize=1)
def known_tickers() -> frozenset[str]:
    """Tickers the database holds. Cached: changes only on ingestion."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM companies")
            return frozenset(r["ticker"].upper() for r in cur.fetchall())
    finally:
        conn.close()


@lru_cache(maxsize=64)
def latest_filing_year(ticker: str) -> int | None:
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


@lru_cache(maxsize=1)
def edge_sides() -> tuple[dict[str, int], dict[str, int]]:
    """(edges-as-supplier, edges-as-customer) counts per ticker. Cached."""
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


def resolve_ticker(ticker: str | None) -> str | None:
    """Canonical ticker, or ``None`` when nothing was asked.

    ``"No rows for SKWS"`` and ``"SKWS is not a company"`` are different facts;
    v1 measured a transposed ticker (SKWS for SWKS) becoming an authoritative
    "no supply-chain data for Skyworks". So an unknown ticker is a retryable
    ``ToolError`` with ``did_you_mean``, never a silent miss.
    """
    if not ticker:
        return None
    t = ticker.upper()
    known = known_tickers()
    if t in known:
        return t

    from copilot.agent.slots import find_companies
    named = [c for c in find_companies(ticker) if c in known]
    if len(named) == 1:
        return named[0]

    # difflib, not a prefix rule: the mistakes are transpositions and doubled
    # letters (SKWS/SWKS, APPL/AAPL), which a prefix match answers with a
    # confidently wrong neighbour.
    close = difflib.get_close_matches(t, sorted(known), n=3, cutoff=0.6)
    raise ToolError(
        ToolErrorKind.UNKNOWN_TICKER,
        f"{ticker!r} is not a company in this database. Use a known ticker; "
        f"an unknown ticker is a typo, not evidence the data is missing.",
        data={"asked_for": ticker, "did_you_mean": (close[:3] or named[:3])},
    )


def relation_side_error(customer: str | None, supplier: str | None) -> None:
    """Raise if exactly one side is named and that ticker only exists on the other.

    Concentration edges come from 10-K disclosures, which are supplier-reported:
    a company that files them is a supplier and the companies it names are
    customers. ``graph_query(customer="CRUS")`` (right company, wrong side) once
    returned nothing and the model reported it as an absence in the filings.
    Only checked when ONE side is given -- with both, an empty result is a real
    finding.
    """
    if bool(customer) == bool(supplier):
        return
    side, ticker = ("customer", customer) if customer else ("supplier", supplier)
    other = "supplier" if side == "customer" else "customer"
    as_supplier, as_customer = edge_sides()
    reversed_count = (as_supplier if other == "supplier" else as_customer).get(
        (ticker or "").upper(), 0)
    if not reversed_count:
        return  # no edges either way -- a genuine absence, not a wrong argument

    why = (f"{ticker} files these disclosures, naming the customers it depends on"
           if other == "supplier" else
           f"{ticker} is NAMED as a customer by its suppliers; it does not file "
           f"these disclosures itself")
    raise ToolError(
        ToolErrorKind.WRONG_RELATION_SIDE,
        f"{ticker} appears in this table only as a {other}, never as a {side}. "
        f"These edges are supplier-reported: {why}. Retry with {other}={ticker!r}. "
        f"This is a wrong argument, not missing data -- do not report {ticker} as "
        f"absent from the supply-chain graph.",
        data={"asked_for": {side: ticker}, "edges_if_reversed": reversed_count},
    )


def year_scope(query: str, ticker: str | None,
               fiscal_year: int | None) -> tuple[int | None, str]:
    """Which filing year to search, and why -- the reason travels into the result.

    A quarter of the corpus is boilerplate repeated across ~10 filings per
    company, so an unscoped top-5 fills with the same paragraph from four years.
    Scoping moved the retrieval probe hit@5 3/7 -> 5/7, MRR 0.286 -> 0.548. Almost
    nobody states a year, so the fallback to the newest filing is what makes it
    worth doing -- and because that is a narrowing the user did not ask for, the
    reason is returned so it can be surfaced.
    """
    if fiscal_year:
        return fiscal_year, "caller specified this fiscal year"
    from copilot.agent.slots import extract_slots
    s = extract_slots(query or "")
    if s["fiscal_year"]:
        return s["fiscal_year"], "the query names this fiscal year"
    if s["is_trend"] or len(s["years"]) > 1:
        return None, "the query spans more than one year, so it is not scoped"
    if not ticker:
        return None, "no company named, so there is no 'most recent filing' to pick"
    latest = latest_filing_year(ticker)
    if not latest:
        return None, f"no filings on record for {ticker}"
    return latest, (f"no year given; searched {ticker}'s most recent filing (FY{latest}) "
                    f"-- say so if the answer depends on it")
