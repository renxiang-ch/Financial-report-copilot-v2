"""Assemble the ``create_agent`` agent.

Step 1: bare prebuilt harness -- v1's SYSTEM prompt, the minimal tool wrappers,
an in-memory checkpointer. No middleware yet (routing / clarify / slot carry /
history trim land in later steps).

The model is built explicitly from ``copilot.config.settings`` rather than from
an ``"openai:..."`` string, because the key lives in ``.env`` (loaded by
pydantic-settings) and is not exported to the environment that ``init_chat_model``
reads.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from copilot.agent.agent import SYSTEM
from copilot.config import settings
from copilot.v2.orchestration.graph.tools import TOOLS

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


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None):
    """Return a compiled agent. ``checkpointer=None`` -> a fresh InMemorySaver."""
    from langchain.agents import create_agent

    return create_agent(
        model=_model(model),
        tools=TOOLS,
        system_prompt=SYSTEM,
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
    )
