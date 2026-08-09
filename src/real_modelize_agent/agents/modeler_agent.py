from __future__ import annotations

import json
import os
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from real_modelize_agent.agents.artifacts import collect_problem_artifacts, ensure_problem_folders, research_path
from real_modelize_agent.agents.handoffs import make_coder_handoff_tool, make_research_handoff_tool
from real_modelize_agent.graph.memory import build_layered_memory, format_layered_memory_for_prompt, memory_event
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.prompts.model import MODELER_PROMPT, MODELER_PROMPT_SHORT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.registry import build_modeler_tools

Writer = Callable[[dict[str, Any]], None]

PLAN_ANALYSIS_FILE = "题目分析.md"
PLAN_JSON_FILE = "建模方案.json"


def run_modeler_agent(
    state: RealModelizeGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 24,
) -> dict[str, Any]:
    """建模手：两阶段反馈闭环（研究↔建模、建模↔代码），产出每问方案并核验求解结果。

    - 阶段1：可调 CallResearchAgentTool 收集资料 → 写题目分析.md + 建模方案.json + problemN/方案/。
    - 阶段2：可调 CallCoderAgentTool 实现 → 读 problemN/结果/ 核验 → 不达标改进模型再下发修改。
    """
    max_loops = int(os.getenv("RMA_MODELER_MAX_LOOPS", str(max_loops)))
    runtime = state["runtime"]
    writer = writer or (lambda _: None)
    memory = build_layered_memory(state, node="modelerAgent")
    writer(memory_event(memory, node="modelerAgent"))
    model = create_model()
    tools = build_modeler_tools(runtime) + [
        make_research_handoff_tool(state, writer, from_agent="modelerAgent"),
        make_coder_handoff_tool(state, writer, from_agent="modelerAgent"),
    ]
    modeler = model.bind_tools(tools)

    first_run = not (runtime.workspace / PLAN_ANALYSIS_FILE).exists() or not (runtime.workspace / PLAN_JSON_FILE).exists()
    ensure_problem_folders(runtime.workspace, state.get("problem_json"))
    system_prompt = MODELER_PROMPT if first_run else MODELER_PROMPT_SHORT

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=_modeler_input(state, instruction, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(max_loops):
        response = modeler.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer({"type": "tool_call", "node": "modelerAgent", "name": call.get("name"), "args": call.get("args", {})})
            tool_message = _execute_tool(runtime, call, tools)
            event = _tool_result_event(tool_message, node="modelerAgent")
            tool_events.append(event)
            writer(event)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content="modelerAgent stopped after the maximum tool loop count."))

    summary = _last_ai_content(produced_messages)
    plan_path = PLAN_JSON_FILE if (runtime.workspace / PLAN_JSON_FILE).exists() else ""
    analysis_path = PLAN_ANALYSIS_FILE if (runtime.workspace / PLAN_ANALYSIS_FILE).exists() else ""
    return {
        "ok": True,
        "summary": summary,
        "plan_path": plan_path,
        "analysis_path": analysis_path,
        "modeler_plan": _read_plan_json(runtime.workspace / PLAN_JSON_FILE),
        "research_path": research_path(runtime.workspace),
        "problem_artifacts": collect_problem_artifacts(runtime.workspace),
        "messages": produced_messages,
        "tool_events": tool_events,
    }


def _read_plan_json(path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _execute_tool(runtime, call: dict[str, Any], tools: list[Any]) -> ToolMessage:
    name = call.get("name", "")
    args = call.get("args") or {}
    tool_map = {tool.name: tool for tool in tools}
    tool = tool_map.get(name)
    if tool is None:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = tool.invoke(args)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )


def _tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(tool_message.content))
    except json.JSONDecodeError:
        parsed = tool_message.content
    return {"type": "tool_result", "node": node, "name": tool_message.name, "result": parsed}


def _modeler_input(state: RealModelizeGraphState, instruction: str, memory: dict[str, Any]) -> str:
    parts = [
        f"题目:\n{state['task']}",
        f"planner 指令:\n{instruction}",
    ]
    if state.get("problem_json"):
        parts.append("结构化的题目信息:\n" + json.dumps(state["problem_json"], ensure_ascii=False))
    if state.get("research_path"):
        parts.append(f"研究资料（只含真实检索结果）: {state['research_path']}，可 FileRead 引用。")
    if state.get("problem_artifacts"):
        parts.append(
            "每问产物现状:\n" + json.dumps(state["problem_artifacts"], ensure_ascii=False, indent=2)
        )
    parts.append("分层记忆快照:\n" + format_layered_memory_for_prompt(memory))
    parts.append(
        "两阶段执行：阶段1 需要资料先调 CallResearchAgentTool 再写 `题目分析.md`、`建模方案.json` 与每问 "
        "`problemN/方案/问题N_方案.md`，并追加 NOTEPAD.md；阶段2 调 CallCoderAgentTool 实现，读结果核验，不达标改进模型再调。"
    )
    return "\n\n".join(parts)


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""
