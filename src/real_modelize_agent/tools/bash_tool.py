from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import time
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from real_modelize_agent.core.approval import ApprovalDecision, classify_command_risk, make_approval_request
from real_modelize_agent.core.state import RuntimeState

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_CHARS = 6000
ALLOWED_EXECUTABLES = {
    "python",
    "python.exe",
    "pytest",
    "pytest.exe",
    "xelatex",
    "xelatex.exe",
    "lualatex",
    "lualatex.exe",
    "pdflatex",
    "pdflatex.exe",
}
SHELL_META_RE = re.compile(r"(?:&&|\|\||[|<>`]|\$\(|\r|\n)")

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
    r"\bdel\s+/[sq]\b",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"(?:^|[^0-9])>\s*(?:[A-Za-z]:\\|/(?!dev/null\b))",
]


def bash_tool_description() -> str:
    system = platform.system().lower()
    common = (
        "Run one allowlisted executable without a shell, with timeout and output capture. "
        "Python scripts may be .py entrypoints under the project root (e.g. assets/ scripts); reads may target "
        "project-root files such as assets/, but writes outside the workspace are blocked by the runtime guard, "
        "and writes inside the workspace still follow the coder whitelist (problemN/代码, utils, tmp). "
        "Shell operators, redirection, background jobs, python -c/-m/stdin, package installation, and command "
        "composition are rejected. "
        "Write a reviewable .py entrypoint under problemN/代码, utils, or tmp and run that file directly."
    )
    if system == "windows":
        return (
            common
            + " Current platform: Windows. Commands are executed by cmd.exe, not bash or PowerShell. "
            "Use Windows cmd syntax: dir for listing, type file.txt for printing a file, copy/move/del for simple file operations, "
            "&& for chaining, and set VAR=value for environment variables. Do not use POSIX-only tools like tail, grep, sed, awk, "
            "cat, ls, export, or here-documents unless you implement the behavior with python -c."
        )
    if system == "darwin":
        return (
            common
            + " Current platform: macOS. Commands are executed by a POSIX shell. "
            "Use portable sh/bash-style commands such as ls, cat, grep, tail, export, and python/python3 as available."
        )
    return (
        common
        + " Current platform: Linux/Unix. Commands are executed by a POSIX shell. "
        "Use portable sh/bash-style commands such as ls, cat, grep, tail, export, and python/python3 as available."
    )


def _coerce_timeout(timeout_seconds: int | str | float) -> int:
    if timeout_seconds is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return int(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _normalize_command(command: str) -> str:
    if os.name == "nt":
        normalized = re.sub(r"^\s*python3(\.exe)?\b", "python", command, count=1, flags=re.IGNORECASE)
        normalized = re.sub(
            r"^\s*cd\s+(?:/workspace|workspace|\.?/workspace|\.real-modelize[\\/]+workspace)\s*(?:&&|&)\s*",
            "",
            normalized,
            count=1,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"^\s*pwd\s*$", "cd", normalized, count=1, flags=re.IGNORECASE)
        normalized = re.sub(r"\bls\s+-la\b", "dir", normalized)
        normalized = re.sub(r"\bls\b", "dir", normalized)
        normalized = re.sub(r"\bcat\s+([^\s|&<>]+)", r"type \1", normalized)
        return normalized
    return re.sub(
        r"^\s*cd\s+(?:/workspace|workspace|\.?/workspace|\.real-modelize[\\/]+workspace)\s*(?:&&|;)\s*pwd\s*$",
        "cd",
        command,
        count=1,
        flags=re.IGNORECASE,
    )


def _handle_tail_command(state: RuntimeState, command: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"\s*tail(?:\s+-n)?\s+(\d+)\s+(.+?)\s*", command)
    if not match:
        match = re.fullmatch(r"\s*tail\s+-(\d+)\s+(.+?)\s*", command)
    if not match:
        return None
    count = int(match.group(1))
    raw_path = shlex.split(match.group(2), posix=False)[0]
    from real_modelize_agent.tools.file_tools import read_text_lossy, resolve_read_path

    path = resolve_read_path(state, raw_path)
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"file does not exist: {raw_path}"}
    lines = read_text_lossy(path).splitlines()
    output = "\n".join(lines[-count:])
    return {
        "ok": True,
        "timed_out": False,
        "command": command,
        "exit_code": 0,
        "stdout": output + ("\n" if output else ""),
        "stderr": "",
        "duration_ms": 0,
    }


