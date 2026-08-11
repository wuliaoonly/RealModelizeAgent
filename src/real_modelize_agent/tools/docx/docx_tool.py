from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

_DOCX_MAGIC = b"PK\x03\x04"


def read_docx(path: str) -> dict[str, Any]:
    """只读 Word 文档（.docx）文本。优先 pandoc（保留修订/注释），缺失时回退 python-docx。
    返回提取出的 Markdown/纯文本，供论文手检查 Word 交付内容。
    """
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    if target.suffix.lower() != ".docx":
        return {"ok": False, "error": f"not a .docx file: {target.suffix or '(none)'}"}

    text = _pandoc_to_markdown(target)
    if text is None:
        text = _python_docx_text(target)
    if text is None:
        return {
            "ok": False,
            "path": str(target),
            "error": (
                "docx text extraction unavailable: need `pandoc` (system binary) or "
                "`python-docx` (pip install python-docx)."
            ),
        }

    text = text.strip()
    return {"ok": True, "path": str(target), "chars": len(text), "text": text}


def convert_to_docx(
    input_path: str,
    output_path: str,
    template: str | None = None,
) -> dict[str, Any]:
    """用 pandoc 把 Markdown（或 LaTeX/HTML 等）转换为 .docx（Word 交付）。

    template 可选：参考 docx 模板控制样式。用于把论文摘要/素材/调研报告转成 Word 版。
    """
    executable = shutil.which("pandoc")
    if executable is None:
        return {"ok": False, "error": "pandoc is not installed (required for docx conversion)"}
    source = Path(input_path)
    if not source.is_file():
        return {"ok": False, "error": f"input file not found: {input_path}"}
    output = Path(output_path)
    if output.suffix.lower() != ".docx":
        return {"ok": False, "error": f"output must be a .docx path: {output_path}"}

    args = [executable, str(source)]
    if template:
        template_path = Path(template)
        if not template_path.is_file():
            return {"ok": False, "error": f"template not found: {template}"}
        args += ["--reference-doc", str(template_path)]
    args += ["-o", str(output)]

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot create output dir: {exc}"}

    try:
        completed = subprocess.run(args, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pandoc conversion timed out after 120s"}
    except OSError as exc:
        return {"ok": False, "error": f"OSError: {exc}"}

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return {"ok": False, "error": f"pandoc exited {completed.returncode}: {detail[-1000:]}"}

    if not output.is_file() or output.read_bytes()[:4] != _DOCX_MAGIC:
        return {"ok": False, "error": "pandoc did not produce a valid .docx file"}
    return {
        "ok": True,
        "input": str(source),
        "output": str(output),
        "bytes": output.stat().st_size,
    }


def build_docx_read_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="DocxReadTool",
        func=read_docx,
        description=(
            "Read-only text extraction from a .docx Word document (markdown-ish, keeps tracked "
            "changes). Args: path (required). Use to inspect/verify Word deliverables."
        ),
    )


def build_docx_convert_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="DocxConvertTool",
        func=convert_to_docx,
        description=(
            "Convert a Markdown/LaTeX/HTML file to a .docx Word document via pandoc. Args: "
            "input_path (required), output_path (required, .docx), optional template (reference docx)."
        ),
    )


def _pandoc_to_markdown(target: Path) -> str | None:
    executable = shutil.which("pandoc")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--track-changes=all", str(target), "-t", "markdown"],
            capture_output=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return completed.stdout.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return completed.stdout.decode("utf-8", errors="replace")


def _python_docx_text(target: Path) -> str | None:
    try:
        import docx  # python-docx
    except ImportError:
        return None
    try:
        document = docx.Document(str(target))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    except Exception:
        return None
