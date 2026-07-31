/**
 * sync-lab.js — Sync Lab: per-session analysis of speaker-sync tests.
 *
 * Renders into #syncLabHost (Media → Group → OpenZone → Results) from the
 * group's own DuckDB via /api/media/sync/{sessions,session,model,trend}:
 *   - guidance: what to do next, one fixed row per speaker
 *   - three group headline stats, counted after the group locked
 *   - ONE per-speaker table: the session's measurements and the corrections
 *     applied to it, side by side, plus the cross-session learned model —
 *     it replaced a grid of cards and a separate data table, because the
 *     question here is always "how do these two compare?"
 *   - a collapsed ledger: when each correction happened
 *   - group spread chart (the headline): how far apart the speakers are,
 *     against the ±20 ms "audibly together" band
 *   - convergence chart: per-speaker playback error vs elapsed time, with
 *     the ±30 ms slew window, ±100 ms jump threshold, hard-resync (◆),
 *     rate-slew (▽) and manual-trim (▲) events
 *   - PLL chart: per-speaker stream rate correction (ppm) locking onto the
 *     device's true clock offset
 *
 * Colours: fixed per speaker by group-member order (colour follows the
 * entity), palette validated for CVD + both themes (dataviz procedure).
 *
 * Live mode: while this group's session is running it refreshes every 3 s,
 * and the whole rendering layer exists to make that invisible — charts merge,
 * every panel's structure is built once and only its [data-v] leaves are
 * written (_patch), advice categories and displayed numbers have hysteresis
 * so nothing flips or twitches, and scroll positions survive. A live view
 * that reflows under the reader is worse than one that updates slowly.
 */
import { createChart } from './chart-utils.js';

const log = zmmLog('sync-lab');

const PALETTE = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#4a3aa7'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#9085e9'],
    extra: { light: '#7d858f', dark: '#8f98a3' },   // members beyond 4 — never cycle hues
};
// Server correction ladder (cast_sync.py): inside ±SLEW_MS the stream is
// rate-slewed (inaudible); beyond ±JUMP_MS it is hard-resynced.
const SLEW_MS = 30;           // STREAM_SLEW_FAST_THRESH_S
const JUMP_MS = 100;          // STREAM_JUMP_MIN_S
const AUDIBLE_MS = 20;        // spread below this reads as echo-free

let _gid = null;              // open group id ('' = closed)
let _group = null;            // {id, name, members:[{player_id, name}]}
let _sessions = [];
let _selected = '';           // selected session_id
let _model = {};              // /api/media/sync/model
let _detail = null;           // /api/media/sync/session payload
let _trend = [];              // /api/media/sync/trend payload
let _spreadChart = null;
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

/** Write innerHTML only when it actually changed — live ticks re-render
 *  every 3 s and must not wipe hover/focus state or flicker static text.
 *  Any scroll position inside the block survives the write.
 *  Returns true when the DOM was touched (callers rebind handlers then). */
function _setHtml(el, html) {
    if (!el) return false;
    if (el._zmmPrev === html) return false;
    const tops = [...el.querySelectorAll('[data-keep-scroll]')]
        .map(n => [n.dataset.keepScroll, n.scrollTop]);
    el._zmmPrev = html;
    el._zmmKey = null;          // structure replaced — _patch must rebuild
    el.innerHTML = html;
    for (const [k, top] of tops) {
        const n = el.querySelector(`[data-keep-scroll="${k}"]`);
        if (n && top) n.scrollTop = top;
    }
    return true;
}

/** Refresh live numbers WITHOUT rebuilding the block around them.
 *
 *  A 3 s tick that rewrites a whole panel's innerHTML costs the reader
 *  everything they were doing: selection and hover die, and any change in
 *  wrapped-line count reflows the page under the cursor — the charts below
 *  visibly jump. So structure is built once per `key` (the set of speakers,
 *  say) and every subsequent tick only writes the leaf `[data-v]` nodes
 *  whose value actually moved. Values are strings, or {text, cls} when the
 *  node also carries a state colour.
 */
function _patch(el, key, buildHtml, values) {
    if (!el) return;
    if (el._zmmKey !== key) {
        el._zmmKey = key;
        el._zmmVals = {};
        el._zmmPrev = null;              // keep _setHtml's cache honest
        el.innerHTML = buildHtml();
    }
    for (const [k, v] of Object.entries(values)) {
        const val = typeof v === 'string' ? { text: v } : v;
        const prev = el._zmmVals[k];
        if (prev && prev.text === val.text && prev.cls === val.cls) continue;
        el._zmmVals[k] = val;
        const n = el.querySelector(`[data-v="${CSS.escape(k)}"]`);
        if (!n) continue;
        if (n.textContent !== val.text) n.textContent = val.text;
        if (val.cls !== undefined && n.dataset.clsBase !== undefined) {
            n.className = `${n.dataset.clsBase} ${val.cls}`.trim();
        }
    }
}
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
/** The group whose lab is on screen, or null. Lets the host pane decide
 *  whether its Results tab needs a group picker. */
export function syncLabGroup() { return _gid; }

/** Fired on the document whenever the lab opens or closes, so the pane
 *  around it can follow without polling. Silent during a switch: the
 *  close-then-open midpoint is not a state anyone should react to (a
 *  listener that re-opens "whatever should be showing" would race the
 *  open still in flight). */
let _switching = false;
function _announce() {
    if (_switching) return;
    document.dispatchEvent(new CustomEvent('synclabchange', { detail: { gid: _gid } }));
}

