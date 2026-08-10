"""共享 handoff 回调与工具工厂：研究手 / 编程手 / 建模手。

供 planner（graph/nodes.py）与建模手 / 论文手（agent 自身 ReAct 循环）复用。
- code_agent / research_agent 用**子模块导入**（非 `from agents import name`），
  既避开 agents/__init__ 部分初始化崩溃，也让测试能 patch `agents.code_agent.run_coder_agent` 等模块属性。
- modeler_agent 在函数体内**懒导入**：建模手会反向 import 本模块，模块级 import 会造成环。
- 各 handoff 用 `from_agent` 标注发起方（planner/modelerAgent/writerAgent），事件与手账随之归属。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import StructuredTool

import real_modelize_agent.agents.code_agent as code_agent
import real_modelize_agent.agents.research_agent as research_agent
from real_modelize_agent.agents.artifacts import collect_problem_artifacts
from real_modelize_agent.graph.state import RealModelizeGraphState

Writer = Callable[[dict[str, Any]], None]


def _join_notes(existing: str, new: str) -> str:
    existing = (existing or "").strip()
    new = (new or "").strip()
    if not new:
        return existing
    return f"{existing}\n\n{new}" if existing else new


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for source in sources:
        url = str(source.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(source)
    return out


def call_research_handoff(
    state: RealModelizeGraphState,
    writer: Writer,
    instruction: str,
    from_agent: str = "planner",
) -> dict[str, Any]:
    writer({"type": "handoff", "from": from_agent, "to": "researchAgent", "instruction": instruction})
    result = research_agent.run_research_agent(state, instruction, writer=writer)
    state["research_notes"] = _join_notes(state.get("research_notes", ""), result.get("summary", ""))
    state["sources"] = _dedupe_sources(list(state.get("sources", [])) + list(result.get("sources", [])))
    state["research_path"] = result.get("research_path") or state.get("research_path", "")
    state["references_bib"] = result.get("references_bib") or state.get("references_bib", "")
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [
        {
            "from_agent": from_agent,
            "to_agent": "researchAgent",
            "instruction": instruction,
            "result": result.get("summary", ""),
        }
    ]
    writer({"type": "handoff_result", "from": "researchAgent", "to": from_agent, "result": result.get("summary", "")})
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "sources": state.get("sources", []),
        "queries": result.get("queries", []),
        "research_path": state.get("research_path", ""),
        "references_bib": state.get("references_bib", ""),
    }


def call_coder_handoff(
    state: RealModelizeGraphState,
    writer: Writer,
    instruction: str,
    from_agent: str = "planner",
) -> dict[str, Any]:
    writer({"type": "handoff", "from": from_agent, "to": "coderAgent", "instruction": instruction})
    result = code_agent.run_coder_agent(state, instruction, writer=writer)
    state["todos"] = result.get("todos", state.get("todos", []))
    state["coder_summary"] = result.get("summary", "")
    state["figures"] = result.get("figures", [])
    state["results_summary"] = result.get("results_summary", "")
    state["figure_audit"] = result.get("figure_audit", {})
    # 权威磁盘扫描（非 agent 返回值），保证 modeler/writer/planner 三方看到一致事实
    state["problem_artifacts"] = collect_problem_artifacts(state["runtime"].workspace)
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [
        {
            "from_agent": from_agent,
            "to_agent": "coderAgent",
            "instruction": instruction,
            "result": result.get("summary", ""),
        }
    ]
    writer({"type": "handoff_result", "from": "coderAgent", "to": from_agent, "result": result.get("summary", "")})
    return {
        "ok": bool(result.get("ok", True)),
        "summary": result.get("summary", ""),
        "todos": state.get("todos", []),
        "figures": state.get("figures", []),
        "problem_artifacts": state.get("problem_artifacts", {}),
        "figure_audit": state.get("figure_audit", {}),
    }


def call_modeler_handoff(
    state: RealModelizeGraphState,
    writer: Writer,
    instruction: str,
    from_agent: str = "planner",
) -> dict[str, Any]:
    writer({"type": "handoff", "from": from_agent, "to": "modelerAgent", "instruction": instruction})
    # 懒导入：modeler_agent 模块级 import 了本模块（取研究/编程手工具），这里反向 import 会成环
    from real_modelize_agent.agents.modeler_agent import run_modeler_agent

    result = run_modeler_agent(state, instruction, writer=writer)
    state["modeler_summary"] = result.get("summary", "")
    state["modeler_plan"] = result.get("modeler_plan")
    state["modeler_plan_path"] = result.get("plan_path", "")
    state["research_path"] = result.get("research_path") or state.get("research_path", "")
    state["problem_artifacts"] = collect_problem_artifacts(state["runtime"].workspace)
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [
        {
            "from_agent": from_agent,
            "to_agent": "modelerAgent",
            "instruction": instruction,
            "result": result.get("summary", ""),
        }
    ]
    writer({"type": "handoff_result", "from": "modelerAgent", "to": from_agent, "result": result.get("summary", "")})
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "plan_path": state.get("modeler_plan_path", ""),
        "modeler_plan": state.get("modeler_plan"),
        "research_path": state.get("research_path", ""),
        "problem_artifacts": state.get("problem_artifacts", {}),
    }


def make_research_handoff_tool(state: RealModelizeGraphState, writer: Writer, *, from_agent: str = "planner") -> StructuredTool:
    return StructuredTool.from_function(
        name="CallResearchAgentTool",
        func=lambda instruction: call_research_handoff(state, writer, instruction, from_agent),
        description="委派研究手联网检索背景/文献并写入 research/研究资料.md。Args: instruction.",
    )


def make_coder_handoff_tool(state: RealModelizeGraphState, writer: Writer, *, from_agent: str = "planner") -> StructuredTool:
    return StructuredTool.from_function(
        name="CallCoderAgentTool",
        func=lambda instruction: call_coder_handoff(state, writer, instruction, from_agent),
        description="委派编程手按指定问题求解/生成图表与结果文件，返回路径清单。Args: instruction.",
    )


def make_modeler_handoff_tool(state: RealModelizeGraphState, writer: Writer, *, from_agent: str = "planner") -> StructuredTool:
    return StructuredTool.from_function(
        name="CallModelerAgentTool",
        func=lambda instruction: call_modeler_handoff(state, writer, instruction, from_agent),
        description="委派建模手产出/补充 题目分析.md、建模方案.json 与 problemN/方案/。Args: instruction.",
    )
