"""Agent 代码执行：白名单 scripts/ 脚本与临时代码（subprocess，禁止 shell）。"""

from __future__ import annotations

import ast
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.script_cron import (
    ROOT,
    SCRIPTS_DIR,
    validate_script_args,
    validate_script_name,
)

GENERATED_SCRIPTS_DIR = ROOT / "data" / "generated_scripts"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 32_000

_FORBIDDEN_IMPORT_MODULES = frozenset(
    {
        "subprocess",
        "ctypes",
        "pty",
        "multiprocessing",
        "importlib",
    }
)
_FORBIDDEN_CALL_NAMES = frozenset({"eval", "exec", "compile", "__import__"})


def _python_executable() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _ensure_generated_dir() -> Path:
    GENERATED_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    return GENERATED_SCRIPTS_DIR


def _resolve_under_root(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    raise ValueError(f"路径必须在项目目录内: {path}")


def validate_generated_code(code: str) -> None:
    """简单 AST 检查，拒绝明显危险的 import / 调用。"""
    text = (code or "").strip()
    if not text:
        raise ValueError("code 不能为空")
    if len(text) > 200_000:
        raise ValueError("code 过长（上限 200KB）")

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"Python 语法错误: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = (alias.name or "").split(".")[0]
                if root_name in _FORBIDDEN_IMPORT_MODULES:
                    raise ValueError(f"禁止 import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _FORBIDDEN_IMPORT_MODULES:
                raise ValueError(f"禁止 from {node.module} import …")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
                raise ValueError(f"禁止调用: {func.id}()")


def _truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 80] + f"\n... [输出已截断，共 {len(text)} 字符]", True


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    subprocess_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> dict[str, Any]:
    runner = subprocess_run or subprocess.run
    _resolve_under_root(cwd)
    for part in cmd:
        if part.startswith("-"):
            continue
        candidate = Path(part)
        if candidate.is_absolute() and candidate.suffix == ".py":
            _resolve_under_root(candidate)

    try:
        proc = runner(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        stdout, stdout_trunc = _truncate_output(proc.stdout or "")
        stderr, stderr_trunc = _truncate_output(proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        partial_out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        partial_err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        stdout, _ = _truncate_output(partial_out)
        stderr, _ = _truncate_output(partial_err)
        return {
            "ok": False,
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "error": f"执行超时 ({timeout}s)",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "timed_out": False}


def execute_whitelist_script(
    script: str,
    args: Any = None,
    *,
    timeout_seconds: Optional[int] = None,
    subprocess_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> dict[str, Any]:
    """模式 A：执行 scripts/ 白名单内已有脚本。"""
    script_name = validate_script_name(script)
    safe_args = validate_script_args(args)
    script_path = _resolve_under_root(SCRIPTS_DIR / script_name)
    timeout = max(1, min(int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), 7200))
    cmd = [_python_executable(), str(script_path), *safe_args]
    result = _run_subprocess(cmd, cwd=ROOT, timeout=timeout, subprocess_run=subprocess_run)
    return {
        **result,
        "mode": "script",
        "script": script_name,
        "args": safe_args,
        "command": cmd,
        "timeout_seconds": timeout,
    }


def execute_generated_code(
    code: str,
    *,
    label: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    subprocess_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> dict[str, Any]:
    """模式 B：写入 data/generated_scripts/ 后 subprocess 执行。"""
    validate_generated_code(code)
    timeout = max(1, min(int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS), 7200))
    gen_dir = _ensure_generated_dir()
    _resolve_under_root(gen_dir)

    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in (label or "agent"))[:40]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    script_name = f"{safe_label}_{ts}_{uuid.uuid4().hex[:8]}.py"
    script_path = gen_dir / script_name
    _resolve_under_root(script_path)

    script_path.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    cmd = [_python_executable(), str(script_path)]
    result = _run_subprocess(cmd, cwd=ROOT, timeout=timeout, subprocess_run=subprocess_run)
    return {
        **result,
        "mode": "code",
        "script_path": str(script_path.relative_to(ROOT)),
        "command": cmd,
        "timeout_seconds": timeout,
    }


def execute_code(
    mode: str,
    *,
    script: Optional[str] = None,
    args: Any = None,
    code: Optional[str] = None,
    label: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    subprocess_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> dict[str, Any]:
    """统一入口。"""
    normalized = (mode or "").strip().lower()
    if normalized == "script":
        if not script:
            return {"ok": False, "error": "mode=script 时 script 必填"}
        try:
            return execute_whitelist_script(
                script,
                args,
                timeout_seconds=timeout_seconds,
                subprocess_run=subprocess_run,
            )
        except ValueError as exc:
            return {"ok": False, "mode": "script", "error": str(exc)}
    if normalized == "code":
        if not code:
            return {"ok": False, "error": "mode=code 时 code 必填"}
        try:
            return execute_generated_code(
                code,
                label=label,
                timeout_seconds=timeout_seconds,
                subprocess_run=subprocess_run,
            )
        except ValueError as exc:
            return {"ok": False, "mode": "code", "error": str(exc)}
    return {"ok": False, "error": f"未知 mode: {mode!r}；允许 script | code"}
