"""v2 -- the LangChain / LangGraph rebuild.

Everything under ``copilot.v2`` is new code. Everything else under
``copilot`` is either the frozen v1 loop (``copilot.agent``, ``api``,
``dashboard``), shared infrastructure carried over verbatim from v1
(``config``, ``storage``, ``retrieval``, ``pipeline``), or v1-ported eval
harnesses (``copilot.eval.harness*`` / ``probe*``). v2 code imports those as
libraries; it never modifies them.
"""
