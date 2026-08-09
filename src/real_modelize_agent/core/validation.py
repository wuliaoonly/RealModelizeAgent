from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image

from real_modelize_agent.tools.latex_tool import INCLUDEGRAPHICS_RE, latex_compile_status


PLACEHOLDER_RE = re.compile(r"\\underline\{|待填|TODO|\?\?|PLACEHOLDER", re.IGNORECASE)
CITE_RE = re.compile(r"\\cite\{([^}]*)\}")
BIB_KEY_RE = re.compile(r"@[A-Za-z]+\s*\{\s*([^,\s]+)")
REQUIRED_EVIDENCE_KEYS = {
    "schema_version",
    "problem_id",
    "entrypoint",
    "inputs",
    "model",
    "metrics",
    "validation",
    "sensitivity",
    "figures",
    "claims",
    "random_seed",
}
REQUIRED_PAPER_PATTERNS = {
    "摘要": r"摘要",
    "问题重述": r"问题.{0,4}重述|问题的提出和重述",
    "模型假设": r"模型假设",
    "符号说明": r"符号说明",
    "数据处理": r"数据.{0,3}处理",
    "模型评价": r"模型.{0,3}评价",
    "推广应用": r"推广.{0,3}应用",
    "参考文献": r"参考文献|thebibliography",
    "附录": r"附录|appendix",
}


