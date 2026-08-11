"""Stage contracts for the Planner-led mathematical-modeling workflow.

The verifier in this module deliberately answers one question only: are all
required artifacts present?  Scientific correctness remains the responsibility
of Model/Code/Write agents and must never be inferred from file names here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable


class Stage(StrEnum):
    PREPARE = "prepare"
    ANALYSIS = "analysis"
    CODE = "code"
    WRITING = "writing"
    COMPLETE = "complete"


STAGE_ORDER = (Stage.PREPARE, Stage.ANALYSIS, Stage.CODE, Stage.WRITING)


@dataclass(frozen=True)
class ArtifactRequirement:
    label: str
    candidates: tuple[str, ...] = ()
    glob: str = ""
    min_count: int = 1
    nonempty: bool = True


@dataclass
class StageVerification:
    stage: Stage
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "passed": self.passed,
            "checks": self.checks,
            "missing": self.missing,
            "summary": (
                f"{self.stage.value} stage artifacts complete"
                if self.passed
                else f"{self.stage.value} stage missing: {', '.join(self.missing)}"
            ),
        }


def next_stage(stage: Stage | str) -> Stage:
    current = Stage(stage)
    if current is Stage.COMPLETE:
        return Stage.COMPLETE
    index = STAGE_ORDER.index(current)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else Stage.COMPLETE


def verify_stage(
    workspace: Path,
    stage: Stage | str,
    problem_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check artifact existence/count only; never judge scientific quality."""
    workspace = workspace.resolve()
    selected = Stage(stage)
    problem_names = _problem_names(problem_json)
    requirements = _requirements(selected, problem_names)
    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    for requirement in requirements:
        matches = _matches(workspace, requirement)
        passed = len(matches) >= requirement.min_count
        if passed and requirement.nonempty:
            passed = all(path.is_dir() or path.stat().st_size > 0 for path in matches)
        detail = ", ".join(path.relative_to(workspace).as_posix() for path in matches[:8])
        if not passed:
            missing.append(requirement.label)
            detail = detail or _expected_text(requirement)
        checks.append({"name": requirement.label, "passed": passed, "detail": detail})
    return StageVerification(selected, not missing, checks, missing).to_dict()


def _requirements(stage: Stage, problem_names: list[str]) -> list[ArtifactRequirement]:
    if stage is Stage.PREPARE:
        return [
            ArtifactRequirement("TODO.md", ("TODO.md",)),
            ArtifactRequirement("NOTEPAD.md", ("NOTEPAD.md",)),
            ArtifactRequirement("raw/题目.md", ("raw/题目.md",)),
            ArtifactRequirement("article/main.tex", ("article/main.tex",)),
        ]
    if stage is Stage.ANALYSIS:
        items = [
            ArtifactRequirement("research/研究资料.md", ("research/研究资料.md",)),
            ArtifactRequirement(
                "research/refs.bib",
                ("research/refs.bib", "research/参考文献.bib"),
            ),
            ArtifactRequirement("建模方案.md", ("建模方案.md",)),
            ArtifactRequirement("术语符号表.md", ("术语符号表.md", "术语表格.md")),
            ArtifactRequirement(
                "敏感性分析方案",
                ("problem_sensitivity/敏感性分析方案.md", "problem_sensitivity/敏感性分析方案 .md"),
            ),
        ]
        for name in problem_names:
            items.extend(
                [
                    ArtifactRequirement(f"{name}/方案/方案.md", (f"{name}/方案/方案.md",)),
                    ArtifactRequirement(f"{name}/方案/模型公式.md", (f"{name}/方案/模型公式.md",)),
                ]
            )
        return items
    if stage is Stage.CODE:
        items = [ArtifactRequirement("建模结果.md", ("建模结果.md",))]
        for name in [*problem_names, "problem_sensitivity"]:
            items.extend(
                [
                    ArtifactRequirement(f"{name}/代码/*.py", glob=f"{name}/代码/*.py"),
                    ArtifactRequirement(f"{name}/代码/README.md", (f"{name}/代码/README.md",)),
                    ArtifactRequirement(f"{name}/结果/*evidence.json", glob=f"{name}/结果/*evidence.json"),
                ]
            )
        return items
    if stage is Stage.WRITING:
        items = [
            ArtifactRequirement("article/main.tex", ("article/main.tex",)),
            ArtifactRequirement("article/main.pdf", ("article/main.pdf",)),
            ArtifactRequirement(
                "敏感性分析文稿",
                ("problem_sensitivity/敏感性分析文稿.md", "problem_sensitivity/敏感性分析文稿 .md"),
            ),
        ]
        items.extend(
            ArtifactRequirement(f"{name}/文稿.md", (f"{name}/文稿.md",))
            for name in problem_names
        )
        return items
    return []


def _matches(workspace: Path, requirement: ArtifactRequirement) -> list[Path]:
    if requirement.glob:
        return sorted(path for path in workspace.glob(requirement.glob) if path.is_file())
    return [workspace / candidate for candidate in requirement.candidates if (workspace / candidate).exists()]


def _expected_text(requirement: ArtifactRequirement) -> str:
    if requirement.glob:
        return requirement.glob
    return " or ".join(requirement.candidates)


def _problem_names(problem_json: dict[str, Any] | None) -> list[str]:
    raw = problem_json.get("ques_count", 1) if isinstance(problem_json, dict) else 1
    try:
        count = max(1, min(16, int(raw)))
    except (TypeError, ValueError):
        count = 1
    return [f"problem{index}" for index in range(1, count + 1)]

