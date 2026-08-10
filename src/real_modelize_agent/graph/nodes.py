from __future__ import annotations

import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from real_modelize_agent.agents.handoffs import call_coder_handoff, call_research_handoff
from real_modelize_agent.agents.modeler_agent import run_modeler_agent
from real_modelize_agent.agents.write_agent import paper_status, run_writer_agent
from real_modelize_agent.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
    persist_history_summary,
)
from real_modelize_agent.graph.state import RealModelizeGraphState, TodoItem, VerificationCheck
from real_modelize_agent.core.validation import validate_workspace
from real_modelize_agent.prompts.contextCompression import CONTEXT_COMPRESSION_PROMPT
from real_modelize_agent.prompts.multiAgent import COORDINATOR_PROMPT, VERIFIER_PROMPT
from real_modelize_agent.prompts.planExecute import PLANNER_PROMPT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.file_tools import read_file
from real_modelize_agent.tools.grep_tool import grep
from real_modelize_agent.tools.registry import build_verifier_tools
from real_modelize_agent.tools.todo_tool import persist_todos, write_todos

DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

# 数模任务默认计划：建模手 → 编程手 → 论文手（每问独立目录 problemN/{方案,代码,图表,结果}）
CUMCM_TODOS = [
    "建模手：撰写 题目分析.md、建模方案.json 与每问 problemN/方案/（逐问建模思路、≤2 个主模型、敏感性分析）",
    "编程手：逐问独立求解，生成代码、图表、结果与 evidence.json 证据链",
    "论文手：仅依据 evidence.json 写作，调用专用编译工具生成并验证论文.pdf",
]

CUMCM_CRITERIA = [
    "题目分析.md 与 建模方案.json 存在且内容充实。",
    "按 ques_count 每问方案、独立可执行代码、有效图表、结果与 evidence.json 齐全。",
    "论文.tex 经专用 LaTeX 工具真实编译通过，编译记录与当前源码指纹一致。",
    "论文结构完整（国赛模板章节）：摘要、问题的提出和重述、问题的分析、模型假设、符号说明、数据的处理、各问模型建立和求解、模型的评价和改进、模型的推广和应用、参考文献、附录。",
]

CUMCM_COMMANDS = [
    "WorkspaceValidationTool（强制确定性产物、证据链与论文门禁）",
    "LatexStatusTool（校验编译记录、源码指纹与 PDF 有效性）",
]

DEFAULT_TODOS = [
    "明确交付物与验收标准。",
    "委派专家 Agent 完成题目所需工作。",
    "验证生成结果。",
]

# \includegraphics 引用（可含 [width=...] 选项）：只捕获大括号里的路径
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}")

# 与 LLM 检查项重名时以运行时结果为准的检查名（LLM 拼的 python -c 易受编码/正则影响而误判）
DETERMINISTIC_CHECK_NAMES = {"图片引用"}


def _paper_reference_checks(workspace) -> tuple[list[str], int]:
    """确定性核对：论文.tex 的 \\includegraphics 引用是否全部落在存在文件。

    相对工作区根解析路径；正则全 ASCII，文件按 UTF-8 读取，不受 Windows 控制台编码影响。
    """
    refs: list[str] = []
    tex_files = [workspace / "论文.tex"]
    for tex in tex_files:
        if not tex.exists():
            continue
        try:
            text = tex.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs.extend(m.group(1).strip() for m in INCLUDEGRAPHICS_RE.finditer(text))
    missing = sorted({ref for ref in refs if not (workspace / ref).exists()})
    return missing, len(refs)


def _merge_deterministic_checks(checks: list[dict], deterministic: list[dict]) -> list[dict]:
    """把确定性检查合并进 LLM 检查：重名项用运行时结果替换，其余原样保留。"""
    names = {item.get("name") for item in deterministic}
    merged = [item for item in checks if item.get("name") not in names]
    return merged + deterministic


