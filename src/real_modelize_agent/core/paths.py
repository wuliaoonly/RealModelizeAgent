from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4


def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest project root marker from ``start`` upward."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return current


def default_read_roots(workspace: Path) -> list[Path]:
    """项目读取根：从 workspace 向上找最近的含 pyproject.toml/.git 的项目根。

    默认布局下 workspace 位于 `<项目根>/.real-modelize/workspaces/` 内，读根即项目根，
    覆盖 assets/（算法资料库）、src/ 与 workspace 本身；外部 workspace 找不到项目根时
    退化为仅 workspace（读范围=写范围，最保守）。
    """
    project = find_project_root(start=workspace)
    if project.is_dir():
        return [project]
    return [workspace]


def default_workspace(root: Path | None = None) -> Path:
    return new_task_workspace(root)


def default_workspace_root(root: Path | None = None) -> Path:
    return (root or find_project_root()) / ".real-modelize" / "workspaces"


def new_task_workspace(root: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid4().hex[:6]
    return default_workspace_root(root) / f"workspace-{stamp}-{suffix}"
