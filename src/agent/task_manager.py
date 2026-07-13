"""爬取任务管理：内存 + MongoDB 持久化 + 事件流。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from src.agent.crawl_logger import get_agent_crawl_logger
from src.agent.events import LOG, make_event
from src.agent.executor import AgentExecutor
from src.agent.models import AgentEvent, CrawlPlan, CrawlTaskRecord, TaskStatus
from src.agent.planner import AgentPlanner
from src.core.browser import BrowserPool
from src.core.config import get_settings
from src.core.pipeline import Pipeline
from src.core.site_sync import get_site_by_id, load_enabled_sites

logger = logging.getLogger(__name__)

COLLECTION = "crawl_tasks"


class CrawlTaskManager:
    """管理 Agent 爬取任务的生命周期与事件流。"""

    def __init__(self) -> None:
        self._tasks: dict[str, CrawlTaskRecord] = {}
        self._queues: dict[str, asyncio.Queue[Optional[AgentEvent]]] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._mongo_coll = None
        self._connect_mongo()

    def _connect_mongo(self) -> None:
        settings = get_settings()
        if not settings.mongodb_uri:
            return
        try:
            from pymongo import MongoClient
            from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

            client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            db = client[settings.mongodb_db]
            self._mongo_coll = db[COLLECTION]
            self._mongo_coll.create_index("task_id", unique=True)
            self._mongo_coll.create_index([("created_at", -1)])
            logger.info("Agent 任务存储: MongoDB %s", COLLECTION)
        except (ConnectionFailure, ServerSelectionTimeoutError, ImportError) as e:
            logger.warning("Agent 任务 MongoDB 不可用，仅内存: %s", e)

    def _persist(self, task: CrawlTaskRecord) -> None:
        task.updated_at = datetime.now(timezone.utc)
        if self._mongo_coll is None:
            return
        try:
            doc = task.model_dump(mode="json")
            doc["status"] = task.status.value
            if task.plan:
                doc["plan"] = task.plan.model_dump(mode="json")
            doc["events"] = [e.to_sse_dict() for e in task.events[-100:]]
            self._mongo_coll.update_one(
                {"task_id": task.task_id},
                {"$set": doc},
                upsert=True,
            )
        except Exception as e:
            logger.warning("持久化任务失败: %s", e)

    def _push_event(self, task_id: str, event: AgentEvent) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.events.append(event)
            self._persist(task)
            try:
                get_agent_crawl_logger().write_event(
                    event,
                    task_id=task_id,
                    site_id=task.site_id,
                )
            except Exception as e:
                logger.warning("写入 agent-crawl.log 失败: %s", e)
        queue = self._queues.get(task_id)
        if queue:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _resolve_site(
        self,
        *,
        site_id: Optional[str] = None,
        url: Optional[str] = None,
    ) -> dict[str, Any]:
        if site_id:
            site = get_site_by_id(site_id)
            if site:
                return site
            if url:
                return {
                    "id": site_id,
                    "name": site_id,
                    "url": url,
                    "adapter": "generic",
                    "enabled": True,
                }
            raise ValueError(f"站点不存在: {site_id}")

        if url:
            custom_id = f"custom_{uuid.uuid4().hex[:8]}"
            return {
                "id": custom_id,
                "name": url,
                "url": url,
                "adapter": "generic",
                "enabled": True,
            }
        raise ValueError("需提供 site_id 或 url")

    async def create_task(
        self,
        *,
        site_id: Optional[str] = None,
        url: Optional[str] = None,
        max_items: int = 10,
    ) -> CrawlTaskRecord:
        site = self._resolve_site(site_id=site_id, url=url)
        task_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]

        task = CrawlTaskRecord(
            task_id=task_id,
            site_id=site["id"],
            site_name=site.get("name", site["id"]),
            base_url=site.get("url", ""),
            status=TaskStatus.PENDING,
        )
        self._tasks[task_id] = task
        self._queues[task_id] = asyncio.Queue(maxsize=500)
        self._cancel_flags[task_id] = False
        self._persist(task)

        bg = asyncio.create_task(self._run_task(task_id, site, max_items=max_items))
        self._running[task_id] = bg
        return task

    async def _run_task(
        self,
        task_id: str,
        site: dict[str, Any],
        *,
        max_items: int,
    ) -> None:
        task = self._tasks[task_id]
        pool = BrowserPool(headless=True)
        pipeline = Pipeline(settings=get_settings())

        def emit(event: AgentEvent) -> None:
            self._push_event(task_id, event)

        try:
            task.status = TaskStatus.PLANNING
            self._persist(task)
            emit(make_event(LOG, f"任务启动: {task.site_name}", level="info"))

            await pool.start()

            planner = AgentPlanner(pool, emit=emit)
            plan = await planner.plan(
                site_id=site["id"],
                site_name=site.get("name", site["id"]),
                base_url=site.get("url", ""),
                adapter_name=site.get("adapter"),
            )
            task.plan = plan
            task.status = TaskStatus.RUNNING
            self._persist(task)
            emit(make_event(LOG, "爬取计划已生成，开始执行", level="info", data={"plan": plan.model_dump(mode="json")}))

            if self._cancel_flags.get(task_id):
                task.status = TaskStatus.CANCELLED
                self._persist(task)
                emit(make_event(LOG, "任务已取消", level="warn"))
                return

            executor = AgentExecutor(
                pool,
                pipeline,
                site,
                plan,
                max_items=max_items,
                emit=emit,
                cancelled=lambda: self._cancel_flags.get(task_id, False),
            )
            result = await executor.run()
            task.result = result
            if result.get("success"):
                task.status = TaskStatus.COMPLETED
            else:
                task.status = TaskStatus.FAILED
                task.error = result.get("error")
            self._persist(task)

        except Exception as e:
            logger.exception("任务 %s 异常", task_id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            self._persist(task)
            emit(make_event(LOG, f"任务异常: {e}", level="error"))
        finally:
            await pool.stop()
            queue = self._queues.get(task_id)
            if queue:
                await queue.put(None)
            self._running.pop(task_id, None)

    def get_task(self, task_id: str) -> Optional[CrawlTaskRecord]:
        task = self._tasks.get(task_id)
        if task:
            return task
        if self._mongo_coll is not None:
            doc = self._mongo_coll.find_one({"task_id": task_id})
            if doc:
                return self._doc_to_task(doc)
        return None

    def _doc_to_task(self, doc: dict[str, Any]) -> CrawlTaskRecord:
        plan_data = doc.get("plan")
        plan = CrawlPlan.model_validate(plan_data) if plan_data else None
        events = [
            AgentEvent(
                type=e["type"],
                message=e["message"],
                level=e.get("level", "info"),
                timestamp=datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                if isinstance(e.get("timestamp"), str)
                else datetime.now(timezone.utc),
                data=e.get("data"),
            )
            for e in doc.get("events", [])
        ]
        return CrawlTaskRecord(
            task_id=doc["task_id"],
            site_id=doc["site_id"],
            site_name=doc.get("site_name", ""),
            base_url=doc.get("base_url", ""),
            status=TaskStatus(doc.get("status", "pending")),
            plan=plan,
            events=events,
            result=doc.get("result"),
            error=doc.get("error"),
            created_at=doc.get("created_at", datetime.now(timezone.utc)),
            updated_at=doc.get("updated_at", datetime.now(timezone.utc)),
        )

    def list_tasks(self, limit: int = 20) -> list[CrawlTaskRecord]:
        items = sorted(
            self._tasks.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )[:limit]
        if self._mongo_coll is not None and len(items) < limit:
            seen = {t.task_id for t in items}
            for doc in self._mongo_coll.find().sort("created_at", -1).limit(limit):
                tid = doc.get("task_id")
                if tid and tid not in seen:
                    items.append(self._doc_to_task(doc))
                    seen.add(tid)
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items[:limit]

    async def stream_events(self, task_id: str) -> AsyncIterator[AgentEvent]:
        """SSE 事件流：先回放已有事件，再实时推送。"""
        task = self.get_task(task_id)
        if not task:
            return

        replay_count = len(task.events)
        for event in task.events:
            yield event

        queue = self._queues.get(task_id)
        if not queue:
            return

        # 跳过队列中与历史回放重复的事件
        drained = 0
        while drained < replay_count:
            try:
                ev = queue.get_nowait()
                if ev is None:
                    return
                drained += 1
            except asyncio.QueueEmpty:
                break

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=120.0)
            except asyncio.TimeoutError:
                yield make_event(LOG, "心跳", level="debug")
                continue
            if event is None:
                break
            yield event
            if event.type in ("done", "error") and task.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                break

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "message": "任务不存在"}
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return {"ok": False, "message": f"任务已结束: {task.status.value}"}
        self._cancel_flags[task_id] = True
        task.status = TaskStatus.CANCELLED
        self._persist(task)
        self._push_event(task_id, make_event(LOG, "收到取消请求", level="warn"))
        return {"ok": True, "message": "已发送取消信号", "task_id": task_id}

    def list_sites(self) -> list[dict[str, str]]:
        return [
            {"id": s["id"], "name": s.get("name", s["id"]), "url": s.get("url", "")}
            for s in load_enabled_sites()
        ]


_manager: Optional[CrawlTaskManager] = None


def get_task_manager() -> CrawlTaskManager:
    global _manager
    if _manager is None:
        _manager = CrawlTaskManager()
    return _manager
