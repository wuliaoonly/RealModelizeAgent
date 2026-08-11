from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from real_modelize_agent.agents.artifacts import collect_problem_artifacts
from real_modelize_agent.agents.handoffs import make_coder_handoff_tool, make_modeler_handoff_tool, make_research_handoff_tool
from real_modelize_agent.core.state import RuntimeState
from real_modelize_agent.graph.memory import build_layered_memory, format_layered_memory_for_prompt, memory_event
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.prompts.write import WRITER_PROMPT, WRITER_PROMPT_SHORT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.registry import build_writer_tools
from real_modelize_agent.tools.latex_tool import latex_compile_status
from real_modelize_agent.tools.skill_briefing import load_skill_briefing
from real_modelize_agent.tools.todo_tool import persist_todos, update_todo

Writer = Callable[[dict[str, Any]], None]

PAPER_TEX = "论文.tex"
PAPER_PDF = "论文.pdf"


def build_latex_command(tex_name: str, engine: str = "pdflatex") -> str:
    """生成 pdflatex 编译命令（在 tex 所在目录下执行）。"""
    return f"{engine} -interaction=nonstopmode {tex_name}"


def parse_latex_errors(log_path: Path, limit: int = 8) -> list[str]:
    """从 .log 中提取 LaTeX 错误行（^! 开头的行及其下一行）。"""
    if not log_path.exists():
        return [f"log file not found: {log_path.name}"]
    errors: list[str] = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [f"cannot read log file: {log_path.name}"]
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("!"):
            context = lines[index + 1].strip() if index + 1 < len(lines) else ""
            errors.append(f"{stripped}  |  {context}")
            if len(errors) >= limit:
                break
    return errors or ["no '!' error lines found in log"]


def paper_status(workspace: Path) -> dict[str, Any]:
    """Return status only for a successful, fresh, recorded compilation."""
    return latex_compile_status(workspace, PAPER_TEX)