def coordinator_node(state: RealModelizeGraphState) -> dict[str, Any]:
    """判定输入是否为数学建模问题：是 → 结构化 JSON；否 → 拒绝并结束。"""
    writer = _get_writer()
    detected = False
    problem_json: dict[str, Any] | None = None
    reason = "coordinator fallback: not detected as math modeling"
    try:
        response = create_model().invoke(
            [
                SystemMessage(content=COORDINATOR_PROMPT),
                HumanMessage(content=f"用户输入：\n{state.get('task', '')}"),
            ]
        )
        text = str(getattr(response, "content", "") or "").strip()
        parsed = _extract_json(text)
        if parsed and parsed.get("title") and parsed.get("ques_count") is not None:
            count = parsed.get("ques_count")
            valid_count = isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 16
            complete_questions = valid_count and all(
                str(parsed.get(f"ques{i}", "")).strip() for i in range(1, count + 1)
            )
            if valid_count and complete_questions:
                detected = True
                problem_json = parsed
                reason = "detected as math modeling problem"
            else:
                reason = "coordinator JSON schema invalid: ques_count must be 1..16 and every quesN must be present"
        elif parsed:
            reason = f"returned JSON but missing title/ques_count: {_short_text(json.dumps(parsed, ensure_ascii=False), 300)}"
        else:
            reason = _short_text(text or "no response", 300) or "refused: not a math modeling problem"
    except Exception as exc:
        reason = f"coordinator error: {type(exc).__name__}: {exc}"

    event = {
        "type": "problem_decision",
        "detected": detected,
        "problem_json": problem_json,
        "reason": reason,
    }
    writer(event)
    return {
        "problem_detected": detected,
        "problem_json": problem_json,
        "coordinator_reason": reason,
    }


def coordinator_route_fn(state: RealModelizeGraphState) -> str:
    return "refuse" if not state.get("problem_detected") else "planner"


def refuse_node(state: RealModelizeGraphState) -> dict[str, Any]:
    final_answer = (
        "这不是数学建模题目，RealModelizeAgent 暂不处理。\n\n"
        f"判定依据：{state.get('coordinator_reason', '')}\n\n"
        "请提供一道数学建模题（如 CUMCM / 美赛题，含问题描述与数据）后重试。"
    )
    return {"final_answer": final_answer}


def planner_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _get_writer()
    working_state: RealModelizeGraphState = {**state}
    if not working_state.get("todos"):
        _apply_plan(working_state, _default_plan(working_state["task"]))
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )
    _inject_user_instruction(working_state)
    if working_state.get("user_instruction"):
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    memory = build_layered_memory(working_state, node="planner")
    writer(memory_event(memory, node="planner"))
    model = create_model()
    planner = model.bind_tools(_build_planner_tools(working_state, writer))
    messages: list[Any] = [
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=_planner_input(working_state, memory)),
    ]
    produced_messages: list[Any] = []

    writer(
        {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "verification_commands": working_state.get("verification_commands", []),
            "execution_commands": working_state.get("execution_commands", []),
            "attempts": state.get("attempts", 0),
        }
    )

    for _ in range(_loop_budget("RMA_PLANNER_MAX_LOOPS", 12)):
        response = planner.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = _execute_planner_tool(working_state, writer, call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(AIMessage(content="planner stopped after the maximum supervisor tool loop count."))

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = _last_ai_content(produced_messages)
    final_memory = build_layered_memory(working_state, node="planner")
    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "execution_commands": working_state.get("execution_commands", []),
        "user_instruction": working_state.get("user_instruction", {}),
        "instruction_applied": working_state.get("instruction_applied", False),
        "chart_style_request": working_state.get("chart_style_request", {}),
        "paragraph_edit_request": working_state.get("paragraph_edit_request", {}),
        "figure_audit": working_state.get("figure_audit", {}),
        "research_notes": working_state.get("research_notes", ""),
        "research_path": working_state.get("research_path", ""),
        "references_bib": working_state.get("references_bib", ""),
        "sources": working_state.get("sources", []),
        "agent_handoffs": working_state.get("agent_handoffs", []),
        "modeler_summary": working_state.get("modeler_summary", ""),
        "modeler_plan": working_state.get("modeler_plan"),
        "modeler_plan_path": working_state.get("modeler_plan_path", ""),
        "coder_summary": working_state.get("coder_summary", ""),
        "figures": working_state.get("figures", []),
        "results_summary": working_state.get("results_summary", ""),
        "problem_artifacts": working_state.get("problem_artifacts", {}),
        "writer_summary": working_state.get("writer_summary", ""),
        "paper_path": working_state.get("paper_path", ""),
        "paper_compile_ok": working_state.get("paper_compile_ok", False),
        "messages": produced_messages,
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
    }


