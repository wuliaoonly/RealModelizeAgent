"""Temporary work records and parallel draft workers for parent agents.

Sub-agents never write authoritative artifacts.  Every run is isolated under
``tmp/agents/<agent>/<run-id>/`` and the parent agent remains responsible for
reviewing and merging the drafts into the real workspace.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4


def record_agent_work(
    workspace: Path,
    agent: str,
    status: str,
    summary: str,
    details: str = "",
) -> dict[str, Any]:
    """Append an agent progress record below ``tmp`` and update ``current.json``."""
    root = _agent_root(workspace, agent)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "agent": agent,
        "status": status,
        "summary": summary,
        "details": details,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    current = root / "current.json"
    current.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history = root / "history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"ok": True, "path": current.relative_to(workspace).as_posix(), "record": payload}


def dispatch_parallel_drafts(
    workspace: Path,
    agent: str,
    assignments: Mapping[str, str],
    worker: Callable[[str, str], str],
    *,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Run independent draft workers concurrently and persist all outputs in ``tmp``."""
    cleaned = {_safe_name(name): str(task) for name, task in assignments.items() if str(task).strip()}
    if not cleaned:
        return {"ok": False, "error": "no sub-agent assignments supplied", "results": {}}

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    run_root = _agent_root(workspace, agent) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "agent": agent,
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "assignments": cleaned,
    }
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    results: dict[str, dict[str, Any]] = {}
    workers = max(1, min(int(max_workers), len(cleaned)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{agent}-subagent") as pool:
        futures = {pool.submit(worker, name, task): name for name, task in cleaned.items()}
        for future in as_completed(futures):
            name = futures[future]
            child_root = run_root / name
            child_root.mkdir(parents=True, exist_ok=True)
            try:
                content = str(future.result() or "")
                output = child_root / "draft.md"
                output.write_text(content, encoding="utf-8")
                results[name] = {
                    "ok": True,
                    "path": output.relative_to(workspace).as_posix(),
                    "preview": content[:800],
                }
            except Exception as exc:  # a failed child must not cancel sibling work
                error = f"{type(exc).__name__}: {exc}"
                (child_root / "error.txt").write_text(error, encoding="utf-8")
                results[name] = {"ok": False, "error": error}

    manifest.update(
        {
            "status": "complete" if all(item["ok"] for item in results.values()) else "partial",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": all(item["ok"] for item in results.values()),
        "run_id": run_id,
        "manifest": manifest_path.relative_to(workspace).as_posix(),
        "results": results,
    }


def read_workspace_context(
    workspace: Path,
    relative_paths: list[str],
    *,
    max_chars: int = 16000,
) -> dict[str, str]:
    """Read a bounded set of text evidence for a child without granting it file tools."""
    root = workspace.resolve()
    remaining = max(0, int(max_chars))
    context: dict[str, str] = {}
    for relative in relative_paths:
        if remaining <= 0:
            break
        candidate = (root / str(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")[:remaining]
        except OSError:
            continue
        context[candidate.relative_to(root).as_posix()] = text
        remaining -= len(text)
    return context


def _agent_root(workspace: Path, agent: str) -> Path:
    return workspace.resolve() / "tmp" / "agents" / _safe_name(agent)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip("-.")
    return cleaned or "unnamed"
