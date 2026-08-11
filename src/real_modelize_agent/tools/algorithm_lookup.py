from __future__ import annotations

import os
from pathlib import Path

_ASSETS_DIRNAME = "assets"
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


def resolve_assets_dir() -> Path | None:
    """算法资料库目录：env RMA_ALGORITHM_ASSETS_DIR 优先（相对 cwd），否则 repo 根下 assets/。"""
    env_dir = os.getenv("RMA_ALGORITHM_ASSETS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.is_dir():
            return candidate
    repo_assets = Path(__file__).resolve().parents[3] / _ASSETS_DIRNAME
    return repo_assets if repo_assets.is_dir() else None


def _read_text(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def _index_documents(assets_dir: Path) -> list[dict[str, object]]:
    """扫描 assets 下 0X-*.md，返回 [{file, title, headings:[算法名]}]。

    章节即二级标题 `## N. 算法名`（跳过多余的 `## 概述`）。
    """
    docs: list[dict[str, object]] = []
    for path in sorted(assets_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        headings: list[str] = []
        for line in _read_text(path).splitlines():
            if line.startswith("## ") and not line.startswith("### "):
                heading = line[3:].strip()
                if heading and heading != "概述":
                    headings.append(heading)
        docs.append({"file": path.name, "title": path.stem, "headings": headings})
    return docs


def load_algorithm_briefing(max_chars: int = 1600) -> str:
    """读取 repo 根 assets/ 算法库索引 + README「按题型查找」表，生成一段建模手简报。

    简报只做指针：告诉建模手算法库在哪、每个文件涵盖哪些算法、怎么用 FileReadTool/GrepTool
    读取（项目根现已可读，无需专用查询工具）。
    """
    assets_dir = resolve_assets_dir()
    if assets_dir is None:
        return "（算法资料库缺失：未找到 assets/ 目录，可设置 RMA_ALGORITHM_ASSETS_DIR）"
    docs = _index_documents(assets_dir)
    if not docs:
        return "（算法资料库为空：assets/ 下没有 0X-*.md 文档）"

    parts = [
        "[算法资料库] 项目根 `assets/` 下有 7 类算法说明文档（每类含数学原理/公式、适用范围表、可视化建议、关键文献、代码要点）。"
        "项目根已可读：选算法前先用 FileReadTool 读对应 0X-*.md（大文件可用 offset/limit 分段），或 GrepTool 按算法名/关键词搜章节定位。"
    ]
    for doc in docs:
        names = [str(heading) for heading in doc["headings"]][:6]
        cover = "、".join(names)
        if len(doc["headings"]) > 6:
            cover += f" 等{len(doc['headings'])}个算法"
        parts.append(f"{doc['file']}: {cover}")
    quick = _readme_quick_index(assets_dir / "README.md")
    if quick:
        parts.append(quick)
    return _truncate("\n".join(parts), max_chars)


def _readme_quick_index(readme: Path) -> str:
    """从 README.md 提取「按题型查找」表格（题型→推荐算法→文档），压缩成一行串。"""
    if not readme.is_file():
        return ""
    lines = _read_text(readme).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "### 按题型查找"), None)
    if start is None:
        return ""
    rows: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            break
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 3 and not cells[0].startswith("题型") and not set(cells[0]) <= {"-", ":", " "}:
            rows.append(f"{cells[0]}→{cells[1]}({cells[2]})")
        if len(rows) >= 8:
            break
    return "题型速查: " + " | ".join(rows) if rows else ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…[简报截断，共 {len(text)} 字符]"
