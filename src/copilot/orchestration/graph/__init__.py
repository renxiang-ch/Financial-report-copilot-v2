"""LangChain ``create_agent`` re-implementation of the v1 agent loop.

Incremental port (see docs/devlog). Step 1: bare ``create_agent`` + minimal
``@tool`` wrappers around the frozen v1 tools + a runner that returns the
v1-shaped result dict. Routing / clarify / slot-inheritance / history-trim are
added as middleware in later steps.
"""

from copilot.orchestration.graph.build import build_agent
from copilot.orchestration.graph.runner import run

__all__ = ["build_agent", "run"]
