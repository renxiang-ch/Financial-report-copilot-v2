"""Standardized tool library (Phase 1 -- devlog 005).

The five v1 tools rebuilt into a uniform library:

* ``base`` -- ``ToolError`` / ``ToolErrorKind``, the ``content_and_artifact``
  ``pack`` helper, and the ``financial_tool`` decorator.
* ``resolve`` -- ticker resolution (+ typo hints), fiscal-year scope, relation
  side; one implementation, shared by the tools.
* ``schemas`` -- one Pydantic ``args_schema`` per tool.
* ``financials`` / ``retrieval`` / ``graph`` / ``compute`` -- one module each.
  Domain logic (SQL, RRF fusion, recursive CTE, the AST sandbox) is carried over
  from ``copilot.agent.tools`` verbatim; only the interface is rewritten.
* ``registry`` -- the single ``TOOLS`` list the graph binds.

Errors ``raise ToolError``; the graph wires ``ToolErrorMiddleware`` +
``ToolCallLimitMiddleware`` around it (no retry middleware -- see
``graph.build._tool_middleware``). The frozen v1
tools still live at ``copilot.agent.tools`` for ``copilot.v2.orchestration.v1_loop``.
"""
