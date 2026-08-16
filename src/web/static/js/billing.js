/** Billing settings UI — /settings/billing (Commercial C1-14) */
(function () {
  'use strict';

  function authHeaders() {
    var token = localStorage.getItem('access_token');
    if (!token) return {};
    return { Authorization: 'Bearer ' + token };
  }

  function pct(used, limit) {
    if (limit == null || limit <= 0) return 0;
    return Math.min(100, Math.round((used / limit) * 100));
  }

  function barHtml(label, used, limit, unit) {
    var unlimited = limit == null;
    var p = unlimited ? 0 : pct(used, limit);
    var limitText = unlimited ? '无限制' : limit + ' ' + (unit || '');
    return (
      '<div><div class="flex justify-between text-sm mb-1">' +
      '<span class="text-slate-700">' + label + '</span>' +
      '<span class="text-slate-500">' + used + ' / ' + limitText + '</span></div>' +
      '<div class="h-2 bg-slate-100 rounded-full overflow-hidden">' +
      '<div class="h-full bg-brand-600 rounded-full transition-all" style="width:' + p + '%"></div></div></div>'
    );
  }

  function showError(msg) {
    var el = document.getElementById('billing-error');
    el.textContent = msg;
    el.classList.remove('hidden');
    document.getElementById('billing-loading').classList.add('hidden');
  }

  function loadBilling() {
    var token = localStorage.getItem('access_token');
    if (!token) {
      window.location.href = '/login?redirect=' + encodeURIComponent('/settings/billing');
      return;
    }

    Promise.all([
      fetch('/api/billing/subscription', { headers: authHeaders() }).then(function (r) {
        if (r.status === 401) throw new Error('auth');
        return r.json();
      }),
      fetch('/api/billing/usage', { headers: authHeaders() }).then(function (r) { return r.json(); }),
      fetch('/api/billing/plans').then(function (r) { return r.json(); })
    ]).then(function (results) {
      var sub = results[0];
      var usage = results[1];
      var plans = results[2];

      document.getElementById('billing-loading').classList.add('hidden');
      document.getElementById('billing-plan-card').classList.remove('hidden');
      document.getElementById('billing-usage-section').classList.remove('hidden');
      document.getElementById('billing-buyer-section').classList.remove('hidden');

      var planName = sub.plan_id || usage.plan_id || 'free';
      var planMeta = (plans.plans || []).find(function (p) { return p.id === planName; });
      document.getElementById('billing-plan-name').textContent = planMeta ? planMeta.name : planName;
      document.getElementById('billing-plan-status').textContent =
        sub.status ? ('状态: ' + sub.status) : '';

      var upgradeBtn = document.getElementById('billing-upgrade-btn');
      if (planName === 'free') {
        upgradeBtn.classList.remove('hidden');
        upgradeBtn.addEventListener('click', function () {
          fetch('/api/billing/subscribe', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            body: JSON.stringify({ plan_id: 'pro', billing_cycle: 'monthly' })
          }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
              if (res.ok) window.location.reload();
              else alert(res.body.detail || '升级失败');
            });
        });
      }

      var stripeBtn = document.getElementById('billing-stripe-checkout-btn');
      if (stripeBtn && planName === 'free') {
        stripeBtn.classList.remove('hidden');
        stripeBtn.addEventListener('click', function () {
          fetch('/api/billing/checkout-session', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            body: JSON.stringify({
              plan_id: 'pro',
              billing_cycle: 'monthly',
              success_url: window.location.origin + '/settings/billing?checkout=success',
              cancel_url: window.location.origin + '/settings/billing?checkout=cancel'
            })
          }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
              if (res.ok && res.body.checkout_url) window.location.href = res.body.checkout_url;
              else alert(res.body.detail || '无法创建 Checkout');
            });
        });
      }

      var portalBtn = document.getElementById('billing-portal-btn');
      if (portalBtn && planName !== 'free') {
        portalBtn.classList.remove('hidden');
        portalBtn.addEventListener('click', function () {
          fetch('/api/billing/portal?return_url=' + encodeURIComponent(window.location.href), {
            headers: authHeaders()
          }).then(function (r) { return r.json(); })
            .then(function (body) {
              if (body.portal_url) window.location.href = body.portal_url;
              else alert(body.detail || '无法打开 Portal');
            });
        });
      }

      var buyerSave = document.getElementById('billing-buyer-save');
      if (buyerSave) {
        buyerSave.addEventListener('click', function () {
          fetch('/api/billing/buyer-info', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            body: JSON.stringify({
              buyer_name: document.getElementById('billing-buyer-name').value,
              buyer_tax_id: document.getElementById('billing-buyer-tax-id').value || null
            })
          }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
            .then(function (res) {
              if (res.ok) alert('已保存');
              else alert(res.body.detail || '保存失败');
            });
        });
      }

      var metrics = usage.metrics || {};
      var bars = document.getElementById('billing-usage-bars');
      var labels = {
        crawl_runs: '每日爬取',
        llm_extraction_tokens: 'LLM Token',
        storage_gb: '存储',
        hermes_messages: 'Hermes 消息',
        api_calls: 'API 调用'
      };
      bars.innerHTML = Object.keys(labels).map(function (key) {
        var m = metrics[key] || { used: 0, limit: null, unit: '' };
        return barHtml(labels[key], m.used, m.limit, m.unit);
      }).join('');
    }).catch(function (err) {
      if (err && err.message === 'auth') {
        window.location.href = '/login?redirect=' + encodeURIComponent('/settings/billing');
        return;
      }
      showError('无法加载订阅信息');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadBilling);
  } else {
    loadBilling();
  }
})();
