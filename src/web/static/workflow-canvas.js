/**
 * 工作流画布视口控制器 — 平移、缩放、最大化、localStorage 视图布局。
 * 供站点爬取规则工作流编辑页使用。
 */
(function(global) {
  'use strict';

  var MIN_SCALE = 0.35;
  var MAX_SCALE = 2.5;
  var ZOOM_STEP = 0.12;

  function initWorkflowCanvasViewport(options) {
    options = options || {};
    var shell = document.getElementById(options.shellId || 'crawl-workflow-shell');
    var viewport = document.getElementById(options.viewportId || 'workflow-canvas');
    var transformEl = document.getElementById(options.transformId || 'workflow-canvas-transform');
    var storageKey = options.storageKey || 'crawl_workflow_viewport_v1';

    if (!shell || !viewport || !transformEl) return null;

    var state = {
      panX: 40,
      panY: 40,
      scale: 1,
      isPanning: false,
      panStartX: 0,
      panStartY: 0,
      panOriginX: 0,
      panOriginY: 0,
      maximized: false
    };

    function applyTransform() {
      transformEl.style.transform = 'translate(' + state.panX + 'px, ' + state.panY + 'px) scale(' + state.scale + ')';
      var label = document.getElementById(options.zoomLabelId || 'wf-zoom-label');
      if (label) label.textContent = Math.round(state.scale * 100) + '%';
      if (typeof options.onTransformChange === 'function') {
        options.onTransformChange(state);
      }
    }

    function screenToWorld(clientX, clientY) {
      var rect = viewport.getBoundingClientRect();
      return {
        x: (clientX - rect.left - state.panX) / state.scale,
        y: (clientY - rect.top - state.panY) / state.scale
      };
    }

    function worldToScreen(worldX, worldY) {
      var rect = viewport.getBoundingClientRect();
      return {
        x: worldX * state.scale + state.panX + rect.left,
        y: worldY * state.scale + state.panY + rect.top
      };
    }

    function shouldIgnorePanTarget(target) {
      if (!target || !target.closest) return false;
      return !!(
        target.closest('.workflow-node-card') ||
        target.closest('.workflow-edge-group') ||
        target.closest('.workflow-node-port')
      );
    }

    function onViewportMouseDown(e) {
      if (e.button !== 0) return;
      if (shouldIgnorePanTarget(e.target)) return;
      state.isPanning = true;
      state.panStartX = e.clientX;
      state.panStartY = e.clientY;
      state.panOriginX = state.panX;
      state.panOriginY = state.panY;
      viewport.classList.add('is-panning');
      document.addEventListener('mousemove', onViewportMouseMove);
      document.addEventListener('mouseup', onViewportMouseUp);
    }

    function onViewportMouseMove(e) {
      if (!state.isPanning) return;
      state.panX = state.panOriginX + (e.clientX - state.panStartX);
      state.panY = state.panOriginY + (e.clientY - state.panStartY);
      applyTransform();
    }

    function onViewportMouseUp() {
      state.isPanning = false;
      viewport.classList.remove('is-panning');
      document.removeEventListener('mousemove', onViewportMouseMove);
      document.removeEventListener('mouseup', onViewportMouseUp);
      saveLayout(true);
    }

    function zoomAt(factor, clientX, clientY) {
      var rect = viewport.getBoundingClientRect();
      var cx = clientX != null ? clientX : rect.left + rect.width / 2;
      var cy = clientY != null ? clientY : rect.top + rect.height / 2;
      var before = screenToWorld(cx, cy);
      var next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, state.scale * factor));
      if (next === state.scale) return;
      state.scale = next;
      state.panX = cx - rect.left - before.x * state.scale;
      state.panY = cy - rect.top - before.y * state.scale;
      applyTransform();
    }

    function resetView() {
      state.panX = 40;
      state.panY = 40;
      state.scale = 1;
      applyTransform();
      saveLayout(true);
    }

    function fitToBBox(bbox, padding) {
      padding = padding != null ? padding : 48;
      if (!bbox) return;
      var nodesW = Math.max(1, bbox.maxX - bbox.minX);
      var nodesH = Math.max(1, bbox.maxY - bbox.minY);
      var rect = viewport.getBoundingClientRect();
      var scaleX = (rect.width - padding * 2) / nodesW;
      var scaleY = (rect.height - padding * 2) / nodesH;
      state.scale = Math.min(1, Math.max(MIN_SCALE, Math.min(scaleX, scaleY, 1)));
      state.panX = (rect.width - nodesW * state.scale) / 2 - bbox.minX * state.scale;
      state.panY = (rect.height - nodesH * state.scale) / 2 - bbox.minY * state.scale;
      applyTransform();
    }

    function saveLayout(silent) {
      try {
        var payload = {
          panX: state.panX,
          panY: state.panY,
          scale: state.scale
        };
        localStorage.setItem(storageKey, JSON.stringify(payload));
        if (!silent && typeof showToast === 'function') {
          showToast('视图布局已保存', 'success');
        }
      } catch (err) {
        if (!silent && typeof showToast === 'function') {
          showToast('保存失败: ' + String(err), 'error');
        }
      }
    }

    function loadLayout() {
      try {
        var raw = localStorage.getItem(storageKey);
        if (!raw) return false;
        var data = JSON.parse(raw);
        if (typeof data.panX === 'number') state.panX = data.panX;
        if (typeof data.panY === 'number') state.panY = data.panY;
        if (typeof data.scale === 'number') state.scale = data.scale;
        applyTransform();
        return true;
      } catch (_err) {
        return false;
      }
    }

    function setMaximized(on) {
      state.maximized = !!on;
      shell.classList.toggle('is-maximized', state.maximized);
      document.body.classList.toggle('crawl-workflow-body-lock', state.maximized);
      var label = document.getElementById(options.maximizeLabelId || 'maximize-label');
      var btn = document.getElementById(options.maximizeBtnId || 'btn-maximize');
      if (label) label.textContent = state.maximized ? '退出全屏' : '最大化';
      if (btn) btn.setAttribute('aria-pressed', state.maximized ? 'true' : 'false');
      if (state.maximized) viewport.focus();
    }

    function toggleMaximize() {
      setMaximized(!state.maximized);
    }

    viewport.addEventListener('mousedown', onViewportMouseDown);
    viewport.addEventListener('wheel', function(e) {
      e.preventDefault();
      var factor = e.deltaY < 0 ? 1 + ZOOM_STEP : 1 - ZOOM_STEP;
      zoomAt(factor, e.clientX, e.clientY);
    }, { passive: false });

    var btnZoomIn = document.getElementById(options.zoomInBtnId || 'btn-zoom-in');
    var btnZoomOut = document.getElementById(options.zoomOutBtnId || 'btn-zoom-out');
    var btnReset = document.getElementById(options.resetBtnId || 'btn-reset-view');
    var btnSave = document.getElementById(options.saveBtnId || 'btn-save-viewport');
    var btnMax = document.getElementById(options.maximizeBtnId || 'btn-maximize');

    if (btnZoomIn) btnZoomIn.addEventListener('click', function() { zoomAt(1 + ZOOM_STEP); });
    if (btnZoomOut) btnZoomOut.addEventListener('click', function() { zoomAt(1 - ZOOM_STEP); });
    if (btnReset) btnReset.addEventListener('click', resetView);
    if (btnSave) btnSave.addEventListener('click', function() { saveLayout(false); });
    if (btnMax) btnMax.addEventListener('click', toggleMaximize);

    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && state.maximized) {
        setMaximized(false);
      }
    });

    applyTransform();

    return {
      screenToWorld: screenToWorld,
      worldToScreen: worldToScreen,
      getState: function() { return Object.assign({}, state); },
      applyTransform: applyTransform,
      saveLayout: saveLayout,
      loadLayout: loadLayout,
      resetView: resetView,
      fitToBBox: fitToBBox,
      zoomAt: zoomAt,
      setMaximized: setMaximized,
      toggleMaximize: toggleMaximize
    };
  }

  global.initWorkflowCanvasViewport = initWorkflowCanvasViewport;
})(window);
