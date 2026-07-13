"""Chrome 插件爬取工作流录制会话服务。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.core.crawl_recorder_compiler import compile_recorded_workflow, suggest_pagination_from_html
from src.core.dom_selector_hints import analyze_list_dom
from src.core.site_sync import get_site_by_id
from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXTENSION_DIR = PROJECT_ROOT / "extensions" / "crawl-workflow-recorder"
EXTENSION_VERSION = "1.0.0"
SESSION_TTL_SECONDS = 3600


@dataclass
class RecorderSession:
    session_id: str
    site_id: str
    entry_url: str
    created_at: float
    expires_at: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    html_snapshot: str = ""
    page_url: str = ""
    list_analysis: Optional[dict[str, Any]] = None
    list_page_confirmed: bool = False
    list_page_confirmed_url: str = ""
    extension_connected: bool = False
    extension_last_ping: float = 0.0
    page_connected: bool = False
    extension_version: str = ""
    recording_stopped: bool = False
    site_name: str = ""

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def touch_extension(self) -> None:
        self.extension_connected = True
        self.extension_last_ping = time.time()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "site_id": self.site_id,
            "entry_url": self.entry_url,
            "site_name": self.site_name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "step_count": len(self.steps),
            "steps": self.steps,
            "extension_connected": self.extension_connected,
            "extension_last_ping": self.extension_last_ping or None,
            "page_connected": self.page_connected,
            "extension_version": self.extension_version or None,
            "recording_stopped": self.recording_stopped,
            "page_url": self.page_url or None,
            "has_html_snapshot": bool(self.html_snapshot),
            "list_analysis": self.list_analysis,
            "list_page_confirmed": self.list_page_confirmed,
            "list_page_confirmed_url": self.list_page_confirmed_url or None,
        }


class CrawlRecorderService:
    def __init__(self) -> None:
        self._sessions: dict[str, RecorderSession] = {}
        self._lock = threading.Lock()

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if s.expires_at < now]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _get_session(self, session_id: str) -> Optional[RecorderSession]:
        self._purge_expired()
        session = self._sessions.get(session_id)
        if session and session.is_expired():
            self._sessions.pop(session_id, None)
            return None
        return session

    def get_extension_info(self) -> dict[str, Any]:
        path = EXTENSION_DIR.resolve()
        return {
            "ok": True,
            "version": EXTENSION_VERSION,
            "path": str(path),
            "load_unpacked_path": str(path),
            "chrome_extensions_url": "chrome://extensions",
            "instructions": [
                "打开 chrome://extensions",
                "开启右上角「开发者模式」",
                f"点击「加载已解压的扩展程序」，选择目录：{path}",
                "在工作流页「浏览器录制」标签点击「开始录制」",
            ],
        }

    def create_session(self, *, site_id: str, entry_url: str = "") -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "error": f"站点不存在: {site_id}"}

        url = (entry_url or site.get("url") or "").strip()
        now = time.time()
        session_id = uuid.uuid4().hex
        session = RecorderSession(
            session_id=session_id,
            site_id=site_id,
            entry_url=url,
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
            site_name=site.get("name") or site_id,
        )
        with self._lock:
            self._purge_expired()
            self._sessions[session_id] = session

        return {
            "ok": True,
            "session_id": session_id,
            "site_id": site_id,
            "entry_url": url,
            "expires_at": session.expires_at,
            "extension_query": f"crawl_recorder_session={session_id}",
            "session": session.to_public_dict(),
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        return {"ok": True, "session": session.to_public_dict()}

    def list_waiting_sessions(self, *, site_id: str = "") -> dict[str, Any]:
        """返回 workflow 页已连接、等待插件接入的会话（供扩展轮询发现）。"""
        self._purge_expired()
        waiting: list[dict[str, Any]] = []
        with self._lock:
            for session in self._sessions.values():
                if session.is_expired():
                    continue
                if site_id and session.site_id != site_id:
                    continue
                if session.page_connected and not session.extension_connected:
                    waiting.append(session.to_public_dict())
        waiting.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
        return {"ok": True, "sessions": waiting}

    def ping_session(self, session_id: str, *, source: str = "extension") -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        if source == "page":
            session.page_connected = True
        else:
            session.touch_extension()
        return {"ok": True, "session_id": session_id, "source": source}

    def mark_extension_ready(
        self, session_id: str, *, version: str = ""
    ) -> Optional[RecorderSession]:
        session = self._get_session(session_id)
        if not session:
            return None
        session.touch_extension()
        if version:
            session.extension_version = version
        return session

    def mark_page_ready(self, session_id: str) -> Optional[RecorderSession]:
        session = self._get_session(session_id)
        if not session:
            return None
        session.page_connected = True
        return session

    def mark_client_disconnected(self, session_id: str, *, role: str) -> None:
        session = self._get_session(session_id)
        if not session:
            return
        if role == "page":
            session.page_connected = False
        elif role == "extension":
            session.extension_connected = False

    def mark_recording_stopped(self, session_id: str) -> Optional[RecorderSession]:
        session = self._get_session(session_id)
        if not session:
            return None
        session.recording_stopped = True
        session.touch_extension()
        return session

    def append_step(self, session_id: str, step: dict[str, Any]) -> dict[str, Any]:
        """追加单条步骤（WebSocket 实时路径）。"""
        return self.append_steps(session_id, [step])

    def append_steps(self, session_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}
        if not isinstance(steps, list):
            return {"ok": False, "error": "steps 必须为数组"}

        for step in steps:
            if isinstance(step, dict):
                session.steps.append(step)
        session.touch_extension()
        return {
            "ok": True,
            "step_count": len(session.steps),
            "session": session.to_public_dict(),
        }

    def analyze_session(
        self,
        session_id: str,
        *,
        html: str = "",
        url: str = "",
        confirmed_list_page: bool = False,
        list_hints: Optional[dict[str, Any]] = None,
        pagination: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}

        snapshot = (html or session.html_snapshot or "").strip()
        page_url = (url or session.page_url or session.entry_url or "").strip()
        if html:
            session.html_snapshot = html
        if url:
            session.page_url = url

        if confirmed_list_page:
            session.list_page_confirmed = True
            session.list_page_confirmed_url = page_url

        analysis = analyze_list_dom(snapshot, url=page_url)

        if list_hints:
            analysis["selectors"] = {
                "strategy": list_hints.get("strategy") or "dom",
                "container": list_hints.get("container") or "",
                "item": list_hints.get("item") or "",
                "title": list_hints.get("title") or "a",
                "link": list_hints.get("link") or "a",
            }
            analysis["ok"] = True
            if list_hints.get("itemCount"):
                analysis["item_count"] = list_hints["itemCount"]

        for step in reversed(session.steps):
            if (step.get("type") or step.get("action")) != "mark_list_page":
                continue
            hints = (step.get("params") or {}).get("list_hints") or {}
            if hints and not list_hints:
                analysis["selectors"] = {
                    "strategy": hints.get("strategy") or "dom",
                    "container": hints.get("container") or "",
                    "item": hints.get("item") or "",
                    "title": hints.get("title") or "a",
                    "link": hints.get("link") or "a",
                }
                analysis["ok"] = True
            ext_pagination = (step.get("params") or {}).get("pagination")
            if ext_pagination:
                analysis["extension_pagination"] = ext_pagination
            break

        if pagination:
            analysis["extension_pagination"] = pagination

        pagination_result = suggest_pagination_from_html(snapshot)
        if analysis.get("extension_pagination"):
            pagination_result.update(analysis["extension_pagination"])
        session.list_analysis = {
            **analysis,
            "confirmed": session.list_page_confirmed,
            "confirmed_url": session.list_page_confirmed_url or None,
            "pagination": pagination_result,
            "suggested_nodes": {
                "extract_list": analysis.get("selectors") or {},
                "paginate": pagination_result,
                "extract_detail": {
                    "fetch_detail": True,
                    "strategy": "dom",
                    "title_selector": (analysis.get("selectors") or {}).get("title") or "h1",
                    "content_selector": ".content, #content, main",
                },
            },
        }
        return {
            "ok": True,
            "analysis": session.list_analysis,
            "session": session.to_public_dict(),
        }

    def compile_session(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}

        workflow = compile_recorded_workflow(
            site_id=session.site_id,
            site_name=session.site_name,
            entry_url=session.entry_url,
            steps=session.steps,
            list_analysis=session.list_analysis,
            html_snapshot=session.html_snapshot,
        )
        return {
            "ok": True,
            "workflow": workflow,
            "step_count": len(session.steps),
            "session": session.to_public_dict(),
        }

    def import_session(self, session_id: str, *, save: bool = False) -> dict[str, Any]:
        compiled = self.compile_session(session_id)
        if not compiled.get("ok"):
            return compiled

        session = self._get_session(session_id)
        if not session:
            return {"ok": False, "error": "会话不存在或已过期"}

        workflow = compiled["workflow"]
        result: dict[str, Any] = {
            "ok": True,
            "workflow": workflow,
            "saved": False,
            "session": session.to_public_dict(),
        }
        if save:
            save_result = get_crawl_rule_workflow_service().save_workflow(
                session.site_id, workflow
            )
            result["saved"] = bool(save_result.get("ok"))
            result["save_result"] = save_result
            if not save_result.get("ok"):
                result["ok"] = False
                result["error"] = save_result.get("error") or "保存失败"
        return result


_service: Optional[CrawlRecorderService] = None


def get_crawl_recorder_service() -> CrawlRecorderService:
    global _service
    if _service is None:
        _service = CrawlRecorderService()
    return _service
