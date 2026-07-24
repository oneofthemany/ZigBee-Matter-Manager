/*
 * adblock.js — frontend for the Beekeeper DNS ad/tracker blocker tab.
 *
 * Talks to /api/adblock/* (the main app proxies these to the Beekeeper sidecar's
 * loopback control API). Self-registers on DOMContentLoaded: polls while the
 * #beekeeper tab is visible and stops when it's hidden. No build step / ES
 * module — loaded as a classic <script> like the other page scripts.
 */
(function () {
  'use strict';

  var log = (typeof zmmLog === 'function') ? zmmLog('beekeeper') : function () {};
  var POLL_MS = 5000;
  var pollTimer = null;
  var chart = null;
  var lastAvailable = null;

  function $(id) { return document.getElementById(id); }

  async function api(path, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }
    try {
      var r = await fetch('/api/adblock' + path, opts);
      return await r.json();
    } catch (e) {
      log('fetch failed', path, e);
      return { available: false, error: 'request failed' };
    }
  }

  // ── rendering ──────────────────────────────────────────────────────────────
  function setOffline(msg) {
    var el = $('bkOffline');
    if (el) {
      el.classList.remove('d-none');
      if (msg) $('bkOfflineMsg').textContent = msg;
    }
    var pill = $('bkStatusPill');
    if (pill) { pill.className = 'badge bk-pill bk-offline ms-2'; pill.textContent = 'Offline'; }
  }
  function clearOffline() {
    var el = $('bkOffline');
    if (el) el.classList.add('d-none');
  }

  function renderStatus(st) {
    var pill = $('bkStatusPill');
    var sw = $('bkMasterSwitch');
    var running = st.running;
    var rt = st.runtime || {};
    sw.checked = !!running;
    var cls = 'badge bk-pill ms-2 ', label;
    if (!running) { cls += 'bk-off'; label = 'Off'; }
    else if (rt.paused) { cls += 'bk-paused'; label = 'Paused'; }
    else if (!rt.blocking_active) { cls += 'bk-paused'; label = 'Not blocking'; }
    else { cls += 'bk-on'; label = 'Active'; }
    pill.className = cls;
    pill.textContent = label;

    if (st.matcher) $('bkStatDomains').textContent = fmt((st.matcher.blocked || 0) + (st.matcher.denied || 0));
  }

  function fmt(n) {
    if (n == null) return '–';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  function renderSummary(s) {
    $('bkStatTotal').textContent = fmt(s.total);
    $('bkStatBlocked').textContent = fmt(s.blocked);
    $('bkStatPct').textContent = (s.blocked_pct != null ? s.blocked_pct : 0) + '%';
    $('bkStatClients').textContent = fmt(s.clients);
  }

  function renderTopList(elId, items, valueKey, nameKey) {
    var ul = $(elId);
    if (!items || !items.length) {
      ul.innerHTML = '<li class="list-group-item bk-empty">Nothing yet</li>';
      return;
    }
    ul.innerHTML = items.map(function (it) {
      var extra = (valueKey === 'total' && it.blocked != null)
        ? ' <span class="text-danger small">(' + it.blocked + ' blocked)</span>' : '';
      return '<li class="list-group-item"><span class="bk-list-name" title="' + esc(it[nameKey]) + '">'
        + esc(it[nameKey]) + '</span><span class="bk-list-count">' + fmt(it[valueKey]) + extra + '</span></li>';
    }).join('');
  }

  function renderRecent(items) {
    var body = $('bkRecentBody');
    if (!items || !items.length) {
      body.innerHTML = '<tr><td colspan="5" class="text-muted text-center py-3">No queries logged</td></tr>';
      return;
    }
    body.innerHTML = items.map(function (q) {
      var t = new Date(q.ts * 1000).toLocaleTimeString();
      var result = q.blocked
        ? '<span class="badge bk-badge-blocked">blocked</span>'
        : (q.cached ? '<span class="badge bk-badge-cached">cached</span>'
                    : '<span class="badge bk-badge-allowed">allowed</span>');
      return '<tr><td>' + t + '</td><td>' + esc(q.client) + '</td><td>' + esc(q.qname)
        + '</td><td>' + qtype(q.qtype) + '</td><td>' + result + '</td></tr>';
    }).join('');
  }

  function qtype(n) {
    var m = { 1: 'A', 28: 'AAAA', 5: 'CNAME', 15: 'MX', 16: 'TXT', 2: 'NS', 12: 'PTR', 65: 'HTTPS', 6: 'SOA' };
    return m[n] || String(n);
  }

  function renderLists(data) {
    var body = $('bkListsBody');
    var lists = (data && data.lists) || [];
    if (!lists.length) {
      body.innerHTML = '<tr><td colspan="4" class="text-muted text-center py-3">No lists yet — click “Refresh now”.</td></tr>';
      return;
    }
    body.innerHTML = lists.map(function (l) {
      var when = l.fetched_at ? new Date(l.fetched_at * 1000).toLocaleString() : '<span class="text-muted">never</span>';
      var err = l.error ? ' <span class="bk-list-err" title="' + esc(l.error) + '"><i class="fas fa-triangle-exclamation"></i></span>' : '';
      var name = esc(l.name) + err + (l.enabled ? '' : ' <span class="badge bg-secondary">disabled</span>');
      return '<tr><td>' + name + '</td><td class="text-end">' + fmt(l.count) + '</td><td class="small">' + when + '</td>'
        + '<td class="small text-muted text-truncate" style="max-width:220px" title="' + esc(l.url) + '">' + esc(l.url) + '</td></tr>';
    }).join('');
  }

  function renderRules(data) {
    renderChips('bkAllowList', (data && data.allow) || [], 'allow');
    renderChips('bkDenyList', (data && data.deny) || [], 'deny');
  }
  function renderChips(elId, domains, kind) {
    var el = $(elId);
    if (!domains.length) { el.innerHTML = '<span class="bk-rule-empty">None</span>'; return; }
    el.innerHTML = domains.map(function (d) {
      return '<span class="bk-chip">' + esc(d)
        + '<button title="Remove" data-kind="' + kind + '" data-domain="' + esc(d) + '">&times;</button></span>';
    }).join('');
  }

  function renderChart(series) {
    if (typeof echarts === 'undefined') return;
    var el = $('bkChart');
    if (!el) return;
    if (!chart) chart = echarts.init(el);
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var labels = series.map(function (b) {
      return new Date(b.start * 1000).getHours() + ':00';
    });
    chart.setOption({
      grid: { left: 40, right: 12, top: 24, bottom: 24 },
      tooltip: { trigger: 'axis' },
      legend: { data: ['Allowed', 'Blocked'], right: 0, textStyle: { color: isDark ? '#ccc' : '#333' } },
      xAxis: { type: 'category', data: labels, axisLabel: { color: isDark ? '#aaa' : '#666' } },
      yAxis: { type: 'value', axisLabel: { color: isDark ? '#aaa' : '#666' }, splitLine: { lineStyle: { color: isDark ? '#333' : '#eee' } } },
      series: [
        { name: 'Allowed', type: 'bar', stack: 'q', data: series.map(function (b) { return b.allowed; }), itemStyle: { color: '#6c9bd1' } },
        { name: 'Blocked', type: 'bar', stack: 'q', data: series.map(function (b) { return b.blocked; }), itemStyle: { color: '#dc3545' } }
      ]
    });
    chart.resize();
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── data refresh ─────────────────────────────────────────────────────────────
  async function refreshAll() {
    var st = await api('/status');
    if (st.available === false) {
      if (lastAvailable !== false) log('sidecar offline');
      lastAvailable = false;
      setOffline(st.error);
      return;
    }
    lastAvailable = true;
    clearOffline();
    renderStatus(st);

    var results = await Promise.all([
      api('/stats/summary?hours=24'),
      api('/stats/top-blocked?limit=8&hours=24'),
      api('/stats/top-clients?limit=8&hours=24'),
      api('/stats/recent?limit=40'),
      api('/stats/series?hours=24&buckets=24')
    ]);
    if (results[0].available !== false) renderSummary(results[0]);
    renderTopList('bkTopBlocked', results[1].items, 'count', 'qname');
    renderTopList('bkTopClients', results[2].items, 'total', 'client');
    renderRecent(results[3].items);
    if (results[4].series) renderChart(results[4].series);
  }

  async function refreshListsAndRules() {
    renderLists(await api('/lists'));
    renderRules(await api('/rules'));
  }

  // ── controls ─────────────────────────────────────────────────────────────────
  function toast(msg, ok) {
    if (typeof showToast === 'function') showToast(msg, ok ? 'success' : 'danger');
    else log(msg);
  }

  function wireControls() {
    $('bkMasterSwitch').addEventListener('change', async function () {
      var want = this.checked;
      var res = await api('/service', { method: 'POST', body: { action: want ? 'start' : 'stop' } });
      if (res.ok === false) {
        toast(res.error || 'Could not change service state', false);
        this.checked = !want; // revert
      } else {
        toast('Beekeeper ' + (want ? 'enabled' : 'disabled'), true);
      }
      refreshAll();
    });

    document.querySelectorAll('.bk-pause-opt').forEach(function (a) {
      a.addEventListener('click', async function (e) {
        e.preventDefault();
        var min = parseFloat(this.getAttribute('data-min'));
        if (min > 0) { await api('/pause', { method: 'POST', body: { minutes: min } }); toast('Blocking paused for ' + min + ' min', true); }
        else { await api('/resume', { method: 'POST' }); toast('Blocking resumed', true); }
        refreshAll();
      });
    });
    $('bkPauseBtn').addEventListener('click', async function () {
      await api('/pause', { method: 'POST', body: { minutes: 5 } });
      toast('Blocking paused for 5 min', true);
      refreshAll();
    });

    $('bkRefreshBtn').addEventListener('click', async function () {
      var btn = this; btn.disabled = true;
      btn.innerHTML = '<i class="fas fa-rotate fa-spin"></i> Refreshing…';
      var res = await api('/refresh', { method: 'POST' });
      btn.disabled = false; btn.innerHTML = '<i class="fas fa-rotate"></i> Refresh now';
      if (res.available === false) toast(res.error, false);
      else toast('Blocklists refreshed — ' + fmt(res.block_count) + ' domains', true);
      refreshListsAndRules(); refreshAll();
    });

    $('bkFlushBtn').addEventListener('click', async function () {
      var res = await api('/cache/flush', { method: 'POST' });
      toast('DNS cache flushed (' + (res.cleared || 0) + ' entries)', true);
    });

    document.querySelectorAll('.bk-add-rule').forEach(function (btn) {
      btn.addEventListener('click', function () { addRule(btn.getAttribute('data-kind')); });
    });
    $('bkAllowInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') addRule('allow'); });
    $('bkDenyInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') addRule('deny'); });

    // Remove chip (event delegation on both containers).
    ['bkAllowList', 'bkDenyList'].forEach(function (id) {
      $(id).addEventListener('click', async function (e) {
        var b = e.target.closest('button[data-domain]');
        if (!b) return;
        await api('/rules/remove', { method: 'POST', body: { kind: b.getAttribute('data-kind'), domain: b.getAttribute('data-domain') } });
        refreshListsAndRules();
      });
    });

    $('bkCheckBtn').addEventListener('click', doCheck);
    $('bkCheckInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') doCheck(); });

    var docsLink = $('bkDocsLink');
    if (docsLink) docsLink.addEventListener('click', function (e) {
      e.preventDefault();
      var docsTab = document.querySelector('button[data-bs-target="#wiki"]');
      if (docsTab && typeof bootstrap !== 'undefined') new bootstrap.Tab(docsTab).show();
    });

    var mgrLink = $('bkManagerLink');
    if (mgrLink) mgrLink.addEventListener('click', function (e) {
      e.preventDefault();
      // The manager sidecar publishes on :8001 on the same host, same scheme.
      var port = window.ZMM_MANAGER_PORT || 8001;
      window.open(window.location.protocol + '//' + window.location.hostname + ':' + port + '/', '_blank');
    });
  }

  async function addRule(kind) {
    var input = kind === 'allow' ? $('bkAllowInput') : $('bkDenyInput');
    var domain = (input.value || '').trim();
    if (!domain) return;
    var res = await api('/rules', { method: 'POST', body: { kind: kind, domain: domain } });
    if (res.available === false) { toast(res.error, false); return; }
    input.value = '';
    if (res.rules) renderRules(res.rules); else refreshListsAndRules();
  }

  async function doCheck() {
    var d = ($('bkCheckInput').value || '').trim();
    var out = $('bkCheckResult');
    if (!d) { out.classList.add('d-none'); return; }
    var res = await api('/check?domain=' + encodeURIComponent(d));
    out.classList.remove('d-none');
    if (res.available === false) { out.textContent = res.error; return; }
    out.innerHTML = res.blocked
      ? '<span class="bk-blocked">' + esc(d) + ' would be BLOCKED</span> (' + (res.reason || 'list') + ')'
      : '<span class="bk-allowed">' + esc(d) + ' is allowed</span>';
  }

  // ── lifecycle ────────────────────────────────────────────────────────────────
  function startPolling() {
    refreshAll();
    refreshListsAndRules();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refreshAll, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var tabBtn = document.querySelector('button[data-bs-target="#beekeeper"]');
    if (!tabBtn) return;
    wireControls();
    tabBtn.addEventListener('shown.bs.tab', function () {
      if (chart) chart.resize();
      startPolling();
    });
    tabBtn.addEventListener('hidden.bs.tab', stopPolling);
    window.addEventListener('resize', function () { if (chart) chart.resize(); });
    // If the page loads with the tab already active (deep link), start now.
    if (document.querySelector('#beekeeper.active')) startPolling();
  });
})();
