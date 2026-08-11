from __future__ import annotations

import re
from pathlib import Path

_SKILL_FOLDERS = {
    "docx": "docx",
    "paper_search": "paper_search",
    "paper-search": "paper_search",
    "pdf": "pdf",
    "xlsx": "xlsx",
}
_BASE = Path(__file__).resolve().parent


def load_skill_briefing(skill_name: str, max_chars: int = 1500) -> str:
    """读取某个技能文件夹的 SKILL.md，压缩成一段简报字符串。

    简报含：front-matter description（触发条件）、主要章节标题、scripts/*.py 清单、
    以及 SKILL.md 的绝对路径（agent 可再用 FileReadTool 按需读全文）。
    """
    folder = _SKILL_FOLDERS.get(str(skill_name).strip().lower(), "")
    if not folder:
        return f"（无此技能简报：{skill_name}）"
    skill_md = _BASE / folder / "SKILL.md"
    if not skill_md.is_file():
        return f"（技能 {folder}/SKILL.md 不存在）"
    text = skill_md.read_text(encoding="utf-8", errors="replace")

    description = _frontmatter_description(text) or _first_heading(text)
    headings = _headings(text)
    scripts = _script_list(_BASE / folder / "scripts")

    parts = [f"[技能 {folder}] {description}".strip()]
    if headings:
        parts.append("章节: " + " / ".join(headings[:8]))
    if scripts:
        parts.append("可用脚本: " + ", ".join(scripts))
    parts.append(f"完整说明: {skill_md}（需要时用 FileReadTool 读取）")
    return _truncate("\n".join(parts), max_chars)


def _frontmatter_description(text: str) -> str:
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not match:
        return ""
    for line in match.group(1).splitlines():
        if line.strip().lower().startswith("description:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _headings(text: str, limit: int = 8) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            found.append(stripped.lstrip("#").strip())
        if len(found) >= limit:
            break
    return found


def _script_list(scripts_dir: Path) -> list[str]:
    if not scripts_dir.is_dir():
        return []
    names = sorted(p.name for p in scripts_dir.rglob("*.py") if p.is_file())
    return [f"scripts/{name}" for name in names if not name.startswith("__")]


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…[简报截断，共 {len(text)} 字符]"
