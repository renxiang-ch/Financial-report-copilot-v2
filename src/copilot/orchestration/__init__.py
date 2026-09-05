"""Orchestration layer.

* ``v1_loop``  -- the frozen hand-written ReAct loop from
  Financial-Report-Research-Copilot @ ``v1.0-teaching``, kept verbatim under
  ``copilot.agent`` and re-exported here. It is the A/B behavior baseline and
  must never be modified.

The LangGraph re-implementation (``graph``) will be added in Phase 2. The
Phase 0a stub was removed 2026-09-05 -- no LangChain/LangGraph is *used* in the
project yet (deps stay pinned in pyproject for interactive study).

See ``docs/langgraph-migration-plan.md``.
"""