def verifier_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _get_writer()
    memory = build_layered_memory(state, node="verifier")
    writer(memory_event(memory, node="verifier"))
    writer(
        {
            "type": "plan_snapshot",
            "node": "verifier",
            "plan_summary": state.get("plan_summary", ""),
            "todos": state.get("todos", []),
            "verification_commands": state.get("verification_commands", []),
        }
    )

    model = create_model()
    verifier = model.bind_tools(build_verifier_tools(state["runtime"], state.get("problem_json")))
    messages: list[Any] = [
        SystemMessage(content=VERIFIER_PROMPT),
        HumanMessage(content=_verifier_input(state, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(_loop_budget("RMA_VERIFIER_MAX_LOOPS", 10)):
        response = verifier.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer({"type": "tool_call", "node": "verifier", "name": call.get("name"), "args": call.get("args", {})})
            tool_message = _execute_read_only_tool(state, call)
            event = _tool_result_event(tool_message, node="verifier")
            tool_events.append(event)
            writer(event)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(
            AIMessage(
                content=json.dumps(
                    {
                        "passed": False,
                        "reason": "Verifier stopped after the maximum tool loop count.",
                        "checks": [],
                        "recommended_next_instruction": "检查工作区，继续完成未完成的数模任务。",
                    },
                    ensure_ascii=False,
                )
            )
        )

    parsed = _extract_json(_last_ai_content(produced_messages)) or {
        "passed": False,
        "reason": "Verifier 未返回合法 JSON。",
        "checks": [
            {
                "name": "verifier_json",
                "passed": False,
                "detail": _last_ai_content(produced_messages)[:800],
            }
        ],
        "recommended_next_instruction": "检查工作区后返回合法 verifier JSON。",
    }
    checks = _normalize_checks(parsed.get("checks"))
    # 确定性检查（权威）：完整产物门禁不可由 LLM 省略或覆盖。
    deterministic_result = validate_workspace(state["runtime"].workspace, state.get("problem_json"))
    missing_refs, total_refs = _paper_reference_checks(state["runtime"].workspace)
    deterministic_checks = list(deterministic_result["checks"]) + [
        {
            "name": "图片引用",
            "passed": not missing_refs,
            "detail": (
                f"{total_refs} 处 \\includegraphics 引用全部落在存在文件"
                if not missing_refs
                else f"缺失 {len(missing_refs)} 处：{', '.join(missing_refs[:5])}"
            ),
        }
    ]
    checks = _merge_deterministic_checks(checks, deterministic_checks)
    # 最终通过 = 不可省略的确定性门禁 AND LLM 语义质检。
    llm_passed = bool(parsed.get("passed")) and all(
        item.get("passed", False)
        for item in _normalize_checks(parsed.get("checks"))
    )
    passed = bool(deterministic_result["passed"]) and llm_passed
    reason = str(parsed.get("reason") or "")
    recommended = str(parsed.get("recommended_next_instruction") or "")
    attempts = state.get("attempts", 0) + 1
    todos = [dict(todo) for todo in state.get("todos", [])]
    if passed:
        todos = [
            {
                **todo,
                "status": "completed" if todo.get("status") != "blocked" else todo.get("status", "blocked"),
                "note": todo.get("note") or "verified",
            }
            for todo in todos
        ]
        writer(
            {
                "type": "todo_update",
                "node": "verifier",
                "plan_summary": state.get("plan_summary", ""),
                "todos": todos,
                "verification_commands": state.get("verification_commands", []),
            }
        )
    last_error = "" if passed else _format_verifier_error(reason, recommended, tool_events)

    status = paper_status(state["runtime"].workspace)
    return {
        "messages": produced_messages,
        "verification_results": _tool_events_to_verification_results(tool_events),
        "verification_checks": checks,
        "verifier_summary": reason,
        "passed": passed,
        "attempts": attempts,
        "last_error": last_error,
        "todos": todos,
        "paper_path": status["paper_tex"],
        "paper_compile_ok": status["compile_ok"],
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get("history_summary", ""),
        "context_next_node": verifier_route({**state, "passed": passed, "attempts": attempts}),
    }


def context_monitor_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _get_writer()
    token_limit = get_context_token_limit()
    token_count = estimate_context_tokens(state)
    should_compress = token_count >= token_limit
    next_node = state.get("context_next_node") or "verifier"
    event = {
        "type": "context_monitor",
        "token_count": token_count,
        "token_limit": token_limit,
        "should_compress": should_compress,
        "next_node": next_node,
        "message_count": len(state.get("messages", [])),
    }
    writer(event)
    return {
        "context_token_count": token_count,
        "context_token_limit": token_limit,
        "context_should_compress": should_compress,
        "context_next_node": next_node,
    }


def context_monitor_route(state: RealModelizeGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    before_messages = list(state.get("messages", []))
    memory = build_layered_memory(state, node="context_compressor")
    writer(memory_event(memory, node="context_compressor"))
    compressed = _compress_context_with_model(state)
    summary = _format_compressed_context(compressed, state)
    summary_message = AIMessage(content=summary)
    persist_history_summary(state["runtime"], summary)

    post_state: RealModelizeGraphState = {
        **state,
        "messages": [summary_message],
        "context_summary": summary,
        "history_summary": summary,
        "memory_snapshot": build_layered_memory(
            {**state, "context_summary": summary, "history_summary": summary},
            node="context_compressor",
        ),
        "research_notes": _short_text(state.get("research_notes", ""), 1200),
        "agent_handoffs": _trim_handoffs(state.get("agent_handoffs", [])),
        "last_error": _short_text(state.get("last_error", ""), 1600),
        "coder_summary": _short_text(state.get("coder_summary", ""), 1200),
        "verifier_summary": _short_text(state.get("verifier_summary", ""), 1200),
    }
    after_tokens = estimate_context_tokens(post_state)
    compression_event = {
        "before_tokens": int(before_tokens),
        "after_tokens": int(after_tokens),
        "removed_messages": len(before_messages),
        "summary": _short_text(summary, 1200),
        "next_node": state.get("context_next_node", "verifier"),
    }
    events = list(state.get("compression_events", [])) + [compression_event]
    writer({"type": "context_compression", **compression_event})
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_message],
        "context_summary": summary,
        "context_token_count": after_tokens,
        "context_should_compress": False,
        "research_notes": post_state.get("research_notes", ""),
        "agent_handoffs": post_state.get("agent_handoffs", []),
        "last_error": post_state.get("last_error", ""),
        "coder_summary": post_state.get("coder_summary", ""),
        "verifier_summary": post_state.get("verifier_summary", ""),
        "memory_snapshot": post_state.get("memory_snapshot", {}),
        "history_summary": summary,
        "compression_events": events,
    }


def context_compressor_route(state: RealModelizeGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: RealModelizeGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: RealModelizeGraphState) -> dict[str, Any]:
    status = "PASSED" if state.get("passed") else "FAILED"
    checks = "\n".join(
        f"- {check.get('name', 'check')}: {'PASS' if check.get('passed') else 'FAIL'} - {check.get('detail', '')}"
        for check in state.get("verification_checks", [])
    )
    todos = "\n".join(f"- [{todo.get('status', '')}] {todo.get('content', '')}" for todo in state.get("todos", []))
    sources = "\n".join(f"- {source.get('title', '')}: {source.get('url', '')}" for source in state.get("sources", []))
    paper_text = _paper_status_text(state)
    final_answer = (
        f"数模 Agent 工作流结束：{status}\n\n"
        f"计划：{state.get('plan_summary', '')}\n\n"
        f"任务清单：\n{todos or '(empty)'}\n\n"
        f"论文状态：\n{paper_text}\n\n"
        f"参考资料：\n{sources or '(none)'}\n\n"
        f"Verifier：{state.get('verifier_summary', '')}\n\n"
        f"检查项：\n{checks or '(none)'}\n\n"
        f"建模手：{state.get('modeler_summary', '')}\n\n"
        f"编程手：{state.get('coder_summary', '')}\n\n"
        f"论文手：{state.get('writer_summary', '')}"
    )
    return {"final_answer": final_answer}


def get_context_token_limit() -> int:
    load_dotenv()
    raw = os.getenv("RMA_CONTEXT_TOKEN_LIMIT", str(DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: RealModelizeGraphState) -> int:
    messages = list(state.get("messages", []))
    payload = build_layered_memory(state, node="context_monitor")
    payload_message = HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))
    try:
        model = create_model()
        return int(model.get_num_tokens_from_messages(messages + [payload_message]))
    except Exception:
        text = "\n".join(_message_text(message) for message in messages)
        text += "\n" + payload_message.content
        return max(1, len(text) // 4)


def _build_planner_tools(state: RealModelizeGraphState, writer) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="TodoWriteTool",
            func=lambda todos, acceptance_criteria, verification_commands, plan_summary="", execution_commands=None: _todo_write_tool(
                state, writer, todos, acceptance_criteria, verification_commands, plan_summary, execution_commands
            ),
            description=(
                "发布或修订计划状态。Args: todos, acceptance_criteria, verification_commands, optional plan_summary and execution_commands."
            ),
        ),
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state["runtime"], file_path, offset, limit),
            description="全链路监督：读取工作区文件核对上一环节产物（如 题目分析.md、problemN/方案/）。",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state["runtime"], pattern, path, glob, head_limit, ignore_case
            ),
            description="全链路监督：在工作区搜索核对产物（如 problemN/图表 下的图片清单）。",
        ),
        StructuredTool.from_function(
            name="CallResearchAgentTool",
            func=lambda instruction: _call_research_agent_tool(state, writer, instruction),
            description="委派研究手联网检索背景/文献并写入 research/研究资料.md。Args: instruction.",
        ),
        StructuredTool.from_function(
            name="CallModelerAgentTool",
            func=lambda instruction: _call_modeler_agent_tool(state, writer, instruction),
            description="委派建模手产出 题目分析.md、建模方案.json 与每问 problemN/方案/。Args: instruction.",
        ),
        StructuredTool.from_function(
            name="CallCoderAgentTool",
            func=lambda instruction: _call_coder_agent_tool(state, writer, instruction),
            description="委派编程手写 Python 求解并生成每问 problemN/{代码,图表,结果} 文件。Args: instruction.",
        ),
        StructuredTool.from_function(
            name="CallWriterAgentTool",
            func=lambda instruction: _call_writer_agent_tool(state, writer, instruction),
            description="委派论文手按国赛模板撰写 论文.tex 并用 xelatex 编译出 论文.pdf。Args: instruction.",
        ),
    ]


