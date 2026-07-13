'use strict';

const $ = (sel) => document.querySelector(sel);

function send(type, payload = {}) {
  return chrome.runtime.sendMessage({ type, ...payload });
}

function actionLabel(action) {
  const map = {
    navigate: '导航',
    click: '点击',
    wait_network: '网络请求',
    pick_selector: '选中元素',
  };
  return map[action] || action;
}

function stepSummary(step) {
  const parts = [];
  if (step.url) parts.push(step.url);
  if (step.selector) parts.push(step.selector);
  if (step.text) parts.push(`"${step.text.slice(0, 30)}"`);
  if (step.url_pattern) parts.push(step.url_pattern);
  if (step.note) parts.push(`[${step.note}]`);
  return parts.join(' · ') || '—';
}

function renderSteps(steps) {
  const list = $('#steps-list');
  const count = $('#step-count');
  count.textContent = String(steps.length);
  list.innerHTML = '';

  if (!steps.length) {
    list.innerHTML = '<li class="step-item"><span class="step-detail">暂无步骤，开始录制后操作页面</span></li>';
    return;
  }

  steps.forEach((step, i) => {
    const li = document.createElement('li');
    li.className = 'step-item';
    li.innerHTML = `
      <span class="step-index">${i + 1}</span>
      <div class="step-body">
        <div class="step-action">${actionLabel(step.action)}</div>
        <div class="step-detail">${escapeHtml(stepSummary(step))}</div>
      </div>
      <button class="step-delete" data-index="${i}" title="删除">×</button>
    `;
    list.appendChild(li);
  });

  list.querySelectorAll('.step-delete').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.index, 10);
      await send('DELETE_STEP', { index: idx });
      await refresh();
    });
  });
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function setStatus(msg, isError = false) {
  const el = $('#status');
  el.textContent = msg;
  el.className = isError ? 'status error' : 'status';
}

async function refresh() {
  const state = await send('GET_STATE');
  const siteInput = $('#site-id');
  if (document.activeElement !== siteInput) {
    siteInput.value = state.siteId || '';
  }

  $('#btn-start').disabled = state.recording;
  $('#btn-stop').disabled = !state.recording;

  const urlEl = $('#current-url');
  if (state.recording) {
    urlEl.innerHTML = '<span class="recording-indicator"></span>录制中…';
  }

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url?.startsWith('http')) {
      const info = await chrome.tabs.sendMessage(tab.id, { type: 'GET_PAGE_INFO' }).catch(() => null);
      urlEl.textContent = info?.url || tab.url;
    } else if (!state.recording) {
      urlEl.textContent = tab?.url || '—';
    }
  } catch {
    if (!state.recording) urlEl.textContent = state.entryUrl || '—';
  }

  renderSteps(state.steps || []);
  return state;
}

$('#site-id').addEventListener('change', () => {
  send('SET_SITE_ID', { siteId: $('#site-id').value.trim() });
});

$('#btn-start').addEventListener('click', async () => {
  const siteId = $('#site-id').value.trim();
  if (!siteId) {
    setStatus('请先填写站点 ID', true);
    return;
  }
  await send('SET_SITE_ID', { siteId });
  await send('START_RECORDING', { siteId });
  setStatus('录制已开始，请在页面中操作');
  await refresh();
});

$('#btn-stop').addEventListener('click', async () => {
  await send('STOP_RECORDING');
  setStatus('录制已停止，可导出 JSON');
  await refresh();
});

$('#btn-clear').addEventListener('click', async () => {
  if (!confirm('确定清空所有步骤？')) return;
  await send('CLEAR_STEPS');
  setStatus('已清空');
  await refresh();
});

async function getExportPayload() {
  return send('EXPORT_JSON');
}

$('#btn-copy').addEventListener('click', async () => {
  const payload = await getExportPayload();
  if (!payload.steps?.length) {
    setStatus('无步骤可复制', true);
    return;
  }
  const text = JSON.stringify(payload, null, 2);
  await navigator.clipboard.writeText(text);
  setStatus('已复制到剪贴板');
});

$('#btn-export').addEventListener('click', async () => {
  const payload = await getExportPayload();
  if (!payload.steps?.length) {
    setStatus('无步骤可导出', true);
    return;
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${payload.site_id || 'recording'}_${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
  setStatus('JSON 已下载');
});

refresh();
setInterval(refresh, 2000);
