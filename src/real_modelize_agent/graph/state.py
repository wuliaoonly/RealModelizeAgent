from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from real_modelize_agent.core.state import RuntimeState


class TodoItem(TypedDict):
    id: str
    content: str
    status: str
    note: str


class VerificationResult(TypedDict):
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str


class SourceItem(TypedDict, total=False):
    title: str
    url: str
    content: str
    score: float


class AgentHandoff(TypedDict, total=False):
    from_agent: str
    to_agent: str
    instruction: str
    result: str


class VerificationCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class CompressionEvent(TypedDict, total=False):
    before_tokens: int
    after_tokens: int
    removed_messages: int
    summary: str
    next_node: str


class LayeredMemory(TypedDict, total=False):
    rules: dict[str, Any]
    working_memory: dict[str, Any]
    history_summary_store: dict[str, Any]


class RealModelizeGraphState(TypedDict, total=False):
    # 任务与运行时
    task: str
    runtime: RuntimeState
    messages: Annotated[list[BaseMessage], add_messages]
    # coordinator 判定
    problem_detected: bool
    problem_json: dict[str, Any]
    coordinator_reason: str
    # 计划
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    # 验证
    verification_results: list[VerificationResult]
    verification_checks: list[VerificationCheck]
    verifier_summary: str
    passed: bool
    attempts: int
    max_attempts: int
    last_error: str
    final_answer: str
    # 建模手产物
    modeler_summary: str
    modeler_plan: dict[str, Any] | None
    modeler_plan_path: str
    # 编程手产物
    coder_summary: str
    figures: list[str]
    results_summary: str
    # 每问独立文件夹产物索引：problemN -> {"plan":[], "code":[], "figures":[], "results":[]}
    problem_artifacts: dict[str, dict[str, list[str]]]
    # 论文手产物
    writer_summary: str
    paper_path: str
    paper_compile_ok: bool
    # 研究手
    research_notes: str
    research_path: str
    references_bib: str
    sources: list[SourceItem]
    agent_handoffs: list[AgentHandoff]
    # 上下文工程
    context_summary: str
    context_token_count: int
    context_token_limit: int
    context_should_compress: bool
    context_next_node: str
    compression_events: list[CompressionEvent]
    memory_snapshot: LayeredMemory
    history_summary: str
    # 会话
    session_id: str
    session_turn: int
    session_context: str
    # 杂项
    metadata: dict[str, Any]
