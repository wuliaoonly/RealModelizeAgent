from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from rich.pretty import Pretty
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header, Input, Static

from real_modelize_agent.cli.event_summary import EventSummary, shorten, summarize_event
from real_modelize_agent.core.agent import stream_agent_events

StreamFactory = Callable[..., Iterable[dict[str, Any]]]


class AgentEventMessage(Message):
    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self.event = event


class RunFinishedMessage(Message):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status


class RealModelizeTuiApp(App[None]):
    """基础 textual App：Header + 事件滚动列表 + 输入框。审批走 CLI 提示。"""

    CSS = """
    Screen {
        background: #101113;
        color: #d7d1c9;
    }

    #root {
        height: 1fr;
    }

    #top {
        height: 3;
        border-bottom: solid #2f3437;
        padding: 0 2;
        background: #151719;
        content-align: left middle;
    }

    #title {
        text-style: bold;
        color: #f3ede3;
    }

    #status {
        color: #9aa4a6;
        padding-left: 2;
    }

    #events {
        height: 1fr;
        padding: 1 1;
        background: #101113;
    }

    #input-row {
        height: 3;
        border: round #4a8f86;
        padding: 0 1;
        background: #151719;
    }

    #prompt {
        width: 3;
        height: 1;
        content-align: center middle;
        color: #7fd6c2;
        text-style: bold;
    }

    #task-input {
        width: 1fr;
        height: 1;
        border: none;
        background: #151719;
        color: #f3ede3;
    }

    #hint {
        color: #8a9294;
        width: 42;
        height: 1;
        padding-left: 1;
        content-align: right middle;
    }

    .event-line {
        height: auto;
        min-height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
        border-left: solid #3f474b;
    }

    .event-summary {
        height: auto;
        min-height: 1;
    }

    .event-running {
        border-left: solid #f4bf75;
    }

    .event-success {
        border-left: solid #7fd68a;
    }

    .event-error {
        border-left: solid #ef6f6c;
    }

    .event-info {
        border-left: solid #7fd6c2;
    }

    .event-user {
        border-left: solid #f4bf75;
        background: #222426;
    }

    .detail {
        height: auto;
        max-height: 10;
        color: #b7b0a8;
        padding: 0 1 1 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "cancel_or_quit", "Cancel/Quit"),
        ("ctrl+l", "clear_events", "Clear"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        initial_task: str | None = None,
        workspace: Path | None = None,
        max_attempts: int = 3,
        approval_mode: Literal["inline", "auto", "deny"] = "inline",
        checkpoint_mode: Literal["light", "strict", "off"] = "light",
        trace_mode: Literal["on", "off"] = "on",
        resume: Path | None = None,
        stream_factory: StreamFactory = stream_agent_events,
    ) -> None:
        super().__init__()
        self.initial_task = initial_task
        self.workspace = resume or workspace
        self.max_attempts = max_attempts
        self.approval_mode = approval_mode
        self.checkpoint_mode = checkpoint_mode
        self.trace_mode = trace_mode
        self.resume = resume
        self.stream_factory = stream_factory
        self.running = False
        self.run_count = 0
        self.latest_workspace = str(self.workspace or "")
        self.last_error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="root"):
            with Horizontal(id="top"):
                yield Static("RealModelizeAgent", id="title")
                yield Static("ready", id="status")
            with VerticalScroll(id="events"):
                pass
            with Horizontal(id="input-row"):
                yield Static("❯", id="prompt")
                yield Input(placeholder="输入数学建模题目，回车开始（Ctrl+Q 退出）", id="task-input")
                yield Static("Enter send · Ctrl+L clear", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self._write_welcome()
        self.query_one("#task-input", Input).focus()
        if self.initial_task:
            self.call_after_refresh(self.start_task, self.initial_task)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "task-input":
            return
        task = event.value.strip()
        if not task or self.running:
            return
        event.input.value = ""
        self.start_task(task)

    def on_agent_event_message(self, message: AgentEventMessage) -> None:
        self._handle_event(message.event)

    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self.running = False
        self.query_one("#task-input", Input).disabled = False
        self.query_one("#task-input", Input).focus()
        self.query_one("#status", Static).update(f"{message.status}; ready for next task")
        self._mount_event_card(
            "Run Finished",
            message.status,
            category="success" if message.status == "finished" else "error",
        )

    def action_cancel_or_quit(self) -> None:
        if self.running:
            self.notify("任务进行中。按 Ctrl+Q 退出，checkpoint 会保存进度。", severity="warning")
            return
        self.exit()

    def action_clear_events(self) -> None:
        self.query_one("#events", VerticalScroll).remove_children()
        self._write_welcome()

    def start_task(self, task: str) -> None:
        if self.running:
            self.notify("RealModelizeAgent 正在运行任务。", severity="warning")
            return
        self.running = True
        self.run_count += 1
        self.last_error = ""
        self.query_one("#task-input", Input).disabled = True
        self.query_one("#status", Static).update("running")
        self._mount_event_card(
            f"You · task {self.run_count}",
            shorten(task, 600),
            category="user",
        )
        self.run_worker(
            lambda: self._run_stream(task),
            thread=True,
            exclusive=False,
            name=f"rma-run-{self.run_count}",
        )

    def _run_stream(self, task: str) -> None:
        status = "finished"
        try:
            approval_handler = None  # 审批走 CLI 提示（inline 时由 bash_tool 阻塞回调处理）
            for event in self.stream_factory(
                task,
                workspace=self.workspace,
                max_attempts=self.max_attempts,
                approval_mode=self.approval_mode,
                approval_handler=approval_handler,
                checkpoint_mode=self.checkpoint_mode,
                resume_workspace=self.resume,
                trace_mode=self.trace_mode,
            ):
                self.call_from_thread(self.post_message, AgentEventMessage(event))
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception as exc:
            status = "failed"
            self.last_error = f"{type(exc).__name__}: {exc}"
            error_event = {
                "type": "custom_event",
                "event": {"type": "tui_error", "error": self.last_error},
            }
            self.call_from_thread(self.post_message, AgentEventMessage(error_event))
        finally:
            self.call_from_thread(self.post_message, RunFinishedMessage(status))

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "workspace":
            self.latest_workspace = str(event.get("path", ""))
            return
        summary = summarize_event(event)
        if self._should_hide_event(event):
            return
        self._write_summary(summary)

    def _write_welcome(self) -> None:
        self._mount_event_card(
            "RealModelizeAgent",
            "输入数学建模题目，回车开始。依次运行：建模手 → 编程手 → 论文手 → verifier。",
            category="info",
        )

    def _write_summary(self, summary: EventSummary) -> None:
        self._mount_event_card(summary.title, shorten(summary.body, 900), category=summary.category)

    def _mount_event_card(self, title: str, body: str, *, category: str = "info") -> None:
        events = self.query_one("#events", VerticalScroll)
        card = Vertical(
            Static(Text(title, style=f"bold {self._category_style(category)}")),
            Static(Text(body or " ", style=self._category_style(category)), classes="event-summary"),
            classes=f"event-line {self._category_class(category)}",
        )
        card.styles.height = "auto"
        events.mount(card)
        events.scroll_end(animate=False)

    def _should_hide_event(self, event: dict[str, Any]) -> bool:
        payload = event.get("event")
        if event.get("type") == "custom_event" and isinstance(payload, dict):
            return payload.get("type") in {"memory_snapshot", "checkpoint_saved", "trace_summary"}
        return False

    def _detail_renderable(self, detail: str) -> Any:
        text = detail or "(no details)"
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return Text(text[:1600])
        return Pretty(parsed, max_depth=3)

    def _category_style(self, category: str) -> str:
        return {
            "running": "#f4bf75",
            "success": "#7fd68a",
            "error": "#ef6f6c",
            "info": "#7fd6c2",
            "user": "#f3ede3",
        }.get(category, "#d7d1c9")

    def _category_class(self, category: str) -> str:
        return {
            "running": "event-running",
            "success": "event-success",
            "error": "event-error",
            "info": "event-info",
            "user": "event-user",
        }.get(category, "event-info")
