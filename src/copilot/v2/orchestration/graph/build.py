"""Assemble the ``create_agent`` agent.

v1's SYSTEM prompt, the standardized tool library (``copilot.v2.tools``), a
checkpointer, and the middleware stack: ``agent_middleware()`` (v1's pre-loop
policy as hooks, carrying its own state schema) plus the prebuilt tool
middleware -- ``ToolRetryMiddleware`` (retries ``ToolError.retryable``),
``ToolErrorMiddleware`` (turns the rest into model-visible messages),
``ToolCallLimitMiddleware`` (the framework's ``MAX_ROUNDS``).

The model is built explicitly from ``copilot.config.settings`` rather than from
an ``"openai:..."`` string, because the key lives in ``.env`` (loaded by
pydantic-settings) and is not exported to the environment that ``init_chat_model``
reads.

Checkpointer selection is ``COPILOT_CHECKPOINTER=memory|postgres`` (default
``memory``) -- see ``_checkpointer``.
"""

from __future__ import annotations

import os

from langchain.agents.middleware import (
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

# v1_loop's default agent model (see model_router.select_model / the eval runs).
DEFAULT_MODEL = "gpt-4o-mini"

# Matches v1's OpenAI client settings (copilot.agent.agent._ask_openai).
_LLM_TIMEOUT_S = 90
_MAX_RETRIES = 2

# Checkpoint pool. Separate from copilot.storage.db (psycopg2, domain queries) --
# langgraph-checkpoint-postgres needs psycopg 3. Two drivers, one database,
# different concerns; storage/db.py is shared/frozen and stays untouched.
_PG_POOL_MAX = 5
_pg_pool = None  # module-level so the singleton agent's pool outlives one call


def _checkpointer(kind: str | None = None):
    """``memory`` (default) or ``postgres``.

    Default stays in-memory so eval sweeps neither write checkpoint rows nor
    depend on DB state; ``postgres`` is opt-in via ``COPILOT_CHECKPOINTER``.

    ``PostgresSaver.from_conn_string`` is a context manager, which does not fit
    a module-level agent singleton -- so a ``ConnectionPool`` is held instead.
    The pool needs ``autocommit`` and ``dict_row``: the saver issues its own
    transactions and reads rows by name.
    """
    kind = (kind or os.getenv("COPILOT_CHECKPOINTER", "memory")).lower()
    if kind == "memory":
        return InMemorySaver()
    if kind != "postgres":
        raise ValueError(f"COPILOT_CHECKPOINTER must be memory|postgres, got {kind!r}")

    global _pg_pool
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    if _pg_pool is None:
        _pg_pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=_PG_POOL_MAX,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
    saver = PostgresSaver(_pg_pool)
    saver.setup()  # idempotent DDL -- safe to call on every build
    return saver


def _model(name: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=_LLM_TIMEOUT_S,
        max_retries=_MAX_RETRIES,
    )


def _model_fallback() -> list:
    """Alternate models tried when the primary call *errors*, from
    ``COPILOT_FALLBACK_MODELS`` (comma-separated). Empty (default) = no fallback.

    Models are passed as instances, not ``"openai:..."`` strings, for the same
    reason as ``_model``: the key lives in ``.env``, not the environment.

    This fires on **exceptions only** (429 / 5xx / timeout / context overflow),
    never on answer quality -- v1's ``model_router`` documents why escalating a
    bad-looking answer to a stronger model is the wrong reflex (a stronger model
    fabricates more fluently; the fix for a wrong figure is verification).
    """
    names = [n.strip() for n in os.getenv("COPILOT_FALLBACK_MODELS", "").split(",") if n.strip()]
    if not names:
        return []
    from langchain.agents.middleware import ModelFallbackMiddleware

    return [ModelFallbackMiddleware(*[_model(n) for n in names])]


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
    """Return a compiled agent. ``checkpointer=None`` -> ``_checkpointer()``."""
    from langchain.agents import create_agent

    # No `state_schema=` here: the `_resolve` middleware declares its own
    # (`ResolvedState`) and the factory merges middleware schemas at compile
    # time, so each state field stays next to the hook that writes it.
    return create_agent(
        model=_model(model),
        tools=TOOLS,
        system_prompt=SYSTEM,
        middleware=[*agent_middleware(), *_model_fallback(), *_tool_middleware()],
        checkpointer=checkpointer if checkpointer is not None else _checkpointer(),
    )