export async function openSyncLab(gid, group, opts = {}) {
    // Re-selecting the open group from a tab or a picker means "show me
    // this", never "close it" — only the toolbar button toggles.
    if (_gid === gid) {
        if (opts.toggle) closeSyncLab();
        return;
    }
    _switching = true;
    try {
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
    } finally {
        _switching = false;
    }
    _announce();
    // Only jump the viewport on a user-initiated open — a restore after a
    // pane rebuild must not yank the page around mid-test.
    if (!opts.restore) {
        document.getElementById('syncLab')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

export function closeSyncLab() {
    _stopLive();
    if (_spreadChart) { _spreadChart.dispose(); _spreadChart = null; }
    if (_convChart) { _convChart.dispose(); _convChart = null; }
    if (_pllChart) { _pllChart.dispose(); _pllChart = null; }
    if (_trendAChart) { _trendAChart.dispose(); _trendAChart = null; }
    if (_trendBChart) { _trendBChart.dispose(); _trendBChart = null; }
    if (_themeHook) { document.removeEventListener('themechange', _themeHook); _themeHook = null; }
    const host = document.getElementById('syncLabHost');
    if (host) host.innerHTML = '';
    _gid = null; _group = null; _detail = null; _sessions = []; _selected = '';
    _guideState = {}; _sticky = {};
    _announce();
}

/** Re-open after the sync pane re-renders with a FRESH host (the normal
 *  path keeps the old DOM node alive, so this is only the fallback). */
export function restoreSyncLab() {
    if (!_gid) return;
    const gid = _gid;
    _gid = null;                        // defeat the toggle in openSyncLab
    openSyncLab(gid, _group, { restore: true });
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
                // A failed/empty fetch is a transient (auth blip, poll race)
                // — keep showing the last good data rather than blanking
                // the charts for a tick. A genuinely fresh session has a
                // new id, so its (legitimately) empty detail still lands.
                if (detail && (detail.series?.length
                               || detail.session_id !== _detail?.session_id)) {
                    _detail = detail;
                }
                if (tr && tr.trend) _trend = tr.trend;
                _renderDetail(true);   // merged in-place update — no reset
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
    log.debug('shell rebuild — all charts recreated (expected only on open/theme/session switch)');
    if (_spreadChart) { _spreadChart.dispose(); _spreadChart = null; }
    if (_convChart) { _convChart.dispose(); _convChart = null; }
    if (_pllChart) { _pllChart.dispose(); _pllChart = null; }
    // The trend pair is recreated below too — dropping the old instances on
    // the floor leaks their ResizeObserver and themechange listener, and the
    // orphaned listener re-inits a chart into a detached node on every theme
    // toggle.
    if (_trendAChart) { _trendAChart.dispose(); _trendAChart = null; }
    if (_trendBChart) { _trendBChart.dispose(); _trendBChart = null; }
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
          <p class="small text-muted mb-2">
            One server timeline, one PCM stream cut from it per speaker. Each reports its
            playback position every 1–3&nbsp;s — the median of 3–5 reads, polled fast while
            acquiring and relaxed once locked — and the charts below plot how far that sits
            from the group target. Whatever this session measures about startup latency and
            clock drift pre-aligns the next one.
          </p>
          <div class="d-flex flex-wrap gap-3 small text-muted mb-3">
            <span><span aria-hidden="true">◆</span> <strong>hard resync</strong> — a buffer
              jump, only past ±${JUMP_MS}&nbsp;ms (startup, rebuffer). Audible.</span>
            <span><span aria-hidden="true">▽</span> <strong>rate slew</strong> — everything
              smaller, corrected in parts per million. Inaudible.</span>
            <span><span aria-hidden="true">▲</span> <strong>trim</strong> — set by hand or by
              mic calibration.</span>
          </div>
          <div id="syncLabGuide" class="mb-3"></div>
          <div class="row g-2 mb-3" id="syncLabHeadline"></div>
          <div class="fw-semibold small mb-1">Per speaker — how it held, and what was done to it</div>
          <p class="small text-muted mb-2">One row each: the session's measurements on the
            left, the corrections the engine applied on the right. Buffer jumps and trims are
            steps you could hear; rate slews and the drift term are parts-per-million changes
            you cannot. <em>pre-align</em> is how far a speaker was held back at launch so it
            would land with the slowest one. Fewer and smaller corrections over successive
            sessions means the model is learning this group.</p>
          <div id="syncLabSpeakers" class="mb-3"></div>
          <div id="syncLabLedger" class="mb-3"></div>
          <div class="fw-semibold small mb-1">Group spread — how far apart the speakers are</div>
          <p class="small text-muted mb-1">Worst pairwise gap at each poll. Inside the
            shaded ±${AUDIBLE_MS}&nbsp;ms band the group sounds echo-free.</p>
          <div id="syncLabSpread" style="height: 170px" role="img"
               aria-label="Worst speaker-to-speaker sync gap over time"></div>
          <div class="fw-semibold small mb-1 mt-3">Convergence — playback error vs group target</div>
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
        </div>
      </div>`;
    document.getElementById('syncLabClose').onclick = () => closeSyncLab();
    const sel = document.getElementById('syncLabSession');
    sel.onchange = async () => {
        _selected = sel.value;
        _guideState = {};        // another session's history proves nothing
        _sticky = {};
        _detail = await _fetchDetail(_selected);
        _renderDetail();
    };
    _refreshPicker();
    _spreadChart = createChart(document.getElementById('syncLabSpread'));
    _convChart = createChart(document.getElementById('syncLabConv'));
    _pllChart = createChart(document.getElementById('syncLabPll'));
    _trendAChart = createChart(document.getElementById('syncLabTrendA'));
    _trendBChart = createChart(document.getElementById('syncLabTrendB'));
}

function _refreshPicker() {
    const sel = document.getElementById('syncLabSession');
    if (!sel) return;
    if (document.activeElement === sel) return;   // don't fight an open dropdown
    if (!_sessions.length) {
        _setHtml(sel, '<option>No sessions recorded yet — run a test</option>');
        sel.disabled = true;
        return;
    }
    sel.disabled = false;
    // The running session's label changes every tick (it gets longer). Only
    // the label text is patched — rewriting the <option> list would drop the
    // selection and, in some browsers, shut an open dropdown.
    const label = (s, i) => {
        const d = new Date(s.started);
        const when = d.toLocaleString([], { month: 'short', day: 'numeric',
                                            hour: '2-digit', minute: '2-digit' });
        const dur = s.duration_s >= 60
            ? `${Math.floor(s.duration_s / 60)}m${String(Math.round(s.duration_s % 60)).padStart(2, '0')}s`
            : `${Math.round(s.duration_s)}s`;
        return `${when} · ${dur} · ${s.players} speakers · ${s.resyncs} resyncs`
             + `${i === 0 ? ' (latest)' : ''}`;
    };
    _patch(sel, `sessions:${_sessions.map(s => s.session_id).join('|')}`,
        () => _sessions.map(s => `
            <option value="${esc(s.session_id)}"
                    data-v="opt:${esc(s.session_id)}"></option>`).join(''),
        Object.fromEntries(_sessions.map((s, i) => [`opt:${s.session_id}`, label(s, i)])));
    if (sel.value !== _selected) sel.value = _selected;
}

// ---------------------------------------------------------------------------
// Detail (tiles, charts, table)
// ---------------------------------------------------------------------------
function _renderDetail(merge = false) {
    const live = document.getElementById('syncLabLive');
    if (live) {
        const isLatest = _sessions.length && _selected === _sessions[0].session_id;
        live.textContent = _live && isLatest ? 'live' : 'recorded';
        live.className = _live && isLatest
            ? 'badge bg-success' : 'badge bg-light text-muted border';
    }
    if (!_detail || !_detail.series || !_detail.series.length) {
        _renderHeadline([]);
        _renderGuidance([]);
        _renderSpeakers([], []);
        _setChart(_spreadChart, _emptyOption(''), false, true);
        _setChart(_convChart, _emptyOption('No measurements in this session yet'),
                  false, true);
        _setChart(_pllChart, _emptyOption(''), false, true);
        return;
    }
    const players = _detail.players || [];
    const spread = _spreadSeries(_detail.series, players.map(p => p.player_id));
    // The group is locked once its slowest member is.
    const lockAt = Math.max(0, ...players.map(p => p.lock_s ?? 0));
    _renderGuidance(players, spread, lockAt);
    _renderHeadline(spread, lockAt);
    _renderSpeakers(_detail.series, players);
    _renderSpread(spread, merge);
    _renderCharts(_detail.series, players.map(p => p.player_id), merge);
    _renderTrend(merge);
}

// ---------------------------------------------------------------------------
// Group spread — the metric the ears actually hear
// ---------------------------------------------------------------------------
/** Worst pairwise error gap over time: [[elapsed_s, spread_ms], ...].
 *  Uses each speaker's most recent poll; a point is emitted only while every
 *  speaker has reported within the last 8 s (≈2 poll cycles). */
function _spreadSeries(series, pids) {
    if (pids.length < 2) return [];
    const lastErr = {}, lastT = {};
    const out = [];
    for (const r of series) {
        if (r.kind !== 'poll' || r.error_ms == null) continue;
        lastErr[r.player_id] = r.error_ms;
        lastT[r.player_id] = r.elapsed_s;
        if (pids.every(p => lastT[p] != null && r.elapsed_s - lastT[p] <= 8)) {
            const errs = pids.map(p => lastErr[p]);
            out.push([r.elapsed_s, Math.round(Math.max(...errs) - Math.min(...errs))]);
        }
    }
    return out;
}

/** Headline stats, counted AFTER the group locked.
 *
 *  Including the acquisition phase made the three panels contradict each
 *  other: a session where the slowest speaker took 24 s to lock read "49% in
 *  sync" next to a verdict of "within 4 ms, echo-free", and both were true of
 *  different windows. Startup is measured on its own (time-to-lock, the trend
 *  charts); what these three answer is "once it settled, how did it hold?"
 */
function _renderHeadline(spread, lockAt = 0) {
    const el = document.getElementById('syncLabHeadline');
    if (!el) return;
    if (!spread.length) { _setHtml(el, ''); return; }
    const settled = spread.filter(p => p[0] >= lockAt);
    const vals = (settled.length >= 3 ? settled : spread).map(p => p[1]);
    const sorted = [...vals].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const now = spread[spread.length - 1][1];
    const inSyncPct = Math.round(100 * vals.filter(v => v <= AUDIBLE_MS).length / vals.length);
    const stat = (k, label, sub) => `
      <div class="col-4">
        <div class="border rounded p-2 text-center h-100">
          <div data-v="${k}" data-cls-base="fs-4 fw-semibold" class="fs-4 fw-semibold"></div>
          <div class="small text-muted">${label}${sub ? `<br>${sub}` : ''}</div>
        </div>
      </div>`;
    const good = ok => ok ? 'text-success' : 'text-warning';
    const win = settled.length >= 3 ? 'after lock' : 'whole session';
    _patch(el, `headline:${win}`,
        () => stat('now', 'group spread now', 'latest poll')
            + stat('median', 'median spread', win)
            + stat('pct', 'time in sync', `within ±${AUDIBLE_MS} ms, ${win}`),
        {
            now: { text: `${now} ms`, cls: good(now <= AUDIBLE_MS) },
            median: { text: `${median} ms`, cls: good(median <= AUDIBLE_MS) },
            pct: { text: `${inSyncPct}%`, cls: good(inSyncPct >= 80) },
        });
}

function _renderSpread(spread, merge = false) {
    if (!_spreadChart) return;
    if (!spread.length) {
        _setChart(_spreadChart, _emptyOption('Needs two speakers reporting'),
                  false, true);
        return;
    }
    const c = _pal()[0];
    _setChart(_spreadChart, {
        grid: { left: 48, right: 16, top: 10, bottom: 26 },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ms`) },
        xAxis: { type: 'value', name: 's', nameGap: 6, min: 0,
                 axisLabel: { formatter: v => `${v}` } },
        yAxis: { type: 'value', name: 'ms', min: 0 },
        series: [{
            id: 'spread', name: 'spread', type: 'line', data: spread,
            showSymbol: false, lineStyle: { width: 2, color: c },
            itemStyle: { color: c },
            areaStyle: { opacity: 0.12, color: c },
            markArea: {
                silent: true,
                itemStyle: { color: _dark() ? 'rgba(25,158,112,0.12)'
                                            : 'rgba(27,175,122,0.10)' },
                data: [[{ yAxis: 0 }, { yAxis: AUDIBLE_MS }]],
            },
        }],
    }, merge);
}

