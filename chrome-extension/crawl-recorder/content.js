/**
 * Content script：捕获点击、导航、XHR/fetch（录制开启时）。
 */
(function () {
  'use strict';

  const { generateSelector, getElementText } = window.CrawlRecorderSelector || {};

  let recording = false;
  let pickMode = false;
  let networkHooked = false;
  const seenNetwork = new Set();

  function nowIso() {
    return new Date().toISOString();
  }

  function sendMessage(msg) {
    chrome.runtime.sendMessage(msg).catch(() => {});
  }

  function addStep(step) {
    sendMessage({ type: 'ADD_STEP', step });
  }

  function hookNetwork() {
    if (networkHooked) return;
    networkHooked = true;

    const recordUrl = (rawUrl) => {
      if (!recording || !rawUrl) return;
      let url = rawUrl;
      try {
        url = new URL(rawUrl, location.href).href;
      } catch {
        /* keep raw */
      }
      const pattern = extractUrlPattern(url);
      if (!pattern || seenNetwork.has(pattern)) return;
      seenNetwork.add(pattern);
      addStep({
        action: 'wait_network',
        url_pattern: pattern,
        full_url: url,
        timestamp: nowIso(),
      });
    };

    const origFetch = window.fetch;
    window.fetch = function (...args) {
      const input = args[0];
      const url = typeof input === 'string' ? input : input?.url;
      recordUrl(url);
      return origFetch.apply(this, args);
    };

    const origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      recordUrl(url);
      return origOpen.call(this, method, url, ...rest);
    };
  }

  function extractUrlPattern(url) {
    try {
      const u = new URL(url, location.href);
      const path = u.pathname;
      const parts = path.split('/').filter(Boolean);
      const last = parts[parts.length - 1] || path;
      if (/\.(do|json|api|ashx|jsp|php)/i.test(last)) return last;
      if (parts.length >= 2) return parts.slice(-2).join('/');
      return last || path;
    } catch {
      const m = String(url).match(/[\w/]+\.(?:do|json|api)[\w/?=&.-]*/i);
      return m ? m[0].split('?')[0].split('/').pop() : String(url).slice(0, 80);
    }
  }

  function hookHistory() {
    const notifyNav = (url) => {
      if (!recording) return;
      addStep({ action: 'navigate', url, timestamp: nowIso() });
    };

    const wrapHistory = (fn) =>
      function (...args) {
        const ret = fn.apply(this, args);
        notifyNav(location.href);
        return ret;
      };

    history.pushState = wrapHistory(history.pushState);
    history.replaceState = wrapHistory(history.replaceState);
    window.addEventListener('popstate', () => notifyNav(location.href));
    window.addEventListener('hashchange', () => notifyNav(location.href));
  }

  function onClick(e) {
    if (!recording) return;

    const el = e.target.closest('a, button, input[type="button"], input[type="submit"], [role="button"], li, span, div');
    if (!el) return;

    if (e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      const selector = generateSelector(el);
      addStep({
        action: 'pick_selector',
        selector,
        text: getElementText(el),
        tag: el.tagName.toLowerCase(),
        timestamp: nowIso(),
        note: 'Alt+点击选中元素',
      });
      flashHighlight(el);
      return;
    }

    if (pickMode) return;

    const selector = generateSelector(el);
    const text = getElementText(el);
    const step = {
      action: 'click',
      selector,
      text,
      tag: el.tagName.toLowerCase(),
      timestamp: nowIso(),
    };

    if (/下一页|下页|next|»|›/i.test(text) || el.classList?.contains('next-page') || el.classList?.contains('next')) {
      step.note = 'pagination';
    }

    addStep(step);
  }

  function flashHighlight(el) {
    const prev = el.style.outline;
    el.style.outline = '2px solid #2563eb';
    setTimeout(() => {
      el.style.outline = prev;
    }, 800);
  }

  function detectListHints() {
    const containers = ['#noticeShow', '#list', '.list', 'ul.notice-list', '[class*="list"]'];
    for (const sel of containers) {
      try {
        const el = document.querySelector(sel);
        if (el && el.querySelector('a')) {
          const links = Array.from(el.querySelectorAll('a[href]'))
            .slice(0, 5)
            .map((a) => a.href);
          sendMessage({
            type: 'UPDATE_HINTS',
            hints: { list_container: sel, sample_links: links },
          });
          return;
        }
      } catch {
        /* ignore */
      }
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === 'SET_RECORDING') {
      recording = !!msg.recording;
      if (recording) {
        hookNetwork();
        hookHistory();
        seenNetwork.clear();
        addStep({ action: 'navigate', url: location.href, timestamp: nowIso() });
        setTimeout(detectListHints, 1500);
      }
      sendResponse({ ok: true, url: location.href });
      return true;
    }
    if (msg.type === 'GET_PAGE_INFO') {
      sendResponse({ url: location.href, title: document.title });
      return true;
    }
    if (msg.type === 'MARK_PAGINATION') {
      sendMessage({
        type: 'ADD_STEP',
        step: {
          action: 'click',
          selector: msg.selector || '',
          text: msg.text || '下一页',
          note: 'pagination',
          timestamp: nowIso(),
        },
      });
      sendResponse({ ok: true });
      return true;
    }
  });

  document.addEventListener('click', onClick, true);
})();