def _todo_write_tool(
    state: RealModelizeGraphState,
    writer,
    todos: Any,
    acceptance_criteria: Any,
    verification_commands: Any,
    plan_summary: str = "",
    execution_commands: Any = None,
) -> dict[str, Any]:
    result = write_todos(todos, acceptance_criteria, verification_commands)
    if result.get("ok"):
        state["plan_summary"] = plan_summary or state.get("plan_summary") or "MultiAgent plan"
        state["todos"] = _todo_items(result["todos"], existing=state.get("todos", []))
        state["acceptance_criteria"] = result["acceptance_criteria"]
        state["verification_commands"] = result["verification_commands"]
        if execution_commands is not None:
            state["execution_commands"] = [str(item) for item in execution_commands if str(item).strip()]
        persist_todos(
            state["runtime"],
            state["todos"],
            state["acceptance_criteria"],
            state["verification_commands"],
            state.get("plan_summary", ""),
        )
        writer(
            {
                "type": "plan_snapshot",
                "node": "planner",
                "plan_summary": state.get("plan_summary", ""),
                "todos": state.get("todos", []),
                "verification_commands": state.get("verification_commands", []),
                "execution_commands": state.get("execution_commands", []),
                "acceptance_criteria": state.get("acceptance_criteria", []),
            }
        )
    return {
        **result,
        "plan_summary": state.get("plan_summary", ""),
        "todo_items": state.get("todos", []),
        "execution_commands": state.get("execution_commands", []),
    }


