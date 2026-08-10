from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


HEADING_RE = re.compile(r"\\(?P<level>section|subsection|subsubsection)\*?\{(?P<title>[^{}]+)\}")
LEVEL = {"section": 1, "subsection": 2, "subsubsection": 3}


def edit_paper_paragraph(
    workspace: Path,
    section: str,
    replacement: str,
    paragraph_index: int | None = None,
    anchor: str | None = None,
    tex_name: str = "论文.tex",
) -> dict[str, Any]:
    """Replace exactly one prose paragraph inside a named LaTeX section.

    A paragraph can be selected by 1-based index or by a unique anchor. Requiring a
    unique target prevents a broad model edit from accidentally rewriting the paper.
    """
    if Path(tex_name).name != tex_name or tex_name != "论文.tex":
        return {"ok": False, "error": "only the workspace root 论文.tex may be edited"}
    path = workspace / tex_name
    if not path.exists():
        return {"ok": False, "error": "论文.tex not found"}
    if not section.strip() or not replacement.strip():
        return {"ok": False, "error": "section and replacement are required"}
    if paragraph_index is None and not (anchor or "").strip():
        return {"ok": False, "error": "paragraph_index or anchor is required"}

    text = path.read_text(encoding="utf-8", errors="strict")
    headings = list(HEADING_RE.finditer(text))
    matches = [item for item in headings if section.strip() == item.group("title").strip()]
    if not matches:
        matches = [item for item in headings if section.strip() in item.group("title").strip()]
    if len(matches) != 1:
        return {"ok": False, "error": f"section selector matched {len(matches)} headings"}

    heading = matches[0]
    level = LEVEL[heading.group("level")]
    end = len(text)
    for candidate in headings:
        if candidate.start() > heading.start() and LEVEL[candidate.group("level")] <= level:
            end = candidate.start()
            break
    body_start = heading.end()
    body = text[body_start:end]
    paragraphs = _paragraph_spans(body)
    if not paragraphs:
        return {"ok": False, "error": "target section has no editable prose paragraphs"}

    selected: list[tuple[int, int, str]]
    if (anchor or "").strip():
        needle = str(anchor).strip()
        selected = [item for item in paragraphs if needle in item[2]]
        if len(selected) != 1:
            return {"ok": False, "error": f"anchor matched {len(selected)} paragraphs"}
    else:
        index = int(paragraph_index or 0)
        if index < 1 or index > len(paragraphs):
            return {"ok": False, "error": f"paragraph_index must be 1..{len(paragraphs)}"}
        selected = [paragraphs[index - 1]]

    start, stop, old = selected[0]
    new_paragraph = replacement.strip()
    updated_body = body[:start] + new_paragraph + body[stop:]
    updated = text[:body_start] + updated_body + text[end:]
    path.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "path": tex_name,
        "section": heading.group("title"),
        "paragraph_index": paragraphs.index(selected[0]) + 1,
        "old_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
        "new_sha256": hashlib.sha256(new_paragraph.encode("utf-8")).hexdigest(),
        "old_preview": old.strip()[:240],
        "new_preview": new_paragraph[:240],
        "compile_invalidated": True,
    }


def _paragraph_spans(body: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?s)(?:(?<=\n\n)|\A)(.*?)(?=(?:\n[ \t]*\n)|\Z)", body):
        value = match.group(1)
        stripped = value.strip()
        if not stripped or _command_only(stripped):
            continue
        leading = len(value) - len(value.lstrip())
        trailing = len(value.rstrip())
        spans.append((match.start(1) + leading, match.start(1) + trailing, stripped))
    return spans


def _command_only(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith(("\\begin", "\\end", "\\label", "\\addcontentsline")) for line in lines)