// ---------------------------------------------------------------------------
// Guidance — turn the numbers into actions
//
// Deliberately offers no per-speaker "apply this trim" button. The settled
// bias it would be computed from is measured with the trim EXCLUDED
// (cast_sync._measure_lag_once), so a trim can never move that number:
// the suggestion would survive being applied, invite a second application,
// and integrate open-loop. A sensor-VISIBLE bias is the rate loop's job and
// it is already draining it; a sensor-INVISIBLE one (output-pipeline
// latency) can only be seen by the mic, which is what Calibrate is for.
// ---------------------------------------------------------------------------
// Advice is state, not telemetry, and it is read while it is being written.
// Two things kept it from settling down:
//
//   - the panel was rebuilt every 3 s, and its rows appeared and disappeared
//     as speakers crossed a threshold, so the block changed height under the
//     reader and shoved the charts below it around;
//   - the thresholds were bare comparisons, so a speaker sitting on ±10 ms
//     flipped its advice on every poll;
//   - and the advice strings themselves ran from a dozen words to sixty, so
//     even a legitimate, well-damped category change re-wrapped the row and
//     moved everything below it. During the first minute of a live session
//     every speaker walks nolock → converging → off → ok, so this fired on
//     the one view most likely to be watched.
//
// So: one fixed row per speaker (never added, never removed), values patched
// in place, both the categories and the numbers move only when they have
// moved enough to mean something, and each row's copy is bounded to one line
// with the paragraph behind a collapsed detail.
let _guideState = {};     // player_id | 'group' → last category, for hysteresis
let _sticky = {};         // key → last shown number, for the same reason

