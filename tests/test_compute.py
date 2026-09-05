"""Unit tests for the compute tool — pure Python, no external dependencies."""

from copilot.agent.tools import compute


def test_addition():
    assert compute("a + b", {"a": 1, "b": 2})["result"] == 3


def test_gross_margin():
    r = compute("(revenue - cogs) / revenue * 100", {"revenue": 100, "cogs": 60})
    assert abs(r["result"] - 40.0) < 0.001


def test_yoy_negative():
    r = compute("(new - old) / old * 100", {"new": 90, "old": 100})
    assert abs(r["result"] - (-10.0)) < 0.001


def test_order_cut_impact():
    r = compute("revenue * pct / 100 * cut", {"revenue": 1000, "pct": 46, "cut": 0.20})
    assert abs(r["result"] - 92.0) < 0.001


def test_division_by_zero_returns_error():
    result = compute("a / b", {"a": 1, "b": 0})
    assert "error" in result


def test_unsafe_expression_blocked():
    result = compute("__import__('os').system('ls')", {})
    assert "error" in result
