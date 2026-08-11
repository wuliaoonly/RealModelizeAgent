from __future__ import annotations

from langchain_core.tools import StructuredTool

from real_modelize_agent.core.state import RuntimeState
from real_modelize_agent.tools.bash_tool import bash_tool_description, run_bash
from real_modelize_agent.tools.file_tools import (
    coder_edit_file,
    coder_write_file,
    edit_file,
    read_file,
    write_file,
)
from real_modelize_agent.tools.grep_tool import grep
from real_modelize_agent.tools.notepad_tool import append_notepad, read_notepad
from real_modelize_agent.tools.latex_tool import compile_latex, latex_compile_status
from real_modelize_agent.core.figure_style import audit_figure_workspace, load_figure_style
from real_modelize_agent.core.validation import validate_workspace
from real_modelize_agent.tools.paper_edit_tool import edit_paper_paragraph
from real_modelize_agent.tools.todo_tool import build_todo_update_tool
from real_modelize_agent.tools.paper_search.web_search_tool import build_web_search_tool
from real_modelize_agent.tools.paper_search.paper_search_tool import build_paper_search_tool
from real_modelize_agent.tools.docx.docx_tool import build_docx_convert_tool, build_docx_read_tool
from real_modelize_agent.tools.pdf.pdf_tool import build_pdf_read_tool
from real_modelize_agent.tools.xlsx.xlsx_tool import build_xlsx_read_tool
from real_modelize_agent.core.stages import verify_stage
from real_modelize_agent.core.work_modes import CodeWorkType


def build_tools(state: RuntimeState) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace or the project root (e.g. assets/ algorithm library). Supports offset and limit. Writes stay workspace-only.",
        ),
        StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: write_file(state, file_path, content),
            description="Create a new file or rewrite an existing file inside the workspace.",
        ),
        StructuredTool.from_function(
            name="FileEditTool",
            func=lambda file_path, old_text, new_text: edit_file(state, file_path, old_text, new_text),
            description="Edit an existing workspace file by replacing one unique old_text snippet.",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search text files by regex pattern under the workspace or project root and return matching lines.",
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        StructuredTool.from_function(
            name="NotepadAppendTool",
            func=lambda heading, content: append_notepad(state, heading, content),
            description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
        ),
    ]


def build_research_tools(state: RuntimeState) -> list[StructuredTool]:
    """研究手：Web 通用检索 + 学术论文双引擎检索（OpenAlex+AnySearch 交叉验证）；只读、无 Shell。"""
    return [
        build_web_search_tool(),
        build_paper_search_tool(),
    ]


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    """Strictly read-only inspection tools; intentionally excludes shell/network."""
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace or the project root (e.g. assets/ algorithm library). Supports offset and limit. Writes stay workspace-only.",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search text files by regex pattern under the workspace or project root and return matching lines.",
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
    ]


def build_verifier_tools(
    state: RuntimeState,
    problem_json: dict | None = None,
    stage: str | None = None,
) -> list[StructuredTool]:
    """Read-only verifier tools plus deterministic validators; no generic shell."""
    tools = build_read_only_tools(state) + [
        StructuredTool.from_function(
            name="WorkspaceValidationTool",
            func=lambda: validate_workspace(state.workspace, problem_json),
            description="Run the authoritative deterministic math-modeling workspace acceptance gate.",
        ),
        StructuredTool.from_function(
            name="LatexStatusTool",
            func=lambda: latex_compile_status(state.workspace),
            description="Read the verified LaTeX compile record and source-freshness status.",
        ),
    ]
    if stage:
        tools.append(
            StructuredTool.from_function(
                name="StageCompletenessTool",
                func=lambda: verify_stage(state.workspace, stage, problem_json),
                description=(
                    "Check only whether every file required by the current stage exists and is non-empty. "
                    "It never evaluates scientific correctness or writing quality."
                ),
            )
        )
    return tools