/** A number that only changes when it has changed enough to be worth reading. */
function _stick(key, v, tol = 3) {
    const prev = _sticky[key];
    if (prev != null && Math.abs(v - prev) < tol) return prev;
    _sticky[key] = v;
    return v;
}

/** Which advice a speaker gets. Sticky at the edges: the exit threshold sits
 *  inside the entry one, so noise around the boundary cannot make the row
 *  oscillate. Priority is worst-first — an unstable link outranks an offset,
 *  because it explains it. */
function _guideCat(p) {
    const pid = p.player_id;
    if (p.lock_s == null) return (_guideState[pid] = 'nolock');
    if (p.settled_bias_ms == null) return (_guideState[pid] = 'converging');
    if ((p.resyncs ?? 0) >= 3) return (_guideState[pid] = 'unstable');
    const b = Math.abs(p.settled_bias_ms);
    const was = _guideState[pid];
    const off = was === 'off' ? b >= 8 : b > 12;
    return (_guideState[pid] = off ? 'off' : 'ok');
}

function _renderGuidance(players, spread = [], lockAt = 0) {
    const el = document.getElementById('syncLabGuide');
    if (!el) return;
    if (!players.length) { el.style.minHeight = ''; el._zmmMinH = 0; _setHtml(el, ''); return; }

    // --- group verdict ---------------------------------------------------
    // Two different truths, and quoting only the first made the verdict read
    // as "echo-free" over a stats row saying half the polls were outside the
    // band. The medians say where each speaker SITS; the poll-by-poll spread
    // says how much it WANDERS around that, and the ear hears both.
    const locked = players.filter(p => p.lock_s != null && p.settled_bias_ms != null);
    let verdict = { cls: 'secondary', icon: 'fa-headphones',
                    text: 'needs two speakers reporting after lock.',
                    why: 'Two speakers have to lock before there is a group spread to '
                         + 'judge at all.' };
    if (locked.length >= 2) {
        const biases = locked.map(p => p.settled_bias_ms);
        const centres = _stick('centres',
                               Math.round(Math.max(...biases) - Math.min(...biases)));
        const settled = spread.filter(s => s[0] >= lockAt).map(s => s[1]);
        const sorted = [...settled].sort((a, b) => a - b);
        const p90 = sorted.length
            ? _stick('p90', sorted[Math.min(sorted.length - 1,
                                            Math.floor(sorted.length * 0.9))])
            : null;
        const was = _guideState.group;
        const steady = p90 == null
            || (was === 'jittery' ? p90 <= AUDIBLE_MS - 4 : p90 <= AUDIBLE_MS + 4);
        const cat = !steady ? 'jittery'
            : centres <= 20 ? 'tight' : centres <= 45 ? 'close' : 'apart';
        _guideState.group = cat;
        // The one-liner carries the category; the numbers that justify it live
        // in the detail. The jitter is not dropped by that — it IS the category
        // when it matters, so the short line still says so.
        const head = {
            tight: `within ${centres} ms after lock — echo-free to the ear.`,
            close: `within ${centres} ms after lock — still closing.`,
            apart: `${centres} ms apart after lock — audibly apart.`,
            jittery: `within ${centres} ms on average, but jittery.`,
        }[cat];
        const tail = {
            tight: 'Echo-free to the ear.',
            close: 'Close; the rate loop is still closing it.',
            apart: 'Audibly apart; if it persists, calibrate with the mic.',
            jittery: 'Aligned on average but jittery, which is a link problem, not '
                     + 'an alignment one: check WiFi before trimming.',
        }[cat];
        verdict = {
            cls: cat === 'tight' ? 'success' : cat === 'close' ? 'secondary' : 'primary',
            icon: 'fa-headphones',
            text: head,
            why: `The speakers sit within ${centres} ms of each other after lock`
                 + (p90 == null ? '. ' : `, wandering up to ${p90} ms apart poll to `
                                       + 'poll (9 in 10). ')
                 + tail,
        };
    }

    // --- one row per speaker, always ------------------------------------
    // Every category speaks twice: `text` is a bounded one-liner that holds the
    // row at one line, `why` is the paragraph behind the collapsed detail. They
    // used to be one string, and that was the bug — `ok` was a dozen words and
    // `off` was sixty, so a speaker crossing a threshold changed this panel's
    // height by four wrapped lines and shoved five charts down the page. The
    // hysteresis below stops the flicker; only bounded copy stops the reflow.
    const ADVICE = {
        nolock: { cls: 'warning', icon: 'fa-triangle-exclamation',
                  text: () => 'never locked this session.',
                  why: () => 'It reported no position the engine could lock onto — check '
                             + 'it is powered and on WiFi, then re-run the test.' },
        converging: { cls: 'secondary', icon: 'fa-hourglass-half',
                      text: () => 'still converging.',
                      why: () => 'The rate loop needs another minute of measurements '
                                 + 'before its settled numbers mean anything.' },
        unstable: { cls: 'warning', icon: 'fa-wifi',
                    text: p => `${p.resyncs} hard resyncs — an unstable link.`,
                    why: () => 'Hard resyncs are audible steps, and this many of them '
                               + 'means the stream keeps arriving late. Prefer 5 GHz '
                               + 'WiFi, reduce congestion, or move the speaker closer '
                               + 'to the AP.' },
        off: { cls: 'primary', icon: 'fa-sliders',
               text: p => `settles ${Math.abs(Math.round(p.settled_bias_ms))} ms `
                          + `${p.settled_bias_ms > 0 ? 'behind' : 'ahead of'} the group.`,
               why: () => 'The rate loop is still trickling that in (≤20 ppm, inaudible) '
                          + 'and needs no help. If the same offset returns every session '
                          + 'it is output-pipeline latency the position sensor cannot '
                          + 'see: run Calibrate (mic) to set its trim from the sound in '
                          + 'the air.' },
        ok: { cls: 'success', icon: 'fa-check',
              text: p => `in sync, ${_sign(_stick('b:' + p.player_id,
                                                  Math.round(p.settled_bias_ms), 2))} ms `
                         + 'from target — nothing to do.',
              why: () => `Its settled median sits inside the ±${AUDIBLE_MS} ms the ear `
                         + 'cannot resolve; there is nothing to trim.' },
    };

    const vals = { 'v:text': verdict.text,
                   'v:why': verdict.why,
                   'v:icon': { text: '', cls: `fas ${verdict.icon} text-${verdict.cls}` } };
    for (const p of players) {
        const a = ADVICE[_guideCat(p)];
        vals[`g:${p.player_id}`] = a.text(p);
        vals[`w:${p.player_id}`] = a.why(p);
        vals[`i:${p.player_id}`] = { text: '', cls: `fas ${a.icon} text-${a.cls}` };
    }

    const key = `guide:${players.map(p => p.player_id).join('|')}`;
    if (el._zmmKey !== key) { el._zmmMinH = 0; el.style.minHeight = ''; }
    _patch(el, key, () => `
      <div class="border rounded p-2">
        <div class="fw-semibold small mb-1"><i class="fas fa-lightbulb me-1"></i>What to do next</div>
        <div class="small d-flex align-items-baseline gap-2 mb-1">
          <i data-v="v:icon" data-cls-base="" class="fas fa-headphones"
             aria-hidden="true" style="width:14px;flex:0 0 14px"></i>
          <span><strong>Group verdict:</strong> <span data-v="v:text"></span></span>
        </div>
        ${players.map(p => `
          <div class="small d-flex align-items-baseline gap-2 mb-1">
            <i data-v="i:${esc(p.player_id)}" data-cls-base="" class="fas fa-check"
               aria-hidden="true" style="width:14px;flex:0 0 14px"></i>
            <span><strong>${esc(_nameFor(p.player_id))}</strong>
              <span data-v="g:${esc(p.player_id)}"></span></span>
          </div>`).join('')}
        <details class="small mt-2">
          <summary class="text-muted">Why, and what to do about it</summary>
          <div class="mt-1">
            <div class="mb-1"><strong>Group</strong> <span data-v="v:why"></span></div>
            ${players.map(p => `
              <div class="mb-1"><strong>${esc(_nameFor(p.player_id))}</strong>
                <span data-v="w:${esc(p.player_id)}"></span></div>`).join('')}
          </div>
        </details>
      </div>`, vals);

    // Belt and braces: the rows are one line by construction, but a narrow
    // viewport can still wrap one. Hold the panel at the tallest it has been
    // for this set of speakers, so a wrap costs the reader one jump rather
    // than one per tick. Reset above whenever the structure is rebuilt.
    const h = el.firstElementChild ? el.firstElementChild.offsetHeight : 0;
    if (h > (el._zmmMinH || 0)) { el._zmmMinH = h; el.style.minHeight = `${h}px`; }
}

