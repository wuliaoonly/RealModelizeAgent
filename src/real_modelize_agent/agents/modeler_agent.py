from __future__ import annotations

import json
import os
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from real_modelize_agent.agents.artifacts import collect_problem_artifacts, ensure_problem_folders, research_path
from real_modelize_agent.agents.handoffs import make_coder_handoff_tool, make_research_handoff_tool
from real_modelize_agent.agents.subagent_workspace import (
    dispatch_parallel_drafts,
    read_workspace_context,
    record_agent_work,
)
from real_modelize_agent.graph.memory import build_layered_memory, format_layered_memory_for_prompt, memory_event
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.prompts.model import MODELER_PROMPT, MODELER_PROMPT_SHORT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.algorithm_lookup import load_algorithm_briefing
from real_modelize_agent.tools.registry import build_modeler_tools
from real_modelize_agent.tools.skill_briefing import load_skill_briefing

Writer = Callable[[dict[str, Any]], None]

PLAN_ANALYSIS_FILE = "题目分析.md"
PLAN_JSON_FILE = "建模方案.json"
PLAN_MARKDOWN_FILE = "建模方案.md"


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
    tools = build_modeler_tools(runtime) + _model_subagent_tools(state, writer) + [
        make_research_handoff_tool(state, writer, from_agent="modelerAgent"),
        make_coder_handoff_tool(state, writer, from_agent="modelerAgent"),
    ]
    modeler = model.bind_tools(tools)

    first_run = not (runtime.workspace / PLAN_ANALYSIS_FILE).exists() or not (runtime.workspace / PLAN_MARKDOWN_FILE).exists()
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
    plan_path = PLAN_MARKDOWN_FILE if (runtime.workspace / PLAN_MARKDOWN_FILE).exists() else ""
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
        "两阶段执行：Analysis Stage 需要资料先调 CallResearchAgentTool，再写 `题目分析.md`、`建模方案.md`、"
        "`术语符号表.md`、每问 `problemN/方案/{方案.md,模型公式.md}` 与敏感性方案；Code Stage 审查代码与结果，"
        "不达标则调 CallCoderAgentTool 返工，固化后写 `建模结果.md`。每轮追加 NOTEPAD.md。"
    )
    parts.append(
        "主建模手并行协议：开始/阶段切换时先用 RecordModelWorkTool 把当前进度记录到 tmp。"
        "当存在多个独立问题时，用 DispatchModelSubagentsTool 为每问并行生成分析草稿；"
        "子代理草稿只在 tmp/agents/modelerAgent/，主建模手必须逐份 FileRead、消解跨问符号/假设冲突后，"
        "再统一写入正式建模方案，不能直接把草稿当最终产物。"
    )
    parts.append(
        "[可用技能]\nXlsxReadTool：只读预览 .xlsx/.xlsm 赛题附件（工作表清单、表头、前几行样例），"
        "写方案前先用它了解数据结构。\n" + load_skill_briefing("xlsx")
    )
    parts.append(
        "[算法资料库]\n"
        "项目根 `assets/` 的 7 类算法文档已可读（FileReadTool/GrepTool 现在能读项目根）。"
        "选算法/写方案前：用 FileReadTool 读对应 0X-*.md（大文件用 offset/limit 分段），或 GrepTool 按算法名/关键词搜章节，"
        "把算法原理、适用范围与公式写进 `建模方案.md` 与 `problemN/方案/{方案.md,模型公式.md}`。\n"
        + load_algorithm_briefing()
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


def _model_subagent_tools(state: RealModelizeGraphState, writer: Writer) -> list[StructuredTool]:
    runtime = state["runtime"]

    def record_work(status: str, summary: str, details: str = "") -> dict[str, Any]:
        return record_agent_work(runtime.workspace, "modelerAgent", status, summary, details)

    def dispatch(assignments: dict[str, str] | None = None) -> dict[str, Any]:
        selected = assignments or _default_model_assignments(state)

        def worker(name: str, task: str) -> str:
            files = [
                "raw/题目.md",
                str(state.get("research_path", "")),
                "建模方案.md",
                f"{name}/方案/方案.md",
                f"{name}/方案/模型公式.md",
            ]
            context = {
                "problem_json": state.get("problem_json", {}),
                "research_path": state.get("research_path", ""),
                "problem_artifacts": state.get("problem_artifacts", {}).get(name, {}),
                "source_text": read_workspace_context(runtime.workspace, files),
            }
            response = create_model().invoke(
                [
                    SystemMessage(
                        content=(
                            "你是建模子代理，只负责一个问题的独立技术草稿。提出假设、变量、公式、算法、"
                            "校验与风险；不得写正式工作区文件，也不得声称代表总方案。"
                        )
                    ),
                    HumanMessage(
                        content=f"子任务={name}\n要求={task}\n共享上下文={json.dumps(context, ensure_ascii=False)}"
                    ),
                ]
            )
            return str(getattr(response, "content", ""))

        result = dispatch_parallel_drafts(runtime.workspace, "modelerAgent", selected, worker)
        writer({"type": "subagent_batch", "node": "modelerAgent", **result})
        return result

    return [
        StructuredTool.from_function(
            name="RecordModelWorkTool",
            func=record_work,
            description="Record the main model agent's current status/summary/details under tmp/agents/modelerAgent.",
        ),
        StructuredTool.from_function(
            name="DispatchModelSubagentsTool",
            func=dispatch,
            description=(
                "Run one independent model-drafting sub-agent per problem concurrently. Optional assignments is a "
                "mapping like {'problem1':'analyze question 1'}. Drafts and manifest are written only under tmp."
            ),
        ),
    ]


def _default_model_assignments(state: RealModelizeGraphState) -> dict[str, str]:
    problem_json = state.get("problem_json") or {}
    try:
        count = max(1, min(16, int(problem_json.get("ques_count", 1))))
    except (TypeError, ValueError):
        count = 1
    return {
        f"problem{index}": str(problem_json.get(f"ques{index}") or f"分析并设计问题 {index} 的模型")
        for index in range(1, count + 1)
    }
