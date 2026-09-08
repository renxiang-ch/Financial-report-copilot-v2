"""query_financials + list_metrics -- XBRL numeric facts from Postgres.

Domain SQL carried over from ``copilot.agent.tools`` unchanged. Differences:
uniform ``(content, artifact)`` return, ``ToolError`` instead of ``found:false``
dicts, ticker resolution via ``resolve``. Annual (10-K) only -- the stored 10-Q
rows are year-to-date cumulative, not discrete quarters (see v1's
``_QUARTERLY_UNSUPPORTED``); ``form`` is not exposed.
"""

from __future__ import annotations

from copilot.storage.db import get_conn
from copilot.v2.tools.base import ToolError, ToolErrorKind, financial_tool, pack
from copilot.v2.tools.resolve import resolve_ticker
from copilot.v2.tools.schemas import ListMetricsArgs, QueryFinancialsArgs

_FORM = "10-K"


def _citation(accn: str, doc_url: str | None) -> str:
    url = doc_url or (
        "https://www.sec.gov/Archives/edgar/data/"
        + accn.split("-")[0].lstrip("0") + "/" + accn.replace("-", "") + "/"
    )
    return f"SEC 10-K filing accession {accn} ({url})"


@financial_tool("query_financials", args_schema=QueryFinancialsArgs, cache=True,
                description="Query a single financial fact (XBRL) from the database. "
                "Always reads from SQL, never estimated. Annual 10-K figures only.")
def query_financials(ticker: str, metric: str, fiscal_year: int | None = None):
    ticker = resolve_ticker(ticker)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if fiscal_year:
                cur.execute(
                    """
                    SELECT f.ticker, f.label, f.value, f.unit, f.period_end,
                           f.fiscal_year, f.form, f.accn, fi.doc_url
                    FROM financial_facts f
                    LEFT JOIN filings fi ON fi.accn = f.accn
                    WHERE f.ticker = %s AND f.label = %s
                      AND f.fiscal_year = %s AND f.form = %s
                    ORDER BY f.period_end DESC
                    LIMIT 1
                    """,
                    (ticker.upper(), metric, fiscal_year, _FORM),
                )
            else:
                cur.execute(
                    """
                    SELECT f.ticker, f.label, f.value, f.unit, f.period_end,
                           f.fiscal_year, f.form, f.accn, fi.doc_url
                    FROM financial_facts f
                    LEFT JOIN filings fi ON fi.accn = f.accn
                    WHERE f.ticker = %s AND f.label = %s AND f.form = %s
                    ORDER BY f.period_end DESC
                    LIMIT 1
                    """,
                    (ticker.upper(), metric, _FORM),
                )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise ToolError(
            ToolErrorKind.NOT_FOUND,
            f"No {metric} row for {ticker}"
            + (f" in FY{fiscal_year}" if fiscal_year else "")
            + ". Call list_metrics to see which metrics/years exist for this company.",
            data={"ticker": ticker, "metric": metric, "fiscal_year": fiscal_year},
        )

    citation = _citation(row["accn"], row["doc_url"])
    artifact = {
        "found": True,
        "ticker": row["ticker"],
        "metric": row["label"],
        "value": float(row["value"]),
        "unit": row["unit"],
        "period_end": str(row["period_end"]),
        "fiscal_year": row["fiscal_year"],
        "form": row["form"],
        "citation": citation,
    }
    text = (f"{row['ticker']} {row['label']} FY{row['fiscal_year']} = "
            f"{float(row['value']):,.0f} {row['unit']} ({citation})")
    return pack(text, artifact)


@financial_tool("list_metrics", args_schema=ListMetricsArgs, cache=True,
                description="List the metrics and fiscal years available for a "
                "company. Call before a multi-step question to confirm coverage.")
def list_metrics(ticker: str):
    ticker = resolve_ticker(ticker)
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
                (ticker.upper(), _FORM),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise ToolError(
            ToolErrorKind.NOT_FOUND,
            f"No 10-K financial facts on record for {ticker}.",
            data={"ticker": ticker},
        )

    metrics = [
        {"metric": r["label"], "years_available": r["years"], "n_years": len(r["years"])}
        for r in rows
    ]
    artifact = {"found": True, "ticker": ticker.upper(), "form": _FORM, "metrics": metrics}
    preview = ", ".join(
        f"{m['metric']} (FY{m['years_available'][0]}-{m['years_available'][-1]})"
        for m in metrics[:8]
    )
    text = (f"{ticker.upper()}: {len(metrics)} metrics available. "
            f"{preview}{' ...' if len(metrics) > 8 else ''}")
    return pack(text, artifact)
