from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from real_modelize_agent.agents.artifacts import (
    REFERENCES_BIB,
    RESEARCH_DIR,
    RESEARCH_FILE,
    references_bib_path,
    research_path,
)
from real_modelize_agent.graph.state import RealModelizeGraphState
from real_modelize_agent.prompts.multiAgent import SEARCH_AGENT_PROMPT
from real_modelize_agent.providers.openai_provider import create_model
from real_modelize_agent.tools.web_search_tool import build_web_search_tool

Writer = Callable[[dict[str, Any]], None]


def run_research_agent(
    state: RealModelizeGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 6,
) -> dict[str, Any]:
    """研究手：真实 TAVILY 联网检索，把每次真实结果追加写入 research/研究资料.md，
    并为每条真实来源在 research/参考文献.bib 生成 BibTeX 条目（键名 rN，供写作手 \\cite 引用）。

    **禁止编造**：只有真实返回的 answer 与 results（title/url/content/published_date）才会被持久化；
    bib 条目由**运行时**从真实返回字段生成（title/url/year 取自 published_date，作者字段省略不编造）；
    检索失败或空结果时只写"（本次无检索结果）"说明，绝不虚构内容、数字或文献。
    """
    load_dotenv()
    writer = writer or (lambda _: None)
    if not os.getenv("TAVILY_API_KEY"):
        note = "researchAgent skipped: TAVILY_API_KEY not configured."
        writer({"type": "search_summary", "summary": note, "queries": [], "sources": []})
        return {
            "ok": True,
            "summary": note,
            "queries": [],
            "sources": [],
            "skipped": True,
            "research_path": "",
            "references_bib": "",
        }

    model = create_model()
    search_agent = model.bind_tools([build_web_search_tool()])
    messages = [
        SystemMessage(content=SEARCH_AGENT_PROMPT),
        HumanMessage(
            content=(
                f"题目:\n{state['task']}\n\n"
                f"planner 指令:\n{instruction}\n\n"
                f"已有研究笔记:\n{state.get('research_notes', '')}\n\n"
                "检索需要的背景资料与参考文献。每次检索结果会被自动追加到 research/研究资料.md。"
            )
        ),
    ]

    produced_messages: list[Any] = []
    queries: list[str] = []
    sources: list[dict[str, Any]] = []
    answers: list[str] = []
    research_dir = state["runtime"].workspace / RESEARCH_DIR
    research_dir.mkdir(parents=True, exist_ok=True)
    file_path = research_dir / RESEARCH_FILE

    for _ in range(max_loops):
        response = search_agent.invoke(messages)
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            args = call.get("args") or {}
            query = str(args.get("query", ""))
            if query:
                queries.append(query)
            writer({"type": "tool_call", "node": "researchAgent", "name": call.get("name"), "args": args})
            tool_result = _execute_search_tool(call)
            event = _tool_result_event(tool_result)
            writer(event)
            parsed = _parse_tool_content(tool_result.content)
            if isinstance(parsed, dict):
                ok = parsed.get("ok")
                if ok:
                    if parsed.get("answer"):
                        answers.append(str(parsed["answer"]))
                    for item in parsed.get("results", []) or []:
                        if isinstance(item, dict):
                            sources.append(item)
                    writer(
                        {
                            "type": "search_results",
                            "query": parsed.get("query", query),
                            "answer": parsed.get("answer", ""),
                            "sources": parsed.get("results", []),
                        }
                    )
                # 把真实结果追加到 research/研究资料.md（失败/空 → 仅无结果说明，不编造）
                _append_research_round(state, file_path, query, parsed)
            produced_messages.append(tool_result)
            messages.append(tool_result)

    summary = _last_ai_content(produced_messages) or "\n".join(answers)
    result = {
        "ok": True,
        "summary": summary,
        "queries": queries,
        "sources": _dedupe_sources(sources),
        "messages": produced_messages,
        "research_path": research_path(state["runtime"].workspace),
        "references_bib": references_bib_path(state["runtime"].workspace),
    }
    writer(
        {
            "type": "search_summary",
            "summary": result["summary"],
            "queries": result["queries"],
            "sources": result["sources"],
        }
    )
    writer(
        {
            "type": "research_file",
            "path": result["research_path"],
            "references_bib": result["references_bib"],
            "sources": len(result["sources"]),
            "queries": result["queries"],
        }
    )
    return result


