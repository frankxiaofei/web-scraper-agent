"""微信公众号文章发现与提取（搜狗搜索 + 链式发现）。"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
CST = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weixin.sogou.com/",
}


def resolve_wechat_nickname(site_config: dict) -> str:
    """从站点配置解析公众号昵称。"""
    if site_config.get("wechat_nickname"):
        return str(site_config["wechat_nickname"])
    name = site_config.get("name", "")
    match = re.match(r"^(.+?)（微信公众号）", name)
    if match:
        return match.group(1)
    return name or site_config.get("id", "")


def resolve_wechat_biz(site_config: dict) -> str:
    """从站点配置解析公众号 __biz（可选，用于严格匹配）。"""
    return str(site_config.get("wechat_biz") or "").strip()


def extract_biz_from_url(url: str) -> str:
    """从文章 URL 的 __biz 查询参数提取公众号标识。"""
    match = re.search(r"__biz=([^&]+)", url)
    return match.group(1) if match else ""


def normalize_account_name(name: str) -> str:
    """归一化公众号显示名，便于比对。"""
    name = html_mod.unescape(name or "").strip()
    name = re.sub(r"\s+", "", name)
    return name.replace("（", "(").replace("）", ")").lower()


def accounts_match(actual: str, expected: str) -> bool:
    """判断页面公众号名与目标昵称是否一致（含包含关系）。"""
    actual_n = normalize_account_name(actual)
    expected_n = normalize_account_name(expected)
    if not actual_n or not expected_n:
        return False
    if actual_n == expected_n:
        return True
    shorter, longer = (
        (actual_n, expected_n) if len(actual_n) <= len(expected_n) else (expected_n, actual_n)
    )
    return len(shorter) >= 4 and shorter in longer


def extract_article_account(text: str, soup: BeautifulSoup) -> tuple[str, str]:
    """从文章页提取公众号显示名与 __biz。"""
    author = ""
    name_el = soup.select_one("#js_name")
    if name_el:
        author = name_el.get_text(strip=True)
    if not author:
        for pattern in (
            r'var nickname\s*=\s*htmlDecode\(["\'](.+?)["\']\)',
            r'var nickname\s*=\s*["\'](.+?)["\']',
            r'window\.nickname\s*=\s*["\'](.+?)["\']',
            r'"nick_name"\s*:\s*"([^"]+)"',
        ):
            match = re.search(pattern, text)
            if match:
                author = html_mod.unescape(match.group(1).strip())
                break

    biz = ""
    for pattern in (
        r'var biz\s*=\s*["\']([^"\']+)["\']',
        r'window\.biz\s*=\s*["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, text)
        if match:
            biz = match.group(1).strip()
            break
    return author, biz


def extract_biz_from_seed(seed_url: str) -> str:
    """从种子文章页推断目标公众号 __biz。"""
    url_biz = extract_biz_from_url(seed_url)
    if url_biz:
        return url_biz
    try:
        resp = requests.get(seed_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        _, biz = extract_article_account(resp.text, BeautifulSoup(resp.text, "html.parser"))
        return biz
    except requests.RequestException as exc:
        logger.debug("种子页 __biz 提取失败 (%s): %s", seed_url, exc)
        return ""


def resolve_expected_biz(site_config: dict, seed_url: str) -> str:
    """合并配置与种子页，得到目标 __biz。"""
    configured = resolve_wechat_biz(site_config) if site_config else ""
    if configured:
        return configured
    return extract_biz_from_seed(seed_url)


def article_belongs_to_account(
    *,
    author: str,
    biz: str,
    url: str,
    expected_nickname: str,
    expected_biz: str = "",
) -> bool:
    """校验文章是否属于目标公众号。"""
    url_biz = extract_biz_from_url(url)
    effective_biz = biz or url_biz

    if expected_biz:
        if effective_biz and effective_biz != expected_biz:
            return False
        if url_biz and url_biz != expected_biz:
            return False

    if author and expected_nickname:
        return accounts_match(author, expected_nickname)

    if expected_biz and effective_biz == expected_biz:
        return True
    return False


def _url_matches_expected_biz(url: str, expected_biz: str) -> bool:
    if not expected_biz:
        return True
    url_biz = extract_biz_from_url(url)
    return not url_biz or url_biz == expected_biz


def seen_urls_path(site_id: str) -> Path:
    path = ROOT / "data" / site_id / "seen_urls.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_seen(site_id: str) -> Set[str]:
    path = seen_urls_path(site_id)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(site_id: str, seen: Set[str]) -> None:
    path = seen_urls_path(site_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def clean_url(url: str) -> str:
    """去除 URL 的查询参数（保留 __biz&mid&sn 类有意义的参数）。"""
    url = re.sub(r"#.*", "", url)
    if "__biz=" in url:
        return url
    # /s/{slug} 形式可去掉 tracking 参数；/s? 重定向 URL 需保留 query
    if re.search(r"/s/[a-zA-Z0-9_-]+", url):
        return re.sub(r"\?.*", "", url)
    return url


def resolve_sogou_link(sogou_url: str) -> Optional[str]:
    """跟随搜狗跳转链接的 302 重定向，获取真实 mp.weixin.qq.com/s/ URL。"""
    try:
        resp = requests.get(
            sogou_url, headers=HEADERS, timeout=15, allow_redirects=True
        )
        final_url = resp.url
        if "mp.weixin.qq.com/s/" in final_url or "mp.weixin.qq.com/s?" in final_url:
            return clean_url(final_url)
        return None
    except requests.RequestException as exc:
        logger.warning("搜狗链接解析失败: %s", exc)
        return None


def search_via_sogou(nickname: str, max_pages: int = 2) -> Set[str]:
    """通过搜狗微信搜索获取公众号文章真实链接 (已弃用，搜狗 antispider 无法绕过)。
    
    搜狗微信对 Python requests 有 antispider 检测，返回 0 条结果。
    请使用 WebBridge (search_via_webbridge) 替代。
    """
    logger.warning("search_via_sogou 已弃用 (搜狗 antispider)，返回空结果")
    return set()


def search_via_webbridge(
    nickname: str,
    max_pages: int = 2,
    session_id: str | None = None,
) -> Set[str]:
    """通过 WebBridge (Kimi真实浏览器) 搜狗搜索获取公众号文章真实链接。

    使用用户在本地浏览器中已登录的会话，绕过搜狗 antispider。
    需要在 WebBridge daemon 运行且扩展已连接的环境下使用。

    返回: 发现的 mp.weixin.qq.com/s/ URL 集合
    """
    from src.web.webbridge_skills import (
        check_webbridge,
        daemon_command,
        webbridge_evaluate,
    )

    check = check_webbridge()
    if not check.get("available"):
        logger.warning("WebBridge 不可用，跳过搜狗搜索: %s", check.get("error"))
        return set()

    found: Set[str] = set()
    sid = session_id or f"wechat-{nickname}-{int(time.time())}"
    nickname_json = json.dumps(nickname, ensure_ascii=False)
    eval_code = (
        "(function() {\n"
        f"  var target = {nickname_json};\n"
        "  var items = [];\n"
        "  document.querySelectorAll('ul.news-list > li').forEach(function(li) {\n"
        "    var titleEl = li.querySelector('h3 a');\n"
        "    var linkEl = li.querySelector('.img-box a[data-z=\"art\"]');\n"
        "    var sourceEl = li.querySelector('.s-p');\n"
        "    var source = sourceEl ? sourceEl.textContent.replace(/document\\.write\\([^)]+\\)/g, '').trim() : '';\n"
        "    if (titleEl && linkEl && source.indexOf(target) !== -1) {\n"
        "      var href = linkEl.getAttribute('href') || '';\n"
        "      var fullUrl = href.startsWith('http') ? href : 'https://weixin.sogou.com' + href;\n"
        "      items.push({title: titleEl.textContent.trim(), url: fullUrl});\n"
        "    }\n"
        "  });\n"
        "  return JSON.stringify(items.slice(0, 20));\n"
        "})()"
    )

    try:
        for page in range(1, max_pages + 1):
            sogou_url = (
                f"https://weixin.sogou.com/weixin"
                f"?type=2&query={nickname}&page={page}&ie=utf8"
            )
            nav = daemon_command(
                "navigate",
                {"url": sogou_url, "newTab": page == 1},
                session=sid,
            )
            if not nav.get("ok"):
                logger.warning(
                    "WebBridge navigate 失败 (page %d): %s",
                    page,
                    nav.get("error"),
                )
                break

            time.sleep(3)

            ev = webbridge_evaluate(eval_code, session_id=sid)
            if not ev.get("ok"):
                logger.warning(
                    "WebBridge evaluate 失败 (page %d): %s",
                    page,
                    ev.get("error"),
                )
                break

            try:
                items = json.loads(ev.get("value") or "[]")
            except json.JSONDecodeError:
                logger.warning("WebBridge 搜狗列表解析失败 (page %d)", page)
                break

            page_found = 0
            for item in items:
                jump_url = (item.get("url") or "").strip()
                if not jump_url:
                    continue
                nav2 = daemon_command(
                    "navigate",
                    {"url": jump_url, "newTab": False},
                    session=sid,
                )
                time.sleep(3)
                if not nav2.get("ok"):
                    continue
                snap = daemon_command("snapshot", {}, session=sid)
                final_url = ((snap.get("url") if snap.get("ok") else None) or nav2.get("url") or "").strip()
                if "mp.weixin.qq.com/s/" in final_url or "mp.weixin.qq.com/s?" in final_url:
                    found.add(clean_url(final_url))
                    page_found += 1

            logger.info("WebBridge 搜狗 page %d: %d 个真实链接", page, page_found)
            if page_found == 0:
                break
            time.sleep(2)
    except Exception as exc:
        logger.error("WebBridge 搜狗搜索异常: %s", exc)
    finally:
        daemon_command("close_session", {}, session=sid)

    return found


_JUNK_SLUGS = frozenset({"_2kC-fXw7UjneZSrsC9CVQ"})


def _normalize_wechat_page_html(text: str) -> str:
    """统一 HTML/JS 转义，便于从页面源码提取 __biz 文章链接。"""
    text = html_mod.unescape(text)
    text = text.replace("\\x26amp;", "&").replace("\\x26", "&")
    text = re.sub(r"&amp;+", "&", text)
    return text


def _is_junk_article_url(url: str) -> bool:
    slug_match = re.search(r"/s/([a-zA-Z0-9_-]+)", url)
    if slug_match and slug_match.group(1) in _JUNK_SLUGS:
        return True
    return False


def extract_links_from_page(url: str, *, expected_biz: str = "") -> Set[str]:
    """从公众号文章页面提取有效 mp.weixin.qq.com/s/ 链接。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        page_text = _normalize_wechat_page_html(resp.text)
        found: Set[str] = set()
        for m in re.finditer(
            r"https://mp\.weixin\.qq\.com/s/[a-zA-Z0-9_-]+", page_text
        ):
            u = clean_url(m.group(0))
            if u and len(u) > 40 and not _is_junk_article_url(u):
                if _url_matches_expected_biz(u, expected_biz):
                    found.add(u)
        for m in re.finditer(
            r"https://mp\.weixin\.qq\.com/s\?__biz=[A-Za-z0-9_=]+&mid=\d+&idx=\d+&sn=[a-f0-9]+",
            page_text,
        ):
            u = clean_url(m.group(0))
            if u and not _is_junk_article_url(u) and _url_matches_expected_biz(u, expected_biz):
                found.add(u)
        return found
    except requests.RequestException as exc:
        logger.warning("链式提取失败 (%s): %s", url, exc)
        return set()


