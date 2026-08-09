from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

import typer
from rich import box
from rich.panel import Panel
from typer.core import TyperGroup

from real_modelize_agent.cli.formatter import print_event, safe_echo, safe_secho
from real_modelize_agent.core.approval import ApprovalDecision, ApprovalRequest
from real_modelize_agent.core.agent import stream_agent_events


class RealModelizeGroup(TyperGroup):
    """Let ``real-modelize "task"`` coexist with real subcommands."""

    # 布尔开关型选项：不消费后面的参数值
    no_value_flags = {"--dry-run"}

    def parse_args(self, ctx, args):  # type: ignore[no-untyped-def]
        commands = set(self.commands)
        remaining: list[str] = []
        task_parts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in commands or arg == "--help":
                remaining.extend(args[index:])
                break
            if arg.startswith("-"):
                remaining.append(arg)
                takes_value = arg not in self.no_value_flags and "=" not in arg
                if takes_value and index + 1 < len(args) and not args[index + 1].startswith("-"):
                    remaining.append(args[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            task_parts.extend(args[index:])
            break
        if task_parts:
            ctx.obj = dict(ctx.obj or {})
            ctx.obj["task_arg"] = " ".join(task_parts)
        return super().parse_args(ctx, remaining)


app = typer.Typer(
    cls=RealModelizeGroup,
    help=(
        'RealModelizeAgent: 数学建模 Code Agent。使用 `real-modelize "题目"` 直接运行，'
        '或 `real-modelize tui` 进入 TUI，`real-modelize compile --workspace <path>` 编译论文。'
    ),
)


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="生成文件的工作区。默认新建 .real-modelize/workspaces/workspace-* 目录。"),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="planner/actor/verifier 最大尝试次数。"),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="高危 BashTool 命令的人工审批模式：inline、auto 或 deny。"),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="检查点模式：light、strict 或 off。"),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="Trace 记录模式：on 或 off。"),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="从已存在的 RealModelizeAgent 工作区恢复。"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="不调用真实模型，用罐头事件流演示 UI（无 API key）。"),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    configure_console()
    task = None
    if isinstance(ctx.obj, dict):
        task = ctx.obj.get("task_arg")
    if not task and not dry_run and resume is None:
        safe_echo(ctx.get_help())
        raise typer.Exit()

    safe_secho("RealModelizeAgent — 数学建模 Code Agent", fg=typer.colors.MAGENTA)
    approval_handler = _inline_approval_handler if approval_mode == "inline" else None
    events = _stream_dry_run(task) if dry_run else stream_agent_events(
        task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume,
        trace_mode=trace_mode,
    )
    for event in events:
        print_event(event)


@app.command("tui")
def tui(
    task: Annotated[str | None, typer.Argument(help="Textual TUI 的初始任务（可选）。")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="TUI 会话的工作区。"),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="planner/actor/verifier 最大尝试次数。"),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="高危 BashTool 命令的人工审批模式：inline、auto 或 deny。"),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="检查点模式：light、strict 或 off。"),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="Trace 记录模式：on 或 off。"),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="从已存在的 RealModelizeAgent 工作区恢复。"),
    ] = None,
) -> None:
    """打开 Textual 终端界面。"""
    configure_console()
    from real_modelize_agent.cli.tui import RealModelizeTuiApp

    RealModelizeTuiApp(
        initial_task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        checkpoint_mode=checkpoint_mode,
        trace_mode=trace_mode,
        resume=resume,
    ).run()


@app.command("compile")
def compile_paper(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="工作区路径（应包含 论文.tex）。"),
    ] = Path("."),
    tex: Annotated[
        str,
        typer.Option("--tex", help="LaTeX 主文件名，默认 论文.tex。"),
    ] = "论文.tex",
    engine: Annotated[
        str | None,
        typer.Option("--engine", help="LaTeX 引擎（xelatex 或 pdflatex；默认取 RMA_LATEX_ENGINE/xelatex）。"),
    ] = None,
) -> None:
    """对工作区中的 LaTeX 论文跑两遍编译生成 PDF。"""
    configure_console()
    from real_modelize_agent.agents.write_agent import build_latex_command, latex_engine

    workspace = workspace.expanduser()
    if not workspace.is_dir():
        safe_secho(f"workspace not found: {workspace}", fg="red")
        raise typer.Exit(code=1)
    tex_path = workspace / tex
    if not tex_path.exists():
        safe_secho(f"tex not found: {tex_path}", fg="red")
        raise typer.Exit(code=1)
    engine = engine or latex_engine()
    command = build_latex_command(tex, engine)
    for run in (1, 2):
        safe_secho(f"run {run}: {command}", fg="cyan")
        result = subprocess.run(command.split(), cwd=str(workspace), capture_output=True, text=True)
        if result.returncode != 0:
            safe_secho(f"{engine} failed (exit {result.returncode})", fg="red")
            safe_secho(result.stderr[-1200:], fg="red")
    pdf = workspace / (tex.rsplit(".", 1)[0] + ".pdf")
    if pdf.exists():
        safe_secho(f"OK: {pdf}", fg="green")
    else:
        safe_secho("FAIL: PDF not produced. See 论文.log for details.", fg="red")
        raise typer.Exit(code=1)