def resolve_paper_template_dir() -> Path | None:
    """国赛模板目录：env RMA_PAPER_TEMPLATE_DIR 优先（相对 cwd），否则 repo 根下 数模论文模板。"""
    env_dir = os.getenv("RMA_PAPER_TEMPLATE_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate if candidate.is_dir() else None
    repo_template = Path(__file__).resolve().parents[3] / "数模论文模板"
    return repo_template if repo_template.is_dir() else None


def seed_paper_template(workspace: Path, template_dir: Path | None = None) -> dict[str, Any]:
    """把国赛模板 seed 进工作区：main.tex→论文.tex、references.bib、Pictures/、字体。

    **不覆盖保证**：论文.tex 已存在（真实成品/旧工作区）→ 跳过，--resume 安全。
    **模板强制**：已不再回退旧 header+sections 流程——模板缺失即返回 ok=False，调用方必须中止。
    返回 {ok, reason, files}。
    """
    tex = workspace / PAPER_TEX
    if tex.exists():
        return {"ok": True, "reason": "论文.tex 已存在，跳过 seed（不覆盖成品）", "files": []}
    template_dir = template_dir if template_dir is not None else resolve_paper_template_dir()
    if template_dir is None or not template_dir.is_dir():
        return {
            "ok": False,
            "reason": "未找到数模论文模板，已不再支持回退旧流程：请将国赛模板放入 数模论文模板/ 或设置 RMA_PAPER_TEMPLATE_DIR",
            "files": [],
        }
    copied: list[str] = []
    main_tex = template_dir / "main.tex"
    if main_tex.exists():
        shutil.copy2(main_tex, tex)
        copied.append(PAPER_TEX)
    for name in ("references.bib", "SIMLI.TTF", "lishugbk.ttf"):
        src = template_dir / name
        if src.exists():
            shutil.copy2(src, workspace / name)
            copied.append(name)
    pictures = template_dir / "Pictures"
    if pictures.is_dir():
        shutil.copytree(pictures, workspace / "Pictures", dirs_exist_ok=True)
        copied.append("Pictures/")
    return {"ok": True, "reason": f"已按国赛模板生成论文骨架（{', '.join(copied)}）", "files": copied}


def latex_engine() -> str:
    """LaTeX 编译引擎：RMA_LATEX_ENGINE 覆盖，默认 xelatex（国赛模板仅支持 XeLaTeX）。"""
    return (os.getenv("RMA_LATEX_ENGINE", "xelatex") or "xelatex").strip() or "xelatex"


def run_writer_agent(
    state: RealModelizeGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 28,
) -> dict[str, Any]:
    """论文手：写作轮（全文结构+素材索要）+ 图表轮（每问图表把关），撰写并编译 论文.pdf。

    写作轮可调 CallResearchAgentTool/CallModelerAgentTool/CallCoderAgentTool 按需索要素材；
    图表轮用 PIL 逐问核对 problemN/图表/ 并驱动编程手修图。
    """
    max_loops = int(os.getenv("RMA_WRITER_MAX_LOOPS", str(max_loops)))
    runtime = state["runtime"]
    todos = [dict(todo) for todo in state.get("todos", [])]
    writer = writer or (lambda _: None)
    memory = build_layered_memory({**state, "todos": todos}, node="writerAgent")
    writer(memory_event(memory, node="writerAgent"))
    model = create_model()
    tools = build_writer_tools(runtime, todos) + [
        make_research_handoff_tool(state, writer, from_agent="writerAgent"),
        make_modeler_handoff_tool(state, writer, from_agent="writerAgent"),
        make_coder_handoff_tool(state, writer, from_agent="writerAgent"),
    ]
    writer_agent = model.bind_tools(tools)

    writer(
        {
            "type": "plan_snapshot",
            "node": "writerAgent",
            "plan_summary": state.get("plan_summary", ""),
            "todos": todos,
            "verification_commands": state.get("verification_commands", []),
        }
    )

    seed_info = seed_paper_template(runtime.workspace)
    if not seed_info.get("ok"):
        detail = seed_info.get("reason", "未找到数模论文模板")
        writer({"type": "workspace_note", "node": "writerAgent", "detail": detail})
        raise RuntimeError(detail)
    if seed_info.get("files"):
        writer(
            {
                "type": "workspace_note",
                "node": "writerAgent",
                "detail": f"论文骨架已按国赛模板生成：{', '.join(seed_info['files'])}",
            }
        )

    status = paper_status(runtime.workspace)
    system_prompt = WRITER_PROMPT if not status["compile_ok"] else WRITER_PROMPT_SHORT
    writer({"type": "paper_compile", "node": "writerAgent", "phase": "start", "status": status})

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=_writer_input(state, instruction, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(max_loops):
        response = writer_agent.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer({"type": "tool_call", "node": "writerAgent", "name": call.get("name"), "args": call.get("args", {})})
            if call.get("name") == "CallCoderAgentTool":
                # 图表轮：论文手要求编程手重做图 → 发 figure_check 事件
                writer(
                    {
                        "type": "figure_check",
                        "node": "writerAgent",
                        "passed": False,
                        "detail": f"图表轮未达标，要求编程手修改：{str(call.get('args', {}).get('instruction', ''))[:300]}",
                    }
                )
            tool_result, todos = _execute_writer_tool(runtime, todos, call, tools)
            event = _tool_result_event(tool_result, node="writerAgent")
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
        produced_messages.append(AIMessage(content="writerAgent stopped after the maximum tool loop count."))

    summary = _last_ai_content(produced_messages)
    final_status = paper_status(runtime.workspace)
    writer({"type": "paper_compile", "node": "writerAgent", "phase": "done", "status": final_status})
    return {
        "ok": True,
        "summary": summary,
        "todos": todos or state.get("todos", []),
        "paper_path": final_status["paper_tex"],
        "compile_ok": final_status["compile_ok"],
        "problem_artifacts": collect_problem_artifacts(runtime.workspace),
        "messages": produced_messages,
        "tool_events": tool_events,
    }


def _execute_writer_tool(runtime: RuntimeState, todos: list[dict[str, str]], call: dict[str, Any], tools: list[Any]):
    name = call.get("name", "")
    args = call.get("args") or {}
    if name == "TodoUpdateTool":
        result = update_todo(todos, args.get("todo_id", ""), args.get("status", ""), args.get("note", ""))
        if result.get("ok"):
            todos = result["todos"]
    else:
        tool_map = {tool.name: tool for tool in tools}
        tool = tool_map.get(name)
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


def _writer_input(state: RealModelizeGraphState, instruction: str, memory: dict[str, Any]) -> str:
    engine = latex_engine()
    parts = [
        f"题目:\n{state['task']}",
        f"planner 指令:\n{instruction}",
        "论文骨架：工作区根目录 `论文.tex` 已按国赛模板（数模论文模板/main.tex，XeLaTeX）生成，"
        "含完整章节结构与 `\\underline{...}` 占位。**用 FileEditTool 就地逐节填充/替换**："
        "不要重写前导，不要新建 header.tex / sections/，不要改成 pdflatex 头。",
    ]
    if state.get("problem_json"):
        parts.append("结构化的题目信息（含 ques_count，几问就写几问章节并核对几问图表）:\n"
                     + json.dumps(state["problem_json"], ensure_ascii=False))
    if state.get("modeler_plan"):
        parts.append("建模方案:\n" + json.dumps(state["modeler_plan"], ensure_ascii=False, default=str))
    if state.get("research_path"):
        parts.append(f"研究资料（真实检索结果，可引用作问题背景）: {state['research_path']}")
    if state.get("references_bib"):
        parts.append(
            f"参考文献（研究手真实检索生成的 BibTeX，键名 rN）: {state['references_bib']}\n"
            "写作时把其中的**真实条目**转为行内 thebibliography（保留键名，`\\cite{rN}` 引用），"
            "禁止自造文献/作者/年份。"
        )
    if state.get("problem_artifacts"):
        parts.append(
            "每问产物（图表轮按此逐问核对）:\n"
            + json.dumps(state["problem_artifacts"], ensure_ascii=False, indent=2)
        )
    if state.get("figures"):
        parts.append("已生成图片（须全部插入论文）:\n" + "\n".join(f"- {path}" for path in state["figures"]))
    if state.get("paragraph_edit_request"):
        parts.append(
            "人工指定的单段修改请求（优先执行，严格限制为一个段落）：\n"
            + json.dumps(state["paragraph_edit_request"], ensure_ascii=False, indent=2)
        )
        parts.append("使用 PaperParagraphEditTool 精确修改后，必须 CompileLatexTool 编译两遍；不要重写其他段落。")
    parts.append("分层记忆快照:\n" + format_layered_memory_for_prompt(memory))
    parts.append(
        "[Word/PDF 技能]\n"
        "PdfReadTool：只读提取 PDF 文本（检查 论文.pdf、阅读参考文献 PDF）。\n"
        "DocxReadTool：只读提取 Word 文档文本。\n"
        "DocxConvertTool：把 Markdown 等转成 Word 版论文交付（需要时用，主交付仍是 LaTeX→PDF）。\n"
        + "\n".join(load_skill_briefing(name, max_chars=360) for name in ("pdf", "docx"))
    )
    parts.append(
        "写作轮：面向全文按模板章节就地填充写作（摘要/一问题的提出和重述/二问题的分析/三模型假设/四符号说明/"
        "五数据的处理/六模型建立和求解(每问)/七模型的评价和改进/八模型的推广和应用/参考文献/附录），"
        "缺素材按需调 CallResearchAgentTool/CallModelerAgentTool/CallCoderAgentTool 索要。"
        "模板本身即可编译，直接就地完善各节即可；每完成 2-3 节编译一遍确认无致命错误；"
        f"图表轮逐问核对 `problem*/图表/` 与 evidence.json 达标后，调用 CompileLatexTool："
        f"tex_name=论文.tex、engine={engine}、passes=2；必须以工具返回 ok=true 为准。"
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