def chain_discovery(
    site_id: str,
    seen: Set[str],
    seed_url: str,
    max_seed: int = 5,
    *,
    expected_biz: str = "",
) -> Set[str]:
    """链式发现：从种子 URL 和最近已爬文章中提取链接。"""
    start_urls = [seed_url]
    recent = sorted(seen)[-max_seed:]
    start_urls.extend(
        u for u in recent if u != seed_url and _url_matches_expected_biz(u, expected_biz)
    )

    all_urls: Set[str] = set()
    for url in start_urls:
        links = extract_links_from_page(url, expected_biz=expected_biz)
        all_urls |= links
        time.sleep(1)
    return all_urls


def discover_article_urls(
    *,
    site_id: str,
    nickname: str,
    seed_url: str,
    seen: Set[str],
    sogou_max_pages: int = 2,
    chain_max_seed: int = 5,
    use_sogou: bool = False,  # 默认关闭搜狗 HTTP（antispider 无法绕过）
    use_webbridge: bool = True,  # 默认使用 WebBridge
    use_chain: bool = True,
    expected_biz: str = "",
) -> Set[str]:
    """综合 WebBridge 搜索与链式发现，返回全部发现的链接（含已爬）。
    
    WebBridge 通过用户本地浏览器绕过搜狗 antispider。
    链式发现从已有文章页面中提取更多链接。
    """
    discovered: Set[str] = set()
    if use_sogou:
        discovered |= search_via_sogou(nickname, max_pages=sogou_max_pages)
    if use_webbridge:
        discovered |= search_via_webbridge(nickname, max_pages=sogou_max_pages)
    if use_chain:
        discovered |= chain_discovery(
            site_id,
            seen,
            seed_url,
            max_seed=chain_max_seed,
            expected_biz=expected_biz,
        )
    if expected_biz:
        discovered = {u for u in discovered if _url_matches_expected_biz(u, expected_biz)}
    return discovered


