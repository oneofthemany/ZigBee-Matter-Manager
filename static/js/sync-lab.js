/**
 * sync-lab.js — Sync Lab: per-session analysis of speaker-sync tests.
 *
 * Renders into #syncLabHost (Media → Group → sync pane) from the group's
 * own DuckDB via /api/media/sync/{sessions,session,model}:
 *   - convergence chart: per-speaker playback error vs elapsed time, with
 *     the ±60 ms lock window, hard-resync (◆) and manual-trim (▲) events
 *   - PLL chart: per-speaker stream rate correction (ppm) locking onto the
 *     device's true clock offset
 *   - per-speaker tiles: startup lag, time-to-lock, settled median/p95
 *     error, resyncs, final drift — plus the cross-session learned model
 *   - a data table (accessibility relief + exact numbers)
 *
 * Colours: fixed per speaker by group-member order (colour follows the
 * entity), palette validated for CVD + both themes (dataviz procedure).
 * Live mode: while this group's session is running, refreshes every 3 s.
 */
import { createChart } from './chart-utils.js';

const log = zmmLog('sync-lab');

const PALETTE = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#4a3aa7'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#9085e9'],
    extra: { light: '#7d858f', dark: '#8f98a3' },   // members beyond 4 — never cycle hues
};
const LOCK_MS = 60;           // matches STREAM_CORRECT_MIN_S server-side

let _gid = null;              // open group id ('' = closed)
let _group = null;            // {id, name, members:[{player_id, name}]}
let _sessions = [];
let _selected = '';           // selected session_id
let _model = {};              // /api/media/sync/model
let _detail = null;           // /api/media/sync/session payload
let _trend = [];              // /api/media/sync/trend payload
let _convChart = null;
let _pllChart = null;
let _trendAChart = null;
let _trendBChart = null;
let _timer = null;
let _live = false;            // this group's session is actually running
let _themeHook = null;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const _dark = () => document.documentElement.getAttribute('data-theme') === 'dark';
const _pal = () => _dark() ? PALETTE.dark : PALETTE.light;
const fmtMs = v => (v == null || !isFinite(v)) ? '—' : `${Math.round(v)} ms`;
const fmtS = v => (v == null || !isFinite(v)) ? '—' : `${Number(v).toFixed(1)} s`;
const fmtPpm = v => (v == null || !isFinite(v)) ? '—' : `${Math.round(v)} ppm`;

function _colorFor(pid) {
    const members = (_group && _group.members) || [];
    const i = members.findIndex(m => m.player_id === pid);
    if (i < 0 || i >= 4) return PALETTE.extra[_dark() ? 'dark' : 'light'];
    return _pal()[i];
}

function _nameFor(pid) {
    const m = ((_group && _group.members) || []).find(x => x.player_id === pid);
    return m ? m.name : pid;
}