def _handle_workspace_query(state: RuntimeState, command: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"\s*(?:cd|pwd)\s*", command, flags=re.IGNORECASE):
        return None
    return {
        "ok": True,
        "timed_out": False,
        "command": command.strip() or "cd",
        "exit_code": 0,
        "stdout": f"{state.workspace}\n",
        "stderr": "",
        "duration_ms": 0,
    }


def _looks_dangerous(command: str) -> str | None:
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


def _safe_argv(state: RuntimeState, command: str) -> tuple[list[str] | None, str | None]:
    """Parse an allowlisted command without invoking a shell.

    This deliberately rejects Python code strings and shell composition. Agent
    code must live in a reviewed workspace script and be invoked by relative
    path, which preserves the coder write contract and an auditable entrypoint.
    """
    if SHELL_META_RE.search(command) or ";" in command:
        return None, "shell operators, redirection, substitutions, and multiline commands are not allowed"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return None, f"cannot parse command: {exc}"
    if not argv:
        return None, "command must not be empty"
    executable = Path(argv[0]).name.lower()
    if executable not in ALLOWED_EXECUTABLES:
        return None, f"executable is not allowlisted: {argv[0]}"
    if executable in {"python", "python.exe"}:
        if any(arg in {"-c", "-m", "-"} for arg in argv[1:]):
            return None, "python -c/-m/stdin execution is not allowed; run a project-root .py entrypoint"
        unsupported_flags = [arg for arg in argv[1:] if arg.startswith("-") and arg != "--version"]
        if unsupported_flags:
            return None, f"python interpreter flags are not allowed: {', '.join(unsupported_flags)}"
        non_flags = [arg for arg in argv[1:] if not arg.startswith("-")]
        if non_flags and non_flags[0] not in {"--version"}:
            script = Path(non_flags[0])
            if script.suffix.lower() != ".py":
                return None, "python must execute a .py entrypoint"
            script_path = script if script.is_absolute() else state.workspace / script
            try:
                state.assert_readable_path(script_path)
            except ValueError as exc:
                return None, str(exc)
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.is_absolute():
            try:
                state.assert_readable_path(candidate)
            except ValueError:
                return None, f"absolute path outside readable root (workspace or project root) is not allowed: {arg}"
        if ".." in candidate.parts:
            return None, f"parent traversal is not allowed: {arg}"
    return argv, None


def _decode_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return output.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return output.decode("utf-8", errors="replace")


