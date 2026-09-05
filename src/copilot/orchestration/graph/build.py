"""Graph assembly.

Phase 0: a single-node graph that compiles cleanly but refuses to run. This
proves the package layout, the LangGraph install, and the state schema hold
together, and gives ``ab_compare`` something importable to call. Phase 2
replaces ``_agent_stub`` with the real ``router -> agent -> tools -> verify``
node set.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from copilot.orchestration.graph.state import AgentState


def _agent_stub(state: AgentState) -> AgentState:
    raise NotImplementedError(
        "LangGraph orchestration is a Phase 0 stub. The parity graph lands in "
        "Phase 2 -- see docs/langgraph-migration-plan.md."
    )


def build_graph(checkpointer=None):
    """Compile and return the graph.

    checkpointer: a LangGraph checkpointer (MemorySaver in dev, PostgresSaver in
    Phase 3). ``None`` compiles a stateless graph.
    """
    builder = StateGraph(AgentState)
    builder.add_node("agent", _agent_stub)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=checkpointer)