// ---------------------------------------------------------------------------
// Public API (wired from media.js)
// ---------------------------------------------------------------------------
export async function openSyncLab(gid, group) {
    if (_gid === gid) { closeSyncLab(); return; }   // toggle
    closeSyncLab();
    _gid = gid;
    _group = group || { id: gid, name: gid, members: [] };
    await _loadAll();
    _renderShell();
    _renderDetail();
    _startLive();
    if (!_themeHook) {
        _themeHook = () => { if (_gid) { _renderShell(); _renderDetail(); } };
        document.addEventListener('themechange', _themeHook);
    }
    document.getElementById('syncLab')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function closeSyncLab() {
    _stopLive();
    if (_convChart) { _convChart.dispose(); _convChart = null; }
    if (_pllChart) { _pllChart.dispose(); _pllChart = null; }
    if (_trendAChart) { _trendAChart.dispose(); _trendAChart = null; }
    if (_trendBChart) { _trendBChart.dispose(); _trendBChart = null; }
    if (_themeHook) { document.removeEventListener('themechange', _themeHook); _themeHook = null; }
    const host = document.getElementById('syncLabHost');
    if (host) host.innerHTML = '';
    _gid = null; _group = null; _detail = null; _sessions = []; _selected = '';
}

/** Re-open after the sync pane re-renders (renderSyncPane wipes the host). */
export function restoreSyncLab() {
    if (!_gid) return;
    const gid = _gid;
    _gid = null;                        // defeat the toggle in openSyncLab
    openSyncLab(gid, _group);
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------
async function _loadAll() {
    const [s, m, st, tr] = await Promise.all([
        fetch(`/api/media/sync/sessions?group_id=${encodeURIComponent(_gid)}`).then(r => r.json()),
        fetch('/api/media/sync/model').then(r => r.json()),
        fetch('/api/media/sync/status').then(r => r.json()).catch(() => null),
        fetch(`/api/media/sync/trend?group_id=${encodeURIComponent(_gid)}`).then(r => r.json()).catch(() => null),
    ]);
    _sessions = s.sessions || [];
    _model = m.model || {};
    _trend = (tr && tr.trend) || [];
    _live = !!(st && st.running && st.group_id === _gid);
    if (!_selected || !_sessions.some(x => x.session_id === _selected)) {
        _selected = _sessions.length ? _sessions[0].session_id : '';
    }
    _detail = _selected ? await _fetchDetail(_selected) : null;
}

async function _fetchDetail(sessionId) {
    const r = await fetch(`/api/media/sync/session?group_id=${encodeURIComponent(_gid)}`
        + `&session_id=${encodeURIComponent(sessionId)}`).then(x => x.json());
    return r.success ? r : null;
}

function _startLive() {
    _stopLive();
    _timer = setInterval(async () => {
        if (!_gid) return;
        try {
            const st = await (await fetch('/api/media/sync/status')).json();
            const wasLive = _live;
            _live = !!(st.running && st.group_id === _gid);
            if (!_live) {
                if (wasLive) _renderDetail();   // session just ended — drop badge
                return;
            }
            const s = await (await fetch(
                `/api/media/sync/sessions?group_id=${encodeURIComponent(_gid)}`)).json();
            const sessions = s.sessions || [];
            const latest = sessions.length ? sessions[0].session_id : '';
            const followingLive = _selected && _sessions.length
                && _selected === _sessions[0].session_id;
            _sessions = sessions;
            _refreshPicker();
            if (latest && (followingLive || !_selected)) {
                _selected = latest;
                const [detail, tr] = await Promise.all([
                    _fetchDetail(latest),
                    fetch(`/api/media/sync/trend?group_id=${encodeURIComponent(_gid)}`)
                        .then(r => r.json()).catch(() => null),
                ]);
                _detail = detail;
                if (tr && tr.trend) _trend = tr.trend;
                _renderDetail();
            }
        } catch (e) { /* transient — next tick */ }
    }, 3000);
}

function _stopLive() {
    if (_timer) { clearInterval(_timer); _timer = null; }
}

// ---------------------------------------------------------------------------
// Shell (card, picker, chart divs)
// ---------------------------------------------------------------------------
function _renderShell() {
    const host = document.getElementById('syncLabHost');
    if (!host) return;
    if (_convChart) { _convChart.dispose(); _convChart = null; }
    if (_pllChart) { _pllChart.dispose(); _pllChart = null; }
    host.innerHTML = `
      <div class="card mt-3" id="syncLab">
        <div class="card-header bg-light d-flex align-items-center gap-2 py-2 flex-wrap">
          <span class="fw-bold"><i class="fas fa-wave-square me-1"></i>
            Sync Lab — ${esc(_group.name)}</span>
          <span class="badge bg-light text-muted border" id="syncLabLive"></span>
          <select class="form-select form-select-sm ms-auto" id="syncLabSession"
                  style="max-width: 300px" aria-label="Session"></select>
          <button class="btn btn-sm btn-outline-secondary" id="syncLabClose"
                  title="Close Sync Lab"><i class="fas fa-times"></i></button>
        </div>
        <div class="card-body">
          <p class="small text-muted mb-3">
            Each speaker pulls its own PCM stream cut from one server timeline and reports
            its playback position back every 2.5&nbsp;s. Deviation from the group target is
            plotted below: outside ±${LOCK_MS}&nbsp;ms the stream is hard-resynced
            (<span aria-hidden="true">◆</span>); inside it, a rate-lock loop reshapes the
            stream by parts-per-million until the device's clock offset is cancelled.
            Manual trims show as <span aria-hidden="true">▲</span>. Startup latency and
            drift learned here pre-align the next session.
          </p>
          <div id="syncLabGuide" class="mb-3"></div>
          <div class="row g-2 mb-3" id="syncLabTiles"></div>
          <div class="fw-semibold small mb-1">Convergence — playback error vs group target</div>
          <div id="syncLabConv" style="height: 260px" role="img"
               aria-label="Per-speaker playback error over time"></div>
          <div class="fw-semibold small mb-1 mt-3">Rate lock — stream correction (ppm)</div>
          <div id="syncLabPll" style="height: 190px" role="img"
               aria-label="Per-speaker sample-rate correction over time"></div>
          <div class="fw-semibold small mb-1 mt-3">Learning progress — the model, session by session</div>
          <p class="small text-muted mb-2">Every session's startup latency and drift feed the
            group's model. If it's learning, sessions start closer to aligned and lock sooner —
            these should trend toward zero.</p>
          <div class="row g-3">
            <div class="col-md-6">
              <div class="small text-muted">Start misalignment per session (ms)</div>
              <div id="syncLabTrendA" style="height: 140px" role="img"
                   aria-label="Start misalignment per session"></div>
            </div>
            <div class="col-md-6">
              <div class="small text-muted">Time to lock per session (s)</div>
              <div id="syncLabTrendB" style="height: 140px" role="img"
                   aria-label="Time to lock per session"></div>
            </div>
          </div>
          <details class="mt-3 small">
            <summary class="text-muted">Session data table</summary>
            <div class="table-responsive mt-2" id="syncLabTable"></div>
          </details>
        </div>
      </div>`;
    document.getElementById('syncLabClose').onclick = () => closeSyncLab();
    const sel = document.getElementById('syncLabSession');
    sel.onchange = async () => {
        _selected = sel.value;
        _detail = await _fetchDetail(_selected);
        _renderDetail();
    };
    _refreshPicker();
    _convChart = createChart(document.getElementById('syncLabConv'));
    _pllChart = createChart(document.getElementById('syncLabPll'));
    _trendAChart = createChart(document.getElementById('syncLabTrendA'));
    _trendBChart = createChart(document.getElementById('syncLabTrendB'));
}

function _refreshPicker() {
    const sel = document.getElementById('syncLabSession');
    if (!sel) return;
    if (!_sessions.length) {
        sel.innerHTML = '<option>No sessions recorded yet — run a test</option>';
        sel.disabled = true;
        return;
    }
    sel.disabled = false;
    sel.innerHTML = _sessions.map((s, i) => {
        const d = new Date(s.started);
        const when = d.toLocaleString([], { month: 'short', day: 'numeric',
                                            hour: '2-digit', minute: '2-digit' });
        const dur = s.duration_s >= 60
            ? `${Math.floor(s.duration_s / 60)}m${String(Math.round(s.duration_s % 60)).padStart(2, '0')}s`
            : `${Math.round(s.duration_s)}s`;
        return `<option value="${esc(s.session_id)}" ${s.session_id === _selected ? 'selected' : ''}>
                  ${when} · ${dur} · ${s.players} speakers · ${s.resyncs} resyncs${i === 0 ? ' (latest)' : ''}
                </option>`;
    }).join('');
}

// ---------------------------------------------------------------------------
// Detail (tiles, charts, table)
// ---------------------------------------------------------------------------
function _renderDetail() {
    const live = document.getElementById('syncLabLive');
    if (live) {
        const isLatest = _sessions.length && _selected === _sessions[0].session_id;
        live.textContent = _live && isLatest ? 'live' : 'recorded';
        live.className = _live && isLatest
            ? 'badge bg-success' : 'badge bg-light text-muted border';
    }
    if (!_detail || !_detail.series || !_detail.series.length) {
        _renderTiles([]);
        _convChart?.setOption(_emptyOption('No measurements in this session yet'));
        _pllChart?.setOption(_emptyOption(''));
        const tbl = document.getElementById('syncLabTable');
        if (tbl) tbl.innerHTML = '<div class="text-muted">No data.</div>';
        return;
    }
    const players = _detail.players || [];
    _renderGuidance(players);
    _renderTiles(players);
    _renderCharts(_detail.series, players.map(p => p.player_id));
    _renderTrend();
    _renderTable(players);
}

// ---------------------------------------------------------------------------
// Guidance — turn the numbers into actions
// ---------------------------------------------------------------------------
function _renderGuidance(players) {
    const el = document.getElementById('syncLabGuide');
    if (!el) return;
    if (!players.length) { el.innerHTML = ''; return; }
    const items = [];
    const locked = [];
    for (const p of players) {
        const name = esc(_nameFor(p.player_id));
        if (p.lock_s == null) {
            items.push({ cls: 'warning', icon: 'fa-triangle-exclamation',
                html: `<strong>${name}</strong> never locked this session — check it's
                       powered and on WiFi, then re-run the test.` });
            continue;
        }
        const bias = p.settled_bias_ms;
        if (bias == null) {
            items.push({ cls: 'secondary', icon: 'fa-hourglass-half',
                html: `<strong>${name}</strong> is still converging — give the
                       auto-correction another minute before trimming.` });
            continue;
        }
        locked.push(p);
        if (Math.abs(bias) <= 10) {
            items.push({ cls: 'success', icon: 'fa-check',
                html: `<strong>${name}</strong> is in sync (bias ${bias > 0 ? '+' : ''}${Math.round(bias)} ms)
                       — no action needed.` });
        } else {
            const suggested = Math.max(-500, Math.min(500,
                Math.round((p.trim_ms ?? 0) - bias)));
            items.push({ cls: 'primary', icon: 'fa-sliders',
                html: `<strong>${name}</strong> plays ${Math.abs(Math.round(bias))} ms
                       ${bias > 0 ? 'late' : 'early'} after lock — suggested trim
                       <strong>${suggested} ms</strong> (now ${p.trim_ms ?? 0} ms)
                       <button class="btn btn-sm btn-primary py-0 ms-1 syncGuideApply"
                               data-pid="${esc(p.player_id)}" data-trim="${suggested}">
                         Apply</button>` });
        }
        if ((p.resyncs ?? 0) >= 3) {
            items.push({ cls: 'warning', icon: 'fa-wifi',
                html: `<strong>${name}</strong> needed ${p.resyncs} hard resyncs — an
                       unstable link. Prefer 5&nbsp;GHz WiFi, reduce congestion, or move
                       the speaker closer to the AP.` });
        }
    }
    if (locked.length >= 2) {
        const biases = locked.map(p => p.settled_bias_ms);
        const spread = Math.round(Math.max(...biases) - Math.min(...biases));
        items.unshift({
            cls: spread <= 20 ? 'success' : spread <= 45 ? 'secondary' : 'primary',
            icon: 'fa-headphones',
            html: `<strong>Group verdict:</strong> speakers are within
                   <strong>${spread} ms</strong> of each other after lock
                   ${spread <= 20 ? '— echo-free to the ear.'
                     : spread <= 45 ? '— close; a small trim will tighten it.'
                     : '— audibly apart; apply the suggested trims below.'}` });
    }
    el.innerHTML = `
      <div class="border rounded p-2">
        <div class="fw-semibold small mb-1"><i class="fas fa-lightbulb me-1"></i>What to do next</div>
        ${items.map(i => `
          <div class="small d-flex align-items-baseline gap-2 mb-1">
            <i class="fas ${i.icon} text-${i.cls}" aria-hidden="true" style="width:14px"></i>
            <span>${i.html}</span>
          </div>`).join('')}
      </div>`;
    el.querySelectorAll('.syncGuideApply').forEach(btn => {
        btn.onclick = async () => {
            btn.disabled = true;
            try {
                const r = await fetch('/api/media/sync/trim', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ player_id: btn.dataset.pid,
                                           trim_ms: Number(btn.dataset.trim) }),
                }).then(x => x.json());
                if (window.toast) window.toast(r.success
                    ? `Trim set to ${btn.dataset.trim} ms` : (r.error || 'Trim failed'),
                    r.success ? 'success' : 'error');
                _detail = await _fetchDetail(_selected);
                _renderDetail();
            } catch (e) { btn.disabled = false; }
        };
    });
}