// ---------------------------------------------------------------------------
// Adjustments — what the engine actually DID to each speaker, and when
//
// The corrections are the interesting record: they are the work that kept
// the group together, and their size, kind and cadence say more about a
// speaker's link than any single settled number. Split by audibility,
// because that is the distinction that matters to the listener: a buffer
// jump is a heard discontinuity, a rate slew is not.
// ---------------------------------------------------------------------------
const TRIM_RUN_GAP_S = 30;      // trims closer than this are one adjustment
let _ledgerOpen = false;        // reader's choice, kept across live ticks
const _clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const _sign = v => `${v > 0 ? '+' : ''}${Math.round(v)}`;

/** One row per adjustment event, newest first, plus a per-speaker tally. */
// Columns of the one per-speaker table: session measurements first, then the
// corrections applied to it. Key, header, tooltip.
const _SPK_COLS = [
    ['med',   'median',    'Settled median |error| after lock — how tightly it held'],
    ['p95',   'p95',       '95th-percentile |error| after lock — its worst moments'],
    ['bias',  'bias',      'Median signed error after lock — where it sits relative to the group'],
    ['lock',  'lock',      'Time from session start to first lock'],
    ['start', 'start lag', 'Startup latency measured at launch — feeds the model'],
    ['pre',   'pre-align', 'Held back at launch so it would land with the slowest speaker'],
    ['jump',  'jumps',     'Buffer jumps — audible steps'],
    ['slew',  'slews',     'Rate slews — inaudible, parts per million'],
    ['trims', 'trims',     'Trim adjustments (a run of by-ear nudges counts once)'],
    ['mic',   'mic',       'Mic (chirp) calibrations'],
    ['drift', 'drift',     'Rate correction currently held against this device clock'],
    ['trim',  'trim',      'Standing trim, re-applied every session'],
];

