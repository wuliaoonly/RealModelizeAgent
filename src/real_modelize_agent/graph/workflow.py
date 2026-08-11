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
from real_modelize_agent.graph.stage_nodes import (
    analysis_stage_node,
    code_stage_node,
    initial_stage_route,
    prepare_stage_node,
    stage_verifier_node,
    stage_verifier_route,
    writing_stage_node,
)


def build_workflow():
    return build_complex_workflow()


def build_complex_workflow():
    graph = StateGraph(RealModelizeGraphState)
    graph.add_node("prepare_stage", prepare_stage_node)
    graph.add_node("analysis_stage", analysis_stage_node)
    graph.add_node("code_stage", code_stage_node)
    graph.add_node("writing_stage", writing_stage_node)
    graph.add_node("stage_verifier", stage_verifier_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node("context_compressor", context_compressor_node)
    graph.add_node("final", final_node)

    graph.add_conditional_edges(
        START,
        initial_stage_route,
        {
            "prepare_stage": "prepare_stage",
            "analysis_stage": "analysis_stage",
            "code_stage": "code_stage",
            "writing_stage": "writing_stage",
            "final": "final",
        },
    )
    graph.add_edge("prepare_stage", "stage_verifier")
    graph.add_edge("analysis_stage", "stage_verifier")
    graph.add_edge("code_stage", "stage_verifier")
    graph.add_edge("writing_stage", "stage_verifier")
    graph.add_conditional_edges(
        "stage_verifier",
        stage_verifier_route,
        {
            "prepare_stage": "prepare_stage",
            "analysis_stage": "analysis_stage",
            "code_stage": "code_stage",
            "writing_stage": "writing_stage",
            "context_monitor": "context_monitor",
            "final": "final",
        },
    )
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {
            "context_compressor": "context_compressor",
            "analysis_stage": "analysis_stage",
            "code_stage": "code_stage",
            "writing_stage": "writing_stage",
            "final": "final",
        },
    )
    graph.add_conditional_edges(
        "context_compressor",
        context_compressor_route,
        {"analysis_stage": "analysis_stage", "code_stage": "code_stage", "writing_stage": "writing_stage", "final": "final"},
    )
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