function _renderTrend() {
    if (!_trendAChart || !_trendBChart) return;
    if (!_trend.length) {
        _trendAChart.setOption(_emptyOption('First session — nothing to compare yet'));
        _trendBChart.setOption(_emptyOption(''));
        return;
    }
    const c = _pal()[0];
    const labels = _trend.map(t => {
        const d = new Date(t.started);
        return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    });
    const bar = (data, unit) => ({
        grid: { left: 44, right: 8, top: 8, bottom: 22 },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ${unit}`) },
        xAxis: { type: 'category', data: labels,
                 axisLabel: { fontSize: 10, interval: 'auto' } },
        yAxis: { type: 'value', min: 0 },
        series: [{
            type: 'bar', data,
            itemStyle: { color: c, borderRadius: [3, 3, 0, 0] },
            barMaxWidth: 26,
        }],
    });
    _trendAChart.setOption(bar(_trend.map(t => t.start_misalign_ms), 'ms'));
    _trendBChart.setOption(bar(_trend.map(t => t.lock_s), 's'));
}

function _tile(p) {
    const c = _colorFor(p.player_id);
    const m = _model[p.player_id];
    const modelLine = m
        ? `model: lag ${fmtMs(m.lag_s * 1000)} · drift ${fmtPpm(m.drift_ppm)} · ${m.sessions} sessions`
        : 'model: not trained yet';
    return `
      <div class="col-md-6 col-xl-3">
        <div class="border rounded p-2 h-100" style="border-left: 4px solid ${c} !important">
          <div class="d-flex align-items-center small">
            <span class="rounded-circle me-1" aria-hidden="true"
                  style="display:inline-block;width:9px;height:9px;background:${c}"></span>
            <span class="fw-semibold text-truncate">${esc(_nameFor(p.player_id))}</span>
          </div>
          <div class="fs-5 fw-semibold mt-1">${fmtMs(p.settled_med_ms)}
            <span class="small fw-normal text-muted">settled median</span></div>
          <div class="small text-muted">
            p95 ${fmtMs(p.settled_p95_ms)} · lock ${fmtS(p.lock_s)} ·
            start ${fmtMs(p.startup_lag_s == null ? null : p.startup_lag_s * 1000)}<br>
            ${p.resyncs} resyncs · drift ${fmtPpm(p.final_ppm)} · trim ${p.trim_ms ?? 0} ms
          </div>
          <div class="small text-muted fst-italic mt-1">${modelLine}</div>
        </div>
      </div>`;
}

function _renderTiles(players) {
    const el = document.getElementById('syncLabTiles');
    if (el) el.innerHTML = players.map(_tile).join('')
        || '<div class="col text-muted small">No speakers reported in this session.</div>';
}

function _emptyOption(text) {
    return { title: { text, left: 'center', top: 'middle',
                      textStyle: { fontSize: 12, fontWeight: 'normal' } },
             xAxis: { show: false }, yAxis: { show: false }, series: [] };
}

function _renderCharts(series, pids) {
    const byPid = {};
    for (const pid of pids) byPid[pid] = { poll: [], ppm: [], resync: [], trim: [] };
    for (const r of series) {
        const b = byPid[r.player_id];
        if (!b) continue;
        if (r.kind === 'poll') {
            if (r.error_ms != null) b.poll.push([r.elapsed_s, Math.round(r.error_ms)]);
            if (r.rate_ppm != null) b.ppm.push([r.elapsed_s, Math.round(r.rate_ppm)]);
        } else if (r.kind === 'resync' && r.error_ms != null) {
            b.resync.push([r.elapsed_s, Math.round(r.error_ms)]);
        } else if (r.kind === 'trim') {
            b.trim.push([r.elapsed_s, 0]);
        }
    }
    const names = pids.map(_nameFor);
    const axis = {
        xAxis: { type: 'value', name: 's', nameGap: 6, min: 0,
                 axisLabel: { formatter: v => `${v}` } },
    };
    const convSeries = [];
    pids.forEach((pid, i) => {
        const c = _colorFor(pid);
        convSeries.push({
            name: names[i], type: 'line', data: byPid[pid].poll,
            showSymbol: false, lineStyle: { width: 2, color: c },
            itemStyle: { color: c }, emphasis: { focus: 'series' },
            ...(i === 0 ? { markArea: {
                silent: true,
                itemStyle: { color: _dark() ? 'rgba(140,150,160,0.10)'
                                            : 'rgba(120,120,120,0.08)' },
                data: [[{ yAxis: -LOCK_MS }, { yAxis: LOCK_MS }]],
            } } : {}),
        });
        if (byPid[pid].resync.length) convSeries.push({
            name: names[i], type: 'scatter', data: byPid[pid].resync,
            symbol: 'diamond', symbolSize: 11, itemStyle: { color: c },
            tooltip: { valueFormatter: v => `${v} ms (hard resync)` },
        });
        if (byPid[pid].trim.length) convSeries.push({
            name: names[i], type: 'scatter', data: byPid[pid].trim,
            symbol: 'triangle', symbolSize: 10, itemStyle: { color: c },
            tooltip: { valueFormatter: () => 'manual trim' },
        });
    });
    _convChart?.setOption({
        grid: { left: 48, right: 16, top: 28, bottom: 26 },
        legend: { data: names, top: 0, icon: 'roundRect' },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ms`) },
        ...axis,
        yAxis: { type: 'value', name: 'ms',
                 axisLine: { show: false } },
        series: convSeries,
    });
    _pllChart?.setOption({
        grid: { left: 48, right: 16, top: 10, bottom: 26 },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ppm`) },
        ...axis,
        yAxis: { type: 'value', name: 'ppm' },
        series: pids.map((pid, i) => ({
            name: names[i], type: 'line', data: byPid[pid].ppm,
            showSymbol: false, lineStyle: { width: 2, color: _colorFor(pid) },
            itemStyle: { color: _colorFor(pid) },
            ...(i === 0 ? { markLine: {
                silent: true, symbol: 'none',
                lineStyle: { type: 'dashed', width: 1 },
                label: { show: false }, data: [{ yAxis: 0 }],
            } } : {}),
        })),
    });
}

function _renderTable(players) {
    const el = document.getElementById('syncLabTable');
    if (!el) return;
    el.innerHTML = `
      <table class="table table-sm align-middle mb-0">
        <thead><tr>
          <th>Speaker</th><th>Startup lag</th><th>Pre-comp</th><th>Time to lock</th>
          <th>Settled median</th><th>Settled p95</th><th>Resyncs</th>
          <th>Final drift</th><th>Trim</th>
        </tr></thead>
        <tbody>
          ${players.map(p => `
            <tr>
              <td><span class="rounded-circle me-1" aria-hidden="true"
                    style="display:inline-block;width:8px;height:8px;background:${_colorFor(p.player_id)}"></span>
                  ${esc(_nameFor(p.player_id))}</td>
              <td>${fmtMs(p.startup_lag_s == null ? null : p.startup_lag_s * 1000)}</td>
              <td>${fmtMs(p.precomp_s == null ? null : p.precomp_s * 1000)}</td>
              <td>${fmtS(p.lock_s)}</td>
              <td>${fmtMs(p.settled_med_ms)}</td>
              <td>${fmtMs(p.settled_p95_ms)}</td>
              <td>${p.resyncs ?? 0}</td>
              <td>${fmtPpm(p.final_ppm)}</td>
              <td>${p.trim_ms ?? 0} ms</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
}
