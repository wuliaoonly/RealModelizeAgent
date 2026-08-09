from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from real_modelize_agent.graph.nodes import (
    coordinator_node,
    coordinator_route_fn,
    context_compressor_node,
    context_compressor_route,
    context_monitor_node,
    context_monitor_route,
    final_node,
    planner_node,
    refuse_node,
    verifier_node,
)
from real_modelize_agent.graph.state import RealModelizeGraphState


def build_workflow():
    return build_complex_workflow()


def build_complex_workflow():
    graph = StateGraph(RealModelizeGraphState)
    graph.add_node("planner", planner_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node("context_compressor", context_compressor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "context_monitor")
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {"context_compressor": "context_compressor", "verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_conditional_edges(
        "context_compressor",
        context_compressor_route,
        {"verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_edge("verifier", "context_monitor")
    graph.add_edge("final", END)
    return graph.compile()


def build_entry_workflow():
    graph = StateGraph(RealModelizeGraphState)
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("refuse", refuse_node)

    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges(
        "coordinator",
        coordinator_route_fn,
        {"refuse": "refuse", "planner": END},
    )
    graph.add_edge("refuse", END)
    return graph.compile()
