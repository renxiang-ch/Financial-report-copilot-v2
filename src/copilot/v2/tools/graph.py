"""graph_query -- the supply_edges graph (single-hop + recursive-CTE multi-hop).

SQL carried over from ``copilot.agent.tools`` unchanged, including the
fiscal-year resolution and the ``revenue_pct`` / ``pct_display`` split (a
threshold-only edge is printed as ">10%" but still supplies the disclosed floor
for arithmetic). Differences: ``(content, artifact)`` return, ``ToolError`` for
a wrong relation side, ticker resolution via ``resolve``.
"""

from __future__ import annotations

import re

from copilot.storage.db import get_conn
from copilot.v2.tools.base import financial_tool, pack
from copilot.v2.tools.resolve import relation_side_error, resolve_ticker
from copilot.v2.tools.schemas import GraphQueryArgs


def _empty(customer, supplier, fy_label, reason):
    hub = customer or supplier
    text = (f"No disclosed supply-chain edge for {hub} in {fy_label}. {reason}")
    return pack(text, {"found": False, "hub": hub, "fiscal_year": fy_label,
                       "edges": [], "reason": reason})


@financial_tool("graph_query", args_schema=GraphQueryArgs,
                description="Query the supply-chain graph (customer<-supplier "
                "concentration edges from 10-K disclosures). Include the "
                "traversal_trace in the final answer for citations.")
def graph_query(customer: str | None = None, supplier: str | None = None,
                fiscal_year: str = "latest", depth: int = 1):
    customer = resolve_ticker(customer)
    supplier = resolve_ticker(supplier)
    relation_side_error(customer, supplier)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            fy_clause, fy_params, fy_label = "", [], str(fiscal_year)

            if fiscal_year == "latest" or fiscal_year is None:
                ticker_col = "supplier_ticker" if supplier else "customer_ticker"
                ticker_val = (supplier or customer or "").upper()
                cur.execute(
                    f"SELECT MAX(fiscal_year) FROM supply_edges "
                    f"WHERE {ticker_col} = %s AND disclosure_status='named'",
                    (ticker_val,),
                )
                row_fy = cur.fetchone()["max"]
                if row_fy is None:
                    conn.close()
                    return _empty(customer, supplier, "latest",
                                 "No named edges on the side that was asked about.")
                fy_clause, fy_params, fy_label = "AND e.fiscal_year = %s", [row_fy], str(row_fy)

            elif fiscal_year == "trend":
                fy_clause, fy_params, fy_label = "", [], "all years"

            elif isinstance(fiscal_year, str) and re.match(r"\d{4}-\d{4}", fiscal_year):
                y_start, y_end = fiscal_year.split("-")
                fy_clause = "AND e.fiscal_year BETWEEN %s AND %s"
                fy_params = [int(y_start), int(y_end)]
                fy_label = fiscal_year

            else:
                fy_single = int(fiscal_year)
                fy_clause = "AND e.fiscal_year = %s"
                fy_params, fy_label = [fy_single], str(fy_single)

            if depth == 1:
                if customer and supplier:
                    ticker_clause = "e.customer_ticker = %s AND e.supplier_ticker = %s"
                    ticker_param = [customer.upper(), supplier.upper()]
                elif customer:
                    ticker_clause = "e.customer_ticker = %s"
                    ticker_param = [customer.upper()]
                elif supplier:
                    ticker_clause = "e.supplier_ticker = %s"
                    ticker_param = [supplier.upper()]
                else:
                    return _empty(customer, supplier, fy_label,
                                 "Provide a customer or supplier ticker.")

                cur.execute(
                    f"""
                    SELECT e.supplier_ticker, e.customer_ticker,
                           e.revenue_pct, e.threshold_only, e.fiscal_year,
                           e.disclosure_status, e.accn, e.source_text, f.doc_url
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
                if not fy_params:
                    cur.execute(
                        "SELECT MAX(fiscal_year) FROM supply_edges WHERE disclosure_status='named'"
                    )
                    fy_params = [cur.fetchone()["max"]]
                anchor = customer or supplier
                direction = "customer_ticker" if customer else "supplier_ticker"
                follow = "supplier_ticker" if customer else "customer_ticker"
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
        return _empty(customer, supplier, fy_label,
                      "That pair has no disclosed edge for the period.")

    edges, trace = [], []
    for row in rows:
        accn = row["accn"] or ""
        citation = (f"SEC 10-K accession {accn} ({row.get('doc_url', '')})"
                    if accn else "accession not available")
        is_threshold = bool(row.get("threshold_only", False))
        pct_display = ">10%" if is_threshold else f"{row['revenue_pct']}%"
        edges.append({
            "supplier": row["supplier_ticker"], "customer": row["customer_ticker"],
            "revenue_pct": row["revenue_pct"], "pct_display": pct_display,
            "pct_is_floor": is_threshold, "threshold_only": is_threshold,
            "fiscal_year": row["fiscal_year"], "disclosure_status": row["disclosure_status"],
            "citation": citation, "source_text": (row.get("source_text") or "")[:400],
        })
        pct_str = (f">10% (floor; revenue_pct={row['revenue_pct']} is the disclosed "
                   f"lower bound -- exact % not stated in the 10-K)"
                   if is_threshold else pct_display)
        trace.append(f"{row['supplier_ticker']}->{row['customer_ticker']} "
                     f"{pct_str} FY{row['fiscal_year']} [{accn}]")

    hub = customer or supplier
    artifact = {"found": True, "hub": hub, "fiscal_year": fy_label,
                "edge_count": len(edges), "edges": edges, "traversal_trace": trace}
    text = f"{len(edges)} edge(s) for {hub} ({fy_label}): " + "; ".join(trace[:4])
    return pack(text, artifact)
