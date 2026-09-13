"""How a ``ToolError`` actually reaches the model, end to end through the graph.

Until devlog 007 §3.2 nothing tested this. The tool tests assert the *value* of
``ToolError.model_correctable``; nothing asserted what the middleware stack then
*does*. That gap hid a real defect for five A/B rounds: a ``ToolRetryMiddleware``
wired on that flag, nested outside ``ToolErrorMiddleware``, so it never saw an
exception and never retried -- and, separately, retrying those kinds mechanically
would have been wrong anyway (same arguments, same deterministic failure).

These tests pin the behaviour that matters: the tool body runs **once**, and the
model receives the hint so it can correct the call itself.

A fake model keeps this free and deterministic -- no key, no network, no spend.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from copilot.v2.orchestration.graph.build import _tool_middleware
from copilot.v2.tools.base import ToolError, ToolErrorKind


class _ToolCallingFake(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` has no ``bind_tools``; ``create_agent`` needs one."""

    def bind_tools(self, tools, **kwargs):
        return self


def _run(failing_tool, *, turns: int = 4):
    """Drive one agent turn: the model calls the tool, then answers."""
    model = _ToolCallingFake(responses=[
        AIMessage(content="", tool_calls=[
            {"id": "c1", "name": failing_tool.name, "args": {"ticker": "CRUSS"}}]),
        *[AIMessage(content="done") for _ in range(turns)],
    ])
    agent = create_agent(model=model, tools=[failing_tool],
                         middleware=_tool_middleware())
    return agent.invoke({"messages": [{"role": "user", "content": "q"}]})


def _tool_messages(result) -> list[ToolMessage]:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def test_model_correctable_error_executes_the_tool_exactly_once():
    """The regression test for the defect the span tree exposed.

    ``model_correctable`` means "the model can fix this by calling again with
    better arguments" -- NOT "safe to re-execute unchanged". A mechanical retry
    would run this body 3x for an identical deterministic failure.
    """
    calls = []

    @tool
    def flaky(ticker: str) -> str:
        """Look up a ticker."""
        calls.append(ticker)
        raise ToolError(ToolErrorKind.UNKNOWN_TICKER, "use a listed ticker",
                        data={"did_you_mean": ["CRUS"]})

    assert ToolError(ToolErrorKind.UNKNOWN_TICKER, "x").model_correctable is True

    _run(flaky)
    assert calls == ["CRUSS"], f"tool body ran {len(calls)}x, expected exactly 1"


def test_error_is_disclosed_to_the_model_with_its_hint():
    """Disclosure IS the recovery path, so it must carry enough to act on."""
    @tool
    def flaky(ticker: str) -> str:
        """Look up a ticker."""
        raise ToolError(ToolErrorKind.UNKNOWN_TICKER, "use a listed ticker",
                        data={"did_you_mean": ["CRUS", "CRSR"]})

    msgs = _tool_messages(_run(flaky))
    assert len(msgs) == 1
    body = msgs[0].content
    assert "unknown_ticker" in body
    assert "use a listed ticker" in body
    assert "CRUS" in body, "did_you_mean must survive into the model's view"


def test_terminal_error_is_also_disclosed_not_raised():
    """A terminal kind still reaches the model: 'no such row' is an answer, and
    v1's lesson was that swallowing it produces false authority."""
    @tool
    def flaky(ticker: str) -> str:
        """Look up a ticker."""
        raise ToolError(ToolErrorKind.NOT_FOUND, "no rows for fiscal 1999")

    assert ToolError(ToolErrorKind.NOT_FOUND, "x").model_correctable is False

    msgs = _tool_messages(_run(flaky))
    assert len(msgs) == 1
    assert "not_found" in msgs[0].content


def test_unexpected_exception_propagates_rather_than_being_hidden():
    """``on_tool_error`` returns ``None`` for non-``ToolError``, which halts the
    run. A bug in a tool must not be laundered into a plausible answer."""
    @tool
    def broken(ticker: str) -> str:
        """Look up a ticker."""
        raise KeyError("ticker column missing")

    try:
        _run(broken)
    except Exception as exc:
        assert isinstance(exc, KeyError) or "ticker column missing" in str(exc)
    else:
        raise AssertionError("an unexpected exception must not be disclosed as a "
                             "ToolMessage")


def test_stack_carries_no_retry_middleware():
    """Guards the decision itself: re-adding a retry middleware here silently
    multiplies tool executions for deterministic argument errors. Phase 4 may add
    one for genuinely transient HTTP sources -- deliberately, not by reflex."""
    names = [type(m).__name__ for m in _tool_middleware()]
    assert "ToolRetryMiddleware" not in names
    assert "ToolErrorMiddleware" in names
    assert "ToolCallLimitMiddleware" in names