def _append_research_round(state: RealModelizeGraphState, file_path, query: str, parsed: dict[str, Any]) -> None:
    """把一次检索的**真实**结果追加写入研究资料；失败/空只记无结果说明。

    每条真实来源同时由运行时生成一条 BibTeX 条目追加到 research/参考文献.bib
    （键名 rN，只含真实字段：title/url/year 取自 published_date，作者省略不编造），
    并在研究资料对应来源行标注 `〔\\cite{rN}〕`，供写作手引用时一一对应。
    """
    lines = [f"\n## 检索：{query}\n"]
    if parsed.get("ok"):
        answer = str(parsed.get("answer") or "").strip()
        if answer:
            lines.append(f"- **摘要**：{answer}")
        results = parsed.get("results") or []
        if results:
            bib_path = file_path.parent / REFERENCES_BIB
            url_to_key, max_index = _read_bib_index(bib_path)
            lines.append("来源（每条真实来源已同步生成 BibTeX 到参考文献.bib，键名 rN）：")
            for item in results:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                url = str(item.get("url", "")).strip()
                content = str(item.get("content", "")).strip()
                key = url_to_key.get(url, "")
                if url and not key:
                    max_index += 1
                    key = f"r{max_index}"
                    _append_bib_entry(bib_path, key, item)
                    url_to_key[url] = key
                if url and key:
                    _upsert_source_ledger(file_path.parent / "来源台账.json", key, item)
                cite_suffix = f"  〔\\cite{{{key}}}〕" if key else ""
                lines.append(
                    f"  - **{title or url}**{url and f'（{url}）' or ''}{cite_suffix}"
                )
                if content:
                    lines.append(f"    {content[:400]}")
        else:
            lines.append("- （本次无检索结果，未编造内容）")
    else:
        lines.append(f"- （检索失败：{parsed.get('error', 'unknown')}，本次无内容入库）")
    try:
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        state["runtime"].record_read(file_path, complete=True)
    except OSError:
        pass


def _read_bib_index(bib_path) -> tuple[dict[str, str], int]:
    """扫描已有 参考文献.bib，返回 {url: key} 映射与当前最大键序号（r1..rN）。

    幂等：同一 url 不重复建条目；键号在文件追加间稳定（--resume 后继续增长）。
    """
    url_to_key: dict[str, str] = {}
    max_index = 0
    if bib_path.exists():
        try:
            text = bib_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for block in re.split(r"(?=@misc\{)", text):
            key_match = re.match(r"@misc\{(r\d+),", block)
            url_match = re.search(r"url\s*=\s*\{([^}]*)\}", block)
            if not key_match:
                continue
            max_index = max(max_index, int(key_match.group(1)[1:]))
            if url_match:
                url_to_key[url_match.group(1).strip()] = key_match.group(1)
    return url_to_key, max_index


def _append_bib_entry(bib_path, key: str, item: dict[str, Any]) -> None:
    """把一条**真实**来源写成 BibTeX @misc 条目（只含真实字段，作者省略不编造）。"""
    title = _bib_escape(str(item.get("title", "")).strip() or str(item.get("url", "")))
    url = str(item.get("url", "")).strip()
    year = _extract_year(str(item.get("published_date") or ""))
    entry = [f"\n@misc{{{key},", f"  title = {{{title}}},", f"  url = {{{url}}},"]
    if year:
        entry.append(f"  year = {{{year}}},")
    doi = str(item.get("doi") or "").strip()
    if doi:
        entry.append(f"  doi = {{{_bib_escape(doi)}}},")
    entry.append(f"  urldate = {{{datetime.now(timezone.utc).date().isoformat()}}},")
    entry.append("}")
    try:
        with bib_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(entry))
    except OSError:
        pass


def _upsert_source_ledger(path, key: str, item: dict[str, Any]) -> None:
    """Persist only returned metadata; missing author/DOI stays null, never invented."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "sources": []}
    except (OSError, json.JSONDecodeError):
        payload = {"version": 1, "sources": []}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    record = {
        "key": key,
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "author": item.get("author") or item.get("authors"),
        "year": _extract_year(str(item.get("published_date") or "")) or None,
        "doi": item.get("doi") or None,
        "accessed_at": datetime.now(timezone.utc).isoformat(),
        "claim_ids": [],
    }
    sources = [source for source in sources if source.get("url") != record["url"]]
    sources.append(record)
    path.write_text(json.dumps({"version": 1, "sources": sources}, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_year(published_date: str) -> str:
    match = re.search(r"(\d{4})", published_date)
    return match.group(1) if match else ""


def _bib_escape(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("{", "(").replace("}", ")").replace("%", "\\%")


def _execute_search_tool(call: dict[str, Any]) -> ToolMessage:
    tool = build_web_search_tool()
    name = call.get("name", "")
    args = call.get("args") or {}
    if name != tool.name:
        result = {"ok": False, "error": f"unknown tool: {name}"}
    else:
        try:
            result = tool.invoke(args)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )


def _tool_result_event(tool_message: ToolMessage) -> dict[str, Any]:
    return {"type": "tool_result", "node": "researchAgent", "name": tool_message.name, "result": _parse_tool_content(tool_message.content)}


def _parse_tool_content(content: Any) -> Any:
    try:
        return json.loads(str(content))
    except json.JSONDecodeError:
        return content


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for source in sources:
        url = str(source.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""
