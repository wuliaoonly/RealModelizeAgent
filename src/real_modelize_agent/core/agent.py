from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from langgraph.graph import add_messages

from real_modelize_agent.core.checkpoint import CheckpointManager, load_resume_inputs, normalize_checkpoint_mode
from real_modelize_agent.core.paths import default_workspace
from real_modelize_agent.core.human_loop import classify_user_input, record_human_request
from real_modelize_agent.agents.chat_agent import peer_chat
from real_modelize_agent.core.session import (
    append_assistant_turn,
    append_user_turn,
    build_session_context,
    load_or_create_session,
    save_session,
    session_started_event,
    session_turn_saved_event,
    session_turn_started_event,
)
from real_modelize_agent.core.state import RuntimeState
from real_modelize_agent.core.trace import TraceRecorder, normalize_trace_mode
from real_modelize_agent.graph.workflow import build_complex_workflow, build_entry_workflow


def create_runtime(
    workspace: Path | None = None,
    *,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_from: Path | None = None,
    trace_mode: str | None = None,
) -> RuntimeState:
    load_dotenv()
    selected = workspace or resume_from or default_workspace()
    selected.mkdir(parents=True, exist_ok=True)
    return RuntimeState(
        workspace=selected,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int("RMA_BASH_DEFAULT_TIMEOUT_SECONDS", 120),
        bash_max_timeout_seconds=_env_int("RMA_BASH_MAX_TIMEOUT_SECONDS", 600),
        bash_max_output_chars=_env_int("RMA_BASH_MAX_OUTPUT_CHARS", 6000),
        bash_env_file=_env_path("RMA_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(checkpoint_mode or os.getenv("RMA_CHECKPOINT_MODE", "light")),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(trace_mode or os.getenv("RMA_TRACE_MODE", "on")),
    )


def stream_agent_events(
    task: str | None = None,
    *,
    workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Run the 数模 agent and stream events (custom_event / graph_event / workspace).

    流程：entry 图先经 coordinator 判定是否为数模题；是则进入 complex 图
    （planner→建模手/编程手/论文手→verifier 循环），否则直接结束。
    """
    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None
    incoming_text = task or ("继续执行现有计划" if resume_path is not None else "")
    decision = classify_user_input(incoming_text, has_existing_workspace=resume_path is not None)
    yield {"type": "custom_event", "event": {"type": "intent_decision", **decision.to_dict()}}
    if decision.intent == "chat":
        result = peer_chat(incoming_text, writer=lambda event: None)
        for reply in result["replies"]:
            yield {"type": "custom_event", "event": {"type": "peer_reply", **reply}}
        yield {"type": "graph_event", "event": {"chat": {"final_answer": result["final_answer"], "user_intent": "chat"}}}
        return

    if resume_path is None:
        detected = False
        entry_state: dict[str, Any] = {"task": task or "", "messages": []}
        for mode, event in build_entry_workflow().stream(entry_state, stream_mode=["updates", "custom"]):
            if mode == "custom":
                yield {"type": "custom_event", "event": event}
                if isinstance(event, dict) and event.get("type") == "problem_decision":
                    detected = bool(event.get("detected"))
            else:
                _merge_graph_update(entry_state, event)
                yield {"type": "graph_event", "event": event}
        if not detected:
            return

    selected_workspace = resume_path or workspace
    state = create_runtime(
        selected_workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
    )
    workflow = build_complex_workflow()
    yield {"type": "workspace", "path": str(state.workspace)}
    request_log = record_human_request(state.workspace, decision)
    yield {"type": "custom_event", "event": {"type": "human_request_recorded", "path": str(request_log)}}

    session = load_or_create_session(state.workspace)
    turn = append_user_turn(session, incoming_text)
    save_session(state.workspace, session)
    yield {"type": "custom_event", "event": session_started_event(state.workspace, session, resumed=resume_path is not None)}
    yield {"type": "custom_event", "event": session_turn_started_event(state.workspace, session, turn=turn, task=incoming_text)}

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    inputs["user_intent"] = decision.intent
    inputs["user_instruction"] = decision.to_dict() if decision.intent == "instruction" else {}
    inputs["instruction_applied"] = False
    inputs["chart_style_request"] = decision.chart_style
    inputs["paragraph_edit_request"] = decision.paragraph_edit
    inputs["session_id"] = session.get("session_id", "")
    inputs["session_turn"] = turn
    inputs["session_context"] = build_session_context(state.workspace, session)

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(current_state, status="started", latest_node="start")
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"
    final_answer = ""

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                for update in event.values() if isinstance(event, dict) else []:
                    if isinstance(update, dict) and update.get("final_answer"):
                        final_answer = str(update["final_answer"])
                trace.record_graph_update(event)
                saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        saved = manager.save(current_state, status="interrupted", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="interrupted", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(status="finished", latest_node=latest_node, final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}
    append_assistant_turn(session, turn=turn, route=decision.target, content=final_answer or "本轮工作流已结束。")
    save_session(state.workspace, session)
    yield {"type": "custom_event", "event": session_turn_saved_event(state.workspace, session, turn=turn, route=decision.target)}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _latest_graph_node(event: Any) -> str | None:
    if isinstance(event, dict) and event:
        return str(next(reversed(event)))
    return None


def _merge_graph_update(state: dict[str, Any], event: Any) -> None:
    if not isinstance(event, dict):
        return
    for update in event.values():
        if not isinstance(update, dict):
            continue
        for key, value in update.items():
            if key == "messages":
                state["messages"] = list(add_messages(state.get("messages", []), value))
            else:
                state[key] = value


def _custom_event_needs_checkpoint(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "tool_result":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("ok") is False or bool(result.get("requires_approval"))
