"""Orchestration layer.

Two implementations sit side by side:

* ``v1_loop``  -- the frozen hand-written ReAct loop from
  Financial-Report-Research-Copilot @ ``v1.0-teaching``, kept verbatim under
  ``copilot.agent`` and re-exported here. It is the A/B behavior baseline and
  must never be modified.
* ``graph``    -- the LangGraph re-implementation (Phase 2+).

See ``docs/langgraph-migration-plan.md``.
"""
