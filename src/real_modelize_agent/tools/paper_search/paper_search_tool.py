from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
_HYBRID_SCRIPT = _SCRIPTS_DIR / "hybrid_scholar.py"
_SORT_CHOICES = {
    "relevance",
    "cited_by_count:desc",
    "cited_by_count:asc",
    "publication_year:desc",
    "publication_year:asc",
}
_FIELD_CHOICES = {
    "mathematics",
    "computer_science",
    "engineering",
    "statistics",
    "operations_research",
    "physics",
    "economics",
}


def paper_search(
    query: str,
    limit: int | str = 8,
    email: str | None = None,
    sort: str = "relevance",
    min_citations: int | str | None = None,
    year_from: int | str | None = None,
    year_to: int | str | None = None,
    field: str | None = None,
    openalex_only: bool | str = False,
    anysearch_only: bool | str = False,
) -> dict[str, Any]:
    """用 OpenAlex + AnySearch 双引擎并行搜索学术论文并交叉验证。

    通过子进程调用 paper_search/scripts/hybrid_scholar.py --json，返回原生结构
    （cross_validated/openalex_only/anysearch_only/stats），另加扁平 results 数组
    （title/url/content/published_date/doi/author），与 WebSearchTool 的返回结构对齐，
    供研究手的 research/研究资料.md 与 参考文献.bib 持久化管道直接复用。
    """
    if not query.strip():
        return {"ok": False, "error": "query is required"}
    if not _HYBRID_SCRIPT.is_file():
        return {"ok": False, "error": f"hybrid_scholar.py not found: {_HYBRID_SCRIPT}"}
    executable = shutil.which("python") or sys.executable
    if executable is None:
        return {"ok": False, "error": "python executable not found"}

    try:
        limit_value = max(1, min(30, int(limit)))
    except (TypeError, ValueError):
        limit_value = 8
    try:
        min_cit = None if min_citations in (None, "") else int(min_citations)
    except (TypeError, ValueError):
        min_cit = None
    try:
        y_from = None if year_from in (None, "") else int(year_from)
    except (TypeError, ValueError):
        y_from = None
    try:
        y_to = None if year_to in (None, "") else int(year_to)
    except (TypeError, ValueError):
        y_to = None
    if sort not in _SORT_CHOICES:
        sort = "relevance"
    if field not in _FIELD_CHOICES:
        field = None
    openalex_only_value = _coerce_bool(openalex_only)
    anysearch_only_value = _coerce_bool(anysearch_only)
    if openalex_only_value and anysearch_only_value:
        return {"ok": False, "error": "openalex_only and anysearch_only are mutually exclusive"}

    args = [
        executable,
        str(_HYBRID_SCRIPT),
        "--query",
        query,
        "--limit",
        str(limit_value),
        "--json",
    ]
    if email:
        args += ["--email", str(email)]
    if sort != "relevance":
        args += ["--sort", sort]
    if min_cit is not None:
        args += ["--min-citations", str(min_cit)]
    if y_from is not None:
        args += ["--year-from", str(y_from)]
    if y_to is not None:
        args += ["--year-to", str(y_to)]
    if field:
        args += ["--field", field]
    if openalex_only_value:
        args.append("--openalex-only")
    if anysearch_only_value:
        args.append("--anysearch-only")

    try:
        completed = subprocess.run(
            args,
            cwd=_SCRIPTS_DIR,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "query": query, "error": "paper search timed out after 90s"}
    except OSError as exc:
        return {"ok": False, "query": query, "error": f"OSError: {exc}"}

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return {
            "ok": False,
            "query": query,
            "error": f"hybrid_scholar exited {completed.returncode}: {detail[-1000:]}",
        }

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "query": query,
            "error": f"invalid JSON from hybrid_scholar: {exc}",
            "stdout_tail": completed.stdout[-1000:],
        }

    if not isinstance(payload, dict):
        return {"ok": False, "query": query, "error": "unexpected hybrid_scholar output shape"}
    payload["query"] = query  # 脚本 _current_query 在 --json 模式下未设置，这里显式回填
    payload["results"] = [_flatten(paper) for paper in _all_papers(payload)]
    payload["ok"] = True
    return payload


def build_paper_search_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="PaperSearchTool",
        func=paper_search,
        description=(
            "Search academic papers with OpenAlex + AnySearch dual engines (cross-validated). "
            "Args: query (required), limit (1-30, default 8), email (OpenAlex polite-pool, optional), "
            "sort (relevance|cited_by_count:desc|asc|publication_year:desc|asc), min_citations, "
            "year_from, year_to, field (mathematics|computer_science|engineering|statistics|"
            "operations_research|physics|economics), openalex_only, anysearch_only. "
            "Returns cross-validated/openalex_only/anysearch_only papers plus a flat `results` array "
            "for the research ledger. No API key required."
        ),
    )


def _all_papers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    for key in ("cross_validated", "openalex_only", "anysearch_only"):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                papers.append(item)
    return papers


def _flatten(paper: dict[str, Any]) -> dict[str, Any]:
    """对齐 WebSearchTool 的扁平结构，供研究手持久化管道复用。"""
    doi = str(paper.get("doi") or "").strip()
    year = paper.get("year")
    authors = paper.get("authors") or []
    return {
        "title": str(paper.get("title") or ""),
        "url": f"https://doi.org/{doi}" if doi else "",
        "content": str(paper.get("abstract") or ""),
        "published_date": str(year) if year else "",
        "author": ", ".join(str(a) for a in authors) if authors else None,
        "doi": doi or None,
        "citations": paper.get("citations"),
        "sources": paper.get("sources") or [],
        "cross_validated": bool(paper.get("cross_validated")),
    }


def _coerce_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "0", "no", "off"}
