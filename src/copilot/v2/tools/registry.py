"""The single tool list the graph binds. Import ``TOOLS`` from here."""

from __future__ import annotations

from copilot.v2.tools.compute import compute
from copilot.v2.tools.financials import list_metrics, query_financials
from copilot.v2.tools.graph import graph_query
from copilot.v2.tools.retrieval import retrieve_text

TOOLS = [query_financials, list_metrics, retrieve_text, graph_query, compute]

__all__ = ["TOOLS"]
