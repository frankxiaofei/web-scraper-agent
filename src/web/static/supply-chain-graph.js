(function () {
  'use strict';

  var TYPE_COLORS = {
    Company: '#2563eb',
    Industry: '#16a34a',
    Contract: '#ea580c',
    Region: '#9333ea',
    TenderNotice: '#64748b',
    Node: '#94a3b8'
  };

  function pageRoot() {
    return document.querySelector('.industry-graph-page');
  }

  function setStatus(text, isError) {
    var el = document.getElementById('graph-status');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'industry-graph-status' + (isError ? ' is-error' : '');
  }

  function buildQuery() {
    var center = (document.getElementById('graph-center-id') || {}).value || '';
    var industry = (document.getElementById('graph-industry-code') || {}).value || '';
    var depth = (document.getElementById('graph-depth') || {}).value || '2';
    var params = new URLSearchParams();
    if (center.trim()) params.set('center', center.trim());
    if (industry.trim()) params.set('industry_code', industry.trim());
    params.set('depth', depth);
    params.set('limit', '200');
    return '/api/industry/graph?' + params.toString();
  }

  function syncUrl() {
    var root = pageRoot();
    if (!root) return;
    var url = new URL(window.location.href);
    var center = (document.getElementById('graph-center-id') || {}).value || '';
    var industry = (document.getElementById('graph-industry-code') || {}).value || '';
    var depth = (document.getElementById('graph-depth') || {}).value || '2';
    if (center.trim()) url.searchParams.set('center', center.trim());
    else url.searchParams.delete('center');
    if (industry.trim()) url.searchParams.set('industry', industry.trim());
    else url.searchParams.delete('industry');
    url.searchParams.set('depth', depth);
    window.history.replaceState({}, '', url.toString());
  }

  function renderDetail(node) {
    var panel = document.getElementById('graph-node-detail');
    if (!panel || !node) return;
    var data = node.data();
    panel.classList.remove('hidden');
    panel.innerHTML =
      '<h3>' + (data.label || data.id) + '</h3>' +
      '<p><strong>类型</strong> ' + (data.type || 'Node') + '</p>' +
      '<p><strong>ID</strong> <code>' + data.id + '</code></p>';
  }

  var cyInstance = null;

  function renderGraph(payload) {
    var container = document.getElementById('industry-cy-graph');
    if (!container || typeof cytoscape === 'undefined') return;
    var elements = (payload.elements || {});
    var nodes = elements.nodes || [];
    var edges = elements.edges || [];
    if (cyInstance) {
      cyInstance.destroy();
      cyInstance = null;
    }
    cyInstance = cytoscape({
      container: container,
      elements: nodes.concat(edges),
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'text-valign': 'center',
            'text-halign': 'center',
            'font-size': 10,
            'background-color': function (ele) {
              return TYPE_COLORS[ele.data('type')] || TYPE_COLORS.Node;
            },
            width: 36,
            height: 36,
            'text-wrap': 'wrap',
            'text-max-width': 80
          }
        },
        {
          selector: 'edge',
          style: {
            label: 'data(type)',
            'font-size': 8,
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle',
            width: 2,
            'line-color': '#cbd5e1',
            'target-arrow-color': '#94a3b8'
          }
        },
        {
          selector: ':selected',
          style: {
            'border-width': 3,
            'border-color': '#0f172a'
          }
        }
      ],
      layout: { name: 'cose', animate: false, padding: 30 }
    });
    cyInstance.on('tap', 'node', function (evt) {
      renderDetail(evt.target);
    });
    setStatus('节点 ' + nodes.length + ' · 边 ' + edges.length + ' · 来源 ' + (payload.source || 'api'));
  }

  function loadGraph() {
    syncUrl();
    setStatus('加载中…');
    fetch(buildQuery())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderGraph(data);
      })
      .catch(function (err) {
        setStatus('加载失败: ' + err.message, true);
      });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('graph-reload-btn');
    if (btn) btn.addEventListener('click', loadGraph);
    loadGraph();
  });
})();
