"""Agent 爬取文件日志：统一写入 data/agent-crawl.log。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.agent.models import AgentEvent
from src.core.config import get_settings

_LEVEL_MAP = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "success": logging.INFO,
    "debug": logging.DEBUG,
}

_MAIN_LOG_NAME = "agent-crawl.log"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


class _AgentLogFilter(logging.Filter):
    """确保 extra 字段存在，避免格式化报错。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in ("task_id", "site_id", "event_type"):
            if not hasattr(record, key):
                setattr(record, key, "-")
        return True


class AgentCrawlFileLogger:
    """线程安全的 Agent 爬取文件日志（RotatingFileHandler + 按日归档）。"""

    _instance: Optional[AgentCrawlFileLogger] = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        settings = get_settings()
        self.log_dir = Path(settings.data_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.main_log_path = self.log_dir / _MAIN_LOG_NAME
        self._write_lock = threading.Lock()
        self._daily_date: Optional[str] = None
        self._daily_path: Optional[Path] = None

        self._logger = logging.getLogger("agent.crawl.file")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                self.main_log_path,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] task_id=%(task_id)s site_id=%(site_id)s "
                    "[%(event_type)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            handler.addFilter(_AgentLogFilter())
            self._logger.addHandler(handler)

    def _daily_log_path(self) -> Path:
        today = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
        if self._daily_date != today:
            self._daily_date = today
            self._daily_path = self.log_dir / f"agent-crawl-{today}.log"
        assert self._daily_path is not None
        return self._daily_path

    def _format_line(
        self,
        event: AgentEvent,
        *,
        task_id: str,
        site_id: str,
    ) -> str:
        ts = event.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        level = _LEVEL_MAP.get(event.level, logging.INFO)
        level_name = logging.getLevelName(level)
        return (
            f"{ts} [{level_name}] task_id={task_id} site_id={site_id} "
            f"[{event.type}] {event.message}\n"
        )

    def write_event(
        self,
        event: AgentEvent,
        *,
        task_id: str,
        site_id: str,
    ) -> None:
        """追加一条 AgentEvent 到 agent-crawl.log（及当日归档文件）。"""
        if event.type == "log" and event.message == "心跳":
            return

        level = _LEVEL_MAP.get(event.level, logging.INFO)
        with self._write_lock:
            self._logger.log(
                level,
                event.message,
                extra={
                    "task_id": task_id,
                    "site_id": site_id,
                    "event_type": event.type,
                },
            )
            line = self._format_line(event, task_id=task_id, site_id=site_id)
            daily_path = self._daily_log_path()
            with daily_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def tail_lines(self, n: int = 500) -> list[str]:
        """读取主日志文件尾部 N 行。"""
        if n < 1:
            return []
        if not self.main_log_path.exists():
            return []

        chunk_size = 8192
        lines: list[bytes] = []
        with self.main_log_path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            buf = b""
            while pos > 0 and len(lines) <= n:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
                lines = buf.splitlines()
            if len(lines) > n:
                lines = lines[-n:]

        return [line.decode("utf-8", errors="replace") for line in lines]

    def tail_text(self, n: int = 500) -> str:
        return "\n".join(self.tail_lines(n))


_file_logger: Optional[AgentCrawlFileLogger] = None
_file_logger_lock = threading.Lock()


def get_agent_crawl_logger() -> AgentCrawlFileLogger:
    global _file_logger
    if _file_logger is None:
        with _file_logger_lock:
            if _file_logger is None:
                _file_logger = AgentCrawlFileLogger()
    return _file_logger