_IMG_URL_ATTRS = ("data-src", "src", "data-backsrc", "data-original")
_IMG_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _pick_img_url(img_tag) -> str:
    for attr in _IMG_URL_ATTRS:
        val = (img_tag.get(attr) or "").strip()
        if val.startswith("http"):
            return val
    return ""


def extract_image_urls(content_el) -> list[str]:
    """从正文 DOM 提取微信图片 URL（data-src / mmbiz.qpic.cn 等）。"""
    urls: list[str] = []
    seen: set[str] = set()
    for img in content_el.find_all("img"):
        img_url = _pick_img_url(img)
        if img_url and img_url not in seen:
            seen.add(img_url)
            urls.append(img_url)
    return urls


def article_images_dir(site_id: str, article_url: str) -> Path:
    slug = hashlib.md5(clean_url(article_url).encode()).hexdigest()[:12]
    path = ROOT / "data" / site_id / "images" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guess_image_ext(img_url: str, content_type: str = "") -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _IMG_EXT_BY_CONTENT_TYPE:
        return _IMG_EXT_BY_CONTENT_TYPE[ct]
    path_part = img_url.split("?", 1)[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        if path_part.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if "wx_fmt=png" in img_url:
        return ".png"
    if "wx_fmt=gif" in img_url:
        return ".gif"
    if "wx_fmt=webp" in img_url:
        return ".webp"
    return ".jpg"


_WECHAT_IMAGE_PATH_RE = re.compile(r"^data/([^/]+)/images/(.+)$")


def local_image_serve_url(local_path: str) -> str:
    """将 data/{site_id}/images/... 相对路径转为受控静态资源 URL。"""
    rel = local_path.replace("\\", "/").lstrip("/")
    match = _WECHAT_IMAGE_PATH_RE.match(rel)
    if match:
        site_id, subpath = match.group(1), match.group(2)
        if site_id.startswith("wechat_") and ".." not in subpath.split("/"):
            return f"/api/wechat-assets/{site_id}/{subpath}"
    if rel.startswith("data/"):
        return f"/{rel}"
    return f"/data/{rel}"


def _normalize_img_url(url: str) -> str:
    return html_mod.unescape(url.split("#", 1)[0].strip())


def _build_image_url_map(attachments: list[dict]) -> dict[str, str]:
    """attachments 原始 URL → 本地静态服务 URL。"""
    mapping: dict[str, str] = {}
    for att in attachments:
        if att.get("type") != "image":
            continue
        orig = att.get("url") or ""
        local_path = att.get("local_path") or ""
        if not orig or not local_path:
            continue
        served = local_image_serve_url(local_path)
        mapping[_normalize_img_url(orig)] = served
    return mapping


def _pick_served_url(img_url: str, url_map: dict[str, str]) -> str:
    if not img_url or not url_map:
        return ""
    key = _normalize_img_url(img_url)
    if key in url_map:
        return url_map[key]
    base = key.split("?", 1)[0]
    for orig, served in url_map.items():
        if orig.split("?", 1)[0] == base:
            return served
    return ""


def rewrite_wechat_content_images(
    content_html: str,
    attachments: list[dict],
) -> str:
    """将正文 HTML 中 img 的 data-src/src 就地替换为本地服务 URL。"""
    if not content_html or not attachments:
        return content_html

    url_map = _build_image_url_map(attachments)
    if not url_map:
        return content_html

    soup = BeautifulSoup(content_html, "html.parser")

    for root in soup.select("#js_content, .rich_media_content"):
        style = root.get("style") or ""
        style = re.sub(r"visibility\s*:\s*hidden\s*;?", "", style, flags=re.I)
        style = re.sub(r"opacity\s*:\s*0\s*;?", "", style, flags=re.I)
        style = style.strip().rstrip(";")
        if style:
            root["style"] = style
        elif root.has_attr("style"):
            del root["style"]

    for img in soup.find_all("img"):
        served = ""
        for attr in _IMG_URL_ATTRS:
            val = (img.get(attr) or "").strip()
            if not val.startswith("http"):
                continue
            served = _pick_served_url(val, url_map)
            if served:
                break
        if served:
            img["src"] = served
        else:
            for attr in _IMG_URL_ATTRS:
                val = (img.get(attr) or "").strip()
                if val.startswith("http"):
                    img["src"] = _normalize_img_url(val)
                    break

    return str(soup)


def download_wechat_images(
    image_urls: list[str],
    *,
    site_id: str,
    article_url: str,
) -> list[dict]:
    """下载文章图片到 data/{site_id}/images/{slug}/，并返回 attachments 元数据。"""
    if not image_urls:
        return []

    dest_dir = article_images_dir(site_id, article_url)
    headers = {**HEADERS, "Referer": article_url}
    attachments: list[dict] = []

    for i, img_url in enumerate(image_urls, start=1):
        name = f"image_{i}"
        attachment: dict = {"url": img_url, "name": name, "type": "image"}
        ext = _guess_image_ext(img_url)
        local_path = dest_dir / f"{i}{ext}"
        try:
            resp = requests.get(img_url, headers=headers, timeout=30)
            resp.raise_for_status()
            ext = _guess_image_ext(img_url, resp.headers.get("Content-Type", ""))
            if local_path.suffix != ext:
                local_path = dest_dir / f"{i}{ext}"
            local_path.write_bytes(resp.content)
            attachment["local_path"] = str(local_path.relative_to(ROOT))
        except requests.RequestException as exc:
            logger.warning("图片下载失败 (%s): %s", img_url[:80], exc)
        attachments.append(attachment)

    return attachments


def extract_article(
    url: str,
    *,
    site_id: str | None = None,
    download_images: bool = True,
) -> dict:
    """HTTP 直连提取单篇文章信息。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        soup = BeautifulSoup(text, "html.parser")

        title = ""
        og = soup.select_one('meta[property="og:title"]')
        if og:
            title = og.get("content", "")
        if not title:
            m = re.search(r"var msg_title\s*=\s*[\"'](.+?)[\"']", text)
            if m:
                title = m.group(1)
        if not title:
            m = re.search(r"var title\s*=\s*[\"'](.+?)[\"']", text)
            if m:
                title = html_mod.unescape(m.group(1))

        ct_match = re.search(r"var ct\s*=\s*[\"'](\d+)[\"']", text)
        publish_time = ""
        if ct_match:
            dt = datetime.fromtimestamp(int(ct_match.group(1)), tz=CST)
            publish_time = dt.strftime("%Y-%m-%d %H:%M:%S")

        content_el = soup.select_one("#js_content, .rich_media_content")
        content = ""
        content_html = ""
        image_urls: list[str] = []
        attachments: list[dict] = []
        if content_el:
            content = content_el.get_text(strip=True)[:20000]
            content_html = str(content_el)[:100000]
            image_urls = extract_image_urls(content_el)
            if site_id and download_images and image_urls:
                attachments = download_wechat_images(
                    image_urls,
                    site_id=site_id,
                    article_url=clean_url(url),
                )
                if attachments and content_html:
                    content_html = rewrite_wechat_content_images(
                        content_html, attachments
                    )

        author, biz = extract_article_account(text, soup)
        if not biz:
            biz = extract_biz_from_url(url)

        return {
            "title": title[:200],
            "url": clean_url(url),
            "publish_time": publish_time,
            "content": content,
            "content_html": content_html,
            "image_urls": image_urls,
            "attachments": attachments,
            "author": author,
            "biz": biz,
        }
    except requests.RequestException as exc:
        logger.warning("文章提取失败 (%s): %s", url, exc)
        return {}


def parse_publish_datetime(publish_time: str) -> Optional[datetime]:
    """将微信文章发布时间字符串转为 datetime。"""
    if not publish_time:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(publish_time, fmt)
        except ValueError:
            continue
    return None


def crawl_new_articles(
    *,
    site_id: str,
    nickname: str,
    seed_url: str,
    max_articles: int = 15,
    sogou_max_pages: int = 2,
    chain_max_seed: int = 5,
    use_sogou: bool = False,  # 默认关闭搜狗
    use_webbridge: bool = True,  # 默认使用 WebBridge
    use_chain: bool = True,
    article_delay_seconds: float = 3.0,
    force_refresh: bool = False,
    wechat_biz: str = "",
) -> list[dict]:
    """发现、提取并返回新文章列表，同时更新 seen_urls。

    force_refresh=True 时还会重新提取 seen_urls 中已有文章（用于图片/正文回填）。
    """
    expected_biz = (wechat_biz or "").strip() or extract_biz_from_seed(seed_url)
    if expected_biz:
        logger.info("站点 %s 目标 __biz=%s", site_id, expected_biz)

    seen = load_seen(site_id)
    discovered = discover_article_urls(
        site_id=site_id,
        nickname=nickname,
        seed_url=seed_url,
        seen=seen,
        sogou_max_pages=sogou_max_pages,
        chain_max_seed=chain_max_seed,
        use_sogou=use_sogou,
        use_webbridge=use_webbridge,
        use_chain=use_chain,
        expected_biz=expected_biz,
    )

    new_urls = sorted(u for u in discovered if u not in seen)
    if force_refresh:
        refresh_urls = sorted(seen)
        target = list(dict.fromkeys(new_urls + refresh_urls))[:max_articles]
    else:
        target = new_urls[:max_articles]

    if not target:
        save_seen(site_id, seen)
        return []
    articles: list[dict] = []
    for i, url in enumerate(target):
        article = extract_article(url, site_id=site_id)
        seen.add(url)
        if not article or not (article.get("title") or article.get("content")):
            if i < len(target) - 1 and article_delay_seconds > 0:
                time.sleep(article_delay_seconds)
            continue
        if not article_belongs_to_account(
            author=article.get("author") or "",
            biz=article.get("biz") or "",
            url=url,
            expected_nickname=nickname,
            expected_biz=expected_biz,
        ):
            logger.warning(
                "跳过非目标公众号文章: %s (author=%s, biz=%s, expected=%s/%s)",
                url,
                article.get("author") or "?",
                article.get("biz") or extract_biz_from_url(url) or "?",
                nickname,
                expected_biz or "?",
            )
            if i < len(target) - 1 and article_delay_seconds > 0:
                time.sleep(article_delay_seconds)
            continue
        articles.append(article)
        if i < len(target) - 1 and article_delay_seconds > 0:
            time.sleep(article_delay_seconds)

    save_seen(site_id, seen)
    return articles