function _renderSpeakers(series, players) {
    const el = document.getElementById('syncLabSpeakers');
    if (!el) return;
    if (!players.length) {
        _setHtml(el, '<div class="text-muted small">No speakers reported in this session.</div>');
        _setHtml(document.getElementById('syncLabLedger'), '');
        return;
    }

    const KIND = {
        resync: { label: 'buffer jump', icon: 'fa-forward-step', cls: 'warning',
                  audible: true },
        trim:   { label: 'trim set',    icon: 'fa-sliders',      cls: 'primary',
                  audible: true },
        chirp:  { label: 'mic calibration', icon: 'fa-microphone', cls: 'info',
                  audible: false },
        slew:   { label: 'rate slew',   icon: 'fa-angles-right', cls: 'secondary',
                  audible: false },
    };
    const tally = {};
    for (const p of players) {
        tally[p.player_id] = { jump: 0, jumpMs: 0, slew: 0, slewMax: 0,
                               trim: 0, chirp: 0 };
    }
    const events = [];
    for (const r of series) {
        const k = KIND[r.kind];
        const t = tally[r.player_id];
        if (!k || !t) continue;
        if (r.kind === 'resync') { t.jump++; t.jumpMs += Math.abs(r.error_ms || 0); }
        else if (r.kind === 'slew') {
            t.slew++;
            t.slewMax = Math.max(t.slewMax, Math.abs(r.error_ms || 0));
        } else if (r.kind === 'chirp') t.chirp++;
        // 'trim' is tallied after coalescing — see below.
        // Aligning by ear is a RUN of nudges — fifty ±1 ms taps land as fifty
        // rows and bury everything the engine did. One run, one row: where it
        // ended up and how many taps it took.
        const prev = events[events.length - 1];
        if (r.kind === 'trim' && prev && prev.kind === 'trim'
                && prev.pid === r.player_id && r.elapsed_s - prev.t <= TRIM_RUN_GAP_S) {
            prev.t = r.elapsed_s;
            prev.count += 1;
            prev.amount = `→ ${r.trim_ms ?? 0} ms`;
            continue;
        }
        events.push({
            t: r.elapsed_s, pid: r.player_id, kind: r.kind, count: 1, ...k,
            amount: r.kind === 'trim'
                ? `→ ${r.trim_ms ?? 0} ms`
                : (r.error_ms == null ? '' : `${_sign(r.error_ms)} ms`),
        });
    }
    // One tuning run is one adjustment in the count as well as in the list,
    // or the table would say 30 where the ledger shows a single entry.
    for (const e of events) if (e.kind === 'trim') tally[e.pid].trim++;
    events.reverse();                    // newest first — live sessions grow
    const shown = events.slice(0, 60);

    // One row per speaker, every figure in it: what the session measured and
    // what the engine did about it. The card grid this replaced said the same
    // things in five stacked blocks, which made comparing two speakers — the
    // only question anyone actually has here — a scrolling exercise.
    const pids = players.map(p => p.player_id);
    _patch(el, `spk:${pids.join('|')}`, () => `
      <div class="table-responsive">
        <table class="table table-sm small mb-0 align-middle"
               style="font-variant-numeric: tabular-nums">
          <thead><tr class="text-muted">
            <th class="fw-normal">Speaker</th>
            ${_SPK_COLS.map(([k, label, tip]) =>
                `<th class="fw-normal text-end" title="${esc(tip)}">${label}</th>`).join('')}
          </tr></thead>
          <tbody>
            ${players.map(p => {
                const id = esc(p.player_id);
                return `
              <tr>
                <td>
                  <span class="rounded-circle me-1" aria-hidden="true"
                        style="display:inline-block;width:9px;height:9px;background:${_colorFor(p.player_id)}"></span>
                  ${esc(_nameFor(p.player_id))}
                  <div class="text-muted fst-italic" style="font-size:.75rem"
                       data-v="mdl:${id}"></div>
                </td>
                ${_SPK_COLS.map(([k]) =>
                    `<td class="text-end" data-v="${k}:${id}"
                         data-cls-base="text-end"></td>`).join('')}
              </tr>`; }).join('')}
          </tbody>
        </table>
      </div>`,
        // Fixed columns, tabular figures: a tick that moves a count changes
        // one cell's glyphs and nothing else on the page can shift. The
        // sentence form this replaced re-wrapped whenever a number gained a
        // digit, which is what made the panel feel restless.
        Object.assign({}, ...players.map(p => {
            const t = tally[p.player_id];
            const id = p.player_id;
            const m = _model[id];
            const dash = v => v || '—';
            return {
                [`mdl:${id}`]: m
                    ? `model: lag ${fmtMs(m.lag_s * 1000)} · drift ${fmtPpm(m.drift_ppm)}`
                      + ` · ${m.sessions} sessions`
                    : 'model: not trained yet',
                [`med:${id}`]: fmtMs(p.settled_med_ms),
                [`p95:${id}`]: fmtMs(p.settled_p95_ms),
                [`bias:${id}`]: p.settled_bias_ms == null ? '—'
                    : `${_sign(p.settled_bias_ms)} ms`,
                [`lock:${id}`]: fmtS(p.lock_s),
                [`start:${id}`]: fmtMs(p.startup_lag_s == null
                                       ? null : p.startup_lag_s * 1000),
                [`pre:${id}`]: fmtMs(p.precomp_s == null ? null : p.precomp_s * 1000),
                [`jump:${id}`]: {
                    text: t.jump ? `${t.jump} (Σ ${Math.round(t.jumpMs)} ms)` : '—',
                    cls: t.jump ? 'text-end text-warning' : 'text-end text-muted',
                },
                [`slew:${id}`]: dash(t.slew && `${t.slew} (≤ ${Math.round(t.slewMax)} ms)`),
                [`trims:${id}`]: dash(t.trim && String(t.trim)),
                [`mic:${id}`]: dash(t.chirp && String(t.chirp)),
                [`drift:${id}`]: fmtPpm(p.final_ppm),
                [`trim:${id}`]: `${p.trim_ms ?? 0} ms`,
            };
        })));

    // The ledger only changes when an adjustment actually happens, so it is
    // its own element: a tick that merely moved a ppm reading leaves the
    // reader's scroll position in the list alone.
    _setHtml(document.getElementById('syncLabLedger'), shown.length ? `
      <details class="small" ${_ledgerOpen ? 'open' : ''} id="syncLabLedgerBox">
        <summary class="text-muted">When each correction happened
          (${events.length})</summary>
        <div class="mt-2" data-keep-scroll="ledger"
             style="max-height:200px;overflow-y:auto">
          <table class="table table-sm small mb-0">
            <tbody>
              ${shown.map(e => `
                <tr>
                  <td class="text-muted py-1" style="width:3.5rem">${_clock(e.t)}</td>
                  <td class="py-1"><span class="rounded-circle me-1" aria-hidden="true"
                        style="display:inline-block;width:8px;height:8px;background:${_colorFor(e.pid)}"></span>
                    ${esc(_nameFor(e.pid))}</td>
                  <td class="py-1"><i class="fas ${e.icon} text-${e.cls} me-1"
                        aria-hidden="true"></i>${e.label}
                    ${e.count > 1 ? `<span class="text-muted">(${e.count} steps)</span>` : ''}
                    ${e.audible ? '' : '<span class="text-muted">(inaudible)</span>'}</td>
                  <td class="py-1 text-end font-monospace">${e.amount}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${events.length > shown.length
          ? `<div class="small text-muted mt-1">${events.length - shown.length} earlier
               adjustment(s) not shown.</div>` : ''}
      </details>`
      : '<div class="small text-muted">No discrete corrections — the group held on the rate loop alone.</div>');
    // Collapsed by default, and it stays however the reader left it: a live
    // tick that re-renders the list must not shut it.
    const box = document.getElementById('syncLabLedgerBox');
    if (box) box.ontoggle = () => { _ledgerOpen = box.open; };
}

function _renderTrend(merge = false) {
    if (!_trendAChart || !_trendBChart) return;
    if (!_trend.length) {
        _setChart(_trendAChart,
                  _emptyOption('First session — nothing to compare yet'), false, true);
        _setChart(_trendBChart, _emptyOption(''), false, true);
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
    _setChart(_trendAChart, bar(_trend.map(t => t.start_misalign_ms), 'ms'), merge);
    _setChart(_trendBChart, bar(_trend.map(t => t.lock_s), 's'), merge);
}

function _emptyOption(text) {
    return { title: { text, left: 'center', top: 'middle',
                      textStyle: { fontSize: 12, fontWeight: 'normal' } },
             xAxis: { show: false }, yAxis: { show: false }, series: [] };
}

/** Apply a chart option, tracking empty↔data transitions. A merged update
 *  must never be merged INTO an empty-state option (the placeholder title
 *  and hidden axes would survive the merge — stuck "no data" text, missing
 *  axis labels), so any flip between the two states forces a full replace. */
function _setChart(chart, opt, merge = false, empty = false) {
    if (!chart) return;
    const flip = chart._zmmEmpty !== empty;
    if (flip && chart._zmmEmpty !== undefined) {
        log(`chart flip → ${empty ? 'EMPTY placeholder' : 'data'} (full redraw)`);
    }
    chart._zmmEmpty = empty;
    chart.setOption(opt, !merge || flip);
}

function _renderCharts(series, pids, merge = false) {
    const byPid = {};
    for (const pid of pids) byPid[pid] = { poll: [], ppm: [], resync: [], slew: [], trim: [] };
    for (const r of series) {
        const b = byPid[r.player_id];
        if (!b) continue;
        if (r.kind === 'poll') {
            if (r.error_ms != null) b.poll.push([r.elapsed_s, Math.round(r.error_ms)]);
            if (r.rate_ppm != null) b.ppm.push([r.elapsed_s, Math.round(r.rate_ppm)]);
        } else if (r.kind === 'resync' && r.error_ms != null) {
            b.resync.push([r.elapsed_s, Math.round(r.error_ms)]);
        } else if (r.kind === 'slew' && r.error_ms != null) {
            b.slew.push([r.elapsed_s, Math.round(r.error_ms)]);
        } else if (r.kind === 'trim') {
            b.trim.push([r.elapsed_s, 0]);
        }
    }
    const names = pids.map(_nameFor);
    const axis = {
        xAxis: { type: 'value', name: 's', nameGap: 6, min: 0,
                 axisLabel: { formatter: v => `${v}` } },
    };
    // Every series always exists with a stable id (empty data is fine), so
    // merged live updates map onto the same series instead of re-stacking.
    const convSeries = [];
    pids.forEach((pid, i) => {
        const c = _colorFor(pid);
        convSeries.push({
            id: `line-${pid}`, name: names[i], type: 'line', data: byPid[pid].poll,
            showSymbol: false, lineStyle: { width: 2, color: c },
            itemStyle: { color: c }, emphasis: { focus: 'series' },
            ...(i === 0 ? {
                // Inner band: slew window (corrections inaudible). Dashed
                // lines: the ±jump threshold (an audible hard resync).
                markArea: {
                    silent: true,
                    itemStyle: { color: _dark() ? 'rgba(140,150,160,0.10)'
                                                : 'rgba(120,120,120,0.08)' },
                    data: [[{ yAxis: -SLEW_MS }, { yAxis: SLEW_MS }]],
                },
                markLine: {
                    silent: true, symbol: 'none',
                    lineStyle: { type: 'dashed', width: 1 },
                    label: { show: true, position: 'insideEndTop',
                             formatter: 'jump', fontSize: 10 },
                    data: [{ yAxis: JUMP_MS }, { yAxis: -JUMP_MS }],
                },
            } : {}),
        });
        convSeries.push({
            id: `re-${pid}`, name: names[i], type: 'scatter', data: byPid[pid].resync,
            symbol: 'diamond', symbolSize: 11, itemStyle: { color: c },
            tooltip: { valueFormatter: v => `${v} ms (hard resync)` },
        });
        convSeries.push({
            id: `sl-${pid}`, name: names[i], type: 'scatter', data: byPid[pid].slew,
            symbol: 'triangle', symbolRotate: 180, symbolSize: 9,
            itemStyle: { color: c, opacity: 0.75 },
            tooltip: { valueFormatter: v => `${v} ms (rate slew)` },
        });
        convSeries.push({
            id: `tr-${pid}`, name: names[i], type: 'scatter', data: byPid[pid].trim,
            symbol: 'triangle', symbolSize: 10, itemStyle: { color: c },
            tooltip: { valueFormatter: () => 'manual trim' },
        });
    });
    _setChart(_convChart, {
        grid: { left: 48, right: 16, top: 28, bottom: 26 },
        legend: { data: names, top: 0, icon: 'roundRect' },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ms`) },
        ...axis,
        yAxis: { type: 'value', name: 'ms',
                 axisLine: { show: false } },
        series: convSeries,
    }, merge);
    _setChart(_pllChart, {
        grid: { left: 48, right: 16, top: 10, bottom: 26 },
        tooltip: { trigger: 'axis',
                   valueFormatter: v => (v == null ? '—' : `${v} ppm`) },
        ...axis,
        yAxis: { type: 'value', name: 'ppm' },
        series: pids.map((pid, i) => ({
            id: `ppm-${pid}`, name: names[i], type: 'line', data: byPid[pid].ppm,
            showSymbol: false, lineStyle: { width: 2, color: _colorFor(pid) },
            itemStyle: { color: _colorFor(pid) },
            ...(i === 0 ? { markLine: {
                silent: true, symbol: 'none',
                lineStyle: { type: 'dashed', width: 1 },
                label: { show: false }, data: [{ yAxis: 0 }],
            } } : {}),
        })),
    }, merge);
}

