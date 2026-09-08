"""retrieve_text -- hybrid BM25 + dense (RRF) search over 10-K text chunks.

Fusion logic is ``copilot.retrieval.hybrid`` unchanged. Neither ``query`` nor a
cross-turn ``fiscal_year`` is a model argument: v1 overwrites the query with the
raw user question every turn (a compressed rewrite measurably hurt recall) and
inherits the year from earlier turns. Both come from ``runtime.state["resolved"]``
(set by the ``_resolve`` middleware), with the last human message as a fallback.
Single-call year scoping (incl. the "latest filing" fallback) stays in
``resolve.year_scope``; the reason travels into the result.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage

from copilot.retrieval.hybrid import retrieve_hybrid as _retrieve
from copilot.v2.tools.base import ToolError, ToolErrorKind, financial_tool, pack
from copilot.v2.tools.resolve import resolve_ticker, year_scope
from copilot.v2.tools.schemas import RetrieveTextArgs


def _resolved(runtime: ToolRuntime) -> dict:
    return (getattr(runtime, "state", None) or {}).get("resolved") or {}


def _question(runtime: ToolRuntime) -> str:
    if q := _resolved(runtime).get("question"):
        return q
    state = getattr(runtime, "state", None) or {}
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


@financial_tool("retrieve_text", args_schema=RetrieveTextArgs,
                description="Search 10-K prose (risk factors, MD&A, business "
                "commentary) with hybrid retrieval. Never use for numeric data.")
def retrieve_text(runtime: ToolRuntime, ticker: str | None = None, k: int = 5,
                  fiscal_year: int | None = None):
    query = _question(runtime)
    ticker = resolve_ticker(ticker)
    # model arg wins; else the year the resolve middleware inherited; else scope.
    year, why = year_scope(query, ticker, fiscal_year or _resolved(runtime).get("fiscal_year"))

    results = _retrieve(query, ticker=ticker, k=k, fiscal_year=year)
    # A year the caller never asked for must not turn a hit into a miss.
    if not results and year is not None and fiscal_year is None:
        results = _retrieve(query, ticker=ticker, k=k)
        year, why = None, ("scoping to the most recent filing found nothing, "
                           "so all years were searched")

    if not results:
        raise ToolError(
            ToolErrorKind.NOT_FOUND,
            "No matching 10-K passages for this query"
            + (f" in {ticker}'s filings" if ticker else "")
            + (f" (FY{year})" if year else "") + ".",
            data={"ticker": ticker, "scoped_to_fiscal_year": year, "scope_reason": why},
        )

    # `id` is internal to the RRF fusion -- drop it so the model never mistakes
    # it for a citation. The only citable handle is the SEC accession.
    clean = [{k_: v for k_, v in r.items() if k_ != "id"} for r in results]
    artifact = {
        "found": True, "query": query, "ticker": ticker,
        "scoped_to_fiscal_year": year, "scope_reason": why, "results": clean,
    }
    # retrieve_text's whole job is to feed prose to the model, so the passage
    # TEXT goes in content, not just the artifact -- unlike the numeric tools,
    # a summary the model can't read the passages behind is useless. The
    # artifact keeps the full records (scores, section, etc.) for provenance.
    header = (f"{len(clean)} passage(s)"
              + (f" from {ticker}" if ticker else "")
              + (f", FY{year}" if year else "") + f" ({why}):")
    blocks = []
    for i, r in enumerate(clean, 1):
        body = (r.get("text") or "").strip().replace("\n", " ")
        cite = r.get("citation") or f"accession {r.get('accn', '?')}"
        blocks.append(f"[{i}] {cite}\n{body[:700]}")
    return pack(header + "\n\n" + "\n\n".join(blocks), artifact)