def _call_research_agent_tool(state: RealModelizeGraphState, writer, instruction: str) -> dict[str, Any]:
    # 委托共享 handoff（更新 research_notes/sources/research_path + 手账 + 事件）
    return call_research_handoff(state, writer, instruction, from_agent="planner")


def _call_modeler_agent_tool(state: RealModelizeGraphState, writer, instruction: str) -> dict[str, Any]:
    writer({"type": "handoff", "from": "planner", "to": "modelerAgent", "instruction": instruction})
    result = run_modeler_agent(state, instruction, writer=writer)
    state["modeler_summary"] = result.get("summary", "")
    state["modeler_plan"] = result.get("modeler_plan")
    state["modeler_plan_path"] = result.get("plan_path", "")
    state["research_path"] = result.get("research_path") or state.get("research_path", "")
    state["problem_artifacts"] = result.get("problem_artifacts") or state.get("problem_artifacts", {})
    handoff = {
        "from_agent": "planner",
        "to_agent": "modelerAgent",
        "instruction": instruction,
        "result": result.get("summary", ""),
    }
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
    writer({"type": "handoff_result", "from": "modelerAgent", "to": "planner", "result": result.get("summary", "")})
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "plan_path": state.get("modeler_plan_path", ""),
        "research_path": state.get("research_path", ""),
        "problem_artifacts": state.get("problem_artifacts", {}),
    }


