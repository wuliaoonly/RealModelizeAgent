from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_FILE = Path(".real-modelize") / "latex_compile.json"
ALLOWED_ENGINES = {"xelatex", "lualatex", "pdflatex"}
FATAL_LOG_RE = re.compile(r"^!", re.MULTILINE)
UNRESOLVED_RE = re.compile(r"undefined references|undefined citations|There were undefined references", re.IGNORECASE)
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}")


def latex_source_fingerprint(workspace: Path, tex_name: str = "论文.tex") -> str:
    """Hash every source that can change the rendered paper."""
    workspace = workspace.resolve()
    candidates: set[Path] = set()
    for pattern in ("*.tex", "*.bib"):
        candidates.update(path for path in workspace.glob(pattern) if path.is_file())
    tex = workspace / tex_name
    for source in list(candidates):
        if source.suffix.lower() != ".tex":
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        for ref in INCLUDEGRAPHICS_RE.findall(text):
            candidate = (workspace / ref.strip()).resolve()
            if candidate == workspace or workspace in candidate.parents:
                if candidate.is_file():
                    candidates.add(candidate)
    digest = hashlib.sha256()
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        rel = path.relative_to(workspace).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compile_latex(
    workspace: Path,
    tex_name: str = "论文.tex",
    engine: str | None = None,
    passes: int = 2,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Compile a paper without a shell and persist an auditable status record."""
    workspace = workspace.resolve()
    selected = (engine or os.getenv("RMA_LATEX_ENGINE", "xelatex") or "xelatex").strip().lower()
    if selected not in ALLOWED_ENGINES:
        return {"ok": False, "error": f"unsupported LaTeX engine: {selected}"}
    executable = shutil.which(selected)
    if executable is None:
        return {"ok": False, "error": f"LaTeX engine not found: {selected}"}
    tex = workspace / tex_name
    if not tex.is_file() or tex.parent.resolve() != workspace:
        return {"ok": False, "error": f"paper source must be a root workspace file: {tex_name}"}
    passes = max(1, min(3, int(passes)))
    pdf = tex.with_suffix(".pdf")
    if pdf.exists():
        pdf.unlink()

    runs: list[dict[str, Any]] = []
    for pass_number in range(1, passes + 1):
        try:
            completed = subprocess.run(
                [executable, "-interaction=nonstopmode", "-halt-on-error", tex.name],
                cwd=workspace,
                shell=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
            stdout = _decode(completed.stdout)
            stderr = _decode(completed.stderr)
            runs.append(
                {
                    "pass": pass_number,
                    "exit_code": completed.returncode,
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": stderr[-4000:],
                }
            )
            if completed.returncode != 0:
                break
        except subprocess.TimeoutExpired as exc:
            runs.append(
                {
                    "pass": pass_number,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout_tail": _decode(exc.stdout)[-4000:],
                    "stderr_tail": _decode(exc.stderr)[-4000:],
                }
            )
            break

    log = tex.with_suffix(".log")
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    fatal_errors = [line.strip() for line in log_text.splitlines() if line.lstrip().startswith("!")][:20]
    unresolved = bool(UNRESOLVED_RE.search(log_text))
    pdf_valid = pdf.is_file() and pdf.stat().st_size >= 100 and pdf.read_bytes()[:5] == b"%PDF-"
    ok = bool(runs) and all(run.get("exit_code") == 0 for run in runs) and not fatal_errors and not unresolved and pdf_valid
    record = {
        "version": 1,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "engine": selected,
        "tex": tex.name,
        "pdf": pdf.name if pdf.exists() else "",
        "source_fingerprint": latex_source_fingerprint(workspace, tex.name),
        "ok": ok,
        "fatal_errors": fatal_errors,
        "unresolved_references": unresolved,
        "pdf_valid": pdf_valid,
        "runs": runs,
    }
    status_path = workspace / STATUS_FILE
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def latex_compile_status(workspace: Path, tex_name: str = "论文.tex") -> dict[str, Any]:
    workspace = workspace.resolve()
    tex = workspace / tex_name
    pdf = tex.with_suffix(".pdf")
    status_path = workspace / STATUS_FILE
    base = {
        "paper_tex": tex.name if tex.exists() else "",
        "paper_pdf": pdf.name if pdf.exists() else "",
        "compile_ok": False,
        "tex_chars": len(tex.read_text(encoding="utf-8", errors="replace")) if tex.exists() else 0,
        "compile_record": STATUS_FILE.as_posix() if status_path.exists() else "",
        "compile_detail": "missing compile record",
    }
    if not tex.exists() or not pdf.exists() or not status_path.exists():
        return base
    try:
        record = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**base, "compile_detail": "invalid compile record"}
    current_fingerprint = latex_source_fingerprint(workspace, tex.name)
    fresh = record.get("source_fingerprint") == current_fingerprint
    pdf_valid = pdf.stat().st_size >= 100 and pdf.read_bytes()[:5] == b"%PDF-"
    compile_ok = bool(record.get("ok") and fresh and pdf_valid)
    detail = "verified compile" if compile_ok else "compile failed, stale, or PDF invalid"
    return {**base, "compile_ok": compile_ok, "compile_detail": detail, "record": record}


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")
