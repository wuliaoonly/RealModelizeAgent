from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from real_modelize_agent.agents.artifacts import collect_figures, collect_problem_artifacts, has_real_solutions
from real_modelize_agent.core.state import RuntimeState
from real_modelize_agent.graph.memory import build_layered_memory, format_layered_memory_for_prompt, memory_event
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.prompts.code_figure import CODER_PROMPT, CODER_PROMPT_SHORT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.registry import build_coder_tools
from real_modelize_agent.tools.todo_tool import persist_todos, update_todo

Writer = Callable[[dict[str, Any]], None]


def run_coder_agent(
    state: RealModelizeGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 14,
) -> dict[str, Any]:
    """编程手：用 Python 求解，产出每问 problemN/{代码,图表,结果} 文件。"""
    max_loops = int(os.getenv("RMA_CODER_MAX_LOOPS", str(max_loops)))
    runtime = state["runtime"]
    todos = [dict(todo) for todo in state.get("todos", [])]
    writer = writer or (lambda _: None)
    memory = build_layered_memory({**state, "todos": todos}, node="coderAgent")
    writer(memory_event(memory, node="coderAgent"))
    model = create_model()
    coder = model.bind_tools(build_coder_tools(runtime, todos))

    writer(
        {
            "type": "plan_snapshot",
            "node": "coderAgent",
            "plan_summary": state.get("plan_summary", ""),
            "todos": todos,
            "verification_commands": state.get("verification_commands", []),
        }
    )

    # 每问都有"真实"求解脚本才算已有解；占位 docstring（如指向 run_all.py 的注释文件）不算，
    # 否则系统会被占位脚本卡在修补模式，永远不生成真正的逐问实现。
    has_solutions = has_real_solutions(runtime.workspace, state.get("problem_json"))
    system_prompt = CODER_PROMPT if not has_solutions else CODER_PROMPT_SHORT

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=_coder_input(state, instruction, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(max_loops):
        response = coder.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer({"type": "tool_call", "node": "coderAgent", "name": call.get("name"), "args": call.get("args", {})})
            tool_result, todos = _execute_coder_tool(runtime, todos, call)
            event = _tool_result_event(tool_result, node="coderAgent")
            tool_events.append(event)
            writer(event)
            if call.get("name") == "TodoUpdateTool":
                persist_todos(
                    runtime,
                    todos,
                    state.get("acceptance_criteria", []),
                    state.get("verification_commands", []),
                    state.get("plan_summary", ""),
                )
            produced_messages.append(tool_result)
            messages.append(tool_result)
    else:
        produced_messages.append(AIMessage(content="coderAgent stopped after the maximum tool loop count."))

    summary = _last_ai_content(produced_messages)
    figures = collect_figures(runtime.workspace)
    return {
        "ok": True,
        "summary": summary,
        "todos": todos or state.get("todos", []),
        "figures": figures,
        "results_summary": _notepad_append(runtime),
        "problem_artifacts": collect_problem_artifacts(runtime.workspace),
        "messages": produced_messages,
        "tool_events": tool_events,
    }


def _execute_coder_tool(runtime: RuntimeState, todos: list[dict[str, str]], call: dict[str, Any]):
    name = call.get("name", "")
    args = call.get("args") or {}
    if name == "TodoUpdateTool":
        result = update_todo(todos, args.get("todo_id", ""), args.get("status", ""), args.get("note", ""))
        if result.get("ok"):
            todos = result["todos"]
    else:
        tools = {tool.name: tool for tool in build_coder_tools(runtime, todos)}
        tool = tools.get(name)
        if tool is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        else:
            try:
                result = tool.invoke(args)
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tool_call_id = call.get("id") or f"{name}-call"
    return ToolMessage(content=json.dumps(result, ensure_ascii=False), name=name, tool_call_id=tool_call_id), todos


def _tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(tool_message.content))
    except json.JSONDecodeError:
        parsed = tool_message.content
    return {"type": "tool_result", "node": node, "name": tool_message.name, "result": parsed}


def _notepad_append(runtime: RuntimeState) -> str:
    notepad = workspace_notepad(runtime.workspace)
    return notepad[-2400:]


def workspace_notepad(workspace: Path) -> str:
    path = workspace / "NOTEPAD.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _coder_input(state: RealModelizeGraphState, instruction: str, memory: dict[str, Any]) -> str:
    parts = [
        f"题目:\n{state['task']}",
        f"planner 指令:\n{instruction}",
    ]
    if state.get("problem_json"):
        parts.append("结构化的题目信息（含 ques_count）:\n" + json.dumps(state["problem_json"], ensure_ascii=False))
    if state.get("modeler_plan"):
        parts.append("建模方案:\n" + json.dumps(state["modeler_plan"], ensure_ascii=False, default=str))
    if state.get("problem_artifacts"):
        parts.append(
            "每问产物现状（缺的补齐）:\n" + json.dumps(state["problem_artifacts"], ensure_ascii=False, indent=2)
        )
    if state.get("research_path"):
        parts.append(f"研究资料: {state['research_path']}")
    parts.append("分层记忆快照:\n" + format_layered_memory_for_prompt(memory))
    parts.append(
        "请在 Windows 环境下用 python 求解：先读数据 → EDA → 逐问建模求解 → 每问产出 "
        "可独立执行的 problem{i}/代码/问题{i}_求解.py、problem{i}/图表/*.{png,svg}、"
        "problem{i}/结果/evidence.json 与其他结果文件。NOTEPAD 只写摘要，数值证据以 evidence.json 为准。"
        "完成后逐问运行入口脚本，再用 TodoUpdateTool 更新任务状态。"
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