def _call_coder_agent_tool(state: RealModelizeGraphState, writer, instruction: str) -> dict[str, Any]:
    # 委托共享 handoff（更新 todos/coder_summary/figures/results_summary + 权威磁盘扫描 problem_artifacts）
    return call_coder_handoff(state, writer, instruction, from_agent="planner")


def _call_writer_agent_tool(state: RealModelizeGraphState, writer, instruction: str) -> dict[str, Any]:
    writer({"type": "handoff", "from": "planner", "to": "writerAgent", "instruction": instruction})
    result = run_writer_agent(state, instruction, writer=writer)
    state["todos"] = result.get("todos", state.get("todos", []))
    state["writer_summary"] = result.get("summary", "")
    state["paper_path"] = result.get("paper_path", "")
    state["paper_compile_ok"] = bool(result.get("compile_ok"))
    state["problem_artifacts"] = result.get("problem_artifacts") or state.get("problem_artifacts", {})
    handoff = {
        "from_agent": "planner",
        "to_agent": "writerAgent",
        "instruction": instruction,
        "result": result.get("summary", ""),
    }
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
    writer({"type": "handoff_result", "from": "writerAgent", "to": "planner", "result": result.get("summary", "")})
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "todos": state.get("todos", []),
        "paper_path": state.get("paper_path", ""),
        "compile_ok": state.get("paper_compile_ok", False),
        "problem_artifacts": state.get("problem_artifacts", {}),
    }


def _execute_planner_tool(state: RealModelizeGraphState, writer, call: dict[str, Any]) -> ToolMessage:
    name = call.get("name", "")
    args = call.get("args") or {}
    writer({"type": "tool_call", "node": "planner", "name": name, "args": args})
    tools = {tool.name: tool for tool in _build_planner_tools(state, writer)}
    tool = tools.get(name)
    if tool is None:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = tool.invoke(args)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    tool_message = ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )
    writer(_tool_result_event(tool_message, node="planner"))
    return tool_message


def _execute_read_only_tool(state: RealModelizeGraphState, call: dict[str, Any]) -> ToolMessage:
    name = call.get("name", "")
    args = call.get("args") or {}
    tools = {
        tool.name: tool
        for tool in build_verifier_tools(state["runtime"], state.get("problem_json"))
    }
    tool = tools.get(name)
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