def build_modeler_tools(state: RuntimeState) -> list[StructuredTool]:
    """建模手：只读 + 写分析/方案文件 + 便签（无 Bash，不跑代码）。"""
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace or the project root (e.g. assets/ algorithm library). Supports offset and limit. Writes stay workspace-only.",
        ),
        StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: write_file(state, file_path, content),
            description="Create a new file or rewrite an existing file inside the workspace.",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search text files by regex pattern under the workspace or project root and return matching lines.",
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        StructuredTool.from_function(
            name="NotepadAppendTool",
            func=lambda heading, content: append_notepad(state, heading, content),
            description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
        ),
        build_xlsx_read_tool(),
    ]


def build_coder_tools(
    state: RuntimeState,
    todos: list[dict[str, str]] | None = None,
    work_type: CodeWorkType | str = CodeWorkType.MODEL,
) -> list[StructuredTool]:
    """Build the least-privilege tool set for model work or figure work.

    Both modes may edit solver/plot scripts and execute them. Model work gets
    spreadsheet inspection, while figure work gets style and image-audit tools.
    """
    selected = CodeWorkType(work_type)
    tools = [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace or the project root (e.g. assets/ algorithm library). Supports offset and limit. Writes stay workspace-only.",
        ),
        StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: coder_write_file(state, file_path, content),
            description=(
                "Create a new file or rewrite an existing file. Write permission is restricted: "
                "only `*/代码/` folders or `tmp/` are allowed (per-question solve scripts, scratch files)."
            ),
        ),
        StructuredTool.from_function(
            name="FileEditTool",
            func=lambda file_path, old_text, new_text: coder_edit_file(state, file_path, old_text, new_text),
            description=(
                "Edit an existing workspace file by replacing one unique old_text snippet. "
                "Write permission is restricted to `*/代码/` folders or `tmp/`."
            ),
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search text files by regex pattern under the workspace or project root and return matching lines.",
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        StructuredTool.from_function(
            name="NotepadAppendTool",
            func=lambda heading, content: append_notepad(state, heading, content),
            description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
        ),
    ]
    if selected is CodeWorkType.MODEL:
        tools.append(build_xlsx_read_tool())
    else:
        tools.extend(
            [
                StructuredTool.from_function(
                    name="FigureStyleReadTool",
                    func=lambda: {"ok": True, "style": load_figure_style(state.workspace)},
                    description="Read the human-approved chart font sizes, CJK font fallbacks, DPI and palette.",
                ),
                StructuredTool.from_function(
                    name="FigureAuditTool",
                    func=lambda: audit_figure_workspace(state.workspace),
                    description="Audit plot scripts and PNG figures for CJK fonts, palette and image quality.",
                ),
            ]
        )
    return tools + [build_todo_update_tool(list(todos or []))]


def build_writer_tools(state: RuntimeState, todos: list[dict[str, str]] | None = None) -> list[StructuredTool]:
    """论文手：文件工具 + 专用 LaTeX 编译；不授予通用 Shell。"""
    tools = [tool for tool in build_tools(state) if tool.name != "BashTool"]
    tools.append(
        StructuredTool.from_function(
            name="CompileLatexTool",
            func=lambda tex_name="论文.tex", engine=None, passes=2: compile_latex(
                state.workspace, tex_name=tex_name, engine=engine, passes=passes
            ),
            description=(
                "Compile the root paper with an allowlisted LaTeX engine without a shell and persist "
                "an auditable status record. Args: tex_name='论文.tex', optional engine, passes=2."
            ),
        )
    )
    tools.append(
        StructuredTool.from_function(
            name="PaperParagraphEditTool",
            func=lambda section, replacement, paragraph_index=None, anchor=None, tex_name="论文.tex": edit_paper_paragraph(
                state.workspace,
                section=section,
                replacement=replacement,
                paragraph_index=paragraph_index,
                anchor=anchor,
                tex_name=tex_name,
            ),
            description=(
                "Safely replace exactly one paragraph in 论文.tex. Select a unique section and either a 1-based "
                "paragraph_index or unique anchor, provide replacement, then compile."
            ),
        )
    )
    tools.append(build_pdf_read_tool())
    tools.append(build_docx_read_tool())
    tools.append(build_docx_convert_tool())
    return tools + [build_todo_update_tool(list(todos or []))]
