"""Phase 1 tool library: resolve layer, per-tool happy + error paths, error middleware.

Hits the real DB (seed snapshot loaded, like test_slots / test_conversation).
``retrieve_text`` retrieval is not exercised here -- sentence_transformers is
mocked in conftest -- only its arg/resolve behaviour.
"""

from __future__ import annotations

import pytest

from copilot.v2.tools import resolve
from copilot.v2.tools.base import ToolError, ToolErrorKind, _Memo
from copilot.v2.tools.compute import compute
from copilot.v2.tools.financials import list_metrics, query_financials
from copilot.v2.tools.graph import graph_query
from copilot.v2.tools.registry import TOOLS
from copilot.v2.tools.schemas import QueryFinancialsArgs

_TC = {"id": "t1", "type": "tool_call"}


def _msg(tool, args):
    return tool.invoke({"name": tool.name, "args": args, **_TC})


# ── resolve ──────────────────────────────────────────────────────────────────

def test_resolve_ticker_known_and_none():
    assert resolve.resolve_ticker("AAPL") == "AAPL"
    assert resolve.resolve_ticker("aapl") == "AAPL"
    assert resolve.resolve_ticker(None) is None


def test_resolve_ticker_typo_raises_retryable():
    with pytest.raises(ToolError) as ei:
        resolve.resolve_ticker("APPL")
    assert ei.value.kind is ToolErrorKind.UNKNOWN_TICKER
    assert ei.value.retryable is True
    assert "AAPL" in ei.value.data["did_you_mean"]


def test_relation_side_error_wrong_side():
    with pytest.raises(ToolError) as ei:
        resolve.relation_side_error("CRUS", None)   # CRUS files as a supplier
    assert ei.value.kind is ToolErrorKind.WRONG_RELATION_SIDE
    assert ei.value.data["edges_if_reversed"] > 0


def test_relation_side_error_both_given_is_noop():
    assert resolve.relation_side_error("AAPL", "AVGO") is None


def test_year_scope_explicit_trend_and_latest():
    assert resolve.year_scope("x", "AAPL", 2024)[0] == 2024
    assert resolve.year_scope("how has revenue changed since 2019?", "AAPL", None)[0] is None
    y, why = resolve.year_scope("what are the risk factors?", "AAPL", None)
    assert isinstance(y, int) and "most recent filing" in why


# ── schemas ──────────────────────────────────────────────────────────────────

def test_metric_validator_rejects_unknown_label():
    QueryFinancialsArgs(ticker="AAPL", metric="Revenue")  # ok
    with pytest.raises(ToolError) as ei:
        QueryFinancialsArgs(ticker="AAPL", metric="DilutedEPS")
    assert ei.value.kind is ToolErrorKind.BAD_ARGUMENT
    assert "EPS_Diluted" in ei.value.data["did_you_mean"]


# ── query_financials / list_metrics ──────────────────────────────────────────

def test_query_financials_happy():
    m = _msg(query_financials, {"ticker": "AAPL", "metric": "Revenue", "fiscal_year": 2024})
    assert m.artifact["found"] is True
    assert m.artifact["value"] == pytest.approx(391_035_000_000, rel=1e-6)
    assert "accession" in m.artifact["citation"]
    assert "AAPL Revenue FY2024" in m.content


def test_query_financials_not_found_is_terminal():
    with pytest.raises(ToolError) as ei:
        _msg(query_financials, {"ticker": "AAPL", "metric": "Revenue", "fiscal_year": 1999})
    assert ei.value.kind is ToolErrorKind.NOT_FOUND
    assert ei.value.retryable is False


def test_list_metrics_happy():
    m = _msg(list_metrics, {"ticker": "CRUS"})
    assert m.artifact["found"] is True
    assert any(x["metric"] == "Revenue" for x in m.artifact["metrics"])


# ── graph_query ──────────────────────────────────────────────────────────────

def test_graph_query_happy_pair():
    m = _msg(graph_query, {"customer": "AAPL", "supplier": "AVGO", "fiscal_year": "latest"})
    assert m.artifact["found"] is True
    assert m.artifact["edge_count"] >= 1
    assert m.artifact["edges"][0]["customer"] == "AAPL"


def test_graph_query_wrong_side_raises():
    with pytest.raises(ToolError) as ei:
        _msg(graph_query, {"customer": "CRUS"})
    assert ei.value.kind is ToolErrorKind.WRONG_RELATION_SIDE


def test_graph_query_genuine_absence_is_not_an_error():
    # a real pair with (almost certainly) no disclosed edge
    m = _msg(graph_query, {"customer": "AVGO", "supplier": "CRUS", "fiscal_year": "2020"})
    assert m.artifact["found"] is False
    assert "reason" in m.artifact


# ── compute ──────────────────────────────────────────────────────────────────

def test_compute_happy():
    m = _msg(compute, {"expression": "a / b * 100", "variables": {"a": 50.0, "b": 200.0}})
    assert m.artifact["ok"] is True
    assert m.artifact["result"] == pytest.approx(25.0)


def test_compute_non_identifier_var():
    # `R&D` is not a valid Python identifier -- aliased internally, not rejected.
    m = _msg(compute, {"expression": "R&D * 2", "variables": {"R&D": 7.0}})
    assert m.artifact["result"] == pytest.approx(14.0)
    assert m.artifact["expression"] == "R&D * 2"  # caller's original is preserved


def test_compute_rejects_unsafe():
    with pytest.raises(ToolError) as ei:
        _msg(compute, {"expression": "__import__('os').system('x')", "variables": {}})
    assert ei.value.kind is ToolErrorKind.BAD_EXPRESSION
    assert ei.value.retryable is False


# ── error middleware handler ─────────────────────────────────────────────────

def test_on_tool_error_discloses_toolerror_only():
    from types import SimpleNamespace

    from copilot.v2.orchestration.graph.middleware import on_tool_error

    req = SimpleNamespace(tool_call={"name": "query_financials", "id": "t1"})
    te = ToolError(ToolErrorKind.UNKNOWN_TICKER, "typo", data={"did_you_mean": ["AAPL"]})
    out = on_tool_error(te, req)
    assert "query_financials" in out and "typo" in out and "AAPL" in out
    assert on_tool_error(ValueError("boom"), req) is None


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_memo_caches():
    calls = []
    memo = _Memo()

    def fn(**kw):
        calls.append(kw)
        return kw["x"] * 2

    assert memo.get_or_call(fn, {"x": 3}) == 6
    assert memo.get_or_call(fn, {"x": 3}) == 6
    assert len(calls) == 1


def test_registry_shape():
    assert [t.name for t in TOOLS] == [
        "query_financials", "list_metrics", "retrieve_text", "graph_query", "compute",
    ]
    # runtime is injected, hidden from the model schema
    rt = next(t for t in TOOLS if t.name == "retrieve_text")
    assert "runtime" not in rt.args
