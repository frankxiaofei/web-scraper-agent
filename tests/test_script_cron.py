"""script cron 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core import script_cron as sc


@pytest.fixture
def cron_jobs_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs_path = tmp_path / "script_cron_jobs.json"
    monkeypatch.setattr(sc, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    return jobs_path


def test_normalize_cron_standard():
    assert sc.normalize_cron_expression("0 2 * * *") == "0 2 * * *"
    assert sc.normalize_cron_expression("30 4 * * 1") == "30 4 * * 1"


def test_normalize_cron_natural_chinese():
    assert sc.normalize_cron_expression("每天 2 点") == "0 2 * * *"
    assert sc.normalize_cron_expression("每天凌晨3点") == "0 3 * * *"
    assert sc.normalize_cron_expression("每日 6 点") == "0 6 * * *"


def test_normalize_cron_invalid():
    with pytest.raises(ValueError, match="无法解析"):
        sc.normalize_cron_expression("not a cron")
    with pytest.raises(ValueError, match="5 段"):
        sc.normalize_cron_expression("0 2 * *")


def test_validate_script_whitelist():
    assert sc.validate_script_name("crawl_powerchina_http.py") == "crawl_powerchina_http.py"
    assert sc.validate_script_name("crawl_powerchina_http") == "crawl_powerchina_http.py"


def test_validate_script_rejects_path_and_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sc, "SCRIPTS_DIR", Path("/fake/scripts"))
    with pytest.raises(ValueError, match="白名单"):
        sc.validate_script_name("evil.py")
    with pytest.raises(ValueError, match="禁止路径"):
        sc.validate_script_name("../etc/passwd")


def test_validate_script_args_rejects_shell_metachar():
    with pytest.raises(ValueError, match="非法字符"):
        sc.validate_script_args(["--max-pages", "6; rm -rf /"])


def test_validate_script_args_from_dict():
    assert sc.validate_script_args({"max_pages": 6, "fetch_detail": True}) == [
        "--max-pages",
        "6",
        "--fetch-detail",
    ]


def test_build_subprocess_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scripts = tmp_path / "scripts"
    monkeypatch.setattr(sc, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(sc, "SCRIPT_WHITELIST", frozenset({"crawl_powerchina_http.py"}))

    fake_script = scripts / "crawl_powerchina_http.py"
    fake_script.parent.mkdir(parents=True, exist_ok=True)
    fake_script.write_text("# stub", encoding="utf-8")

    cmd = sc.build_subprocess_command(
        "crawl_powerchina_http.py",
        ["--max-pages", "3"],
    )
    assert cmd[1].endswith("crawl_powerchina_http.py")
    assert cmd[-2:] == ["--max-pages", "3"]


def test_persistence_load_save_roundtrip(cron_jobs_file: Path):
    assert sc.load_script_cron_jobs() == []
    job = sc.create_script_cron_job(
        script="crawl_powerchina_http.py",
        cron="0 2 * * *",
        args=["--max-pages", "2"],
        name="测试电建",
        job_id="test-job-1",
    )
    assert job["id"] == "test-job-1"
    assert job["cron"] == "0 2 * * *"
    assert job["next_run_time"] is not None

    raw = json.loads(cron_jobs_file.read_text(encoding="utf-8"))
    assert len(raw["jobs"]) == 1

    loaded = sc.load_script_cron_jobs()
    assert loaded[0]["script"] == "crawl_powerchina_http.py"

    updated = sc.update_script_cron_job("test-job-1", cron="0 3 * * *", enabled=False)
    assert updated["cron"] == "0 3 * * *"
    assert updated["enabled"] is False
    assert updated["next_run_time"] is None

    deleted = sc.delete_script_cron_job("test-job-1")
    assert deleted["deleted_id"] == "test-job-1"
    assert sc.load_script_cron_jobs() == []


def test_run_script_job_mock_subprocess(cron_jobs_file: Path, monkeypatch: pytest.MonkeyPatch):
    scripts = cron_jobs_file.parent / "scripts"
    scripts.mkdir()
    script_file = scripts / "crawl_powerchina_http.py"
    script_file.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(sc, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(sc, "ROOT", cron_jobs_file.parent)

    sc.create_script_cron_job(
        script="crawl_powerchina_http.py",
        cron="0 2 * * *",
        job_id="run-me",
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_run = MagicMock(return_value=mock_proc)

    result = sc.run_script_job("run-me", subprocess_run=mock_run)
    assert result["ok"] is True
    assert result["returncode"] == 0
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("shell") is not True

    job = sc.get_script_cron_job("run-me")
    assert job["last_run_status"] == "success"


def test_script_cron_service_manage(monkeypatch: pytest.MonkeyPatch, cron_jobs_file: Path):
    scripts = cron_jobs_file.parent / "scripts"
    scripts.mkdir()
    (scripts / "crawl_powerchina_http.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(sc, "SCRIPTS_DIR", scripts)

    from src.web.script_cron_service import ScriptCronService

    svc = ScriptCronService()
    created = svc.manage(
        "create",
        script="crawl_powerchina_http.py",
        cron="每天 2 点",
        args={"max_pages": 6},
        name="电建 HTTP",
    )
    assert created["ok"] is True
    job_id = created["job"]["id"]

    listed = svc.manage("list")
    assert listed["count"] == 1

    got = svc.manage("get", job_id=job_id)
    assert got["job"]["name"] == "电建 HTTP"

    deleted = svc.manage("delete", job_id=job_id)
    assert deleted["ok"] is True


def test_crawl_schedule_script_tool(cron_jobs_file: Path, monkeypatch: pytest.MonkeyPatch):
    scripts = cron_jobs_file.parent / "scripts"
    scripts.mkdir()
    (scripts / "crawl_powerchina_http.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(sc, "SCRIPTS_DIR", scripts)

    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "crawl_schedule_script",
        {
            "action": "create",
            "script": "crawl_powerchina_http.py",
            "cron": "0 2 * * *",
            "args": ["--max-pages", "6"],
            "name": "电建定时",
        },
    )
    assert result["ok"] is True
    assert result["job"]["cron"] == "0 2 * * *"
    assert "cron=" in summary or "0 2" in summary
