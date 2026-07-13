/**
 * CSS 选择器生成：优先 id → data-* → 唯一 class 路径，避免过长 xpath。
 */
(function (global) {
  'use strict';

  function escapeCssIdent(value) {
    if (typeof CSS !== 'undefined' && CSS.escape) {
      return CSS.escape(value);
    }
    return String(value).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, '\\$1');
  }

  function isUniqueSelector(selector, root) {
    try {
      return root.querySelectorAll(selector).length === 1;
    } catch {
      return false;
    }
  }

  function getDataAttrSelector(el) {
    if (!el || !el.attributes) return null;
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-') && attr.value) {
        const sel = `[${attr.name}="${escapeCssIdent(attr.value)}"]`;
        if (isUniqueSelector(sel, el.ownerDocument)) return sel;
        const tagged = `${el.tagName.toLowerCase()}${sel}`;
        if (isUniqueSelector(tagged, el.ownerDocument)) return tagged;
      }
    }
    return null;
  }

  function getClassSelector(el) {
    if (!el.classList || el.classList.length === 0) return null;
    const classes = Array.from(el.classList).filter(
      (c) => c && !/^(active|hover|focus|selected|open|show|hide|hidden)$/i.test(c)
    );
    for (let i = 1; i <= classes.length; i++) {
      const combo = classes.slice(0, i).map((c) => '.' + escapeCssIdent(c)).join('');
      const sel = el.tagName.toLowerCase() + combo;
      if (isUniqueSelector(sel, el.ownerDocument)) return sel;
    }
    return null;
  }

  function getNthChildSelector(el) {
    const parent = el.parentElement;
    if (!parent) return el.tagName.toLowerCase();
    const tag = el.tagName.toLowerCase();
    const siblings = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
    if (siblings.length === 1) return tag;
    const idx = siblings.indexOf(el) + 1;
    return `${tag}:nth-of-type(${idx})`;
  }

  function buildPathSegment(el) {
    if (el.id) {
      return '#' + escapeCssIdent(el.id);
    }
    const dataSel = getDataAttrSelector(el);
    if (dataSel) return dataSel;
    const classSel = getClassSelector(el);
    if (classSel) return classSel;
    return getNthChildSelector(el);
  }

  function generateSelector(el, maxDepth = 5) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return '';
    if (el === el.ownerDocument.documentElement) return 'html';
    if (el === el.ownerDocument.body) return 'body';

    if (el.id && isUniqueSelector('#' + escapeCssIdent(el.id), el.ownerDocument)) {
      return '#' + escapeCssIdent(el.id);
    }

    const segments = [];
    let current = el;
    let depth = 0;

    while (current && current.nodeType === Node.ELEMENT_NODE && depth < maxDepth) {
      if (current.id && isUniqueSelector('#' + escapeCssIdent(current.id), el.ownerDocument)) {
        segments.unshift('#' + escapeCssIdent(current.id));
        break;
      }
      segments.unshift(buildPathSegment(current));
      current = current.parentElement;
      depth++;
    }

    const selector = segments.join(' > ');
    if (isUniqueSelector(selector, el.ownerDocument)) return selector;

    // 回退：更短路径 + nth-of-type
    const short = buildPathSegment(el);
    if (isUniqueSelector(short, el.ownerDocument)) return short;

    return selector;
  }

  function getElementText(el) {
    if (!el) return '';
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    return text.slice(0, 80);
  }

  global.CrawlRecorderSelector = {
    generateSelector,
    getElementText,
  };
})(typeof window !== 'undefined' ? window : self);
