from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from real_modelize_agent.core.approval import ApprovalDecision, ApprovalRequest, normalize_approval_mode
from real_modelize_agent.core.checkpoint import normalize_checkpoint_mode
from real_modelize_agent.core.paths import default_read_roots
from real_modelize_agent.core.trace import normalize_trace_mode


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    mtime_ns: int
    complete: bool


@dataclass
class RuntimeState:
    workspace: Path
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)
    read_roots: list[Path] = field(default_factory=list)
    approval_mode: str = "inline"
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None
    checkpoint_mode: str = "light"
    resume_from: Path | None = None
    trace_mode: str = "on"
    trace_id: str | None = None

    def __post_init__(self) -> None:
        self.approval_mode = normalize_approval_mode(self.approval_mode)
        self.checkpoint_mode = normalize_checkpoint_mode(self.checkpoint_mode)
        self.trace_mode = normalize_trace_mode(self.trace_mode)

    def _effective_read_roots(self) -> list[Path]:
        """读根 = workspace 恒可读 + 显式 read_roots（若给定）；否则默认项目根。"""
        roots = [self.workspace]
        if self.read_roots:
            roots.extend(self.read_roots)
        else:
            roots.extend(default_read_roots(self.workspace))
        seen: set[Path] = set()
        result: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
        return result

    def record_read(self, path: Path, *, complete: bool) -> None:
        stat = path.stat()
        resolved = path.resolve()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            complete=complete,
        )

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        return self.read_files.get(path.resolve())

    def assert_workspace_path(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError(f"path must stay inside workspace: {workspace}")
        return resolved

    def assert_readable_path(self, path: Path) -> Path:
        """读路径校验：workspace 或任意读根（默认项目根，含 assets/）之内才允许。

        只约束"读"；写路径仍走 assert_workspace_path，仅限 workspace。
        """
        resolved = path.resolve()
        for root in self._effective_read_roots():
            if resolved == root or root in resolved.parents:
                return resolved
        raise ValueError(
            f"path not inside any readable root (workspace or project root): {resolved}"
        )