def _inline_approval_handler(request: ApprovalRequest) -> ApprovalDecision:
    from real_modelize_agent.cli.formatter import console

    console.print(
        Panel(
            f"Command:\n{request.command}\n\nRisk:\n{request.risk_reason}",
            title=f"Human Approval · {request.tool_name}",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )
    answer = typer.prompt("Approve? [y/N]", default="n", show_default=False).strip().lower()
    console.print()
    approved = answer in {"y", "yes"}
    return ApprovalDecision(approved=approved, reason="" if approved else "Rejected by human operator.")


def _stream_dry_run(task: str | None) -> Iterator[dict[str, Any]]:
    """罐头事件流：无 API key 演示 Rich 事件 UI。"""
    sample_task = task or (
        "2024 CUMCM A 题：基于'板凳龙'舞龙队行进路径的数学建模问题——"
        "请分析螺距与转圈数，建立行进路径模型并求解。"
    )
    yield {"type": "workspace", "path": str(Path(".real-modelize") / "workspaces" / "dry-run")}
    yield {
        "type": "custom_event",
        "event": {
            "type": "problem_decision",
            "detected": True,
            "problem_json": {
                "title": "舞龙队行进路径模型",
                "background": "求龙身各点运动轨迹与行进距离",
                "ques_count": 4,
                "ques1": "求螺线起点到终点路径",
                "ques2": "求相邻板凳间距离变化",
            },
            "reason": "dry-run demo: detected as math modeling problem",
        },
    }
    yield {
        "type": "custom_event",
        "event": {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": "按 CUMCM 流程完成数模论文：建模手分析建模 → 编程手求解出图 → 论文手写作轮+图表轮。",
            "todos": [
                {"id": "todo-1", "content": "建模手：撰写 题目分析.md、建模方案.json 与每问 problemN/方案/", "status": "pending", "note": ""},
                {"id": "todo-2", "content": "编程手：Python 求解并生成每问 problemN/{代码,图表,结果}", "status": "pending", "note": ""},
                {"id": "todo-3", "content": "论文手：按国赛模板就地填充 论文.tex 并 xelatex 编译", "status": "pending", "note": ""},
            ],
            "verification_commands": ["python -c \"...\""],
        },
    }
    yield {"type": "custom_event", "event": {"type": "handoff", "from": "planner", "to": "researchAgent", "instruction": "检索题目背景与参考方法"}}
    yield {
        "type": "custom_event",
        "event": {"type": "research_file", "path": "research/研究资料.md", "sources": 3, "queries": ["舞龙队 板凳龙 建模", "螺距 转圈 路径模型"]},
    }
    yield {"type": "custom_event", "event": {"type": "handoff_result", "from": "researchAgent", "to": "planner", "result": "已检索并写入 research/研究资料.md（3 个来源）。"}}
    yield {"type": "custom_event", "event": {"type": "handoff", "from": "planner", "to": "modelerAgent", "instruction": "分析题目并建立每问建模方案"}}
    yield {"type": "custom_event", "event": {"type": "tool_call", "node": "modelerAgent", "name": "FileWriteTool", "args": {"file_path": "建模方案.json"}}}
    yield {
        "type": "custom_event",
        "event": {"type": "tool_result", "node": "modelerAgent", "name": "FileWriteTool", "result": {"ok": True, "path": "建模方案.json"}},
    }
    yield {"type": "custom_event", "event": {"type": "handoff_result", "from": "modelerAgent", "to": "planner", "result": "已产出题目分析.md、建模方案.json 与 problem1/方案、problem2/方案。"}}
    yield {"type": "custom_event", "event": {"type": "handoff", "from": "planner", "to": "coderAgent", "instruction": "按方案逐问求解并生成每问图表"}}
    yield {"type": "custom_event", "event": {"type": "handoff_result", "from": "coderAgent", "to": "planner", "result": "已生成 problem1/图表 8 张、problem2/图表 4 张与每问结果文件。"}}
    yield {"type": "custom_event", "event": {"type": "handoff", "from": "writerAgent", "to": "coderAgent", "instruction": "图表轮：重新生成 problem2/图表/fig2_path.png（≥300dpi）"}}
    yield {
        "type": "custom_event",
        "event": {"type": "figure_check", "node": "writerAgent", "passed": False, "detail": "problem2/图表/fig2_path.png 分辨率不足 300dpi，已要求编程手重做"},
    }
    yield {
        "type": "custom_event",
        "event": {"type": "figure_check", "node": "writerAgent", "passed": True, "detail": "逐问核对通过：problem1/图表 8 张、problem2/图表 4 张均 ≥300dpi"},
    }
    yield {
        "type": "custom_event",
        "event": {"type": "paper_compile", "node": "writerAgent", "phase": "done", "status": {"paper_tex": "论文.tex", "paper_pdf": "论文.pdf", "compile_ok": True, "tex_chars": 12345}},
    }
    yield {
        "type": "graph_event",
        "event": {
            "verifier": {
                "passed": True,
                "verifier_summary": "dry-run 演示：论文与产物齐全。",
                "verification_checks": [
                    {"name": "建模产物", "passed": True, "detail": "题目分析.md 与 建模方案.json 存在"},
                    {"name": "每问目录", "passed": True, "detail": "problem1/、problem2/ 的 方案/代码/图表/结果 齐全"},
                    {"name": "论文编译", "passed": True, "detail": "论文.pdf 生成"},
                ],
                "attempts": 1,
            }
        },
    }
    yield {
        "type": "graph_event",
        "event": {
            "final": {
                "final_answer": "数模 Agent 工作流结束：PASSED\n\n计划：按 CUMCM 流程完成数模论文。\n\n论文状态：论文.tex；编译成功：是",
            }
        },
    }
