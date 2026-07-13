"""crawl_execute_code / code_executor 单元测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core import code_executor as ce
from src.core import script_cron as sc
from src.web.crawl_agent_tools import CrawlAgentToolExecutor


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gen = tmp_path / "data" / "generated_scripts"
    gen.mkdir(parents=True)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text("#!/bin/sh\necho stub", encoding="utf-8")

    monkeypatch.setattr(ce, "ROOT", tmp_path)
    monkeypatch.setattr(ce, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(ce, "GENERATED_SCRIPTS_DIR", gen)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(sc, "SCRIPT_WHITELIST", frozenset({"run_once.py"}))

    whitelist_script = scripts / "run_once.py"
    whitelist_script.write_text("print('whitelist')\n", encoding="utf-8")
    return tmp_path


def _mock_subprocess(stdout: str = "", stderr: str = "", returncode: int = 0):
    mock_proc = MagicMock()
    mock_proc.stdout = stdout
    mock_proc.stderr = stderr
    mock_proc.returncode = returncode
    return MagicMock(return_value=mock_proc)


def test_execute_generated_code_success(project_root: Path):
    mock_run = _mock_subprocess(stdout="hello\n", returncode=0)
    result = ce.execute_generated_code(
        "print('hello')",
        label="test",
        subprocess_run=mock_run,
    )
    assert result["ok"] is True
    assert result["stdout"] == "hello\n"
    assert result["returncode"] == 0
    assert result["mode"] == "code"
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("shell") is not True
    assert mock_run.call_args.kwargs.get("timeout") == ce.DEFAULT_TIMEOUT_SECONDS
    cmd = mock_run.call_args.args[0]
    assert cmd[0].endswith(".venv/bin/python")
    assert Path(cmd[1]).parent == project_root / "data" / "generated_scripts"


def test_execute_generated_code_timeout(project_root: Path):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="partial", stderr="err")

    result = ce.execute_generated_code(
        "import time\ntime.sleep(999)",
        subprocess_run=raise_timeout,
        timeout_seconds=5,
    )
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert "超时" in (result.get("error") or "")


def test_validate_generated_code_rejects_subprocess():
    with pytest.raises(ValueError, match="禁止 import"):
        ce.validate_generated_code("import subprocess\nsubprocess.run(['ls'])")


def test_validate_generated_code_rejects_eval():
    with pytest.raises(ValueError, match="禁止调用"):
        ce.validate_generated_code("eval('1+1')")


def test_execute_whitelist_script_rejects_path_escape(project_root: Path):
    result = ce.execute_code("script", script="../etc/passwd")
    assert result["ok"] is False
    assert "白名单" in result["error"] or "禁止路径" in result["error"]


def test_execute_whitelist_script_success(project_root: Path):
    mock_run = _mock_subprocess(stdout="done\n", returncode=0)
    result = ce.execute_whitelist_script(
        "run_once.py",
        ["--site-id", "tjbid"],
        subprocess_run=mock_run,
    )
    assert result["ok"] is True
    assert result["script"] == "run_once.py"
    cmd = mock_run.call_args.args[0]
    assert cmd[-2:] == ["--site-id", "tjbid"]


def test_crawl_execute_code_tool(project_root: Path, monkeypatch: pytest.MonkeyPatch):
    mock_run = _mock_subprocess(stdout="2\n", returncode=0)
    monkeypatch.setattr(ce.subprocess, "run", mock_run)

    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "crawl_execute_code",
        {"mode": "code", "code": "print(2)"},
    )
    assert result["ok"] is True
    assert result["stdout"] == "2\n"
    assert "exit=0" in summary


def test_output_truncation():
    long_text = "x" * (ce.MAX_OUTPUT_CHARS + 1000)
    truncated, was = ce._truncate_output(long_text)
    assert was is True
    assert len(truncated) < len(long_text)
    assert "截断" in truncated
