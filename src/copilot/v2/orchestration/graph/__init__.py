"""LangChain ``create_agent`` re-implementation of the v1 agent loop.

``build.py`` assembles the agent (v1 SYSTEM prompt, the standardized tool
library, ``GraphState``, the middleware stack). ``middleware.py`` carries v1's
pre-loop policy as hooks (trim / resolve / refuse+clarify / active-context /
force-tool). ``runner.py`` drives it and returns a v1-shaped result dict for
``ab_compare``. See docs/devlog 004 (loop port) and 005 (tool layer).
"""

from copilot.v2.orchestration.graph.build import build_agent
from copilot.v2.orchestration.graph.runner import run

__all__ = ["build_agent", "run"]
