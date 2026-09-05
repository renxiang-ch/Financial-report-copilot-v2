"""Standardized tool library (Phase 1).

Empty placeholder. Phase 1 rebuilds the five v1 tools
(``query_financials``, ``list_metrics``, ``retrieve_text``, ``graph_query``,
``compute``) into a uniform library: shared result envelope, typed ``ToolError``,
Pydantic arg schemas, provenance, a ``resolve`` cross-cutting layer, caching and
rate limiting. Domain logic (SQL, RRF fusion, recursive CTE, the AST sandbox) is
carried over from ``copilot.agent.tools`` / ``copilot.retrieval`` unchanged;
only the interface is rewritten.

Until then, the frozen v1 tools live at ``copilot.agent.tools`` and are used by
``copilot.orchestration.v1_loop``.

See ``docs/langgraph-migration-plan.md`` -> Phase 1.
"""
