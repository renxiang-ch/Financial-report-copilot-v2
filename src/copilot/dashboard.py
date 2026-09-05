"""Deterministic dashboard queries — direct SQL/tool calls, no LLM involved.

Backs the three Analyst Workflow Dashboard pages (Company Exposure, Supplier
Deep Dive, What-if) via lightweight FastAPI endpoints in api.py. Reuses the
same query_financials/graph_query functions the chat agent uses as tools, so
numbers and citations are guaranteed consistent between the chat and the
dashboard — same SQL, same source_text, same accession numbers.
"""

from copilot.agent.tools import graph_query, query_financials
from copilot.storage.db import get_conn


def _row_to_edge(row: dict) -> dict:
    accn = row["accn"] or ""
    doc_url = row.get("doc_url") or ""
    pct = row["revenue_pct"]
    return {
        "supplier": row["supplier_ticker"],
        "customer": row["customer_ticker"],
        "revenue_pct": float(pct) if pct is not None else None,
        "threshold_only": bool(row["threshold_only"]),
        "fiscal_year": row["fiscal_year"],
        "citation": f"SEC 10-K accession {accn} ({doc_url})" if accn else "accession not available",
        "source_text": (row.get("source_text") or "")[:400],
    }


def get_available_years(customer: str = "AAPL") -> dict:
    """Distinct fiscal years with at least one named disclosure for `customer`, newest first."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT fiscal_year
                FROM supply_edges
                WHERE customer_ticker = %s AND disclosure_status = 'named'
                ORDER BY fiscal_year DESC
                """,
                (customer.upper(),),
            )
            years = [r["fiscal_year"] for r in cur.fetchall()]
    finally:
        conn.close()
    return {"customer": customer.upper(), "years": years}


def get_exposure(customer: str = "AAPL", fiscal_year: str | int | None = None) -> dict:
    """
    Every named supplier's disclosed dependency on `customer`.

    fiscal_year=None/"latest": each supplier's OWN most recent disclosed year
    (suppliers file on different schedules, so this is per-supplier max, not
    one global year — graph_query's "latest" mode resolves a single shared
    year for the whole query, which isn't what a fan-out exposure view needs).
    An explicit year filters to that year only (suppliers with no data that
    year are omitted).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if fiscal_year and fiscal_year != "latest":
                cur.execute(
                    """
                    SELECT e.supplier_ticker, e.customer_ticker, e.revenue_pct, e.threshold_only,
                           e.fiscal_year, e.accn, e.source_text, f.doc_url
                    FROM supply_edges e
                    LEFT JOIN filings f ON f.accn = e.accn
                    WHERE e.customer_ticker = %s AND e.disclosure_status = 'named' AND e.fiscal_year = %s
                    """,
                    (customer.upper(), int(fiscal_year)),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT ON (e.supplier_ticker)
                           e.supplier_ticker, e.customer_ticker, e.revenue_pct, e.threshold_only,
                           e.fiscal_year, e.accn, e.source_text, f.doc_url
                    FROM supply_edges e
                    LEFT JOIN filings f ON f.accn = e.accn
                    WHERE e.customer_ticker = %s AND e.disclosure_status = 'named'
                    ORDER BY e.supplier_ticker, e.fiscal_year DESC
                    """,
                    (customer.upper(),),
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    edges = [_row_to_edge(r) for r in rows]
    edges.sort(key=lambda e: e["revenue_pct"] or 0, reverse=True)
    return {"found": bool(edges), "customer": customer.upper(), "edges": edges}


def get_supplier_trend(ticker: str, customer: str = "AAPL") -> dict:
    """Multi-year dependency-% trend and revenue trend for one supplier→customer pair."""
    graph = graph_query(customer=customer, supplier=ticker, fiscal_year="trend")
    if not graph["found"]:
        return {"found": False, "ticker": ticker.upper(), "customer": customer.upper()}

    trend = []
    for edge in sorted(graph["edges"], key=lambda e: e["fiscal_year"]):
        fy = edge["fiscal_year"]
        rev = query_financials(ticker, "Revenue", fiscal_year=fy)
        trend.append({
            "fiscal_year": fy,
            "revenue_pct": edge["revenue_pct"],
            "threshold_only": edge["threshold_only"],
            "citation": edge["citation"],
            "source_text": edge["source_text"],
            "revenue": rev["value"] if rev.get("found") else None,
        })
    return {"found": True, "ticker": ticker.upper(), "customer": customer.upper(), "trend": trend}


def get_whatif(customer: str = "AAPL", cut_pct: float = 20.0, fiscal_year: str | int | None = None) -> dict:
    """Dollar impact per supplier of a `cut_pct`% cut to `customer`'s orders, ranked by loss."""
    exposure = get_exposure(customer=customer, fiscal_year=fiscal_year)
    if not exposure["found"]:
        return {"found": False, "customer": customer.upper()}

    results = []
    for edge in exposure["edges"]:
        if edge["revenue_pct"] is None:
            continue
        rev = query_financials(edge["supplier"], "Revenue", fiscal_year=edge["fiscal_year"])
        if not rev.get("found"):
            continue
        dollar_loss = rev["value"] * edge["revenue_pct"] / 100 * cut_pct / 100
        results.append({
            "supplier": edge["supplier"],
            "fiscal_year": edge["fiscal_year"],
            "revenue_pct": edge["revenue_pct"],
            "threshold_only": edge["threshold_only"],
            "supplier_revenue": rev["value"],
            "dollar_loss": dollar_loss,
            "citation": edge["citation"],
        })
    results.sort(key=lambda r: r["dollar_loss"], reverse=True)
    return {"found": bool(results), "customer": customer.upper(), "cut_pct": cut_pct, "results": results}
