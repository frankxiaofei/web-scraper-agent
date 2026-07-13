/**
 * Service Worker：维护录制状态与步骤序列。
 */
'use strict';

const DEFAULT_STATE = {
  recording: false,
  siteId: '',
  entryUrl: '',
  recordedAt: null,
  steps: [],
  hints: {},
};

async function getState() {
  const data = await chrome.storage.local.get('recordingState');
  return { ...DEFAULT_STATE, ...(data.recordingState || {}) };
}

async function setState(partial) {
  const state = await getState();
  const next = { ...state, ...partial };
  await chrome.storage.local.set({ recordingState: next });
  return next;
}

async function broadcastRecording(recording) {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (!tab.id || !tab.url?.startsWith('http')) continue;
    chrome.tabs.sendMessage(tab.id, { type: 'SET_RECORDING', recording }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case 'GET_STATE':
        sendResponse(await getState());
        break;

      case 'START_RECORDING': {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        const entryUrl = tab?.url || '';
        const state = await setState({
          recording: true,
          siteId: msg.siteId || '',
          entryUrl,
          recordedAt: new Date().toISOString(),
          steps: [{ action: 'navigate', url: entryUrl, timestamp: new Date().toISOString() }],
          hints: {},
        });
        if (tab?.id) {
          await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            files: ['selector.js', 'content.js'],
          }).catch(() => {});
          chrome.tabs.sendMessage(tab.id, { type: 'SET_RECORDING', recording: true });
        }
        await broadcastRecording(true);
        sendResponse(state);
        break;
      }

      case 'STOP_RECORDING': {
        const state = await setState({ recording: false });
        await broadcastRecording(false);
        sendResponse(state);
        break;
      }

      case 'ADD_STEP': {
        const state = await getState();
        if (!state.recording) return sendResponse(state);
        const step = msg.step;
        const last = state.steps[state.steps.length - 1];
        if (
          last &&
          step.action === 'navigate' &&
          last.action === 'navigate' &&
          last.url === step.url
        ) {
          return sendResponse(state);
        }
        state.steps.push(step);
        await setState({ steps: state.steps });
        sendResponse(state);
        break;
      }

      case 'DELETE_STEP': {
        const state = await getState();
        state.steps.splice(msg.index, 1);
        await setState({ steps: state.steps });
        sendResponse(state);
        break;
      }

      case 'UPDATE_HINTS': {
        const state = await getState();
        state.hints = { ...state.hints, ...msg.hints };
        await setState({ hints: state.hints });
        sendResponse(state);
        break;
      }

      case 'SET_SITE_ID':
        sendResponse(await setState({ siteId: msg.siteId || '' }));
        break;

      case 'CLEAR_STEPS':
        sendResponse(await setState({ steps: [], hints: {}, recordedAt: null, entryUrl: '' }));
        break;

      case 'EXPORT_JSON': {
        const state = await getState();
        const payload = {
          site_id: state.siteId,
          entry_url: state.entryUrl || state.steps.find((s) => s.action === 'navigate')?.url || '',
          recorded_at: state.recordedAt || new Date().toISOString(),
          steps: state.steps,
          hints: state.hints,
        };
        sendResponse(payload);
        break;
      }

      default:
        sendResponse(null);
    }
  })();
  return true;
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete') return;
  const state = await getState();
  if (!state.recording || !tab.url?.startsWith('http')) return;
  chrome.tabs.sendMessage(tabId, { type: 'SET_RECORDING', recording: true }).catch(() => {});
});
