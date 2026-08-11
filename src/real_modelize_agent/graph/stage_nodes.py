"""Planner-owned nodes for the four-stage workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langgraph.config import get_stream_writer

from real_modelize_agent.agents.handoffs import (
    call_coder_handoff,
    call_modeler_handoff,
    call_research_handoff,
)
from real_modelize_agent.agents.write_agent import run_writer_agent
from real_modelize_agent.core.paths import find_project_root
from real_modelize_agent.core.preparation import prepare_workspace
from real_modelize_agent.core.stages import Stage, next_stage, verify_stage
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.tools.notepad_tool import append_notepad

Writer = Callable[[dict[str, Any]], None]

STAGE_TODOS = {
    Stage.PREPARE: "Stage 0 / Prepare：题目、数据、模板、工作区初始化",
    Stage.ANALYSIS: "Stage 1 / Analysis：研究资料、总方案、分问方案与敏感性方案",
    Stage.CODE: "Stage 2 / Code：分问及敏感性代码、结果、证据文件、建模结果",
    Stage.WRITING: "Stage 3 / Writing：分问文稿、图表、LaTeX 工程与 PDF",
}


def prepare_stage_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _writer()
    runtime = state["runtime"]
    project = find_project_root(runtime.workspace)
    if not (project / "数模论文模板").is_dir():
        project = find_project_root(Path.cwd())
    result = prepare_workspace(
        runtime.workspace,
        problem_json=state.get("problem_json"),
        problem_source=project / "problem",
        template_source=project / "数模论文模板",
    )
    summary = (
        f"Stage 0 prepared raw/题目.md and article/main.tex; "
        f"copied {len(result['copied_inputs'])} problem inputs."
    )
    _note(state, "Prepare Stage", summary)
    writer({"type": "stage_execution", "stage": Stage.PREPARE.value, "agent": "planner", **result})
    return {
        "stage": Stage.PREPARE.value,
        "prepare_summary": summary,
        "plan_summary": "Planner: Prepare → Analysis → Code → Writing；每阶段经仅完整性 Verify 门禁。",
        "todos": state.get("todos") or [
            {"id": f"stage-{index}", "content": content, "status": "pending", "note": ""}
            for index, content in enumerate(STAGE_TODOS.values())
        ],
    }


def analysis_stage_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _writer()
    working: RealModelizeGraphState = {**state, "stage": Stage.ANALYSIS.value}
    missing = _missing_instruction(state)
    research = call_research_handoff(
        working,
        writer,
        "Stage 1：检索与题目、候选模型、评价指标相关的真实资料；生成 research/研究资料.md 和 research/refs.bib。"
        + missing,
        from_agent="planner",
    )
    modeler = call_modeler_handoff(
        working,
        writer,
        "Stage 1：基于 raw/题目.md、原始数据和研究资料，生成建模方案.md、术语符号表.md、"
        "每问 problemN/方案/{方案.md,模型公式.md} 及 problem_sensitivity/敏感性分析方案.md。"
        "如证据不足可调用 Research Agent 定向补充；本阶段不写求解代码。"
        + missing,
        from_agent="planner",
    )
    summary = f"Research: {research.get('summary', '')}\nModel: {modeler.get('summary', '')}"
    _note(working, "Analysis Stage", summary)
    return _handoff_state(working, {"stage": Stage.ANALYSIS.value, "modeler_summary": modeler.get("summary", "")})


def code_stage_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _writer()
    working: RealModelizeGraphState = {**state, "stage": Stage.CODE.value}
    missing = _missing_instruction(state)
    coder = call_coder_handoff(
        working,
        writer,
        "Stage 2 / code 状态：依据固化方案逐问实现、运行并输出证据。每个 problemN 与 "
        "problem_sensitivity 必须生成 代码/*.py、代码/README.md、结果/*_evidence.json；"
        "共享模块放 util/，临时文件放 tmp/。" + missing,
        from_agent="planner",
    )
    modeler = call_modeler_handoff(
        working,
        writer,
        "Stage 2 审查：检查代码与方案的一致性及结果合理性；必要时调用 Code Agent 修改并复跑。"
        "固化后更新建模方案.md/分问方案，并生成建模结果.md。" + missing,
        from_agent="planner",
    )
    summary = f"Code: {coder.get('summary', '')}\nModel review: {modeler.get('summary', '')}"
    _note(working, "Code Stage", summary)
    return _handoff_state(
        working,
        {
            "stage": Stage.CODE.value,
            "coder_summary": coder.get("summary", ""),
            "modeler_summary": modeler.get("summary", ""),
        },
    )


def writing_stage_node(state: RealModelizeGraphState) -> dict[str, Any]:
    writer = _writer()
    working: RealModelizeGraphState = {**state, "stage": Stage.WRITING.value}
    missing = _missing_instruction(state)
    writer({
        "type": "handoff",
        "from": "planner",
        "to": "writerAgent",
        "instruction": "Stage 3：分问文稿、按需绘图、填充 article/main.tex 并编译。" + missing,
    })
    result = run_writer_agent(
        working,
        "Stage 3：先生成每问 problemN/文稿.md 与 problem_sensitivity/敏感性分析文稿.md；"
        "缺图时以 figure 状态调用 Code Agent；随后填充 article/main.tex，所有依赖留在 article/，"
        "用 XeLaTeX 编译为 article/main.pdf。" + missing,
        writer=writer,
    )
    working["todos"] = result.get("todos", working.get("todos", []))
    working["writer_summary"] = result.get("summary", "")
    working["paper_path"] = result.get("paper_path", "")
    working["paper_compile_ok"] = bool(result.get("compile_ok"))
    working["problem_artifacts"] = result.get("problem_artifacts", working.get("problem_artifacts", {}))
    summary = result.get("summary", "")
    _note(working, "Writing Stage", summary)
    writer({"type": "handoff_result", "from": "writerAgent", "to": "planner", "result": summary})
    return _handoff_state(working, {"stage": Stage.WRITING.value})


def stage_verifier_node(state: RealModelizeGraphState) -> dict[str, Any]:
    """Verify the current stage deterministically and only for completeness."""
    writer = _writer()
    stage = Stage(state.get("stage", Stage.PREPARE.value))
    result = verify_stage(state["runtime"].workspace, stage, state.get("problem_json"))
    attempts = dict(state.get("stage_attempts", {}))
    attempts[stage.value] = attempts.get(stage.value, 0) + 1
    verifications = dict(state.get("stage_verifications", {}))
    verifications[stage.value] = result
    history = list(state.get("stage_history", []))
    history.append(
        {
            "stage": stage.value,
            "passed": result["passed"],
            "attempt": attempts[stage.value],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "missing": result["missing"],
        }
    )
    max_attempts = int(state.get("max_attempts", 3))
    exhausted = not result["passed"] and attempts[stage.value] >= max_attempts
    verified_stage = stage
    if result["passed"]:
        following = next_stage(stage)
        next_node = "final" if following is Stage.COMPLETE else f"{following.value}_stage"
        new_stage = following.value
    elif exhausted:
        next_node = "final"
        new_stage = stage.value
    else:
        next_node = f"{stage.value}_stage"
        new_stage = stage.value
    route_node = "context_monitor" if result["passed"] and verified_stage in {Stage.ANALYSIS, Stage.CODE} else next_node
    todos = [dict(item) for item in state.get("todos", [])]
    if result["passed"]:
        todo_id = f"stage-{list(STAGE_TODOS).index(stage)}"
        for item in todos:
            if item.get("id") == todo_id:
                item["status"] = "completed"
                item["note"] = result["summary"]
        _mark_todo_file(state["runtime"].workspace, STAGE_TODOS[stage])
    writer({"type": "stage_verification", **result, "attempt": attempts[stage.value], "next_node": route_node})
    return {
        "stage": new_stage,
        "stage_attempts": attempts,
        "stage_verifications": verifications,
        "stage_history": history,
        "stage_next_node": route_node,
        "context_next_node": next_node,
        "context_token_limit": 200000 if verified_stage in {Stage.ANALYSIS, Stage.CODE} else state.get("context_token_limit", 0),
        "verification_checks": result["checks"],
        "verifier_summary": result["summary"],
        "passed": bool(result["passed"] and following is Stage.COMPLETE) if result["passed"] else False,
        "last_error": "" if result["passed"] else result["summary"],
        "todos": todos,
    }


def stage_verifier_route(state: RealModelizeGraphState) -> str:
    return state.get("stage_next_node", "final")


def initial_stage_route(state: RealModelizeGraphState) -> str:
    """Resume from the persisted stage instead of replaying passed stages."""
    try:
        stage = Stage(state.get("stage", Stage.PREPARE.value))
    except ValueError:
        stage = Stage.PREPARE
    return "final" if stage is Stage.COMPLETE else f"{stage.value}_stage"


def _missing_instruction(state: RealModelizeGraphState) -> str:
    stage = str(state.get("stage", ""))
    result = state.get("stage_verifications", {}).get(stage, {})
    missing = result.get("missing", []) if isinstance(result, dict) else []
    return f"\n上轮 Verify 缺失项（只补这些并保持已有产物）：{', '.join(missing)}" if missing else ""


def _handoff_state(working: RealModelizeGraphState, extra: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "todos", "research_notes", "research_path", "references_bib", "sources", "agent_handoffs",
        "modeler_summary", "modeler_plan", "modeler_plan_path", "coder_summary", "figures",
        "results_summary", "problem_artifacts", "writer_summary", "paper_path", "paper_compile_ok",
        "code_work_type", "subagent_runs", "work_records",
    )
    return {**{key: working[key] for key in keys if key in working}, **extra}


def _note(state: RealModelizeGraphState, heading: str, content: str) -> None:
    try:
        append_notepad(state["runtime"], heading, content[:4000] or "（无摘要）")
    except Exception:
        pass


def _mark_todo_file(workspace: Path, label: str) -> None:
    path = workspace / "TODO.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace(f"- [ ] {label}", f"- [x] {label}")
    path.write_text(text, encoding="utf-8")


def _writer() -> Writer:
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
