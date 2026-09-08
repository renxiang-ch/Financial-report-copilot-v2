"""Assemble the ``create_agent`` agent.

v1's SYSTEM prompt, the standardized tool library (``copilot.v2.tools``), an
in-memory checkpointer, ``GraphState`` (adds ``resolved``), and the middleware
stack: ``agent_middleware()`` (v1's pre-loop policy as hooks) plus the prebuilt
tool middleware -- ``ToolRetryMiddleware`` (retries ``ToolError.retryable``),
``ToolErrorMiddleware`` (turns the rest into model-visible messages),
``ToolCallLimitMiddleware`` (the framework's ``MAX_ROUNDS``).

The model is built explicitly from ``copilot.config.settings`` rather than from
an ``"openai:..."`` string, because the key lives in ``.env`` (loaded by
pydantic-settings) and is not exported to the environment that ``init_chat_model``
reads.
"""

from __future__ import annotations

from typing import NotRequired

from langchain.agents.middleware import (
    AgentState,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from copilot.agent.agent import SYSTEM
from copilot.config import settings
from copilot.v2.orchestration.graph.middleware import agent_middleware, on_tool_error
from copilot.v2.tools.base import ToolError
from copilot.v2.tools.registry import TOOLS


class GraphState(AgentState):
    # Resolved once per turn by the `_resolve` before_model hook: the raw
    # question + the fiscal year slots inherited (needs message history, so it
    # can't live on the immutable context). Tools read runtime.state["resolved"].
    resolved: NotRequired[dict]

# v1_loop's default agent model (see model_router.select_model / the eval runs).
DEFAULT_MODEL = "gpt-4o-mini"

# Matches v1's OpenAI client settings (copilot.agent.agent._ask_openai).
_LLM_TIMEOUT_S = 90
_MAX_RETRIES = 2


def _model(name: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=_LLM_TIMEOUT_S,
        max_retries=_MAX_RETRIES,
    )


def _tool_middleware() -> list:
    # Order: retry INNER (runs first), error OUTER. Retry re-raises after its
    # budget (on_failure="error") so the exhausted exception reaches the error
    # middleware; `on_tool_error` decides disclose-vs-propagate. Limit is a
    # separate circuit breaker.
    return [
        ToolRetryMiddleware(
            max_retries=2,
            retry_on=lambda e: isinstance(e, ToolError) and e.retryable,
            on_failure="error",
        ),
        ToolErrorMiddleware(on_error=on_tool_error),
        ToolCallLimitMiddleware(run_limit=12, thread_limit=40, exit_behavior="continue"),
    ]


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None):
    """Return a compiled agent. ``checkpointer=None`` -> a fresh InMemorySaver."""
    from langchain.agents import create_agent

    return create_agent(
        model=_model(model),
        tools=TOOLS,
        system_prompt=SYSTEM,
        state_schema=GraphState,
        middleware=[*agent_middleware(), *_tool_middleware()],
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
    )
