"""Assemble the ``create_agent`` agent.

v1's SYSTEM prompt, the standardized tool library (``copilot.v2.tools``), a
checkpointer, and the middleware stack: ``agent_middleware()`` (v1's pre-loop
policy as hooks, carrying its own state schema) plus the prebuilt tool
middleware -- ``ToolErrorMiddleware`` (turns a ``ToolError`` into a model-visible
message) and ``ToolCallLimitMiddleware`` (the framework's ``MAX_ROUNDS``).
See ``_tool_middleware`` for why there is no retry middleware.

The model is built explicitly from ``copilot.config.settings`` rather than from
an ``"openai:..."`` string, because the key lives in ``.env`` (loaded by
pydantic-settings) and is not exported to the environment that ``init_chat_model``
reads.

Checkpointer selection is ``COPILOT_CHECKPOINTER=memory|postgres`` (default
``memory``) -- see ``_checkpointer``.

LangSmith tracing is switched on by ``LANGSMITH_TRACING`` and needs no code here
beyond ``enable_tracing()``, which exists because the key lives in ``.env`` where
the SDK cannot see it -- see ``copilot.v2.observability``.
"""

from __future__ import annotations

import os

from langchain.agents.middleware import (
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from copilot.agent.agent import SYSTEM
from copilot.config import settings
from copilot.v2.observability import enable_tracing
from copilot.v2.orchestration.graph.middleware import agent_middleware, on_tool_error
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
    """Error disclosure + a circuit breaker. Deliberately no ``ToolRetryMiddleware``.

    Phase 1 wired one on ``ToolError.retryable``, and the LangSmith span tree in
    devlog 007 §3.2 showed it nested *outside* ``ToolErrorMiddleware`` -- so the
    inner error middleware converted every ``ToolError`` to a ``ToolMessage`` and
    retry never saw an exception. Dead code for five A/B rounds.

    Swapping the order was the wrong fix. Every kind flagged
    ``model_correctable`` (unknown ticker, wrong relation side, bad argument) is a
    *deterministic* argument error, and ``ToolRetryMiddleware`` re-runs the tool
    with the same arguments: measured 3x the tool executions for an identical
    failure, then the same disclosure. The recovery path that works is the model
    calling again with better arguments, which disclosure already drives.

    A retry middleware earns its place when a genuinely *transient* failure exists
    to retry -- external HTTP sources in Phase 4 (EDGAR / 8-K). Everything here is
    local Postgres.
    """
    return [
        ToolErrorMiddleware(on_error=on_tool_error),
        ToolCallLimitMiddleware(run_limit=12, thread_limit=40, exit_behavior="continue"),
    ]


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None):
    """Return a compiled agent. ``checkpointer=None`` -> ``_checkpointer()``."""
    from langchain.agents import create_agent

    # Bridge LANGSMITH_* from .env before the agent can run; a no-op when tracing
    # is off. An explicit call rather than an import side effect, for the same
    # reason `_model` constructs ChatOpenAI explicitly instead of leaning on the
    # environment (devlog 004 D1).
    enable_tracing()

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
