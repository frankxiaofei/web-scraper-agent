(function () {
  'use strict';

  var root = document.getElementById('stockHermesFabRoot');
  if (!root) return;

  var LS_KEY = 'crawl_agent_last_session_id_stock';
  var AGENT_PROFILE = 'stock';
  var FAQ_ITEMS = [
    { label: '今日板块热点', message: '请查询今日涨幅前列的概念与行业板块，按涨跌幅排序并简要解读热点逻辑。' },
    { label: '申万一级涨跌', message: '请查询申万一级行业（sw_l1）今日涨跌排行，列出涨幅前10与跌幅前5，用表格展示。' },
    { label: '十五五政策影响', message: '请聚合近30天政府政策资讯，分析十五五（2026-2030）背景下受影响的产业与主题。' },
    { label: '新能源产业链', message: '请解读新能源板块产业链结构、上下游关系与代表性龙头，结合实时板块数据。' },
    { label: 'K线说明', message: '请用通俗语言说明如何阅读日K/周K线（开高低收、均线、成交量），并给出分析注意事项（非买卖建议）。' },
    { label: '近7天洞察', message: '请聚合近7天股票领域洞察：主题分布、趋势摘要与数据来源说明。' },
  ];

  var fabBtn = document.getElementById('stockHermesFabBtn');
  var backdrop = document.getElementById('stockHermesBackdrop');
  var drawer = document.getElementById('stockHermesDrawer');
  var closeBtn = document.getElementById('stockHermesCloseBtn');
  var messagesEl = document.getElementById('stockHermesMessages');
  var form = document.getElementById('stockHermesForm');
  var input = document.getElementById('stockHermesInput');
  var sendBtn = document.getElementById('stockHermesSendBtn');
  var agentBar = document.getElementById('stockHermesAgentBar');
  var faqEl = document.getElementById('stockHermesFaq');

  var sessionId = null;
  var history = [];
  var streaming = false;
  var streamAbortController = null;

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function renderMarkdown(text) {
    if (typeof renderAgentMarkdown === 'function') return renderAgentMarkdown(text);
    return escapeHtml(text || '').replace(/\n/g, '<br>');
  }

  function postProcessCharts(container) {
    if (typeof echarts === 'undefined') return;
    container.querySelectorAll('pre code.language-echarts, pre code.language-chart').forEach(function (codeEl) {
      var pre = codeEl.parentElement;
      if (!pre || pre.dataset.chartRendered) return;
      try {
        var opt = JSON.parse(codeEl.textContent.trim());
        var chartDiv = document.createElement('div');
        chartDiv.className = 'stock-hermes-chart';
        pre.replaceWith(chartDiv);
        var chart = echarts.init(chartDiv);
        chart.setOption(opt);
        pre.dataset.chartRendered = '1';
        window.addEventListener('resize', function () { chart.resize(); });
      } catch (e) { /* ignore invalid chart JSON */ }
    });
  }

  function scrollMessages() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setOpen(open) {
    drawer.classList.toggle('is-open', open);
    backdrop.classList.toggle('is-visible', open);
    fabBtn.classList.toggle('is-open', open);
    fabBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) input.focus();
  }

  function setAgentWorking(active) {
    agentBar.classList.toggle('is-active', !!active);
    sendBtn.disabled = streaming;
    faqEl.querySelectorAll('button').forEach(function (b) { b.disabled = streaming; });
  }

  function appendUser(text) {
    var el = document.createElement('div');
    el.className = 'stock-hermes-msg-user';
    el.textContent = text;
    messagesEl.appendChild(el);
    scrollMessages();
  }

  function appendAssistant(html, markdown) {
    var el = document.createElement('div');
    el.className = 'stock-hermes-msg-assistant';
    el.innerHTML = markdown
      ? '<div class="hermes-md">' + html + '</div>'
      : escapeHtml(html);
    messagesEl.appendChild(el);
    if (markdown) postProcessCharts(el);
    scrollMessages();
    return el;
  }

  function appendBlock(type, content, name) {
    var el = document.createElement('div');
    el.className = 'stock-hermes-block' + (type === 'tool' ? ' stock-hermes-block-tool' : '') +
      (type === 'error' ? ' stock-hermes-block-error' : '');
    var label = type === 'tool' ? ('🔧 ' + (name || 'tool')) : (type === 'error' ? '❌ ' : '🧠 ');
    el.textContent = label + (content || '').slice(0, 120);
    messagesEl.appendChild(el);
    scrollMessages();
  }

  function removeWelcome() {
    var w = messagesEl.querySelector('.stock-hermes-welcome');
    if (w) w.remove();
  }

  async function ensureSession() {
    if (sessionId) return sessionId;
    try {
      var resp = await fetch('/api/crawl-agent/chats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '' }),
      });
      if (!resp.ok) return null;
      var data = await resp.json();
      sessionId = data.session.session_id;
      localStorage.setItem(LS_KEY, sessionId);
      return sessionId;
    } catch (e) {
      return null;
    }
  }

  async function consumeSse(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var finalMessage = '';

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (var i = 0; i < parts.length; i++) {
        var line = parts[i].trim();
        if (!line.startsWith('data:')) continue;
        var jsonStr = line.slice(5).trim();
        if (!jsonStr) continue;
        try {
          var event = JSON.parse(jsonStr);
          if (event.type === 'session' && event.session_id) {
            sessionId = event.session_id;
            localStorage.setItem(LS_KEY, sessionId);
          } else if (event.type === 'thinking' || event.type === 'plan') {
            appendBlock('thinking', event.content);
          } else if (event.type === 'tool') {
            appendBlock('tool', event.name, event.name);
          } else if (event.type === 'error') {
            appendBlock('error', event.content);
          } else if (event.type === 'message') {
            finalMessage = event.content || '';
          } else if (event.type === 'done') {
            if (finalMessage) {
              appendAssistant(renderMarkdown(finalMessage), true);
              history.push({ role: 'assistant', content: finalMessage });
            }
            return;
          }
        } catch (e) { /* skip */ }
      }
    }
    if (finalMessage) {
      appendAssistant(renderMarkdown(finalMessage), true);
      history.push({ role: 'assistant', content: finalMessage });
    }
  }

  async function sendMessage(text) {
    if (!text.trim() || streaming) return;
    await ensureSession();
    streaming = true;
    setAgentWorking(true);
    streamAbortController = new AbortController();
    removeWelcome();
    appendUser(text);
    history.push({ role: 'user', content: text });
    input.value = '';

    try {
      var body = {
        message: text,
        history: history.slice(0, -1),
        agent_profile: AGENT_PROFILE,
      };
      if (sessionId) body.session_id = sessionId;

      var resp = await fetch('/api/crawl-agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal: streamAbortController.signal,
      });

      if (!resp.ok) {
        var errText = await resp.text();
        appendAssistant('请求失败: ' + errText, false);
        return;
      }
      await consumeSse(resp);
    } catch (e) {
      if (!streamAbortController || !streamAbortController.signal.aborted) {
        appendAssistant('网络错误: ' + String(e), false);
      }
    } finally {
      streaming = false;
      setAgentWorking(false);
      streamAbortController = null;
      input.focus();
    }
  }

  function renderFaq() {
    faqEl.innerHTML = FAQ_ITEMS.map(function (item, idx) {
      return '<button type="button" class="stock-hermes-faq-btn" data-faq="' + idx + '">' +
        escapeHtml(item.label) + '</button>';
    }).join('');
    faqEl.querySelectorAll('[data-faq]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var item = FAQ_ITEMS[parseInt(btn.getAttribute('data-faq'), 10)];
        if (item) sendMessage(item.message);
      });
    });
  }

  fabBtn.addEventListener('click', function () {
    setOpen(!drawer.classList.contains('is-open'));
  });
  closeBtn.addEventListener('click', function () { setOpen(false); });
  backdrop.addEventListener('click', function () { setOpen(false); });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    sendMessage(input.value.trim());
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      form.dispatchEvent(new Event('submit'));
    }
  });

  renderFaq();

  var lastId = localStorage.getItem(LS_KEY);
  if (lastId) sessionId = lastId;
})();
