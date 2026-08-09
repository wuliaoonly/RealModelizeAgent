"""每问独立文件夹与产物扫描助手（纯 stdlib，零依赖，避免导入环）。

新工作区结构：
    problem1/{方案,代码,图表,结果}/   problem2/{...} ...
    utils/（编程手共享工具，如 common_utils.py）
    tmp/（临时/调试代码与文件）
根目录保留：题目分析.md、建模方案.json、NOTEPAD.md、TODO.md、共享数据、论文。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

RESEARCH_DIR = "research"
RESEARCH_FILE = "研究资料.md"
REFERENCES_BIB = "参考文献.bib"

_PLAN_EXTS = (".md", ".json")
_CODE_EXTS = (".py",)
_FIG_EXTS = (".png", ".jpg", ".jpeg")
_RESULT_EXTS = (".csv", ".json", ".xlsx", ".txt")


def problem_folder_names(problem_json: dict[str, Any] | None) -> list[str]:
    """从 problem_json['ques_count'] 生成 ['problem1'..'problemN']；钳制 1..16。"""
    if not isinstance(problem_json, dict):
        return ["problem1"]
    raw = problem_json.get("ques_count", 1)
    try:
        count = max(1, min(16, int(raw)))
    except (TypeError, ValueError):
        count = 1
    return [f"problem{i}" for i in range(1, count + 1)]


def ensure_problem_folders(workspace: Path, problem_json: dict[str, Any] | None) -> list[str]:
    """确保每问目录 problemN/{方案,代码,图表,结果}、utils/、tmp/ 存在，返回目录名列表。

    tmp/ 用于存放编程手的临时/调试代码与文件；utils/ 存放共享工具（common_utils.py 等）；
    正式产物（代码/图表/结果）一律进每问目录。
    """
    names = problem_folder_names(problem_json)
    (workspace / "utils").mkdir(parents=True, exist_ok=True)
    (workspace / "tmp").mkdir(parents=True, exist_ok=True)
    for name in names:
        for sub in ("方案", "代码", "图表", "结果"):
            (workspace / name / sub).mkdir(parents=True, exist_ok=True)
    return names


def _rel_files(folder: Path, *exts: str) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in exts
    )


def collect_figures(workspace: Path) -> list[str]:
    """权威图清单：全部 problem*/图表/** 图片，返回相对工作区的 posix 路径。"""
    out: list[str] = []
    for problem in sorted(workspace.glob("problem*")):
        if problem.is_dir():
            out.extend(
                path.relative_to(workspace).as_posix()
                for path in _rel_files(problem / "图表", *_FIG_EXTS)
            )
    return out


def collect_solution_files(workspace: Path) -> list[str]:
    """编程手求解脚本：全部 problem*/代码/*.py，相对工作区的 posix 路径。"""
    out: list[str] = []
    for problem in sorted(workspace.glob("problem*")):
        if problem.is_dir():
            out.extend(
                path.relative_to(workspace).as_posix()
                for path in _rel_files(problem / "代码", *_CODE_EXTS)
            )
    return out


def _is_placeholder_script(path: Path) -> bool:
    """占位脚本判定：除模块 docstring / pass 外无任何可执行语句（例如只有说明文字的 .py）。

    用于把"指向 run_all.py 的占位 docstring"与"真实求解脚本"区分开。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # 模块 docstring / 纯字符串
        return False
    return True


def collect_real_solution_files(workspace: Path) -> list[str]:
    """真实求解脚本：problem*/代码/*.py 中排除占位 docstring 脚本，相对工作区 posix 路径。"""
    return [p for p in collect_solution_files(workspace) if not _is_placeholder_script(workspace / p)]


def has_real_solutions(workspace: Path, problem_json: dict[str, Any] | None = None) -> bool:
    """每问是否都有真实求解脚本（占位不算）。

    用于决定编程手用完整提示词（从头求解）还是修补提示词（只改已有脚本）：
    只要有一问缺真实脚本，就认为"尚无解"，回到完整流程，避免占位脚本把系统卡在修补模式。
    """
    expected = set(problem_folder_names(problem_json))
    existing = {d.name for d in workspace.glob("problem*") if d.is_dir()}
    targets = sorted(expected | existing) or ["problem1"]
    for name in targets:
        folder = workspace / name / "代码"
        if not folder.is_dir():
            return False
        if not any(not _is_placeholder_script(p) for p in folder.glob("*.py")):
            return False
    return True


def collect_problem_artifacts(workspace: Path) -> dict[str, dict[str, list[str]]]:
    """每问产物索引：problemN -> {"plan":[], "code":[], "figures":[], "results":[]}（相对 posix 路径）。"""
    result: dict[str, dict[str, list[str]]] = {}
    for problem in sorted(workspace.glob("problem*")):
        if not problem.is_dir():
            continue
        name = problem.name
        result[name] = {
            "plan": [p.relative_to(workspace).as_posix() for p in _rel_files(problem / "方案", *_PLAN_EXTS)],
            "code": [p.relative_to(workspace).as_posix() for p in _rel_files(problem / "代码", *_CODE_EXTS)],
            "figures": [p.relative_to(workspace).as_posix() for p in _rel_files(problem / "图表", *_FIG_EXTS)],
            "results": [p.relative_to(workspace).as_posix() for p in _rel_files(problem / "结果", *_RESULT_EXTS)],
        }
    return result


def research_path(workspace: Path) -> str:
    """research/研究资料.md 存在则返回相对路径，否则空串。"""
    path = workspace / RESEARCH_DIR / RESEARCH_FILE
    return f"{RESEARCH_DIR}/{RESEARCH_FILE}" if path.exists() else ""


def references_bib_path(workspace: Path) -> str:
    """research/参考文献.bib 存在则返回相对路径，否则空串（研究手真实检索生成的 BibTeX）。"""
    path = workspace / RESEARCH_DIR / REFERENCES_BIB
    return f"{RESEARCH_DIR}/{REFERENCES_BIB}" if path.exists() else ""
