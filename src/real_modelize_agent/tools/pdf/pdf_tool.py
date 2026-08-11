from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool


def read_pdf(
    path: str,
    first_pages: int | str | None = None,
    max_chars: int | str | None = None,
) -> dict[str, Any]:
    """只读 PDF 文本（保留版面）。优先 poppler `pdftotext -layout`，缺失/失败时回退
    pypdf → pdfplumber；都不可用时返回依赖缺失说明。用于检查 论文.pdf、阅读参考文献 PDF。
    """
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    if target.suffix.lower() != ".pdf":
        return {"ok": False, "error": f"not a PDF file: {target.suffix or '(none)'}"}
    try:
        page_limit = max(1, int(first_pages)) if first_pages not in (None, "") else None
    except (TypeError, ValueError):
        page_limit = None
    try:
        char_limit = max(200, int(max_chars)) if max_chars not in (None, "") else None
    except (TypeError, ValueError):
        char_limit = None

    text = _pdftotext(target, page_limit)
    page_count = _pdfinfo_pages(target)
    if text is None:
        text, page_count = _library_extract(target, page_limit)
    if text is None:
        return {
            "ok": False,
            "path": str(target),
            "error": (
                "PDF text extraction unavailable: need `pdftotext` (poppler-utils) or "
                "`pypdf`/`pdfplumber` (pip install pypdf pdfplumber)."
            ),
        }

    text = text.strip()
    if page_count is None:
        page_count = _formfeed_page_count(text)
    chars = len(text)
    if char_limit is not None and chars > char_limit:
        text = text[:char_limit] + f"\n…[截断：{chars} 字符，保留前 {char_limit}]"
        chars = len(text)
    return {"ok": True, "path": str(target), "page_count": page_count, "chars": chars, "text": text}


def build_pdf_read_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="PdfReadTool",
        func=read_pdf,
        description=(
            "Read-only PDF text extraction (layout-preserving). Args: path (required), optional "
            "first_pages (limit to first N pages), optional max_chars (truncate output). "
            "Use to inspect 论文.pdf, read reference/attachment PDFs. Backends: pdftotext → pypdf → pdfplumber."
        ),
    )


def _pdftotext(target: Path, page_limit: int | None) -> str | None:
    executable = shutil.which("pdftotext")
    if executable is None:
        return None
    # 不用 -f/-l 页码参数：部分 PDF/打包的 pdftotext 在这些参数下返回非零（退出码 99）。
    # 改为提取全文后用换页符 \f 切片取前 page_limit 页。
    args = [executable, "-layout", str(target), "-"]  # stdout
    try:
        completed = subprocess.run(args, capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    text = _decode(completed.stdout)
    if page_limit is not None and text:
        pages = text.split("\f")
        text = "\n".join(page for page in pages[:page_limit])
    return text


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _formfeed_page_count(text: str) -> int | None:
    if not text:
        return None
    return text.count("\f") + 1


def _pdfinfo_pages(target: Path) -> int | None:
    executable = shutil.which("pdfinfo")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, str(target)], capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _library_extract(target: Path, page_limit: int | None) -> tuple[str | None, int | None]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        page_count = len(reader.pages)
        chunks = []
        for page in reader.pages[:page_limit] if page_limit else reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return "\n\n".join(chunks), page_count
    except ImportError:
        pass
    except Exception:
        return None, None
    try:
        import pdfplumber

        with pdfplumber.open(target) as pdf:
            page_count = len(pdf.pages)
            pages = pdf.pages[:page_limit] if page_limit else pdf.pages
            return "\n\n".join(page.extract_text() or "" for page in pages), page_count
    except ImportError:
        return None, None
    except Exception:
        return None, None