def run_bash(
    state: RuntimeState,
    command: str,
    timeout_seconds: int | str | float | None = None,
    run_in_background: bool | str = False,
) -> dict[str, Any]:
    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}
    max_timeout = _state_int(state, "bash_max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    timeout = _coerce_timeout(timeout_seconds)
    if timeout_seconds is None:
        timeout = _state_int(state, "bash_default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if timeout <= 0 or timeout > max_timeout:
        return {"ok": False, "error": f"timeout_seconds must be between 1 and {max_timeout}"}
    normalized_command = _normalize_command(command)
    background = _coerce_bool(run_in_background)
    if background:
        return {"ok": False, "error": "background execution is disabled for agent commands"}

    handled = _handle_tail_command(state, normalized_command)
    if handled is not None:
        return handled
    handled = _handle_workspace_query(state, normalized_command)
    if handled is not None:
        return handled

    blocked = _looks_dangerous(normalized_command)
    if blocked:
        return {"ok": False, "error": f"blocked potentially dangerous command pattern: {blocked}"}

    argv, argv_error = _safe_argv(state, normalized_command)
    if argv_error is not None or argv is None:
        return {"ok": False, "error": argv_error or "command rejected"}

    approval = _resolve_approval(state, normalized_command)
    if approval is not None and not approval.get("approved"):
        return approval

    started = time.perf_counter()
    env, env_error = _build_env(state)
    if env_error is not None:
        return {"ok": False, "error": env_error}
    if Path(argv[0]).name.lower() in {"python", "python.exe"} and "--version" not in argv:
        _apply_python_guard_environment(state, env)
    max_output_chars = _state_int(state, "bash_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
    try:
        completed = subprocess.run(
            argv,
            cwd=state.workspace,
            shell=False,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            **_format_captured_output(state, _decode_output(exc.stdout), _decode_output(exc.stderr), max_output_chars),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            **(approval or {}),
        }

    output = _format_captured_output(state, _decode_output(completed.stdout), _decode_output(completed.stderr), max_output_chars)
    result = {
        "ok": completed.returncode == 0,
        "timed_out": False,
        "command": normalized_command,
        "exit_code": completed.returncode,
        **output,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        **(approval or {}),
    }
    execution_record = _persist_python_execution_record(state, argv, result)
    if execution_record:
        result["execution_record"] = execution_record
    return result


def _persist_python_execution_record(
    state: RuntimeState, argv: list[str], result: dict[str, Any]
) -> str:
    if Path(argv[0]).name.lower() not in {"python", "python.exe"}:
        return ""
    script_arg = next((arg for arg in argv[1:] if not arg.startswith("-") and arg.endswith(".py")), "")
    if not script_arg:
        return ""
    script = Path(script_arg)
    if not script.is_absolute():
        script = state.workspace / script
    try:
        state.assert_workspace_path(script)
    except ValueError:
        return ""  # 项目根下的外部脚本只记录失败信息，不写执行台账（台账限 workspace）
    if not script.is_file():
        return ""
    rel = script.relative_to(state.workspace).as_posix()
    parts = script.relative_to(state.workspace).parts
    if len(parts) >= 3 and parts[0].startswith("problem") and parts[1] == "代码":
        record_path = state.workspace / parts[0] / "结果" / "execution.json"
    else:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", rel)
        record_path = state.workspace / ".real-modelize" / "executions" / f"{safe_name}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "command": argv,
        "entrypoint": rel,
        "source_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "exit_code": result.get("exit_code"),
        "ok": bool(result.get("ok")),
        "duration_ms": result.get("duration_ms"),
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_path.relative_to(state.workspace).as_posix()


def _apply_python_guard_environment(state: RuntimeState, env: dict[str, str]) -> None:
    """Install a defense-in-depth guard for ordinary Python file/process/network APIs."""
    guard_dir = state.workspace / ".real-modelize" / "python-guard"
    temp_dir = state.workspace / ".real-modelize" / "tmp"
    guard_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    guard = guard_dir / "sitecustomize.py"
    if not guard.exists() or guard.read_text(encoding="utf-8", errors="replace") != _PYTHON_GUARD_SOURCE:
        guard.write_text(_PYTHON_GUARD_SOURCE, encoding="utf-8")
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + previous if previous else "")
    env["RMA_WORKSPACE_ROOT"] = str(state.workspace.resolve())
    env["TMP"] = str(temp_dir)
    env["TEMP"] = str(temp_dir)
    env["TMPDIR"] = str(temp_dir)
    env["MPLCONFIGDIR"] = str(state.workspace / ".real-modelize" / "matplotlib")


_PYTHON_GUARD_SOURCE = r'''"""Runtime guard injected by RealModelizeAgent. Not a replacement for OS sandboxing."""
import builtins
import io
import os
import pathlib
import socket
import subprocess

_ROOT = pathlib.Path(os.environ["RMA_WORKSPACE_ROOT"]).resolve()

def _inside(value):
    path = pathlib.Path(value).expanduser().resolve()
    return path == _ROOT or _ROOT in path.parents

def _require_write_path(value):
    if isinstance(value, int):
        return
    if not _inside(value):
        raise PermissionError(f"RealModelizeAgent blocked write outside workspace: {value}")

_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        _require_write_path(file)
    return _open(file, mode, *args, **kwargs)
builtins.open = guarded_open
io.open = guarded_open

_os_open = os.open
def guarded_os_open(path, flags, *args, **kwargs):
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if flags & write_flags:
        _require_write_path(path)
    return _os_open(path, flags, *args, **kwargs)
os.open = guarded_os_open

_mkdir = os.mkdir
def guarded_mkdir(path, *args, **kwargs):
    _require_write_path(path)
    return _mkdir(path, *args, **kwargs)
os.mkdir = guarded_mkdir

_remove = os.remove
def guarded_remove(path, *args, **kwargs):
    _require_write_path(path)
    return _remove(path, *args, **kwargs)
os.remove = guarded_remove
os.unlink = guarded_remove

_rename = os.rename
def guarded_rename(src, dst, *args, **kwargs):
    _require_write_path(src)
    _require_write_path(dst)
    return _rename(src, dst, *args, **kwargs)
os.rename = guarded_rename
os.replace = guarded_rename

def _blocked(*args, **kwargs):
    raise PermissionError("RealModelizeAgent blocked child process or network access")

subprocess.Popen = _blocked
subprocess.run = _blocked
subprocess.call = _blocked
subprocess.check_call = _blocked
subprocess.check_output = _blocked
os.system = _blocked
os.popen = _blocked
socket.create_connection = _blocked
_socket_connect = socket.socket.connect
socket.socket.connect = _blocked
'''


def _resolve_approval(state: RuntimeState, command: str) -> dict[str, Any] | None:
    risk_reason = classify_command_risk(command)
    if risk_reason is None:
        return None

    request = make_approval_request(command, risk_reason)
    base = {
        "requires_approval": True,
        "approval_id": request.id,
        "risk_reason": risk_reason,
        "command": command,
    }
    if state.approval_mode == "auto":
        return {**base, "approved": True}
    if state.approval_mode == "deny" or state.approval_handler is None:
        return {
            **base,
            "ok": False,
            "approved": False,
            "error": f"human approval required for high-risk command: {risk_reason}",
        }

    decision = state.approval_handler(request)
    if isinstance(decision, ApprovalDecision):
        approved = decision.approved
        decision_reason = decision.reason
    else:
        approved = bool(decision)
        decision_reason = ""
    if approved:
        return {**base, "approved": True}
    return {
        **base,
        "ok": False,
        "approved": False,
        "error": decision_reason or f"human rejected high-risk command: {risk_reason}",
    }


def _state_int(state: RuntimeState, name: str, default: int) -> int:
    try:
        value = int(getattr(state, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _coerce_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_env(state: RuntimeState) -> tuple[dict[str, str], str | None]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    _prepend_harness_paths(state, env)
    env_file = state.bash_env_file or state.workspace / ".real-modelize.env"
    if env_file.exists():
        try:
            env.update(_parse_env_file(env_file, env))
        except OSError as exc:
            return env, f"failed to read bash env file {env_file}: {exc}"
    return env, None


def _prepend_harness_paths(state: RuntimeState, env: dict[str, str]) -> None:
    path_candidates = [
        _ensure_toolchain_shims(state),
        state.workspace / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
        state.workspace / "venv" / ("Scripts" if os.name == "nt" else "bin"),
        state.workspace / "node_modules" / ".bin",
        Path(sys.executable).parent,
    ]
    existing = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for path in [str(candidate) for candidate in path_candidates if candidate.exists()] + existing:
        if path not in merged:
            merged.append(path)
    env["PATH"] = os.pathsep.join(merged)
    if getattr(sys, "prefix", None) and sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        env.setdefault("VIRTUAL_ENV", sys.prefix)


def _ensure_toolchain_shims(state: RuntimeState) -> Path:
    shim_dir = state.workspace / ".real-modelize" / "shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    python_executable = sys.executable
    if os.name == "nt":
        _write_shim(shim_dir / "python.cmd", f'@echo off\r\n"{python_executable}" %*\r\n')
        _write_shim(shim_dir / "python3.cmd", f'@echo off\r\n"{python_executable}" %*\r\n')
        pip_cmd = (
            "@echo off\r\n"
            f'"{python_executable}" -c "import pathlib,sys; import pip; '
            'p=pathlib.Path(pip.__file__).resolve(); '
            'prefix=pathlib.Path(sys.prefix).resolve(); '
            'raise SystemExit(0 if p == prefix or prefix in p.parents else 1)" >nul 2>nul\r\n'
            f'if errorlevel 1 "{python_executable}" -m ensurepip --upgrade >nul 2>nul\r\n'
            f'"{python_executable}" -m pip %*\r\n'
        )
        _write_shim(shim_dir / "pip.cmd", pip_cmd)
        _write_shim(shim_dir / "pip3.cmd", pip_cmd)
        return shim_dir
    _write_shim(shim_dir / "python", f"#!/bin/sh\nexec {shlex.quote(python_executable)} \"$@\"\n")
    _write_shim(shim_dir / "python3", f"#!/bin/sh\nexec {shlex.quote(python_executable)} \"$@\"\n")
    quoted_python = shlex.quote(python_executable)
    pip_shim = (
        "#!/bin/sh\n"
        f"{quoted_python} - <<'PY' >/dev/null 2>&1\n"
        "import pathlib\n"
        "import sys\n"
        "import pip\n"
        "pip_path = pathlib.Path(pip.__file__).resolve()\n"
        "prefix = pathlib.Path(sys.prefix).resolve()\n"
        "raise SystemExit(0 if pip_path == prefix or prefix in pip_path.parents else 1)\n"
        "PY\n"
        "if [ $? -ne 0 ]; then\n"
        f"  {quoted_python} -m ensurepip --upgrade >/dev/null 2>&1 || exit $?\n"
        "fi\n"
        f"exec {quoted_python} -m pip \"$@\"\n"
    )
    _write_shim(shim_dir / "pip", pip_shim)
    _write_shim(shim_dir / "pip3", pip_shim)
    return shim_dir


def _write_shim(path: Path, content: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != content:
        path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)


def _parse_env_file(path, base_env: dict[str, str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        parsed[key] = _expand_env_value(_unquote_env_value(value.strip()), {**base_env, **parsed})
    return parsed


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _expand_env_value(value: str, env: dict[str, str]) -> str:
    def replace_var(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain") or ""
        return env.get(name, "")

    return re.sub(r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)", replace_var, value)


def _format_captured_output(state: RuntimeState, stdout: str, stderr: str, max_output_chars: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    output_dir = state.workspace / ".real-modelize" / "bash-outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    if len(stdout) > max_output_chars:
        stdout_path = output_dir / f"stdout-{time.time_ns()}.log"
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        output["stdout_path"] = str(stdout_path.relative_to(state.workspace))
        output["stdout_truncated"] = True
    if len(stderr) > max_output_chars:
        stderr_path = output_dir / f"stderr-{time.time_ns()}.log"
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
        output["stderr_path"] = str(stderr_path.relative_to(state.workspace))
        output["stderr_truncated"] = True
    output["stdout"] = stdout[:max_output_chars]
    output["stderr"] = stderr[:max_output_chars]
    return output


def _run_background(
    state: RuntimeState,
    command: str,
    env: dict[str, str],
    approval: dict[str, Any] | None,
) -> dict[str, Any]:
    output_dir = state.workspace / ".real-modelize" / "background"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    stdout_path = output_dir / f"job-{stamp}.out"
    stderr_path = output_dir / f"job-{stamp}.err"
    stdout_handle = stdout_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=state.workspace,
            shell=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name != "nt"),
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return {
        "ok": True,
        "timed_out": False,
        "command": command,
        "background": True,
        "pid": process.pid,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_path": str(stdout_path.relative_to(state.workspace)),
        "stderr_path": str(stderr_path.relative_to(state.workspace)),
        "duration_ms": 0,
        **(approval or {}),
    }
