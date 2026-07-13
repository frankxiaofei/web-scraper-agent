"""对话定时任务 Web 层服务。"""

from __future__ import annotations

from typing import Any, Optional

from src.core.chat_scheduled_tasks import (
    CRON_PRESETS,
    PROMPT_PRESETS,
    PUSH_MODES,
    REPORT_KINDS,
    TASK_TYPE_BIM_DAILY_ANALYSIS,
    TASK_TYPE_BIM_DAILY_BRIEF,
    TASK_TYPE_BIM_WEEKLY_BRIEF,
    TASK_TYPE_ENGINEERING_LLM_REPORT,
    TASK_TYPE_HERMES,
    TASK_TYPE_HERMES_SUMMARY,
    TASK_TYPE_INCREMENTAL,
    TASK_TYPE_LABELS,
    create_chat_scheduled_task,
    delete_chat_scheduled_task,
    get_chat_scheduled_task,
    is_hermes_task_type,
    list_chat_scheduled_tasks,
    parse_bim_analysis_schedule_intent,
    parse_hermes_schedule_intent,
    parse_schedule_intent,
    run_chat_scheduled_task_async,
    update_chat_scheduled_task,
    toggle_chat_scheduled_task,
)


class ChatScheduleService:
    def get_meta(self) -> dict[str, Any]:
        from src.core.feishu_push import is_feishu_push_configured
        from src.core.notification.dispatcher import get_channel_meta

        return {
            "ok": True,
            "primary_task_type": TASK_TYPE_HERMES_SUMMARY,
            "task_types": [
                {"value": k, "label": v}
                for k, v in TASK_TYPE_LABELS.items()
                if k
                in (
                    TASK_TYPE_HERMES_SUMMARY,
                    TASK_TYPE_BIM_DAILY_BRIEF,
                    TASK_TYPE_BIM_WEEKLY_BRIEF,
                    TASK_TYPE_BIM_DAILY_ANALYSIS,
                    TASK_TYPE_ENGINEERING_LLM_REPORT,
                )
            ],
            "legacy_task_types": [
                {"value": k, "label": v}
                for k, v in TASK_TYPE_LABELS.items()
                if k
                in (
                    TASK_TYPE_BIM_DAILY_BRIEF,
                    TASK_TYPE_BIM_WEEKLY_BRIEF,
                    TASK_TYPE_BIM_DAILY_ANALYSIS,
                    TASK_TYPE_ENGINEERING_LLM_REPORT,
                )
            ],
            "report_kinds": REPORT_KINDS,
            "prompt_presets": PROMPT_PRESETS,
            "push_modes": PUSH_MODES,
            "cron_presets": CRON_PRESETS,
            "feishu_configured": is_feishu_push_configured("webhook"),
            "notification_channels": get_channel_meta(),
        }

    def list_tasks(self, *, enabled_only: bool = False) -> dict[str, Any]:
        tasks = list_chat_scheduled_tasks(enabled_only=enabled_only)
        return {
            "ok": True,
            "count": len(tasks),
            "tasks": tasks,
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = get_chat_scheduled_task(task_id)
        if not task:
            return {"ok": False, "error": f"任务不存在: {task_id}"}
        from src.core.chat_scheduled_tasks import _serialize_task

        return {"ok": True, "task": _serialize_task(task)}

    def create_from_intent(self, user_message: str) -> dict[str, Any]:
        parsed = parse_schedule_intent(user_message)
        if not parsed.get("ok"):
            return parsed
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_INCREMENTAL,
                site_id=parsed["site_id"],
                cron=parsed["cron"],
                name=parsed.get("name"),
                report_type=parsed.get("report_type", "site"),
                feishu_push=parsed.get("feishu_push", True),
                max_items=parsed.get("max_items", 10),
                feishu_webhook_url=parsed.get("feishu_webhook_url"),
                feishu_webhook_secret=parsed.get("feishu_webhook_secret"),
                created_by="chat_intent",
            )
            return self._create_success_response(task, parsed)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_hermes_from_intent(self, user_message: str) -> dict[str, Any]:
        parsed = parse_hermes_schedule_intent(user_message)
        if not parsed.get("ok"):
            return parsed
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_HERMES_SUMMARY,
                cron=parsed["cron"],
                name=parsed.get("name"),
                hermes_prompt=parsed["hermes_prompt"],
                push_mode=parsed.get("push_mode") or "excerpt",
                feishu_push=parsed.get("feishu_push", True),
                feishu_webhook_url=parsed.get("feishu_webhook_url"),
                feishu_webhook_secret=parsed.get("feishu_webhook_secret"),
                created_by="chat_intent",
            )
            return self._create_success_response(task, parsed)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_bim_analysis_from_intent(self, user_message: str) -> dict[str, Any]:
        parsed = parse_bim_analysis_schedule_intent(user_message)
        if not parsed.get("ok"):
            return parsed
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_BIM_DAILY_ANALYSIS,
                cron=parsed["cron"],
                name=parsed.get("name"),
                top_n=parsed.get("top_n", 10),
                feishu_push=parsed.get("feishu_push", True),
                feishu_webhook_url=parsed.get("feishu_webhook_url"),
                feishu_webhook_secret=parsed.get("feishu_webhook_secret"),
                created_by="chat_intent",
            )
            return self._create_success_response(task, parsed)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_bim_analysis_task(
        self,
        *,
        cron: str,
        name: Optional[str] = None,
        top_n: int = 10,
        feishu_webhook_url: Optional[str] = None,
        feishu_webhook_secret: Optional[str] = None,
        feishu_webhooks: Optional[list[dict[str, Any]] | str] = None,
        feishu_user_ids: Optional[list[str] | str] = None,
        feishu_push: bool = True,
        enabled: bool = True,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_BIM_DAILY_ANALYSIS,
                cron=cron,
                name=name,
                top_n=top_n,
                feishu_push=feishu_push,
                feishu_webhook_url=feishu_webhook_url,
                feishu_webhook_secret=feishu_webhook_secret,
                feishu_webhooks=feishu_webhooks,
                feishu_user_ids=feishu_user_ids,
                enabled=enabled,
                task_id=task_id,
                created_by="chat_ui",
            )
            return {
                "ok": True,
                "task": task,
                "message": (
                    f"已创建 BIM 分析通知任务；下次执行 {task.get('next_run_time') or '—'}；"
                    "run_scheduler 约 60s 内自动加载"
                ),
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_hermes_summary_task(
        self,
        *,
        cron: str,
        name: Optional[str] = None,
        hermes_prompt: Optional[str] = None,
        prompt: Optional[str] = None,
        push_mode: str = "excerpt",
        push_excerpt_max: int = 1500,
        feishu_webhook_url: Optional[str] = None,
        feishu_webhook_secret: Optional[str] = None,
        feishu_webhooks: Optional[list[dict[str, Any]] | str] = None,
        feishu_user_ids: Optional[list[str] | str] = None,
        notification_channels: Optional[list[str] | str] = None,
        channel_config: Optional[dict[str, Any]] = None,
        feishu_push: bool = True,
        enabled: bool = True,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_HERMES_SUMMARY,
                cron=cron,
                name=name,
                hermes_prompt=hermes_prompt,
                prompt=prompt,
                push_mode=push_mode,
                push_excerpt_max=push_excerpt_max,
                feishu_push=feishu_push,
                feishu_webhook_url=feishu_webhook_url,
                feishu_webhook_secret=feishu_webhook_secret,
                feishu_webhooks=feishu_webhooks,
                feishu_user_ids=feishu_user_ids,
                notification_channels=notification_channels,
                channel_config=channel_config,
                enabled=enabled,
                task_id=task_id,
                created_by="scheduled_tasks_ui",
            )
            return {
                "ok": True,
                "task": task,
                "message": (
                    f"已创建 Hermes 提示词通知任务；下次执行 {task.get('next_run_time') or '—'}；"
                    "run_scheduler 约 60s 内自动加载"
                ),
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_brief_task(
        self,
        *,
        task_type: str,
        cron: str,
        name: Optional[str] = None,
        top_n: int = 10,
        days: Optional[int] = None,
        feishu_webhook_url: Optional[str] = None,
        feishu_webhook_secret: Optional[str] = None,
        feishu_webhooks: Optional[list[dict[str, Any]] | str] = None,
        feishu_user_ids: Optional[list[str] | str] = None,
        feishu_push: bool = True,
        enabled: bool = True,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if task_type not in (
            TASK_TYPE_BIM_DAILY_BRIEF,
            TASK_TYPE_BIM_WEEKLY_BRIEF,
            TASK_TYPE_BIM_DAILY_ANALYSIS,
        ):
            return {"ok": False, "error": f"不支持的 task_type: {task_type}"}
        try:
            task = create_chat_scheduled_task(
                task_type=task_type,
                cron=cron,
                name=name,
                top_n=top_n,
                days=days,
                feishu_push=feishu_push,
                feishu_webhook_url=feishu_webhook_url,
                feishu_webhook_secret=feishu_webhook_secret,
                feishu_webhooks=feishu_webhooks,
                feishu_user_ids=feishu_user_ids,
                enabled=enabled,
                task_id=task_id,
                created_by="scheduled_tasks_ui",
            )
            return {
                "ok": True,
                "task": task,
                "message": (
                    f"已创建 {TASK_TYPE_LABELS.get(task_type, task_type)}；"
                    f"下次执行 {task.get('next_run_time') or '—'}；"
                    "run_scheduler 约 60s 内自动加载"
                ),
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def create_engineering_llm_report_task(
        self,
        *,
        cron: str,
        name: Optional[str] = None,
        feishu_user_ids: list[str] | str,
        days: int = 1,
        top_n: int = 10,
        feishu_webhooks: Optional[list[dict[str, Any]] | str] = None,
        feishu_push: bool = True,
        enabled: bool = True,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_ENGINEERING_LLM_REPORT,
                cron=cron,
                name=name,
                feishu_user_ids=feishu_user_ids,
                feishu_webhooks=feishu_webhooks,
                days=days,
                top_n=top_n,
                feishu_push=feishu_push,
                enabled=enabled,
                task_id=task_id,
                created_by="scheduled_tasks_ui",
            )
            users_label = "、".join(task.get("feishu_user_ids") or [])
            return {
                "ok": True,
                "task": task,
                "message": (
                    f"已创建工程大模型应用个人推送任务；收件人 {users_label}；"
                    f"下次执行 {task.get('next_run_time') or '—'}；"
                    "run_scheduler 约 60s 内自动加载"
                ),
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def _create_success_response(self, task: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
        webhook_hint = ""
        if task.get("feishu_webhook_url") or parsed.get("feishu_webhook_url"):
            webhook_hint = "；已绑定任务级飞书 Webhook"
        elif task.get("feishu_push", True):
            webhook_hint = "；飞书推送 fallback 全局 FEISHU_WEBHOOK_URL"
        return {
            "ok": True,
            "task": task,
            "parsed": parsed,
            "message": (
                f"已创建通知任务：{task['name']}；"
                f"下次执行 {task.get('next_run_time') or '—'}；"
                f"run_scheduler 约 60s 内自动加载{webhook_hint}"
            ),
        }

    def create_task(
        self,
        *,
        task_type: str = TASK_TYPE_INCREMENTAL,
        site_id: Optional[str] = None,
        cron: str,
        name: Optional[str] = None,
        report_type: str = "site",
        feishu_push: bool = True,
        max_items: int = 10,
        hermes_prompt: Optional[str] = None,
        feishu_webhook_url: Optional[str] = None,
        feishu_webhook_secret: Optional[str] = None,
        enabled: bool = True,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            task = create_chat_scheduled_task(
                task_type=task_type,
                site_id=site_id,
                cron=cron,
                name=name,
                report_type=report_type,
                feishu_push=feishu_push,
                max_items=max_items,
                hermes_prompt=hermes_prompt,
                feishu_webhook_url=feishu_webhook_url,
                feishu_webhook_secret=feishu_webhook_secret,
                enabled=enabled,
                task_id=task_id,
                created_by="chat_tool",
            )
            return {
                "ok": True,
                "task": task,
                "message": "已创建；run_scheduler 约 60s 内自动加载",
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        try:
            task = update_chat_scheduled_task(task_id, **fields)
            return {"ok": True, "task": task, "message": "已更新；run_scheduler 约 60s 内自动加载"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def toggle_task(self, task_id: str, *, enabled: Optional[bool] = None) -> dict[str, Any]:
        try:
            task = toggle_chat_scheduled_task(task_id, enabled=enabled)
            state = "启用" if task.get("enabled", True) else "禁用"
            return {"ok": True, "task": task, "message": f"已{state}；run_scheduler 约 60s 内自动加载"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def delete_task(self, task_id: str) -> dict[str, Any]:
        try:
            result = delete_chat_scheduled_task(task_id)
            return {**result, "message": "已删除；run_scheduler 约 60s 内自动卸载"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    async def run_now(
        self,
        task_id: str,
        *,
        browser_pool: Any = None,
        pipeline: Any = None,
        dry_run: bool = False,
        force: bool = True,
    ) -> dict[str, Any]:
        return await run_chat_scheduled_task_async(
            task_id,
            browser_pool=browser_pool,
            pipeline=pipeline,
            dry_run=dry_run,
            force=force,
        )

    def manage(
        self,
        action: str,
        *,
        task_type: Optional[str] = None,
        task_id: Optional[str] = None,
        user_message: Optional[str] = None,
        site_id: Optional[str] = None,
        cron: Optional[str] = None,
        name: Optional[str] = None,
        report_type: Optional[str] = None,
        feishu_push: Optional[bool] = None,
        max_items: Optional[int] = None,
        hermes_prompt: Optional[str] = None,
        prompt: Optional[str] = None,
        push_mode: Optional[str] = None,
        push_excerpt_max: Optional[int] = None,
        feishu_webhook_url: Optional[str] = None,
        feishu_webhook_secret: Optional[str] = None,
        top_n: Optional[int] = None,
        days: Optional[int] = None,
        feishu_user_ids: Optional[list[str] | str] = None,
        enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        act = (action or "").strip().lower()
        if act == "list":
            return self.list_tasks()
        if act == "get":
            if not task_id:
                return {"ok": False, "error": "get 需要 task_id"}
            return self.get_task(task_id)
        if act == "parse":
            if not user_message:
                return {"ok": False, "error": "parse 需要 user_message"}
            if (task_type or "").strip() in (TASK_TYPE_HERMES, TASK_TYPE_HERMES_SUMMARY):
                return parse_hermes_schedule_intent(user_message)
            if (task_type or "").strip() == TASK_TYPE_BIM_DAILY_ANALYSIS:
                return parse_bim_analysis_schedule_intent(user_message)
            return parse_schedule_intent(user_message)
        if act == "create_from_intent":
            if not user_message:
                return {"ok": False, "error": "create_from_intent 需要 user_message"}
            if (task_type or "").strip() in (TASK_TYPE_HERMES, TASK_TYPE_HERMES_SUMMARY):
                return self.create_hermes_from_intent(user_message)
            if (task_type or "").strip() == TASK_TYPE_BIM_DAILY_ANALYSIS:
                return self.create_bim_analysis_from_intent(user_message)
            return self.create_from_intent(user_message)
        if act == "create":
            if not cron:
                return {"ok": False, "error": "create 需要 cron"}
            effective_type = (task_type or TASK_TYPE_HERMES_SUMMARY).strip()
            if is_hermes_task_type(effective_type):
                if not (hermes_prompt or prompt):
                    return {"ok": False, "error": "Hermes 任务 create 需要 hermes_prompt / prompt"}
                return self.create_hermes_summary_task(
                    cron=cron,
                    name=name,
                    hermes_prompt=hermes_prompt,
                    prompt=prompt,
                    push_mode=push_mode or "excerpt",
                    push_excerpt_max=push_excerpt_max if push_excerpt_max is not None else 1500,
                    feishu_webhook_url=feishu_webhook_url,
                    feishu_webhook_secret=feishu_webhook_secret,
                    feishu_push=True if feishu_push is None else feishu_push,
                    enabled=True if enabled is None else enabled,
                    task_id=task_id,
                )
            elif effective_type == TASK_TYPE_BIM_DAILY_ANALYSIS:
                return self.create_bim_analysis_task(
                    cron=cron,
                    name=name,
                    top_n=top_n if top_n is not None else 10,
                    feishu_webhook_url=feishu_webhook_url,
                    feishu_webhook_secret=feishu_webhook_secret,
                    feishu_push=True if feishu_push is None else feishu_push,
                    enabled=True if enabled is None else enabled,
                    task_id=task_id,
                )
            elif effective_type in (TASK_TYPE_BIM_DAILY_BRIEF, TASK_TYPE_BIM_WEEKLY_BRIEF):
                return self.create_brief_task(
                    task_type=effective_type,
                    cron=cron,
                    name=name,
                    top_n=top_n if top_n is not None else 10,
                    feishu_webhook_url=feishu_webhook_url,
                    feishu_webhook_secret=feishu_webhook_secret,
                    feishu_push=True if feishu_push is None else feishu_push,
                    enabled=True if enabled is None else enabled,
                    task_id=task_id,
                )
            elif effective_type == TASK_TYPE_ENGINEERING_LLM_REPORT:
                if not feishu_user_ids:
                    return {"ok": False, "error": "engineering_llm_report 需要 feishu_user_ids"}
                return self.create_engineering_llm_report_task(
                    cron=cron,
                    name=name,
                    feishu_user_ids=feishu_user_ids,
                    days=days if days is not None else 1,
                    top_n=top_n if top_n is not None else 10,
                    feishu_push=True if feishu_push is None else feishu_push,
                    enabled=True if enabled is None else enabled,
                    task_id=task_id,
                )
            elif not site_id:
                return {"ok": False, "error": "incremental_sync create 需要 site_id 与 cron"}
            return self.create_task(
                task_type=effective_type,
                site_id=site_id,
                cron=cron,
                name=name,
                report_type=report_type or "site",
                feishu_push=True if feishu_push is None else feishu_push,
                max_items=max_items if max_items is not None else 10,
                hermes_prompt=hermes_prompt,
                feishu_webhook_url=feishu_webhook_url,
                feishu_webhook_secret=feishu_webhook_secret,
                enabled=True if enabled is None else enabled,
                task_id=task_id,
            )
        if act == "update":
            if not task_id:
                return {"ok": False, "error": "update 需要 task_id"}
            fields: dict[str, Any] = {}
            if site_id is not None:
                fields["site_id"] = site_id
            if cron is not None:
                fields["cron"] = cron
            if name is not None:
                fields["name"] = name
            if report_type is not None:
                fields["report_type"] = report_type
            if feishu_push is not None:
                fields["feishu_push"] = feishu_push
            if max_items is not None:
                fields["max_items"] = max_items
            if task_type is not None:
                fields["task_type"] = task_type
            if hermes_prompt is not None:
                fields["hermes_prompt"] = hermes_prompt
            if prompt is not None:
                fields["prompt"] = prompt
            if push_mode is not None:
                fields["push_mode"] = push_mode
            if push_excerpt_max is not None:
                fields["push_excerpt_max"] = push_excerpt_max
            if feishu_webhook_url is not None:
                fields["feishu_webhook_url"] = feishu_webhook_url
            if feishu_webhook_secret is not None:
                fields["feishu_webhook_secret"] = feishu_webhook_secret
            if top_n is not None:
                fields["top_n"] = top_n
            if days is not None:
                fields["days"] = days
            if feishu_user_ids is not None:
                fields["feishu_user_ids"] = feishu_user_ids
            if enabled is not None:
                fields["enabled"] = enabled
            return self.update_task(task_id, **fields)
        if act == "delete":
            if not task_id:
                return {"ok": False, "error": "delete 需要 task_id"}
            return self.delete_task(task_id)
        return {
            "ok": False,
            "error": "未知 action；支持 list/get/parse/create/create_from_intent/update/delete",
        }


_service: Optional[ChatScheduleService] = None


def get_chat_schedule_service() -> ChatScheduleService:
    global _service
    if _service is None:
        _service = ChatScheduleService()
    return _service
