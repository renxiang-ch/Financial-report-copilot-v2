"""Minimal ``@tool`` wrappers around the frozen v1 tool functions.

Step 1 of the port: NOT the standardized tool-layer rebuild -- these are thin
adapters so ``create_agent`` has something to call. Descriptions are lifted from
v1's hand-tuned ``TOOL_SCHEMAS`` (``copilot.agent.agent``) so the model sees the
same guidance. Each returns a JSON string, matching what v1's ``_run_tool`` fed
back into the loop, so the runner can rebuild v1-shaped ``steps``.

``retrieve_text``'s query is not a model-chosen argument: v1 overwrites it with
the raw user question every time (a compressed rewrite measurably hurt recall).
Here it comes from injected state instead.
"""

from __future__ import annotations

import json
from typing import Annotated

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from copilot.agent import tools as _v1
from copilot.agent.agent import TOOL_SCHEMAS


def _desc(name: str) -> str:
    """v1's tool description + its per-parameter guidance appended.

    v1 puts load-bearing hints on the *parameter* descriptions -- e.g. the exact
    list of stored metric labels lives on ``metric``, not on the tool. The
    minimal wrappers don't carry a per-arg schema, so fold it into the tool
    description instead.
    """
    fn = next(s["function"] for s in TOOL_SCHEMAS if s["function"]["name"] == name)
    parts = [fn["description"]]
    for pname, pspec in (fn.get("parameters", {}).get("properties") or {}).items():
        if pspec.get("description"):
            parts.append(f"- {pname}: {pspec['description']}")
    return "\n".join(parts)


@tool("query_financials", description=_desc("query_financials"))
def query_financials(ticker: str, metric: str, fiscal_year: int | None = None) -> str:
    return json.dumps(_v1.query_financials(ticker, metric, fiscal_year))


@tool("list_metrics", description=_desc("list_metrics"))
def list_metrics(ticker: str) -> str:
    return json.dumps(_v1.list_metrics(ticker))


@tool("compute", description=_desc("compute"))
def compute(expression: str, variables: dict) -> str:
    return json.dumps(_v1.compute(expression, variables))


@tool("retrieve_text", description=_desc("retrieve_text"))
def retrieve_text(
    ticker: str | None = None,
    k: int = 5,
    fiscal_year: int | None = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    # v1 overwrites the query with the raw user question every time -- a
    # compressed rewrite measurably hurt recall. Here it comes from state.
    question = ""
    for m in reversed((state or {}).get("messages", [])):
        if isinstance(m, HumanMessage):
            question = m.content if isinstance(m.content, str) else str(m.content)
            break
    return json.dumps(_v1.retrieve_text(question, ticker=ticker, k=k, fiscal_year=fiscal_year))


@tool("graph_query", description=_desc("graph_query"))
def graph_query(
    customer: str | None = None,
    supplier: str | None = None,
    fiscal_year: str = "latest",
    depth: int = 1,
) -> str:
    return json.dumps(_v1.graph_query(customer=customer, supplier=supplier,
                                      fiscal_year=fiscal_year, depth=depth))


TOOLS = [query_financials, list_metrics, compute, retrieve_text, graph_query]
