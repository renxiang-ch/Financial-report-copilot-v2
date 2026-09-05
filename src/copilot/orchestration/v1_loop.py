"""Frozen v1 baseline entry point.

``copilot.agent`` is a verbatim copy of Financial-Report-Research-Copilot
(tag ``v1.0-teaching``, HEAD ``b0e60e2``). It is the A/B behavior baseline for
the LangGraph re-implementation and MUST NOT be modified. This module only gives
it a stable, intention-revealing name.

Usage::

    from copilot.orchestration.v1_loop import ask
    result = ask("What was Apple's FY2024 revenue?")

Return shape: see ``copilot.agent.agent.ask`` --
``{answer, steps, citations, usage, route, provenance, verification, history, context}``.
"""

from copilot.agent.agent import ask

__all__ = ["ask"]
