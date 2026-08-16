(function () {
  'use strict';

  function debounce(fn, wait) {
    var t;
    return function () {
      var ctx = this, args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, wait);
    };
  }

  function metricLabel(metric) {
    return ({ count: '公告数', budget: '预算', score: '评分', companies: '企业数' })[metric] || metric;
  }

  function formatMetric(value, metric) {
    if (metric === 'budget') {
      if (value >= 100000000) return (value / 100000000).toFixed(1) + ' 亿';
      if (value >= 10000) return (value / 10000).toFixed(1) + ' 万';
    }
    return String(Math.round(value * 10) / 10);
  }

  function pageState() {
    var root = document.querySelector('.industry-heatmap-page');
    if (!root) return {};
    return {
      domain: root.dataset.domain || '数字农业',
      industry: root.dataset.industry || '',
      days: parseInt(root.dataset.days || '30', 10),
      metric: root.dataset.metric || 'count',
      compare: root.dataset.compare || ''
    };
  }

  function syncFiltersToUrl() {
    var domain = document.getElementById('industry-domain');
    var industry = document.getElementById('industry-code');
    var metric = document.getElementById('industry-metric');
    var days = document.getElementById('industry-days');
    var compare = document.getElementById('industry-compare');
    if (!domain) return;
    var url = new URL(window.location.href);
    url.searchParams.set('domain', domain.value);
    if (industry && industry.value) url.searchParams.set('industry', industry.value);
    else url.searchParams.delete('industry');
    if (metric) url.searchParams.set('metric', metric.value);
    if (days) url.searchParams.set('days', days.value);
    if (compare && compare.value) url.searchParams.set('compare', compare.value);
    else url.searchParams.delete('compare');
    history.replaceState({}, '', url.toString());
  }

  function loadEcharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js';
      s.onload = function () { resolve(window.echarts); };
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function registerChinaMap(echarts) {
    return fetch('/static/geo/cn_provinces.json')
      .then(function (r) { return r.json(); })
      .catch(function () {
        return fetch('https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json').then(function (r) { return r.json(); });
      })
      .then(function (geoJson) {
        echarts.registerMap('china', geoJson);
        return geoJson;
      });
  }

  function fetchHeatmap(industryCode, opts) {
    var q = new URLSearchParams({
      domain: opts.domain || '数字农业',
      days: String(opts.days || 30),
      metric: opts.metric || 'count',
      level: opts.level || 'province'
    });
    if (opts.region) q.set('region', opts.region);
    return fetch('/api/industry/' + encodeURIComponent(industryCode) + '/heatmap?' + q.toString())
      .then(function (r) { return r.json(); });
  }

  function buildHeatmapOption(regions, metric, domain, sharedMax) {
    var isAgri = domain === '数字农业';
    var colorRange = isAgri
      ? ['#ecfdf5', '#a7f3d0', '#34d399', '#059669']
      : ['#ede9fe', '#c4b5fd', '#8b5cf6', '#7c3aed'];
    var mapData = (regions || []).map(function (r) {
      return { name: r.name, value: r.value, region_code: r.region_code, pct: r.pct, company_count: r.company_count };
    });
    var values = mapData.map(function (d) { return d.value; }).filter(function (v) { return v > 0; });
    var min = values.length ? Math.min.apply(null, values) : 0;
    var max = sharedMax != null ? sharedMax : (values.length ? Math.max.apply(null, values) : 1);
    return {
      tooltip: {
        trigger: 'item',
        formatter: function (params) {
          var d = params.data || {};
          if (!d.value) return params.name + '<br/>暂无数据';
          return [
            '<strong>' + params.name + '</strong>',
            metricLabel(metric) + '：' + formatMetric(d.value, metric),
            d.pct != null ? '占全国：' + d.pct + '%' : '',
            d.company_count != null ? '企业数：' + d.company_count : ''
          ].filter(Boolean).join('<br/>');
        }
      },
      visualMap: { type: 'continuous', min: min, max: max, left: 'left', bottom: 20, inRange: { color: colorRange }, text: ['高', '低'], calculable: true },
      series: [{ name: metricLabel(metric), type: 'map', map: 'china', roam: true, emphasis: { label: { show: true } }, data: mapData }]
    };
  }

  function renderSubIndustryHeatmap(container, data, opts) {
    if (!container || !window.echarts) return null;
    var chart = echarts.getInstanceByDom(container) || echarts.init(container);
    chart.setOption(buildHeatmapOption(data.regions || [], opts.metric, opts.domain, opts.sharedMax));
    chart.off('click');
    chart.on('click', function (params) {
      var code = params.data && params.data.region_code;
      if (!code) return;
      if (opts.level === 'city') {
        openProvinceDrawer(code, params.name, opts);
        return;
      }
      if (opts.onProvinceClick) {
        opts.onProvinceClick(code, params.name, opts);
        return;
      }
      drillToCityHeatmap(code, params.name, opts);
    });
    return chart;
  }

  function updateKpi(data) {
    var cov = document.getElementById('kpi-coverage');
    var total = document.getElementById('kpi-total');
    if (cov) cov.textContent = String(data.coverage_provinces || 0);
    if (total) total.textContent = formatMetric(data.total_value || 0, data.metric || 'count');
    var list = document.getElementById('industry-top-purchasers');
    if (list) {
      list.innerHTML = '';
      (data.top_purchasers || []).slice(0, 8).forEach(function (p) {
        var li = document.createElement('li');
        li.textContent = (p.name || '—') + ' · ' + (p.count || 0);
        list.appendChild(li);
      });
    }
  }

  function drillToCityHeatmap(regionCode, regionName, opts) {
    var next = Object.assign({}, opts, { region: regionCode, level: 'city', provinceName: regionName });
    fetchHeatmap(opts.industry, next).then(function (data) {
      updateKpi(data);
      var chartOpts = Object.assign({}, next, {
        onProvinceClick: function (cityCode, cityName, o) {
          openProvinceDrawer(cityCode, cityName, o);
        }
      });
      renderSubIndustryHeatmap(document.getElementById('industry-heatmap-chart'), data, chartOpts);
      var subtitle = document.querySelector('.industry-heatmap-page .stats-page-subtitle');
      if (subtitle) subtitle.textContent = (regionName || regionCode) + ' · 市级分布 · 近 ' + (opts.days || 30) + ' 天';
    });
  }

  function openProvinceDrawer(regionCode, regionName, opts) {
    var drawer = document.getElementById('industry-province-drawer');
    var title = document.getElementById('industry-drawer-title');
    var list = document.getElementById('industry-drawer-list');
    if (!drawer || !list || !opts.industry) return;
    title.textContent = (regionName || regionCode) + ' · 公告列表';
    drawer.hidden = false;
    list.innerHTML = '<li>加载中…</li>';
    var q = new URLSearchParams({ region: regionCode, domain: opts.domain, days: String(opts.days) });
    fetch('/api/industry/' + encodeURIComponent(opts.industry) + '/heatmap/notices?' + q.toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        list.innerHTML = '';
        (data.items || []).forEach(function (item) {
          var li = document.createElement('li');
          var a = document.createElement('a');
          a.href = item.content_href || item.url || '#';
          a.textContent = item.title || '—';
          a.target = '_blank';
          li.appendChild(a);
          list.appendChild(li);
        });
        if (!(data.items || []).length) list.innerHTML = '<li>暂无数据</li>';
      });
  }

  function renderCompareMode(codeA, codeB, opts) {
    var main = document.getElementById('industry-heatmap-chart');
    var compareEl = document.getElementById('industry-heatmap-compare-chart');
    if (!compareEl) return Promise.resolve();
    compareEl.classList.remove('hidden');
    document.querySelector('.industry-heatmap-layout')?.classList.add('industry-heatmap-compare-stack');
    return Promise.all([fetchHeatmap(codeA, opts), fetchHeatmap(codeB, opts)]).then(function (results) {
      var maxVal = Math.max.apply(null, results.flatMap(function (r) {
        return (r.regions || []).map(function (x) { return x.value || 0; });
      }).concat([1]));
      renderSubIndustryHeatmap(main, results[0], Object.assign({}, opts, { industry: codeA, sharedMax: maxVal }));
      renderSubIndustryHeatmap(compareEl, results[1], Object.assign({}, opts, { industry: codeB, sharedMax: maxVal }));
    });
  }

  function loadTaxonomy(domain) {
    return fetch('/api/industry/taxonomy?domain=' + encodeURIComponent(domain))
      .then(function (r) { return r.json(); });
  }

  function populateIndustrySelects(taxonomy, selected, compareSelected) {
    var sel = document.getElementById('industry-code');
    var cmp = document.getElementById('industry-compare');
    if (!sel) return;
    var items = [];
    (taxonomy.groups || []).forEach(function (g) {
      (g.items || []).forEach(function (it) { items.push(it); });
    });
    sel.innerHTML = '<option value="">请选择细分行业</option>';
    if (cmp) cmp.innerHTML = '<option value="">无对比</option>';
    items.forEach(function (it) {
      var opt = document.createElement('option');
      opt.value = it.code;
      opt.textContent = it.name;
      if (it.code === selected) opt.selected = true;
      sel.appendChild(opt);
      if (cmp) {
        var opt2 = opt.cloneNode(true);
        if (it.code === compareSelected) opt2.selected = true;
        cmp.appendChild(opt2);
      }
    });
  }

  function loadRecommendations(domain, days) {
    var wrap = document.getElementById('industry-recommend-cards');
    if (!wrap) return;
    fetch('/api/industry/recommendations?domain=' + encodeURIComponent(domain) + '&days=' + days)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        wrap.innerHTML = '';
        (data.items || []).forEach(function (item) {
          var card = document.createElement('button');
          card.type = 'button';
          card.className = 'industry-recommend-card';
          card.textContent = item.name + ' (' + item.count + ')';
          card.addEventListener('click', function () {
            var sel = document.getElementById('industry-code');
            if (sel) { sel.value = item.code; refreshHeatmap(); }
          });
          wrap.appendChild(card);
        });
      });
  }

  function refreshHeatmap() {
    var state = pageState();
    var domainEl = document.getElementById('industry-domain');
    var industryEl = document.getElementById('industry-code');
    var metricEl = document.getElementById('industry-metric');
    var daysEl = document.getElementById('industry-days');
    var compareEl = document.getElementById('industry-compare');
    var opts = {
      domain: domainEl ? domainEl.value : state.domain,
      days: daysEl ? parseInt(daysEl.value, 10) : state.days,
      metric: metricEl ? metricEl.value : state.metric,
      industry: industryEl ? industryEl.value : state.industry
    };
    syncFiltersToUrl();
    if (!opts.industry) return;
    var compare = compareEl ? compareEl.value : state.compare;
    if (compare) {
      renderCompareMode(opts.industry, compare, opts);
      return;
    }
    var compareChart = document.getElementById('industry-heatmap-compare-chart');
    if (compareChart) compareChart.classList.add('hidden');
    fetchHeatmap(opts.industry, opts).then(function (data) {
      window.__industryName = data.industry_name;
      updateKpi(data);
      renderSubIndustryHeatmap(document.getElementById('industry-heatmap-chart'), data, opts);
    });
  }

  function initIndustryHeatmapPage() {
    var state = pageState();
    var debouncedRefresh = debounce(refreshHeatmap, 300);
    loadEcharts().then(registerChinaMap).then(function () {
      loadTaxonomy(state.domain).then(function (taxonomy) {
        populateIndustrySelects(taxonomy, state.industry, state.compare);
        loadRecommendations(state.domain, state.days);
        if (state.industry) refreshHeatmap();
      });
    });
    ['industry-domain', 'industry-code', 'industry-metric', 'industry-days', 'industry-compare'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('change', debouncedRefresh);
    });
    var close = document.getElementById('industry-drawer-close');
    if (close) close.addEventListener('click', function () {
      var drawer = document.getElementById('industry-province-drawer');
      if (drawer) drawer.hidden = true;
    });
  }

  function initDashboardTabs() {
    var tabs = document.querySelectorAll('.industry-tab-btn');
    if (!tabs.length) return;
    tabs.forEach(function (btn) {
      btn.addEventListener('click', function () {
        tabs.forEach(function (b) { b.classList.remove('is-active'); });
        btn.classList.add('is-active');
        document.querySelectorAll('.industry-tab-panel').forEach(function (p) { p.classList.add('hidden'); });
        var panel = document.getElementById('tab-' + btn.dataset.tab);
        if (panel) panel.classList.remove('hidden');
        if (btn.dataset.tab === 'distribution') initDistributionTab();
        if (btn.dataset.tab === 'supply-chain') initSupplyChainTab();
        if (btn.dataset.tab === 'policy') initPolicyWindTab();
      });
    });
  }

  function initDistributionTab() {
    var el = document.getElementById('industry-distribution-chart');
    if (!el || el.dataset.loaded) return;
    loadEcharts().then(registerChinaMap).then(function () {
      fetch('/api/industry/distribution?domain=数字农业&days=30')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          renderSubIndustryHeatmap(el, data, { metric: 'count', domain: '数字农业' });
          el.dataset.loaded = '1';
        });
    });
  }

  function initSupplyChainTab() {
    var tbody = document.querySelector('#industry-supply-chain-table tbody');
    if (!tbody || tbody.dataset.loaded) return;
    fetch('/api/industry/domain%3A数字农业/supply-chain?days=30')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tbody.innerHTML = '';
        (data.edges || []).forEach(function (edge) {
          var tr = document.createElement('tr');
          tr.innerHTML = '<td>' + (edge.purchaser || '') + '</td><td>' + (edge.supplier || '') + '</td><td>' + (edge.notice_count || 0) + '</td><td>' + formatMetric(edge.total_amount || 0, 'budget') + '</td><td>' + (edge.latest_date || '—') + '</td><td>' + (edge.sample_url ? '<a href="' + edge.sample_url + '" target="_blank">链接</a>' : '') + '</td>';
          tbody.appendChild(tr);
        });
        tbody.dataset.loaded = '1';
      });
  }

  function initPolicyWindTab() {
    var cards = document.getElementById('industry-policy-cards');
    var chartEl = document.getElementById('industry-policy-trend-chart');
    if (!cards || cards.dataset.loaded) return;
    fetch('/api/industry/policy-bid-lag?domain=数字农业&lag_days=90')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        cards.innerHTML = '';
        (data.themes || []).slice(0, 3).forEach(function (t) {
          var card = document.createElement('div');
          card.className = 'industry-policy-card';
          card.innerHTML = '<strong>' + t.theme + '</strong><br/>lift ' + t.lift_pct + '% · ' + t.signal;
          cards.appendChild(card);
        });
        cards.dataset.loaded = '1';
        if (!chartEl) return;
        var topTheme = ((data.themes || [])[0] || {}).theme;
        if (!topTheme) return;
        loadEcharts().then(function () {
          fetch('/api/industry/policy-bid-trend?theme=' + encodeURIComponent(topTheme))
            .then(function (r) { return r.json(); })
            .then(function (trend) {
              var chart = echarts.init(chartEl);
              chart.setOption({
                tooltip: { trigger: 'axis' },
                legend: { data: ['政策', '招标'] },
                xAxis: { type: 'category', data: (trend.series || []).map(function (s) { return s.week; }) },
                yAxis: [{ type: 'value', name: '政策' }, { type: 'value', name: '招标' }],
                series: [
                  { name: '政策', type: 'line', data: (trend.series || []).map(function (s) { return s.policy_count; }) },
                  { name: '招标', type: 'line', yAxisIndex: 1, data: (trend.series || []).map(function (s) { return s.bid_count; }) }
                ]
              });
            });
        });
      });
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (document.querySelector('.industry-heatmap-page')) initIndustryHeatmapPage();
    initDashboardTabs();
  });

  window.industryDetailInit = function (opts) {
    loadEcharts().then(registerChinaMap).then(function () {
      fetchHeatmap(opts.industry, opts).then(function (data) {
        updateKpi(data);
        renderSubIndustryHeatmap(document.getElementById('industry-detail-chart'), data, opts);
      });
      fetch('/api/industry/' + encodeURIComponent(opts.industry) + '/supply-chain?domain=' + encodeURIComponent(opts.domain) + '&days=' + opts.days)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var tbody = document.querySelector('#industry-detail-supply-chain tbody');
          if (!tbody) return;
          tbody.innerHTML = '';
          (data.edges || []).slice(0, 20).forEach(function (edge) {
            var tr = document.createElement('tr');
            tr.innerHTML = '<td>' + (edge.purchaser || '') + '</td><td>' + (edge.supplier || '') + '</td><td>' + (edge.notice_count || 0) + '</td><td>' + formatMetric(edge.total_amount || 0, 'budget') + '</td><td>' + (edge.latest_date || '—') + '</td>';
            tbody.appendChild(tr);
          });
        });
    });
  };
})();