def validate_workspace(workspace: Path, problem_json: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic acceptance gate. LLM checks may add detail but cannot override it."""
    workspace = workspace.resolve()
    checks: list[dict[str, Any]] = []
    expected, schema_detail = _expected_problem_names(problem_json)
    checks.append(_check("题目结构", schema_detail is None, schema_detail or f"ques_count={len(expected)}"))
    checks.append(_validate_modeler_artifacts(workspace))
    for name in expected:
        checks.extend(_validate_problem(workspace, name))
    checks.append(_validate_exact_problem_set(workspace, expected))
    checks.append(_validate_paper(workspace, expected))
    checks.append(_validate_citations(workspace))
    passed = all(check["passed"] for check in checks)
    failed = [check["name"] for check in checks if not check["passed"]]
    return {
        "ok": True,
        "passed": passed,
        "checks": checks,
        "summary": "确定性验收通过" if passed else f"确定性验收失败：{', '.join(failed)}",
    }


def _expected_problem_names(problem_json: dict[str, Any] | None) -> tuple[list[str], str | None]:
    if not isinstance(problem_json, dict):
        return ["problem1"], "problem_json 缺失或不是对象"
    count = problem_json.get("ques_count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 16:
        try:
            fallback_count = max(1, min(16, int(count)))
        except (TypeError, ValueError):
            fallback_count = 1
        return [f"problem{i}" for i in range(1, fallback_count + 1)], "ques_count 必须是 1..16 的整数"
    missing = [f"ques{i}" for i in range(1, count + 1) if not str(problem_json.get(f"ques{i}", "")).strip()]
    if missing:
        return [f"problem{i}" for i in range(1, count + 1)], f"缺少题目字段：{', '.join(missing)}"
    return [f"problem{i}" for i in range(1, count + 1)], None


def _validate_modeler_artifacts(workspace: Path) -> dict[str, Any]:
    analysis = workspace / "题目分析.md"
    plan = workspace / "建模方案.json"
    problems: list[str] = []
    if not analysis.exists() or len(analysis.read_text(encoding="utf-8", errors="replace").strip()) < 300:
        problems.append("题目分析.md 缺失或少于 300 字符")
    try:
        payload = json.loads(plan.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload:
            problems.append("建模方案.json 为空")
    except (OSError, json.JSONDecodeError):
        problems.append("建模方案.json 缺失或 JSON 非法")
    return _check("建模产物", not problems, "; ".join(problems) or "分析与结构化方案有效")


def _validate_problem(workspace: Path, name: str) -> list[dict[str, Any]]:
    root = workspace / name
    plan_files = list((root / "方案").glob("*.md")) if (root / "方案").is_dir() else []
    code_files = list((root / "代码").glob("*.py")) if (root / "代码").is_dir() else []
    result_files = [p for p in (root / "结果").glob("*") if p.is_file() and p.stat().st_size > 0] if (root / "结果").is_dir() else []
    figure_files = [p for p in (root / "图表").rglob("*.png") if p.is_file()] if (root / "图表").is_dir() else []
    entrypoints = [path for path in code_files if _is_independent_entrypoint(path)]
    valid_figures = [path for path in figure_files if _valid_png(path)]
    evidence = root / "结果" / "evidence.json"
    evidence_errors = _validate_evidence(evidence, name, workspace)
    return [
        _check(f"{name}:方案", bool(plan_files) and all(path.stat().st_size >= 200 for path in plan_files), "至少一个非空详细方案"),
        _check(f"{name}:独立代码", bool(entrypoints), "必须有含 main() 与 __main__ 入口的独立求解脚本"),
        _check(f"{name}:结果", bool(result_files), "结果目录必须含非空文件"),
        _check(f"{name}:图表", bool(valid_figures), "至少一张可解码的 PNG"),
        _check(f"{name}:证据链", not evidence_errors, "; ".join(evidence_errors) or "evidence.json 合同有效"),
    ]


def _validate_exact_problem_set(workspace: Path, expected: list[str]) -> dict[str, Any]:
    actual = sorted(path.name for path in workspace.glob("problem*") if path.is_dir())
    return _check("逐问目录集合", actual == sorted(expected), f"expected={expected}; actual={actual}")


def _validate_paper(workspace: Path, expected: list[str]) -> dict[str, Any]:
    status = latex_compile_status(workspace)
    tex = workspace / "论文.tex"
    issues: list[str] = []
    text = tex.read_text(encoding="utf-8", errors="replace") if tex.exists() else ""
    if len(text.strip()) < 1000:
        issues.append("论文.tex 缺失或内容过短")
    if PLACEHOLDER_RE.search(text):
        issues.append("仍含占位符")
    chinese_numbers = "一二三四五六七八九十"
    for index, _ in enumerate(expected, start=1):
        chinese = chinese_numbers[index - 1] if index <= len(chinese_numbers) else str(index)
        if not re.search(rf"问题\s*(?:{index}|{chinese})", text):
            issues.append(f"未识别到问题{index}章节")
    missing_sections = [
        name for name, pattern in REQUIRED_PAPER_PATTERNS.items()
        if not re.search(pattern, text, re.IGNORECASE)
    ]
    if missing_sections:
        issues.append(f"缺少章节：{', '.join(missing_sections)}")
    refs = [ref.strip() for ref in INCLUDEGRAPHICS_RE.findall(text)]
    missing = [ref for ref in refs if not (workspace / ref).is_file()]
    if missing:
        issues.append(f"缺失图片引用：{', '.join(missing[:5])}")
    if not status.get("compile_ok"):
        issues.append(str(status.get("compile_detail") or "论文未通过可信编译"))
    return _check("论文可信交付", not issues, "; ".join(issues) or "结构、引用、编译记录与 PDF 均有效")


def _validate_citations(workspace: Path) -> dict[str, Any]:
    tex = workspace / "论文.tex"
    if not tex.exists():
        return _check("参考文献溯源", False, "论文.tex 不存在")
    text = tex.read_text(encoding="utf-8", errors="replace")
    cited = {key.strip() for group in CITE_RE.findall(text) for key in group.split(",") if key.strip()}
    bib = workspace / "research" / "参考文献.bib"
    bib_text = bib.read_text(encoding="utf-8", errors="replace") if bib.exists() else ""
    available = set(BIB_KEY_RE.findall(bib_text))
    inline = set(re.findall(r"\\bibitem\{([^}]+)\}", text))
    missing = sorted(cited - available - inline)
    return _check("参考文献溯源", not missing, "全部 cite 键可追溯" if not missing else f"缺少真实条目：{', '.join(missing)}")


def _validate_evidence(path: Path, problem_id: str, workspace: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["evidence.json 缺失或非法"]
    missing = sorted(REQUIRED_EVIDENCE_KEYS - set(payload))
    errors = [f"缺少字段：{', '.join(missing)}"] if missing else []
    if payload.get("problem_id") != problem_id:
        errors.append("problem_id 不一致")
    if str(payload.get("schema_version")) != "1.0":
        errors.append("schema_version 必须为 1.0")
    entry = workspace / str(payload.get("entrypoint", ""))
    if not entry.is_file() or not _is_independent_entrypoint(entry):
        errors.append("entrypoint 不存在或不可独立运行")
    validation = payload.get("validation")
    if not isinstance(validation, dict) or not all(
        validation.get(key) for key in ("strategy", "split", "baseline", "leakage_controls", "diagnostics")
    ):
        errors.append("validation 必须声明 strategy/split/baseline/leakage_controls/diagnostics")
    model = payload.get("model")
    if not isinstance(model, dict) or not all(
        model.get(key)
        for key in ("name", "assumptions", "parameters", "units", "dimension_checks", "identifiability", "constraint_checks")
    ):
        errors.append("model 必须声明假设、参数、单位、量纲、可识别性与约束检查")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        errors.append("metrics 不能为空")
    sensitivity = payload.get("sensitivity")
    if not isinstance(sensitivity, dict) or not sensitivity.get("parameters") or not sensitivity.get("range_basis"):
        errors.append("sensitivity 必须声明参数与范围依据")
    figures = payload.get("figures")
    if (
        not isinstance(figures, list)
        or not figures
        or any(
            not isinstance(item, dict)
            or not all(item.get(key) for key in ("path", "category", "claim", "evidence"))
            or not (workspace / str(item.get("path"))).is_file()
            for item in figures
        )
    ):
        errors.append("figures 必须包含带 claim 的 Figure Contract")
    if not isinstance(payload.get("claims"), list) or not payload.get("claims"):
        errors.append("claims 不能为空")
    return errors


def _is_independent_entrypoint(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False
    has_main = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main" for node in tree.body)
    has_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and "__name__" in ast.unparse(node.test)
        and "__main__" in ast.unparse(node.test)
        for node in tree.body
    )
    return has_main and has_guard


def _valid_png(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.width >= 300 and image.height >= 200
    except (OSError, ValueError):
        return False


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}
