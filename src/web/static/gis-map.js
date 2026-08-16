(function () {
  'use strict';

  function pageRoot() {
    return document.querySelector('.industry-map-page');
  }

  function state() {
    var root = pageRoot();
    if (!root) return {};
    return {
      domain: root.dataset.domain || '数字农业',
      industry: root.dataset.industry || '',
      days: parseInt(root.dataset.days || '30', 10),
      metric: root.dataset.metric || 'count',
      region: root.dataset.region || '',
      level: root.dataset.region ? 'city' : 'province'
    };
  }

  function fetchHeatmap(opts) {
    var q = new URLSearchParams({
      domain: opts.domain,
      days: String(opts.days),
      metric: opts.metric,
      level: opts.level || 'province'
    });
    if (opts.region) q.set('region', opts.region);
    return fetch('/api/industry/' + encodeURIComponent(opts.industry) + '/heatmap?' + q.toString())
      .then(function (r) { return r.json(); });
  }

  function colorScale(value, max) {
    if (!value || !max) return '#f1f5f9';
    var t = Math.min(1, value / max);
    var r = Math.round(236 - t * 180);
    var g = Math.round(253 - t * 120);
    var b = Math.round(245 - t * 100);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  function renderLegend(regions, max) {
    var el = document.getElementById('industry-map-legend');
    if (!el) return;
    el.innerHTML = '<strong>图例</strong> · 最高 ' + Math.round(max) + ' · ' + regions.length + ' 个区域';
  }

  function renderBreadcrumb(data, opts) {
    var el = document.getElementById('industry-map-breadcrumb');
    if (!el) return;
    var parts = ['<a href="/insights/map">全国</a>'];
    if (opts.region) {
      parts.push('<span>›</span><span>' + (data.parent_region_name || opts.region) + '</span>');
    }
    if (data.industry_name) {
      parts.push('<span>›</span><span>' + data.industry_name + '</span>');
    }
    el.innerHTML = parts.join(' ');
  }

  function renderMap(data, opts) {
    var container = document.getElementById('industry-leaflet-map');
    if (!container || !window.L) return;

    if (container._leafletMap) {
      container._leafletMap.remove();
      container._leafletMap = null;
    }

    var map = L.map(container).setView([35.0, 105.0], opts.region ? 7 : 4);
    container._leafletMap = map;
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap'
    }).addTo(map);

    var regions = data.regions || [];
    var maxVal = Math.max.apply(null, regions.map(function (r) { return r.value || 0; }).concat([1]));
    renderLegend(regions, maxVal);
    renderBreadcrumb(data, opts);

    regions.forEach(function (region) {
      if (!region.value) return;
      var lat = 20 + Math.random() * 20;
      var lng = 100 + Math.random() * 20;
      var marker = L.circleMarker([lat, lng], {
        radius: 8 + Math.sqrt(region.value) * 3,
        fillColor: colorScale(region.value, maxVal),
        color: '#334155',
        weight: 1,
        fillOpacity: 0.85
      }).addTo(map);
      marker.bindPopup(
        '<strong>' + region.name + '</strong><br/>' +
        '值：' + region.value + '<br/>' +
        '占比：' + (region.pct || 0) + '%'
      );
      if (!opts.region && data.level === 'province') {
        marker.on('click', function () {
          drillToProvince(region.region_code, opts);
        });
      }
    });
  }

  function drillToProvince(regionCode, opts) {
    var root = pageRoot();
    if (!root) return;
    root.dataset.region = regionCode;
    var next = Object.assign({}, opts, { region: regionCode, level: 'city' });
    var url = new URL(window.location.href);
    url.searchParams.set('region', regionCode);
    history.replaceState({}, '', url.toString());
    fetchHeatmap(next).then(function (data) { renderMap(data, next); });
  }

  function bindFilters() {
    ['industry-domain', 'industry-code', 'industry-metric', 'industry-days'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('change', function () {
        var root = pageRoot();
        if (!root) return;
        var industryEl = document.getElementById('industry-code');
        root.dataset.industry = industryEl ? industryEl.value : '';
        root.dataset.domain = document.getElementById('industry-domain')?.value || '数字农业';
        root.dataset.metric = document.getElementById('industry-metric')?.value || 'count';
        root.dataset.days = document.getElementById('industry-days')?.value || '30';
        root.dataset.region = '';
        var opts = state();
        opts.industry = root.dataset.industry;
        if (!opts.industry) return;
        fetchHeatmap(opts).then(function (data) { renderMap(data, opts); });
      });
    });
  }

  function loadTaxonomy(domain, selected) {
    return fetch('/api/industry/taxonomy?domain=' + encodeURIComponent(domain) + '&taxonomy=GB2017')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var sel = document.getElementById('industry-code');
        if (!sel) return;
        sel.innerHTML = '<option value="">请选择行业</option>';
        (data.groups || []).forEach(function (g) {
          (g.items || []).forEach(function (it) {
            var opt = document.createElement('option');
            opt.value = it.code;
            opt.textContent = it.name + ' (' + it.code + ')';
            if (it.code === selected) opt.selected = true;
            sel.appendChild(opt);
          });
        });
      });
  }

  function init() {
    if (!pageRoot()) return;
    var opts = state();
    bindFilters();
    loadTaxonomy(opts.domain, opts.industry).then(function () {
      if (!opts.industry) return;
      fetchHeatmap(opts).then(function (data) { renderMap(data, opts); });
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
