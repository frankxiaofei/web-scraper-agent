(function () {
  'use strict';

  var BRAND_PIE_COLORS = [
    '#1e40af', '#3b82f6', '#60a5fa', '#2563eb', '#1d4ed8', '#93c5fd',
    '#0ea5e9', '#0284c7', '#0369a1', '#d97706', '#f59e0b', '#fbbf24'
  ];

  var MAX_PIE_SLICES = 6;
  var MIN_LABEL_PCT = 8;

  function prefersReducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function resolveColors(options) {
    return (options && options.colors && options.colors.length) ? options.colors : BRAND_PIE_COLORS;
  }

  function getWrap(canvas) {
    return canvas && canvas.closest('.stats-pie-wrap');
  }

  function showPieEmpty(canvasId, message) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) {
      return;
    }
    var wrap = getWrap(canvas);
    if (!wrap) {
      return;
    }
    canvas.hidden = true;
    canvas.setAttribute('aria-hidden', 'true');
    var empty = wrap.querySelector('.stats-pie-empty');
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'stats-pie-empty';
      empty.setAttribute('role', 'status');
      wrap.appendChild(empty);
    }
    empty.hidden = false;
    empty.innerHTML =
      '<p class="stats-pie-empty-title">' + escapeHtml(message || '暂无分布数据') + '</p>' +
      '<p class="stats-pie-empty-desc">同步或爬取数据后将在此展示占比分布。</p>';
    var table = wrap.querySelector('.stats-pie-data-table');
    if (table) {
      table.hidden = true;
    }
  }

  function hidePieEmpty(canvas) {
    var wrap = getWrap(canvas);
    if (!wrap) {
      return;
    }
    canvas.hidden = false;
    canvas.removeAttribute('aria-hidden');
    var empty = wrap.querySelector('.stats-pie-empty');
    if (empty) {
      empty.hidden = true;
    }
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function capPieRows(rows, labelKey, maxSlices) {
    maxSlices = maxSlices || MAX_PIE_SLICES;
    if (!rows || rows.length <= maxSlices) {
      return rows ? rows.slice() : [];
    }
    var head = rows.slice(0, maxSlices - 1);
    var tail = rows.slice(maxSlices - 1);
    var otherCount = tail.reduce(function (sum, row) {
      return sum + (row.count || 0);
    }, 0);
    var other = {
      count: otherCount,
      pct: null
    };
    other[labelKey] = '其他';
    return head.concat([other]);
  }

  function recalcPct(rows, total) {
    return rows.map(function (row) {
      var pct = total ? Math.round((row.count || 0) / total * 1000) / 10 : 0;
      return Object.assign({}, row, { pct: pct });
    });
  }

  function siteLabel(row) {
    return row.name || '未知站点';
  }

  function siteTooltipTitle(row) {
    var parts = [];
    if (row.company_name) {
      parts.push(row.company_name);
    }
    parts.push(row.name || '未知站点');
    return parts.join(' · ');
  }

  function legendPosition(options) {
    if (options && options.legendPosition) {
      return options.legendPosition;
    }
    if (window.matchMedia('(max-width: 640px)').matches) {
      return 'bottom';
    }
    return 'right';
  }

  function buildAriaSummary(rows, labelFn) {
    if (!rows.length) {
      return '暂无分布数据';
    }
    var top = rows.slice().sort(function (a, b) {
      return (b.count || 0) - (a.count || 0);
    })[0];
    var lead = labelFn(top) + ' ' + (top.pct != null ? top.pct : 0) + '%';
    return '共 ' + rows.length + ' 项，领先项为 ' + lead;
  }

  function renderDataTable(wrap, rows, labelFn) {
    var tableWrap = wrap.querySelector('.stats-pie-data-table');
    if (!tableWrap) {
      tableWrap = document.createElement('div');
      tableWrap.className = 'stats-pie-data-table';
      wrap.appendChild(tableWrap);
    }
    if (!rows.length) {
      tableWrap.hidden = true;
      tableWrap.innerHTML = '';
      return;
    }
    tableWrap.hidden = false;
    var rowsHtml = rows.map(function (row) {
      return '<tr><th scope="row">' + escapeHtml(labelFn(row)) + '</th>' +
        '<td>' + (row.count || 0) + ' 条</td>' +
        '<td>' + (row.pct != null ? row.pct : 0) + '%</td></tr>';
    }).join('');
    tableWrap.innerHTML =
      '<table><caption class="stats-pie-data-caption">分布数据表（辅助阅读）</caption>' +
      '<thead><tr><th scope="col">类别</th><th scope="col">数量</th><th scope="col">占比</th></tr></thead>' +
      '<tbody>' + rowsHtml + '</tbody></table>';
  }

  var pieDirectLabels = {
    id: 'pieDirectLabels',
    afterDatasetsDraw: function (chart) {
      var dataset = chart.data.datasets[0];
      var meta = chart.getDatasetMeta(0);
      if (!dataset || !meta || !meta.data) {
        return;
      }
      var total = dataset.data.reduce(function (sum, value) {
        return sum + value;
      }, 0);
      if (!total) {
        return;
      }
      var ctx = chart.ctx;
      meta.data.forEach(function (arc, index) {
        var value = dataset.data[index];
        var pct = Math.round(value / total * 1000) / 10;
        if (pct < MIN_LABEL_PCT) {
          return;
        }
        var pos = arc.tooltipPosition();
        ctx.save();
        ctx.fillStyle = '#ffffff';
        ctx.font = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.shadowColor = 'rgba(15, 23, 42, 0.35)';
        ctx.shadowBlur = 4;
        ctx.fillText(pct + '%', pos.x, pos.y);
        ctx.restore();
      });
    }
  };

  function buildPieOptions(rows, options, labelFn, tooltipTitleFn, tooltipLabelFn) {
    var colors = resolveColors(options);
    var position = legendPosition(options);
    var reducedMotion = prefersReducedMotion();

    return {
      responsive: true,
      maintainAspectRatio: true,
      aspectRatio: options.aspectRatio || (position === 'bottom' ? 1.35 : 1.15),
      layout: {
        padding: position === 'bottom' ? { top: 4, bottom: 2, left: 4, right: 4 } : 8
      },
      animation: reducedMotion ? false : {
        animateRotate: true,
        animateScale: false,
        duration: 280
      },
      interaction: {
        mode: 'nearest',
        intersect: true
      },
      plugins: {
        legend: {
          position: position,
          align: 'start',
          onClick: function (event, legendItem, legend) {
            Chart.defaults.plugins.legend.onClick.call(this, event, legendItem, legend);
          },
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            padding: 14,
            usePointStyle: true,
            pointStyle: 'circle',
            font: {
              size: 13,
              weight: '500',
              family: 'system-ui, -apple-system, "Segoe UI", sans-serif'
            },
            color: '#334155',
            generateLabels: function (chart) {
              var dataset = chart.data.datasets[0];
              return chart.data.labels.map(function (label, index) {
                var row = rows[index];
                var count = row.count || 0;
                var pct = row.pct != null ? row.pct : 0;
                return {
                  text: label + ' · ' + count + ' 条 · ' + pct + '%',
                  fillStyle: dataset.backgroundColor[index],
                  strokeStyle: dataset.backgroundColor[index],
                  lineWidth: 0,
                  hidden: !chart.getDataVisibility(index),
                  index: index
                };
              });
            }
          }
        },
        tooltip: {
          backgroundColor: '#1e293b',
          titleColor: '#f8fafc',
          bodyColor: '#e2e8f0',
          borderColor: '#334155',
          borderWidth: 1,
          titleFont: { size: 13, weight: '600' },
          bodyFont: { size: 12 },
          padding: 12,
          cornerRadius: 8,
          displayColors: true,
          boxPadding: 6,
          callbacks: {
            title: function (items) {
              if (!items.length) {
                return '';
              }
              return tooltipTitleFn(rows[items[0].dataIndex]);
            },
            label: function (item) {
              return tooltipLabelFn(rows[item.dataIndex]);
            }
          }
        }
      }
    };
  }

  function createPieChart(canvas, rows, options, labelFn, tooltipTitleFn, tooltipLabelFn) {
    var labels = rows.map(labelFn);
    var values = rows.map(function (row) {
      return row.count || 0;
    });
    var colors = rows.map(function (_row, index) {
      return resolveColors(options)[index % resolveColors(options).length];
    });

    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', buildAriaSummary(rows, labelFn));

    return new Chart(canvas, {
      type: 'pie',
      data: {
        labels: labels,
        datasets: [{
          data: values,
          backgroundColor: colors,
          borderColor: '#ffffff',
          borderWidth: 2,
          hoverOffset: prefersReducedMotion() ? 0 : 6,
          hoverBorderColor: '#ffffff'
        }]
      },
      options: buildPieOptions(rows, options, labelFn, tooltipTitleFn, tooltipLabelFn),
      plugins: [pieDirectLabels]
    });
  }

  function initSitePieChart(canvasId, rows, options) {
    options = options || {};
    var canvas = document.getElementById(canvasId);
    if (!canvas) {
      return null;
    }
    if (!rows || !rows.length) {
      showPieEmpty(canvasId, options.emptyMessage || '暂无站点分布数据');
      return null;
    }
    if (typeof Chart === 'undefined') {
      showPieEmpty(canvasId, '图表组件加载失败，请刷新页面');
      return null;
    }

    hidePieEmpty(canvas);
    var capped = capPieRows(rows, 'name', options.maxSlices || MAX_PIE_SLICES);
    var total = capped.reduce(function (sum, row) {
      return sum + (row.count || 0);
    }, 0);
    var enriched = recalcPct(capped, total);
    var wrap = getWrap(canvas);
    if (wrap) {
      renderDataTable(wrap, enriched, siteLabel);
    }

    var chart = createPieChart(
      canvas,
      enriched,
      options,
      siteLabel,
      function (row) {
        return siteTooltipTitle(row);
      },
      function (row) {
        return '数量：' + (row.count || 0) + ' 条（占比 ' + (row.pct != null ? row.pct : 0) + '%）';
      }
    );

    var resizeTimer;
    window.addEventListener('resize', function () {
      if (!chart) {
        return;
      }
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        chart.options.plugins.legend.position = legendPosition(options);
        chart.update('none');
      }, 150);
    });

    return chart;
  }

  function initPricePieChart(canvasId, rows, options) {
    options = options || {};
    var canvas = document.getElementById(canvasId);
    if (!canvas) {
      return null;
    }
    if (!rows || !rows.length) {
      showPieEmpty(canvasId, options.emptyMessage || '暂无价格区间数据');
      return null;
    }
    if (typeof Chart === 'undefined') {
      showPieEmpty(canvasId, '图表组件加载失败，请刷新页面');
      return null;
    }

    hidePieEmpty(canvas);
    var normalized = rows.map(function (row) {
      return {
        bucket: row.bucket || '未知区间',
        count: row.count || 0,
        pct: row.pct
      };
    });
    var capped = capPieRows(normalized, 'bucket', options.maxSlices || MAX_PIE_SLICES);
    var total = capped.reduce(function (sum, row) {
      return sum + (row.count || 0);
    }, 0);
    var enriched = recalcPct(capped, total);
    var wrap = getWrap(canvas);
    if (wrap) {
      renderDataTable(wrap, enriched, function (row) {
        return row.bucket;
      });
    }

    var chart = createPieChart(
      canvas,
      enriched,
      options,
      function (row) {
        return row.bucket;
      },
      function (row) {
        return row.bucket;
      },
      function (row) {
        return '数量：' + (row.count || 0) + ' 条（占比 ' + (row.pct != null ? row.pct : 0) + '%）';
      }
    );

    var resizeTimer;
    window.addEventListener('resize', function () {
      if (!chart) {
        return;
      }
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        chart.options.plugins.legend.position = legendPosition(options);
        chart.update('none');
      }, 150);
    });

    return chart;
  }

  window.BRAND_PIE_COLORS = BRAND_PIE_COLORS;
  window.initSitePieChart = initSitePieChart;
  window.initPricePieChart = initPricePieChart;
})();
