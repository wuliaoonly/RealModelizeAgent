"""Deterministic Stage 0 workspace preparation."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from real_modelize_agent.agents.artifacts import ensure_problem_folders, problem_folder_names


TEXT_EXTENSIONS = {".md", ".txt", ".tex"}
DATA_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsm", ".json", ".tsv"}


def prepare_workspace(
    workspace: Path,
    *,
    problem_json: dict[str, Any] | None = None,
    problem_source: Path | None = None,
    template_source: Path | None = None,
) -> dict[str, Any]:
    """Create the canonical workspace without overwriting resumable artifacts."""
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    raw = workspace / "raw"
    raw.mkdir(exist_ok=True)
    (workspace / "data").mkdir(exist_ok=True)
    (workspace / "util").mkdir(exist_ok=True)
    (workspace / "tmp").mkdir(exist_ok=True)
    ensure_problem_folders(workspace, problem_json)
    for folder in problem_folder_names(problem_json):
        (workspace / folder / "文稿.md").parent.mkdir(parents=True, exist_ok=True)
    for sub in ("方案", "代码", "结果", "图表"):
        (workspace / "problem_sensitivity" / sub).mkdir(parents=True, exist_ok=True)

    copied_inputs = _copy_problem_inputs(problem_source, raw)
    statement = raw / "题目.md"
    if not statement.exists():
        statement.write_text(_extract_statement(raw, problem_json), encoding="utf-8")

    article = workspace / "article"
    article.mkdir(exist_ok=True)
    copied_template = _copy_template(template_source, article)

    todo = workspace / "TODO.md"
    if not todo.exists():
        todo.write_text(_todo_markdown(problem_json), encoding="utf-8")
    notepad = workspace / "NOTEPAD.md"
    if not notepad.exists():
        notepad.write_text(_notepad_markdown(), encoding="utf-8")

    return {
        "ok": statement.exists() and (article / "main.tex").exists(),
        "statement": "raw/题目.md",
        "article": "article/main.tex",
        "copied_inputs": copied_inputs,
        "copied_template": copied_template,
    }


def _copy_problem_inputs(source: Path | None, raw: Path) -> list[str]:
    if source is None or not source.is_dir():
        return []
    copied: list[str] = []
    for item in sorted(source.iterdir()):
        if not item.is_file() or item.name == ".gitkeep":
            continue
        target = raw / item.name
        if not target.exists():
            shutil.copy2(item, target)
        copied.append(target.name)
    return copied


def _copy_template(source: Path | None, article: Path) -> list[str]:
    if source is None or not source.is_dir():
        return []
    copied: list[str] = []
    for item in source.iterdir():
        # Build products are never seed material for a fresh workspace.
        if item.suffix.lower() in {".aux", ".bbl", ".blg", ".log", ".out", ".pdf"}:
            continue
        target = article / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif not target.exists():
            shutil.copy2(item, target)
        copied.append(item.name)
    return copied


def _extract_statement(raw: Path, problem_json: dict[str, Any] | None) -> str:
    chunks: list[str] = []
    for path in sorted(raw.iterdir()):
        if path.suffix.lower() in TEXT_EXTENSIONS and path.name != "题目.md":
            chunks.append(f"## 来源：{path.name}\n\n{path.read_text(encoding='utf-8', errors='replace').strip()}")
    for path in sorted(raw.glob("*.pdf")):
        text = _pdf_text(path)
        if text:
            chunks.append(f"## 来源：{path.name}\n\n{text}")
    if chunks:
        return "# 题目原文\n\n" + "\n\n".join(chunks).strip() + "\n"
    payload = problem_json or {}
    lines = ["# 题目原文", "", str(payload.get("title", "数学建模题目")), ""]
    if payload.get("background"):
        lines.extend([str(payload["background"]), ""])
    count = int(payload.get("ques_count", 1) or 1)
    for index in range(1, count + 1):
        lines.extend([f"## 问题 {index}", "", str(payload.get(f"ques{index}", "（待从题目附件提取）")), ""])
    return "\n".join(lines).rstrip() + "\n"


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader

        return "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(str(path)).pages).strip()
    except Exception:
        return ""


def _todo_markdown(problem_json: dict[str, Any] | None) -> str:
    return (
        "# TODO\n\n"
        "- [ ] Stage 0 / Prepare：题目、数据、模板、工作区初始化\n"
        "- [ ] Stage 1 / Analysis：研究资料、总方案、分问方案与敏感性方案\n"
        "- [ ] Stage 2 / Code：分问及敏感性代码、结果、证据文件、建模结果\n"
        "- [ ] Stage 3 / Writing：分问文稿、图表、LaTeX 工程与 PDF\n"
    )


def _notepad_markdown() -> str:
    return (
        "# NOTEPAD\n\n"
        "> Planner 管理的跨 Stage 信息流。只记录压缩摘要、决策、路径和待办；详细证据留在正式产物中。\n\n"
        "## Prepare\n\n## Analysis\n\n## Code\n\n## Writing\n"
    )
