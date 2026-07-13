#!/usr/bin/env python3
"""为 config/sites.yaml 中所有微信公众号站点生成 WebBridge crawl_rules，并补充 sites 配置字段。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SITES_PATH = ROOT / "config" / "sites.yaml"
RULES_DIR = ROOT / "config" / "crawl_rules"

RULE_TEMPLATE = """version: 1
site_id: {site_id}
name: {name}
enabled: true
# 微信公众号 WebBridge 规则（Kimi 真实浏览器 / mp.weixin.qq.com）
# 不设 list_page：site_sync 仍走 adapter:wechat → search_via_webbridge + 链式发现
# Agent 运维：skill_webbridge_check → navigate(entry_url) → evaluate 提取正文/最新动态链接
entry_url: {entry_url}
entry_steps:
  - action: navigate
    url: "{entry_url}"
  - action: wait
    timeout_ms: 3000
detail:
  fetch_detail: true
  strategy: dom
  url_pattern: "mp\\\\.weixin\\\\.qq\\\\.com/s"
  title_selector: "meta[property=\\"og:title\\"]"
  content_selector: "#js_content, .rich_media_content"
  wait_for: "#js_content, .rich_media_content"
limits:
  max_pages: 1
  max_items: {max_items}
  max_depth: 1
  rate_limit_seconds: {rate_limit}
search:
  enabled: true
  type: webbridge_interactive
  nickname: "{nickname}"
  steps:
    - action: navigate
      url: "https://weixin.sogou.com/weixin?type=2&query={nickname_encoded}&page=1&ie=utf8"
      label: "搜狗微信搜索（WebBridge 绕过 antispider）"
    - action: wait
      timeout_ms: 3000
    - action: evaluate
      label: "提取搜索结果并过滤来源 .s-p 含公众号昵称"
      code: |
        (function() {{
          var target = {nickname_json};
          var items = [];
          document.querySelectorAll('ul.news-list > li').forEach(function(li) {{
            var titleEl = li.querySelector('h3 a');
            var linkEl = li.querySelector('.img-box a[data-z="art"]');
            var sourceEl = li.querySelector('.s-p');
            var source = sourceEl ? sourceEl.textContent.replace(/document\\.write\\([^)]+\\)/g, '').trim() : '';
            if (titleEl && linkEl && source.indexOf(target) !== -1) {{
              var href = linkEl.getAttribute('href') || '';
              var fullUrl = href.startsWith('http') ? href : 'https://weixin.sogou.com' + href;
              items.push({{title: titleEl.textContent.trim(), url: fullUrl}});
            }}
          }});
          return JSON.stringify(items.slice(0, 20));
        }})()
chain_discovery:
  enabled: true
  type: webbridge_evaluate
  link_selector: "a.normal_text_link[href*='mp.weixin.qq.com/s']"
  scroll_before_extract: true
  code: |
    (function() {{
      window.scrollTo(0, document.body.scrollHeight);
      var links = [];
      document.querySelectorAll('a.normal_text_link[href*="mp.weixin.qq.com/s"]').forEach(function(a) {{
        links.push({{url: a.href, title: a.textContent.trim()}});
      }});
      return JSON.stringify(links.slice(0, 20));
    }})()
webbridge:
  session_id: "crawl-{site_id}"
  require_daemon: true
  require_extension: true
  close_session_on_complete: true
  captcha_fallback:
    - action: evaluate
      code: "document.querySelector('button')?.click()"
    - action: go
      direction: back
article_extract:
  evaluate: |
    (function() {{
      var title = document.querySelector('meta[property="og:title"]')?.getAttribute('content') || '';
      var contentEl = document.querySelector('#js_content, .rich_media_content');
      var html = contentEl ? contentEl.innerHTML : '';
      var text = contentEl ? contentEl.innerText : '';
      var ctMatch = document.documentElement.innerHTML.match(/var ct\\s*=\\s*['"](\\d+)['"]/);
      var publishTime = '';
      if (ctMatch) {{
        var d = new Date(parseInt(ctMatch[1]) * 1000);
        publishTime = d.toISOString().slice(0,19).replace('T',' ');
      }}
      return JSON.stringify({{title: title, publish_time: publishTime, content_html: html.substring(0, 50000), content_text: text.substring(0, 20000)}});
    }})()
"""


def load_sites() -> dict:
    with open(SITES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_sites(data: dict) -> None:
    with open(SITES_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def patch_site_entry(site: dict) -> bool:
    """补充 sites.yaml 中的 WebBridge 字段，返回是否有变更。"""
    changed = False
    defaults = {
        "wechat_use_webbridge": True,
        "wechat_sogou_max_pages": 2,
        "wechat_chain_max_seed": 5,
    }
    for key, val in defaults.items():
        if site.get(key) is None:
            site[key] = val
            changed = True

    nickname = site.get("wechat_nickname") or site.get("name", "").replace("（微信公众号）", "")
    note = site.get("notes") or ""
    marker = "WebBridge 规则见 config/crawl_rules/"
    if marker not in note:
        site["notes"] = (
            f"微信公众号「{nickname}」。WebBridge：搜狗搜索发现 + 种子文章链式发现；"
            f"规则见 config/crawl_rules/{site['id']}.yaml"
        )
        changed = True
    return changed


def write_rule(site: dict) -> Path:
    site_id = site["id"]
    nickname = site.get("wechat_nickname") or ""
    import json
    import urllib.parse

    text = RULE_TEMPLATE.format(
        site_id=site_id,
        name=site.get("name", site_id),
        entry_url=site.get("url", ""),
        max_items=int(site.get("max_items", 500)),
        rate_limit=float(site.get("min_delay_seconds", 3)),
        nickname=nickname,
        nickname_encoded=urllib.parse.quote(nickname),
        nickname_json=json.dumps(nickname, ensure_ascii=False),
    )
    path = RULES_DIR / f"{site_id}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> None:
    data = load_sites()
    sites = data.get("sites") or []
    wechat_sites = [s for s in sites if s.get("category") == "wechat" or str(s.get("id", "")).startswith("wechat_")]
    if not wechat_sites:
        print("未找到微信公众号站点")
        sys.exit(1)

    RULES_DIR.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    patched = 0
    for site in wechat_sites:
        write_rule(site)
        created.append(site["id"])
        if patch_site_entry(site):
            patched += 1

    save_sites(data)
    print(f"已生成 {len(created)} 个 crawl_rules YAML")
    print(f"已更新 sites.yaml 中 {patched} 个公众号站点的 WebBridge 字段")
    for sid in created:
        print(f"  - config/crawl_rules/{sid}.yaml")


if __name__ == "__main__":
    main()
