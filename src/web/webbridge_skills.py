"""Kimi WebBridge skill 工具 — 通过本机 daemon 控制用户真实浏览器。"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger(__name__)

WEBBRIDGE_DEFAULT_BIN = Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge"
WEBBRIDGE_INSTALL_URL = "https://kimi-web-img.moonshot.cn/webbridge/install.sh"
WEBBRIDGE_EXTENSION_URL = "https://kimi.com/features/webbridge"
DEFAULT_DAEMON_URL = "http://127.0.0.1:10086"
DEFAULT_SESSION = "crawl-agent"
SNAPSHOT_MAX_CHARS = 6000
HTML_DEFAULT_MAX_CHARS = 50_000
EXTRACT_DEFAULT_MAX_ITEMS = 50
COMMAND_TIMEOUT = 90.0
STATUS_TIMEOUT = 15.0
EVALUATE_MAX_CODE_CHARS = 8000
EVALUATE_RESULT_MAX_CHARS = 8000
WAIT_MAX_SECONDS = 30.0
SCREENSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "webbridge_screenshots"

INSTALL_HINT = (
    "Kimi WebBridge 未安装或不可用。安装步骤：\n"
    f"1. curl -fsSL {WEBBRIDGE_INSTALL_URL} | bash\n"
    f"2. ~/.kimi-webbridge/bin/kimi-webbridge start\n"
    f"3. 在浏览器安装并连接扩展：{WEBBRIDGE_EXTENSION_URL}\n"
    "4. 运行 skill_webbridge_check 确认 running=true 且 extension_connected=true"
)

_subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
_urlopen: Callable[..., Any] = urllib.request.urlopen


def _daemon_url() -> str:
    return (os.environ.get("KIMI_WEBBRIDGE_URL") or DEFAULT_DAEMON_URL).rstrip("/")


def resolve_webbridge_binary() -> Optional[str]:
    """解析 kimi-webbridge CLI 路径（PATH 或默认安装目录）。"""
    found = shutil.which("kimi-webbridge")
    if found:
        return found
    if WEBBRIDGE_DEFAULT_BIN.is_file():
        return str(WEBBRIDGE_DEFAULT_BIN)
    return None


def _parse_status_output(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"raw": text}


def check_webbridge() -> dict[str, Any]:
    """检测 WebBridge daemon 与扩展是否可用。"""
    binary = resolve_webbridge_binary()
    if not binary:
        return {
            "ok": False,
            "available": False,
            "error": "kimi-webbridge 命令未找到",
            "install_hint": INSTALL_HINT,
        }
    try:
        proc = _subprocess_run(
            [binary, "status"],
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "available": False,
            "error": "kimi-webbridge 命令未找到",
            "install_hint": INSTALL_HINT,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "available": False,
            "error": "kimi-webbridge status 超时",
            "install_hint": INSTALL_HINT,
        }

    status = _parse_status_output(proc.stdout)
    if proc.returncode != 0 and not status:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        return {
            "ok": False,
            "available": False,
            "error": err,
            "install_hint": INSTALL_HINT,
        }

    running = bool(status.get("running"))
    extension_connected = bool(status.get("extension_connected"))
    healthy = running and extension_connected
    result: dict[str, Any] = {
        "ok": healthy,
        "available": healthy,
        "running": running,
        "extension_connected": extension_connected,
        "port": status.get("port"),
        "version": status.get("version"),
        "extension_version": status.get("extension_version"),
        "daemon_url": _daemon_url(),
    }
    if not healthy:
        if not running:
            result["error"] = "daemon 未运行，请执行 ~/.kimi-webbridge/bin/kimi-webbridge start"
        elif not extension_connected:
            result["error"] = (
                "浏览器扩展未连接，请在浏览器中打开 Kimi WebBridge 扩展并连接"
            )
        result["install_hint"] = INSTALL_HINT
    return result


def _daemon_error_text(data: dict[str, Any]) -> str:
    err = data.get("error")
    if err is None:
        return ""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return str(err)


def _unwrap_daemon_payload(data: dict[str, Any]) -> dict[str, Any]:
    """兼容 daemon 新旧响应：新版 {ok, data:{...}}，旧版扁平字段。"""
    if data.get("ok") is True and isinstance(data.get("data"), dict):
        return data["data"]
    return data


def daemon_command(
    action: str,
    args: Optional[dict[str, Any]] = None,
    *,
    session: Optional[str] = None,
    timeout: float = COMMAND_TIMEOUT,
) -> dict[str, Any]:
    """向 WebBridge daemon 发送 command（HTTP POST）。"""
    check = check_webbridge()
    if not check.get("available"):
        return {
            "ok": False,
            "error": check.get("error") or "WebBridge 不可用",
            "install_hint": check.get("install_hint", INSTALL_HINT),
            "webbridge_check": check,
        }

    body: dict[str, Any] = {"action": action, "args": args or {}}
    if session:
        body["session"] = session

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_daemon_url()}/command",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return {"ok": False, "error": f"daemon HTTP {exc.code}: {detail[:500]}"}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"无法连接 WebBridge daemon ({_daemon_url()}): {exc.reason}",
            "install_hint": INSTALL_HINT,
        }
    except TimeoutError:
        return {"ok": False, "error": f"WebBridge 命令超时 ({timeout}s): {action}"}

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"daemon 返回非 JSON: {raw[:300]}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "daemon 返回格式异常", "raw": data}

    if data.get("ok") is False:
        err_text = _daemon_error_text(data) or "daemon 命令失败"
        out: dict[str, Any] = {"ok": False, "error": err_text, "daemon": data}
        if "Please update the Kimi WebBridge extension" in err_text:
            out["extension_update_url"] = WEBBRIDGE_EXTENSION_URL
        return out

    err_text = _daemon_error_text(data)
    if err_text and data.get("ok") is not True:
        out = {"ok": False, "error": err_text, "daemon": data}
        if "Please update the Kimi WebBridge extension" in err_text:
            out["extension_update_url"] = WEBBRIDGE_EXTENSION_URL
        return out

    payload = _unwrap_daemon_payload(data)
    if payload.get("success") is False:
        return {"ok": False, "error": payload.get("message") or "daemon 命令失败", "daemon": data}

    return {"ok": True, **payload}


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 3] + "...", True


def _summarize_snapshot_tree(tree: Any, max_chars: int = SNAPSHOT_MAX_CHARS) -> dict[str, Any]:
    if tree is None:
        return {"tree": "", "truncated": False, "char_count": 0}
    if isinstance(tree, (dict, list)):
        text = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(tree)
    truncated_text, truncated = _truncate_text(text, max_chars)
    return {
        "tree": truncated_text,
        "truncated": truncated,
        "char_count": len(text),
    }


def _session_name(session_id: Optional[str]) -> str:
    sid = (session_id or "").strip()
    return sid or DEFAULT_SESSION


def webbridge_navigate(
    url: str,
    *,
    session_id: Optional[str] = None,
    new_tab: bool = True,
    group_title: Optional[str] = None,
) -> dict[str, Any]:
    """打开 URL 并返回 snapshot 摘要。"""
    target = (url or "").strip()
    if not target:
        return {"ok": False, "error": "url 必填"}

    session = _session_name(session_id)
    nav_args: dict[str, Any] = {"url": target, "newTab": bool(new_tab)}
    if group_title:
        nav_args["group_title"] = group_title

    nav = daemon_command("navigate", nav_args, session=session)
    if not nav.get("ok"):
        return nav

    snap = daemon_command("snapshot", {}, session=session)
    if not snap.get("ok"):
        return {
            "ok": True,
            "url": nav.get("url") or target,
            "tab_id": nav.get("tabId"),
            "session_id": session,
            "snapshot_error": snap.get("error"),
        }

    summary = _summarize_snapshot_tree(snap.get("tree"))
    return {
        "ok": True,
        "url": snap.get("url") or nav.get("url") or target,
        "title": snap.get("title"),
        "tab_id": nav.get("tabId"),
        "session_id": session,
        "snapshot": summary,
    }


def _build_extract_js(
    *,
    selector: Optional[str],
    hint: Optional[str],
    max_items: int,
) -> str:
    sel = json.dumps(selector) if selector else "null"
    hint_json = json.dumps(hint or "")
    return f"""(() => {{
  const maxItems = {max_items};
  const selector = {sel};
  const hint = {hint_json}.toLowerCase();
  const items = [];
  const seen = new Set();
  const push = (title, href) => {{
    if (!href || seen.has(href) || items.length >= maxItems) return;
    const t = (title || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length < 2) return;
    if (hint && !t.toLowerCase().includes(hint) && !href.toLowerCase().includes(hint)) return;
    seen.add(href);
    items.push({{ title: t, url: href }});
  }};
  const abs = (href) => {{
    try {{ return new URL(href, location.href).href; }} catch (e) {{ return null; }}
  }};
  if (selector) {{
    document.querySelectorAll(selector).forEach((el) => {{
      const link = el.closest('a') || el.querySelector('a') || (el.tagName === 'A' ? el : null);
      const href = link ? abs(link.getAttribute('href')) : null;
      if (!href) return;
      push((link && link.innerText) || el.innerText || href, href);
    }});
  }} else {{
    document.querySelectorAll('a[href]').forEach((a) => {{
      const href = abs(a.getAttribute('href'));
      if (!href || href.startsWith('javascript:') || href === '#') return;
      push(a.innerText || a.getAttribute('title') || href, href);
    }});
  }}
  return JSON.stringify({{ items, count: items.length, selector: selector, hint: hint || null }});
}})()"""


def webbridge_extract_list(
    *,
    selector: Optional[str] = None,
    hint: Optional[str] = None,
    session_id: Optional[str] = None,
    max_items: int = EXTRACT_DEFAULT_MAX_ITEMS,
    generic: bool = False,
) -> dict[str, Any]:
    """从当前页面提取 {title, url} 列表。"""
    if not generic and not (selector or "").strip() and not (hint or "").strip():
        return {"ok": False, "error": "selector 或 hint 至少提供一个"}

    session = _session_name(session_id)
    limit = max(1, min(int(max_items), 200))
    code = _build_extract_js(
        selector=(selector or "").strip() or None,
        hint=(hint or "").strip() or None,
        max_items=limit,
    )
    result = daemon_command("evaluate", {"code": code}, session=session)
    if not result.get("ok"):
        return result

    raw_value = result.get("value")
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"提取结果解析失败: {raw_value[:200]}"}
    elif isinstance(raw_value, dict):
        parsed = raw_value
    else:
        return {"ok": False, "error": "提取结果格式异常"}

    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {"ok": False, "error": "未解析到 items 列表"}

    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if title and url:
            cleaned.append({"title": title, "url": url})

    return {
        "ok": True,
        "session_id": session,
        "items": cleaned,
        "item_count": len(cleaned),
        "selector": (selector or "").strip() or None,
        "hint": (hint or "").strip() or None,
        "generic": generic,
    }


def webbridge_extract_links_generic(
    *,
    session_id: Optional[str] = None,
    max_items: int = EXTRACT_DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    """通用 DOM 链接提取（无 crawl_rules 时的回退）。"""
    return webbridge_extract_list(
        session_id=session_id,
        max_items=max_items,
        generic=True,
    )


def webbridge_get_html(
    *,
    session_id: Optional[str] = None,
    max_chars: int = HTML_DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """获取当前页面 HTML（截断供规则调试）。"""
    session = _session_name(session_id)
    limit = max(1, min(int(max_chars), 200_000))
    code = (
        "(() => { const h = document.documentElement.outerHTML; "
        f"return JSON.stringify({{ html: h, length: h.length }}); }})()"
    )
    result = daemon_command("evaluate", {"code": code}, session=session)
    if not result.get("ok"):
        return result

    raw_value = result.get("value")
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": "HTML 提取结果解析失败"}

    html = ""
    full_len = 0
    if isinstance(parsed, dict):
        html = str(parsed.get("html") or "")
        full_len = int(parsed.get("length") or len(html))
    elif isinstance(raw_value, str):
        html = raw_value
        full_len = len(html)

    truncated_html, truncated = _truncate_text(html, limit)
    return {
        "ok": True,
        "session_id": session,
        "html": truncated_html,
        "html_length": full_len,
        "truncated": truncated,
        "max_chars": limit,
    }


def webbridge_snapshot(
    *,
    session_id: Optional[str] = None,
    max_chars: int = SNAPSHOT_MAX_CHARS,
) -> dict[str, Any]:
    """获取当前页面 URL、标题与 accessibility tree 摘要。"""
    session = _session_name(session_id)
    snap = daemon_command("snapshot", {}, session=session)
    if not snap.get("ok"):
        return snap
    summary = _summarize_snapshot_tree(snap.get("tree"), max_chars=max_chars)
    return {
        "ok": True,
        "session_id": session,
        "url": snap.get("url"),
        "title": snap.get("title"),
        "snapshot": summary,
    }


def webbridge_click(
    selector: str,
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """点击元素（@e ref 或 CSS selector）。"""
    sel = (selector or "").strip()
    if not sel:
        return {"ok": False, "error": "selector 必填"}
    session = _session_name(session_id)
    result = daemon_command("click", {"selector": sel}, session=session)
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "session_id": session,
        "selector": sel,
        "tag": result.get("tag"),
        "text": result.get("text"),
    }


def webbridge_fill(
    selector: str,
    value: str,
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """填写输入框或可编辑区域（clear-and-insert）。"""
    sel = (selector or "").strip()
    if not sel:
        return {"ok": False, "error": "selector 必填"}
    session = _session_name(session_id)
    result = daemon_command(
        "fill",
        {"selector": sel, "value": value if value is not None else ""},
        session=session,
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "session_id": session,
        "selector": sel,
        "mode": result.get("mode"),
        "tag": result.get("tag"),
    }


def _ensure_screenshot_dir() -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SCREENSHOT_DIR


def webbridge_screenshot(
    *,
    session_id: Optional[str] = None,
    format: Literal["png", "jpeg"] = "png",
    quality: int = 80,
    selector: Optional[str] = None,
) -> dict[str, Any]:
    """截图并保存到 data/webbridge_screenshots/，返回文件路径（不向 Agent 返回 base64）。"""
    session = _session_name(session_id)
    fmt = "jpeg" if format == "jpeg" else "png"
    args: dict[str, Any] = {"format": fmt}
    if fmt == "jpeg":
        args["quality"] = max(1, min(int(quality), 100))
    sel = (selector or "").strip()
    if sel:
        args["selector"] = sel

    result = daemon_command("screenshot", args, session=session, timeout=120.0)
    if not result.get("ok"):
        return result

    raw_data = result.get("data")
    if not raw_data:
        return {"ok": False, "error": "截图数据为空"}

    try:
        image_bytes = base64.b64decode(raw_data)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"截图 base64 解码失败: {exc}"}

    out_dir = _ensure_screenshot_dir()
    ext = "jpg" if fmt == "jpeg" else "png"
    filename = f"{session.replace('/', '_')}_{int(time.time() * 1000)}.{ext}"
    out_path = out_dir / filename
    out_path.write_bytes(image_bytes)

    return {
        "ok": True,
        "session_id": session,
        "image_path": str(out_path),
        "image_url": f"/api/browser/screenshot/{filename}",
        "format": fmt,
        "bytes": len(image_bytes),
        "selector": sel or None,
    }


def webbridge_evaluate(
    code: str,
    *,
    session_id: Optional[str] = None,
    max_result_chars: int = EVALUATE_RESULT_MAX_CHARS,
) -> dict[str, Any]:
    """在当前页面执行 JavaScript（支持 async/await）。"""
    snippet = (code or "").strip()
    if not snippet:
        return {"ok": False, "error": "code 必填"}
    if len(snippet) > EVALUATE_MAX_CODE_CHARS:
        return {
            "ok": False,
            "error": f"code 超过 {EVALUATE_MAX_CODE_CHARS} 字符限制",
        }

    session = _session_name(session_id)
    result = daemon_command("evaluate", {"code": snippet}, session=session)
    if not result.get("ok"):
        return result

    value = result.get("value")
    value_type = result.get("type")
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif value is None:
        text = ""
    else:
        text = str(value)

    truncated_text, truncated = _truncate_text(text, max_result_chars)
    return {
        "ok": True,
        "session_id": session,
        "type": value_type,
        "value": truncated_text,
        "truncated": truncated,
    }


def webbridge_scroll(
    *,
    session_id: Optional[str] = None,
    x: int = 0,
    y: int = 500,
) -> dict[str, Any]:
    """滚动页面（默认向下 500px）。"""
    session = _session_name(session_id)
    code = (
        f"(() => {{ window.scrollBy({int(x)}, {int(y)}); "
        "return JSON.stringify({ x: window.scrollX, y: window.scrollY }); }})()"
    )
    result = webbridge_evaluate(code, session_id=session)
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "session_id": session,
        "scroll_x": int(x),
        "scroll_y": int(y),
        "position": result.get("value"),
    }


def webbridge_wait(
    seconds: float,
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """等待指定秒数（最多 30s），用于页面加载或动画。"""
    wait_s = float(seconds)
    if wait_s < 0:
        return {"ok": False, "error": "seconds 不能为负"}
    if wait_s > WAIT_MAX_SECONDS:
        return {"ok": False, "error": f"seconds 不能超过 {WAIT_MAX_SECONDS}"}

    session = _session_name(session_id)
    check = check_webbridge()
    if not check.get("available"):
        return {
            "ok": False,
            "error": check.get("error") or "WebBridge 不可用",
            "install_hint": check.get("install_hint", INSTALL_HINT),
            "webbridge_check": check,
        }

    time.sleep(wait_s)
    return {"ok": True, "session_id": session, "waited_seconds": wait_s}


def webbridge_go(
    direction: Literal["back", "forward"],
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """浏览器历史后退或前进，并返回新页面 snapshot 摘要。"""
    if direction not in ("back", "forward"):
        return {"ok": False, "error": "direction 须为 back 或 forward"}

    session = _session_name(session_id)
    fn = "back" if direction == "back" else "forward"
    code = f"(() => {{ window.history.{fn}(); return true; }})()"
    nav = webbridge_evaluate(code, session_id=session)
    if not nav.get("ok"):
        return nav

    snap = webbridge_snapshot(session_id=session)
    if not snap.get("ok"):
        return {
            "ok": True,
            "session_id": session,
            "direction": direction,
            "snapshot_error": snap.get("error"),
        }
    return {
        "ok": True,
        "session_id": session,
        "direction": direction,
        "url": snap.get("url"),
        "title": snap.get("title"),
        "snapshot": snap.get("snapshot"),
    }


def webbridge_select(
    selector: str,
    value: str,
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """设置 <select> 下拉选项（按 value 或可见文本匹配）。"""
    sel = (selector or "").strip()
    val = (value or "").strip()
    if not sel:
        return {"ok": False, "error": "selector 必填"}
    if not val:
        return {"ok": False, "error": "value 必填"}

    sel_json = json.dumps(sel)
    val_json = json.dumps(val)
    code = f"""(() => {{
  const el = document.querySelector({sel_json});
  if (!el) return JSON.stringify({{ ok: false, error: 'select not found' }});
  const target = {val_json};
  let matched = false;
  for (const opt of el.options) {{
    if (opt.value === target || (opt.textContent || '').trim() === target) {{
      el.value = opt.value;
      matched = true;
      break;
    }}
  }}
  if (!matched) return JSON.stringify({{ ok: false, error: 'option not found' }});
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return JSON.stringify({{ ok: true, value: el.value, text: el.options[el.selectedIndex]?.text }});
}})()"""

    session = _session_name(session_id)
    result = webbridge_evaluate(code, session_id=session)
    if not result.get("ok"):
        return result

    try:
        parsed = json.loads(result.get("value") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "select 结果解析失败"}

    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error") or "select 失败"}

    return {
        "ok": True,
        "session_id": session,
        "selector": sel,
        "value": parsed.get("value"),
        "text": parsed.get("text"),
    }


def webbridge_close(
    *,
    session_id: Optional[str] = None,
    close_session: bool = False,
) -> dict[str, Any]:
    """关闭 WebBridge tab 或整个 session。"""
    session = _session_name(session_id)
    action = "close_session" if close_session else "close_tab"
    result = daemon_command(action, {}, session=session)
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "session_id": session,
        "closed": result.get("closed"),
        "action": action,
    }


def extension_version_mismatch_hint(text: str) -> Optional[str]:
    if re.search(r"update the Kimi WebBridge extension", text, re.I):
        return f"请更新浏览器扩展后重试：{WEBBRIDGE_EXTENSION_URL}"
    return None
