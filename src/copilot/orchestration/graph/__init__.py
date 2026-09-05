"""LangGraph re-implementation of the agent orchestration.

Phase 0: stub only -- ``build_graph()`` compiles, but the single node raises
``NotImplementedError``. Phase 2 fills in the real
``router -> agent -> tools -> verify`` graph that reaches parity with
``copilot.orchestration.v1_loop``.
"""

from copilot.orchestration.graph.build import build_graph
from copilot.orchestration.graph.runner import run

__all__ = ["build_graph", "run"]