def _compress_context_with_model(state: RealModelizeGraphState) -> dict[str, Any]:
    memory = build_layered_memory(state, node="context_compressor")
    payload = {
        "context_summary": state.get("context_summary", ""),
        "memory": memory,
        "messages": [_message_snapshot(message) for message in state.get("messages", [])],
    }
    messages = [
        SystemMessage(content=CONTEXT_COMPRESSION_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
    ]
    try:
        response = create_model().invoke(messages)
        parsed = _extract_json(str(response.content))
        if parsed:
            return parsed
    except Exception as exc:
        return _fallback_compression(state, error=f"{type(exc).__name__}: {exc}")
    return _fallback_compression(state, error="compressor model did not return valid JSON")


def _fallback_compression(state: RealModelizeGraphState, *, error: str = "") -> dict[str, Any]:
    return {
        "summary": _short_text(
            "\n\n".join(
                [
                    state.get("context_summary", ""),
                    state.get("research_notes", ""),
                    state.get("coder_summary", ""),
                    state.get("verifier_summary", ""),
                    state.get("last_error", ""),
                ]
            ),
            2400,
        ),
        "active_goal": state.get("task", ""),
        "completed_work": state.get("coder_summary", ""),
        "open_todos": [
            todo.get("content", "")
            for todo in state.get("todos", [])
            if todo.get("status") != "completed"
        ],
        "important_files": _important_files_from_state(state),
        "tool_findings": _short_text(state.get("last_error", ""), 1200),
        "sources": [{"title": source.get("title", ""), "url": source.get("url", "")} for source in state.get("sources", [])],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _format_compressed_context(compressed: dict[str, Any], state: RealModelizeGraphState) -> str:
    payload = {
        "type": "rma_context_summary",
        "task": state.get("task", ""),
        "plan_summary": state.get("plan_summary", ""),
        "todos": state.get("todos", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "verification_commands": state.get("verification_commands", []),
        "attempts": state.get("attempts", 0),
        "passed": state.get("passed"),
        "compression": compressed,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _message_snapshot(message: Any) -> dict[str, str]:
    return {
        "type": type(message).__name__,
        "name": str(getattr(message, "name", "") or ""),
        "content": _short_text(_message_text(message), 2000),
    }


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _important_files_from_state(state: RealModelizeGraphState) -> list[str]:
    files: list[str] = []
    for command in state.get("verification_commands", []):
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|tex|md|json|html|csv|txt)", str(command)))
    for text in [state.get("coder_summary", ""), state.get("last_error", "")]:
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|tex|md|json|html|csv|txt)", str(text)))
    for path in state.get("figures", []):
        files.append(str(path))
    seen: set[str] = set()
    deduped = []
    for item in files:
        normalized = item.strip("\"'")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _planner_input(state: RealModelizeGraphState, memory: dict[str, Any]) -> str:
    parts = [
        f"题目：{state['task']}",
        f"Attempt（本轮尝试序号）：{state.get('attempts', 0) + 1}",
    ]
    if state.get("problem_json"):
        parts.append("结构化题目（含 ques_count，按此逐问核对产物）:\n" + json.dumps(state["problem_json"], ensure_ascii=False))
    if state.get("research_path"):
        parts.append(f"研究资料: {state['research_path']}")
    if state.get("problem_artifacts"):
        parts.append(
            "每问产物现状（监督核对）:\n" + json.dumps(state["problem_artifacts"], ensure_ascii=False, indent=2)
        )
    if state.get("session_context"):
        parts.append("多轮会话上下文：\n" + str(state.get("session_context", "")))
    if state.get("user_instruction"):
        parts.append(
            "本轮用户指令（必须决定插入计划还是执行命令，并委派目标 Agent）：\n"
            + json.dumps(state["user_instruction"], ensure_ascii=False, indent=2)
        )
    if state.get("execution_commands"):
        parts.append("待执行命令字段：\n" + json.dumps(state["execution_commands"], ensure_ascii=False, indent=2))
    parts.append("分层记忆快照：\n" + format_layered_memory_for_prompt(memory))
    return "\n\n".join(parts)


def _verifier_input(state: RealModelizeGraphState, memory: dict[str, Any]) -> str:
    parts = [f"题目：{state['task']}"]
    if state.get("problem_json"):
        parts.append("结构化题目（含 ques_count，逐问核对 problemN/）:\n" + json.dumps(state["problem_json"], ensure_ascii=False))
    if state.get("problem_artifacts"):
        parts.append(
            "每问产物（磁盘扫描索引）:\n" + json.dumps(state["problem_artifacts"], ensure_ascii=False, indent=2)
        )
    if state.get("session_context"):
        parts.append("多轮会话上下文：\n" + str(state.get("session_context", "")))
    parts.append("分层记忆快照：\n" + format_layered_memory_for_prompt(memory))
    parts.append("用只读工具检查工作区，只返回 verifier JSON。")
    return "\n\n".join(parts)


def _loop_budget(env_name: str, default: int) -> int:
    load_dotenv()
    raw = os.getenv(env_name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_plan(task: str) -> dict[str, Any]:
    if _is_math_modeling_task(task):
        return {
            "plan_summary": "按 CUMCM 流程完成数模论文：建模手分析建模 → 编程手求解出图 → 论文手写 LaTeX 编译。",
            "todos": CUMCM_TODOS,
            "acceptance_criteria": CUMCM_CRITERIA,
            "verification_commands": CUMCM_COMMANDS,
        }
    return {
        "plan_summary": "协调各专家 Agent 完成并验证请求的交付物。",
        "todos": DEFAULT_TODOS,
        "acceptance_criteria": ["请求的交付物存在。", "verifier 模型确认完成。"],
        "verification_commands": [],
    }


def _inject_user_instruction(state: RealModelizeGraphState) -> None:
    instruction = state.get("user_instruction")
    if not instruction or state.get("instruction_applied"):
        return
    raw = str(instruction.get("raw_text", "")).strip()
    action = instruction.get("plan_action")
    if raw and action == "insert_plan":
        todos = [dict(item) for item in state.get("todos", [])]
        if not any(item.get("content") == f"用户指令：{raw}" for item in todos):
            todos.append({"id": f"todo-{len(todos) + 1}", "content": f"用户指令：{raw}", "status": "pending", "note": "human-in-loop"})
        state["todos"] = todos
    elif raw:
        commands = list(state.get("execution_commands", []))
        if raw not in commands:
            commands.append(raw)
        state["execution_commands"] = commands
    state["instruction_applied"] = True


def _apply_plan(state: RealModelizeGraphState, plan: dict[str, Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = _todo_items([str(item) for item in plan.get("todos", [])], existing=state.get("todos", []))
    state["acceptance_criteria"] = [str(item) for item in plan.get("acceptance_criteria", [])]
    state["verification_commands"] = _verification_commands_for_task(state["task"], plan)


def _verification_commands_for_task(task: str, parsed: dict[str, Any]) -> list[str]:
    if _is_math_modeling_task(task):
        return CUMCM_COMMANDS
    return [str(item) for item in parsed.get("verification_commands") or []]


def _todo_items(todos: list[str], *, existing: list[dict[str, Any]] | None = None) -> list[TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[TodoItem] = []
    for idx, todo in enumerate(todos, start=1):
        previous = existing_by_content.get(todo, {})
        items.append(
            {
                "id": str(previous.get("id") or f"todo-{idx}"),
                "content": todo,
                "status": str(previous.get("status") or "pending"),
                "note": str(previous.get("note") or ""),
            }
        )
    return items


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(tool_message.content))
    except json.JSONDecodeError:
        parsed = tool_message.content
    return {"type": "tool_result", "node": node, "name": tool_message.name, "result": parsed}


def _tool_events_to_verification_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for event in events:
        result = event.get("result", {})
        if not isinstance(result, dict):
            continue
        results.append(
            {
                "command": result.get("command") or event.get("name", ""),
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr") or result.get("error", "")),
            }
        )
    return results


def _normalize_checks(raw: Any) -> list[VerificationCheck]:
    if not isinstance(raw, list):
        return []
    checks: list[VerificationCheck] = []
    for item in raw:
        if isinstance(item, dict):
            checks.append(
                {
                    "name": str(item.get("name") or "check"),
                    "passed": bool(item.get("passed")),
                    "detail": str(item.get("detail") or ""),
                }
            )
    return checks


def _format_verifier_error(reason: str, recommended: str, tool_events: list[dict[str, Any]]) -> str:
    event_text = json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )


def _paper_status_text(state: RealModelizeGraphState) -> str:
    tex = state.get("paper_path", "")
    if tex:
        return f"论文：{tex}；编译成功：{'是' if state.get('paper_compile_ok') else '否'}"
    if state.get("paper_compile_ok") is None:
        return "（未生成论文）"
    return "（未生成论文）"


def _join_notes(existing: str, new: str) -> str:
    if not existing:
        return new
    if not new:
        return existing
    return existing + "\n\n" + new


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for source in sources:
        url = str(source.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for handoff in handoffs[-6:]:
        trimmed.append(
            {
                "from_agent": handoff.get("from_agent", ""),
                "to_agent": handoff.get("to_agent", ""),
                "instruction": _short_text(str(handoff.get("instruction", "")), 500),
                "result": _short_text(str(handoff.get("result", "")), 700),
            }
        )
    return trimmed


def _short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""


def _is_math_modeling_task(task: str) -> bool:
    lowered = task.lower()
    keywords = [
        "建模",
        "数模",
        "数学建模",
        "cumcm",
        "美赛",
        "全国大学生数学建模",
        "预测",
        "回归分析",
        "优化模型",
        "评价模型",
        "时间序列",
        "聚类",
    ]
    return any(keyword.lower() in lowered for keyword in keywords)


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
