"""scripts/ Cron 任务 Web 层服务。"""

from __future__ import annotations

from typing import Any, Optional

from src.core.script_cron import (
    SCRIPT_WHITELIST,
    create_script_cron_job,
    delete_script_cron_job,
    get_script_cron_job,
    list_script_cron_jobs,
    run_script_job,
    update_script_cron_job,
)


class ScriptCronService:
    def list_jobs(self, *, enabled_only: bool = False) -> dict[str, Any]:
        jobs = list_script_cron_jobs(enabled_only=enabled_only)
        return {
            "ok": True,
            "count": len(jobs),
            "whitelist": sorted(SCRIPT_WHITELIST),
            "jobs": jobs,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = get_script_cron_job(job_id)
        if not job:
            return {"ok": False, "error": f"任务不存在: {job_id}"}
        from src.core.script_cron import _serialize_job

        return {"ok": True, "job": _serialize_job(job)}

    def create_job(
        self,
        *,
        script: str,
        cron: str,
        args: Any = None,
        name: Optional[str] = None,
        enabled: bool = True,
        job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            job = create_script_cron_job(
                script=script,
                cron=cron,
                args=args,
                name=name,
                enabled=enabled,
                job_id=job_id,
            )
            return {
                "ok": True,
                "job": job,
                "message": "已创建；run_scheduler 约 60s 内自动加载，或重启 scheduler 立即生效",
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        try:
            job = update_script_cron_job(job_id, **fields)
            return {
                "ok": True,
                "job": job,
                "message": "已更新；run_scheduler 约 60s 内自动加载",
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def delete_job(self, job_id: str) -> dict[str, Any]:
        try:
            result = delete_script_cron_job(job_id)
            return {**result, "message": "已删除；run_scheduler 约 60s 内自动卸载"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def run_now(self, job_id: str) -> dict[str, Any]:
        return run_script_job(job_id)

    def manage(
        self,
        action: str,
        *,
        job_id: Optional[str] = None,
        script: Optional[str] = None,
        cron: Optional[str] = None,
        args: Any = None,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        act = (action or "").strip().lower()
        if act == "list":
            return self.list_jobs()
        if act == "get":
            if not job_id:
                return {"ok": False, "error": "get 需要 job_id"}
            return self.get_job(job_id)
        if act == "create":
            if not script or not cron:
                return {"ok": False, "error": "create 需要 script 与 cron"}
            return self.create_job(
                script=script,
                cron=cron,
                args=args,
                name=name,
                enabled=True if enabled is None else enabled,
                job_id=job_id,
            )
        if act == "update":
            if not job_id:
                return {"ok": False, "error": "update 需要 job_id"}
            fields: dict[str, Any] = {}
            if script is not None:
                fields["script"] = script
            if cron is not None:
                fields["cron"] = cron
            if args is not None:
                fields["args"] = args
            if name is not None:
                fields["name"] = name
            if enabled is not None:
                fields["enabled"] = enabled
            return self.update_job(job_id, **fields)
        if act == "delete":
            if not job_id:
                return {"ok": False, "error": "delete 需要 job_id"}
            return self.delete_job(job_id)
        if act == "run":
            if not job_id:
                return {"ok": False, "error": "run 需要 job_id"}
            return self.run_now(job_id)
        return {"ok": False, "error": f"未知 action: {action}；支持 list/get/create/update/delete/run"}


_service: Optional[ScriptCronService] = None


def get_script_cron_service() -> ScriptCronService:
    global _service
    if _service is None:
        _service = ScriptCronService()
    return _service
