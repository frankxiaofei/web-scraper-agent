"""PDF 下载与文本提取（供电建 getInfo pictureUrl 等场景复用）。"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_PDF_CHARS = 50_000


def is_pdf_bytes(data: bytes) -> bool:
    return bool(data) and data[:4] == b"%PDF"


def extract_pdf_text(pdf_bytes: bytes, *, max_chars: int = DEFAULT_MAX_PDF_CHARS) -> str:
    """从 PDF 二进制提取纯文本。"""
    if not pdf_bytes:
        return ""
    try:
        import PyPDF2
    except ImportError as exc:
        raise RuntimeError("缺少 PyPDF2 依赖，请 pip install PyPDF2") from exc

    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars]
    return text


async def download_url_bytes(
    url: str,
    *,
    referer: str = "",
    timeout_seconds: float = 30.0,
) -> bytes | None:
    """HTTP GET 下载二进制内容。"""
    if not url:
        return None
    import httpx

    from src.core.http_api_client import create_http_client

    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer

    try:
        async with create_http_client() as client:
            client.timeout = httpx.Timeout(timeout_seconds, connect=15.0)
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content = response.content
    except Exception as exc:
        logger.warning("PDF/附件下载失败 url=%s: %s", url[:120], exc)
        return None

    if len(content) < 100:
        logger.debug("下载内容过短 (%d bytes): %s", len(content), url[:120])
        return None
    return content


async def download_pdf_text(
    url: str,
    *,
    referer: str = "",
    max_chars: int = DEFAULT_MAX_PDF_CHARS,
) -> dict[str, Any]:
    """下载 URL 并提取 PDF 文本。"""
    raw = await download_url_bytes(url, referer=referer)
    if raw is None:
        return {"ok": False, "url": url, "error": "download_failed"}
    if not is_pdf_bytes(raw):
        return {"ok": False, "url": url, "error": "not_pdf", "size": len(raw)}
    try:
        text = extract_pdf_text(raw, max_chars=max_chars)
    except Exception as exc:
        logger.warning("PDF 文本提取失败 url=%s: %s", url[:120], exc)
        return {"ok": False, "url": url, "error": str(exc)}
    return {
        "ok": bool(text.strip()),
        "url": url,
        "content_text": text,
        "size": len(raw),
        "text_len": len(text),
    }
