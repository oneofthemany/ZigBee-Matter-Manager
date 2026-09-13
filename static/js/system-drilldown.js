/**
 * System Tab Drill-downs
 * Location: static/js/system-drilldown.js
 *
 * Clicking a gauge card on the System tab opens a modal with the detail
 * behind it (per-core CPU, memory breakdown, every sensor, mounts, threads,
 * pressure stalls). Data comes from /api/telemetry/system/detail/{area};
 * the modal re-fetches every 5s while open (disk only on demand — it walks
 * the data directory).
 */

const AREAS = {
    cpu:         { title: 'CPU',            icon: 'microchip',        render: _renderCpu,    auto: true },
    memory:      { title: 'Memory',         icon: 'memory',           render: _renderMemory, auto: true },
    temperature: { title: 'Temperature',    icon: 'thermometer-half', render: _renderTemp,   auto: true },
    disk:        { title: 'Disk',           icon: 'hdd',              render: _renderDisk,   auto: false },
    process:     { title: 'Manager Process', icon: 'cogs',            render: _renderProcess, auto: true },
    load:        { title: 'Load / Uptime',  icon: 'tachometer-alt',   render: _renderLoad,   auto: true },
};

let _modal = null;
let _area = null;
let _timer = null;
let _busy = false;
let _last = null;
let _filter = '';

export function openSystemDrilldown(area) {
    if (!AREAS[area]) return;
    _ensureModal();
    _area = area;
    _last = null;
    const cfg = AREAS[area];
    document.getElementById('sysDrillTitle').innerHTML =
        `<i class="fas fa-${cfg.icon} me-1"></i> ${cfg.title}`;
    document.getElementById('sysDrillBody').innerHTML =
        '<div class="text-muted small text-center py-4"><i class="fas fa-spinner fa-spin"></i> Sampling...</div>';
    const auto = document.getElementById('sysDrillAuto');
    auto.checked = cfg.auto;
    const filter = document.getElementById('sysDrillFilter');
    filter.hidden = area !== 'process';
    filter.value = _filter;
    _modal.show();
    _fetch();
    _schedule();
}

function _ensureModal() {
    if (_modal) return;
    document.body.insertAdjacentHTML('beforeend', `
    <div class="modal fade" id="sysDrillModal" tabindex="-1" aria-labelledby="sysDrillTitle" aria-hidden="true">
      <div class="modal-dialog modal-xl modal-dialog-scrollable">
        <div class="modal-content">
          <div class="modal-header py-2 gap-2 flex-wrap">
            <h6 class="modal-title me-auto" id="sysDrillTitle"></h6>
            <input type="search" id="sysDrillFilter" class="form-control form-control-sm" style="width:12rem"
                   placeholder="Filter threads..." hidden>
            <div class="form-check form-switch small mb-0">
              <input class="form-check-input" type="checkbox" id="sysDrillAuto">
              <label class="form-check-label" for="sysDrillAuto">Live</label>
            </div>
            <button class="btn btn-sm btn-outline-secondary" id="sysDrillRefresh" title="Refresh">
              <i class="fas fa-sync-alt"></i>
            </button>
            <span class="text-muted small" id="sysDrillStamp"></span>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body small" id="sysDrillBody"></div>
        </div>
      </div>
    </div>`);
    const el = document.getElementById('sysDrillModal');
    _modal = new bootstrap.Modal(el);
    el.addEventListener('hidden.bs.modal', () => { _stop(); _area = null; });
    document.getElementById('sysDrillAuto').addEventListener('change', _schedule);
    document.getElementById('sysDrillRefresh').addEventListener('click', _fetch);
    document.getElementById('sysDrillFilter').addEventListener('input', e => {
        _filter = e.target.value;
        if (_last) _render(_last);
    });
    document.getElementById('sysDrillBody').addEventListener('click', e => {
        const badge = e.target.closest('[data-sys-filter]');
        if (!badge) return;
        const filter = document.getElementById('sysDrillFilter');
        filter.value = _filter === badge.dataset.sysFilter ? '' : badge.dataset.sysFilter;
        filter.dispatchEvent(new Event('input'));
    });
}

function _schedule() {
    _stop();
    if (document.getElementById('sysDrillAuto')?.checked) _timer = setInterval(_fetch, 5000);
}

function _stop() {
    if (_timer) { clearInterval(_timer); _timer = null; }
}

async function _fetch() {
    if (!_area || _busy) return;
    const area = _area;
    _busy = true;
    const btn = document.querySelector('#sysDrillRefresh i');
    btn?.classList.add('fa-spin');
    try {
        const res = await fetch(`/api/telemetry/system/detail/${area}`);
        const d = await res.json();
        if (area !== _area) return;             // user switched cards mid-fetch
        if (!d.success) throw new Error(d.error || d.detail || `HTTP ${res.status}`);
        _last = d;
        _render(d);
        document.getElementById('sysDrillStamp').textContent = new Date().toLocaleTimeString();
    } catch (e) {
        if (area === _area) {
            document.getElementById('sysDrillBody').innerHTML =
                `<div class="alert alert-danger py-2 mb-0">Failed to load: ${_esc(e.message)}</div>`;
        }
    } finally {
        _busy = false;
        btn?.classList.remove('fa-spin');
    }
}

function _render(d) {
    const body = document.getElementById('sysDrillBody');
    const top = body.scrollTop;                 // keep position across live refreshes
    body.innerHTML = AREAS[_area].render(d);
    body.scrollTop = top;
}

// RENDERERS

function _renderCpu(d) {
    const all = d.usage.find(u => u.cpu === 'all');
    const cores = d.usage.filter(u => u.cpu !== 'all');
    const freq = Object.fromEntries((d.freq || []).map(f => [f.cpu, f]));
    const coreCells = cores.map(c => {
        const f = freq[c.cpu];
        return `<div class="border rounded p-1" style="min-width:5.5rem;flex:1 0 5.5rem">
            <div class="d-flex justify-content-between"><span class="text-muted">cpu${_esc(c.cpu)}</span><strong>${c.usage.toFixed(0)}%</strong></div>
            ${_bar(c.usage, 80, 95)}
            <div class="text-muted" style="font-size:0.65rem">${f ? `${f.cur_mhz} MHz` : ''}${c.iowait >= 1 ? ` · io ${c.iowait}%` : ''}</div>
        </div>`;
    }).join('');

    return `
    ${_kv([
        ['Model', d.model || '—'],
        ['Cores', d.cores],
        ['Overall', all ? `${all.usage}%` : '—'],
        ['User / System', all ? `${all.user}% / ${all.system}%` : '—'],
        ['IO wait', all ? `${all.iowait}%` : '—'],
        ['Governor', d.freq?.[0]?.governor || '—'],
    ])}
    <p class="text-muted mb-2" style="font-size:0.7rem">Measured over a 0.5s sample. The card shows the average over the last ~5s, so short spikes can differ.</p>
    ${_section('Per core')}
    <div class="d-flex flex-wrap gap-1 mb-3">${coreCells}</div>
    ${_section('Top processes (whole system)', '100% = one full core')}
    ${_procTable(d.processes)}
    ${_section('Busiest manager threads')}
    ${d.threads.length ? _threadTable(d.threads) : '<div class="text-muted">No manager thread used measurable CPU during the sample.</div>'}`;
}

function _renderMemory(d) {
    const s = d.system, p = d.process;
    const used = s.MemTotal - s.MemAvailable;
    const swapUsed = s.SwapTotal - s.SwapFree;
    return `
    ${_section('System')}
    ${_kv([
        ['Used', `${_bytes(used)} of ${_bytes(s.MemTotal)} (${_pct(used, s.MemTotal)})`],
        ['Available', _bytes(s.MemAvailable)],
        ['Free', _bytes(s.MemFree)],
        ['Page cache', _bytes(s.Cached + s.Buffers)],
        ['Shared (tmpfs)', _bytes(s.Shmem)],
        ['Reclaimable slab', _bytes(s.SReclaimable)],
        ['Dirty', _bytes(s.Dirty)],
        ['Swap', s.SwapTotal ? `${_bytes(swapUsed)} of ${_bytes(s.SwapTotal)} (${_pct(swapUsed, s.SwapTotal)})` : 'none'],
    ])}
    ${_stackBar([
        ['Used', used - s.Shmem, 'bg-primary'],
        ['Shared', s.Shmem, 'bg-info'],
        ['Cache', Math.max(s.MemAvailable - s.MemFree, 0), 'bg-secondary'],
        ['Free', s.MemFree, 'bg-success'],
    ], s.MemTotal)}
    ${d.pressure ? _pressureLine('Memory pressure', d.pressure) : ''}
    ${_section('Manager process')}
    ${_kv([
        ['Resident (RSS)', _bytes(p.VmRSS)],
        ['Peak RSS', _bytes(p.VmHWM)],
        ['Heap / anonymous', _bytes(p.RssAnon)],
        ['Mapped files & libraries', _bytes(p.RssFile)],
        ['Shared memory', _bytes(p.RssShmem)],
        ['Swapped out', _bytes(p.VmSwap)],
        ['Virtual size', _bytes(p.VmSize)],
    ])}
    ${_section('Top processes by memory')}
    <div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <thead><tr><th>PID</th><th>Name</th><th class="text-end">RSS</th><th class="text-end">Swap</th></tr></thead>
      <tbody>${d.processes.map(r => `<tr class="${r.self ? 'table-primary' : ''}">
        <td>${r.pid}</td><td>${_esc(r.name)}${r.self ? ' <span class="badge bg-primary">manager</span>' : ''}</td>
        <td class="text-end">${_bytes(r.rss)}</td><td class="text-end">${r.swap ? _bytes(r.swap) : '—'}</td></tr>`).join('')}
      </tbody></table></div>`;
}

function _renderTemp(d) {
    const cls = (t, hi, crit) => (crit && t >= crit) || t >= 85 ? 'text-danger'
        : (hi && t >= hi) || t >= 75 ? 'text-warning' : '';
    const f = d.cpu_freq;
    const src = d.card_sources || {};
    return `
    <div class="text-muted mb-2">Card reads CPU from <strong>${_esc(src.cpu || 'no CPU sensor found')}</strong>${src.gpu ? ` and GPU from <strong>${_esc(src.gpu)}</strong>` : ''}.</div>
    ${f ? `<div class="alert ${f.throttled ? 'alert-warning' : 'alert-light border'} py-2">
        <i class="fas fa-${f.throttled ? 'exclamation-triangle' : 'check-circle'} me-1"></i>
        CPU0 at ${f.cur_mhz} of ${f.max_mhz} MHz${f.throttled ? ' — possibly thermal throttling (or idle frequency scaling)' : ''}</div>` : ''}
    ${_section('Hardware sensors (hwmon)')}
    ${d.sensors.length ? `<div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <thead><tr><th>Chip</th><th>Sensor</th><th class="text-end">Temp</th><th class="text-end">High</th><th class="text-end">Critical</th></tr></thead>
      <tbody>${d.sensors.map(s => `<tr><td>${_esc(s.chip)}</td><td>${_esc(s.label)}</td>
        <td class="text-end fw-bold ${cls(s.temp, s.high, s.crit)}">${s.temp.toFixed(1)}°C</td>
        <td class="text-end text-muted">${s.high != null ? s.high.toFixed(0) + '°C' : '—'}</td>
        <td class="text-end text-muted">${s.crit != null ? s.crit.toFixed(0) + '°C' : '—'}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="text-muted">No hwmon sensors.</div>'}
    ${_section('Thermal zones')}
    ${d.zones.length ? `<div class="d-flex flex-wrap gap-1">${d.zones.map(z => `
        <div class="border rounded px-2 py-1"><span class="text-muted">${_esc(z.type || z.zone)}</span>
        <strong class="ms-1 ${cls(z.temp)}">${z.temp.toFixed(1)}°C</strong></div>`).join('')}</div>`
        : '<div class="text-muted">No thermal zones.</div>'}
    ${d.fans.length ? `${_section('Fans')}<div class="d-flex flex-wrap gap-1">${d.fans.map(f => `
        <div class="border rounded px-2 py-1"><span class="text-muted">${_esc(f.chip)} ${_esc(f.label)}</span>
        <strong class="ms-1">${f.rpm} RPM</strong></div>`).join('')}</div>` : ''}`;
}

function _renderDisk(d) {
    return `
    ${_section('Filesystems')}
    <div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <thead><tr><th>Mount</th><th>Device</th><th>Type</th><th class="text-end">Used</th><th class="text-end">Size</th><th style="width:20%"></th></tr></thead>
      <tbody>${d.mounts.map(m => `<tr><td>${_esc(m.mount)}</td><td class="text-muted text-break">${_esc(m.device)}</td>
        <td>${_esc(m.fstype)}</td><td class="text-end">${_bytes(m.used)}</td><td class="text-end">${_bytes(m.total)}</td>
        <td class="align-middle"><div class="d-flex align-items-center gap-1">${_bar(m.percent, 85, 95)}<span>${m.percent.toFixed(0)}%</span></div></td></tr>`).join('')}
      </tbody></table></div>
    ${d.pressure ? _pressureLine('IO pressure', d.pressure) : ''}
    ${_section('Manager IO since start')}
    ${_kv([['Read from disk', _bytes(d.process_io.read_bytes)], ['Written to disk', _bytes(d.process_io.write_bytes)]])}
    ${_section('Largest app files & folders', 'data/, logs/, backups/')}
    ${d.app.length ? `<div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <tbody>${d.app.map(e => `<tr><td><i class="fas fa-${e.dir ? 'folder' : 'file'} text-muted me-1"></i>${_esc(e.path)}</td>
        <td class="text-end">${e.size == null ? '<span class="text-muted">too many files</span>' : _bytes(e.size)}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="text-muted">Nothing found.</div>'}
    ${d.app_truncated ? '<div class="text-muted mt-1">Scan stopped early — some folder sizes are incomplete.</div>' : ''}
    <p class="text-muted mt-2 mb-0" style="font-size:0.7rem">Not live-refreshed by default, as the folder scan reads the whole data directory.</p>`;
}

function _renderProcess(d) {
    const q = _filter.trim().toLowerCase();
    const threads = q ? d.threads.filter(t =>
        [t.name, t.os_name, t.target, t.current, String(t.tid)].some(v => v && v.toLowerCase().includes(q)))
        : d.threads;
    const native = d.threads.filter(t => !t.python).length;
    return `
    ${_kv([
        ['PID', d.pid],
        ['Python', d.python],
        ['Manager uptime', _duration(d.uptime_secs)],
        ['CPU (sample)', d.cpu_percent != null ? `${d.cpu_percent}%` : '—'],
        ['Memory (RSS)', _bytes(d.rss)],
        ['Open files / sockets', d.open_fds ?? '—'],
        ['Threads', `${d.threads.length} (${d.threads.length - native} Python, ${native} native)`],
        ['Asyncio tasks', d.asyncio_tasks.reduce((n, t) => n + t.count, 0)],
    ])}
    ${_section('Thread groups')}
    <div class="d-flex flex-wrap gap-1 mb-2">${d.thread_groups.map(g =>
        `<span class="badge bg-light text-dark border" role="button" title="Filter to this group"
               data-sys-filter="${_esc(g.name)}">${_esc(g.name)} <strong>${g.count}</strong></span>`).join('')}
    </div>
    ${_section('Threads', q ? `${threads.length} of ${d.threads.length} matching “${_esc(_filter)}”` : 'sorted by CPU')}
    ${_threadTable(threads)}
    <p class="text-muted mt-1 mb-2" style="font-size:0.7rem">
      “Running” is the innermost frame in manager code, or the library frame if the thread is idle inside a library.
      Native threads are started by C libraries (DuckDB, zeroconf, OpenSSL, etc.) and have no Python details.</p>
    ${_section('Asyncio tasks by coroutine', 'all run on MainThread')}
    <div class="table-responsive" style="max-height:18rem"><table class="table table-sm table-hover mb-0">
      <tbody>${d.asyncio_tasks.map(t => `<tr><td><code>${_esc(t.coroutine)}</code></td><td class="text-end">${t.count}</td></tr>`).join('')}
      </tbody></table></div>`;
}

function _renderLoad(d) {
    const [l1, l5, l15] = d.load;
    const norm = v => d.cores ? `${(v / d.cores * 100).toFixed(0)}% of ${d.cores} cores` : '';
    return `
    ${_kv([
        ['1 min', `${l1?.toFixed(2)} <span class="text-muted">(${norm(l1)})</span>`],
        ['5 min', `${l5?.toFixed(2)} <span class="text-muted">(${norm(l5)})</span>`],
        ['15 min', `${l15?.toFixed(2)} <span class="text-muted">(${norm(l15)})</span>`],
        ['Runnable / total tasks', `${d.running ?? '—'} / ${d.total_tasks ?? '—'}`],
        ['System uptime', _duration(d.uptime_secs)],
        ['Booted', new Date(d.boot_time * 1000).toLocaleString()],
        ['Manager uptime', _duration(d.app_uptime_secs)],
    ], true)}
    ${Object.keys(d.pressure || {}).length ? `${_section('Pressure stalls', '% of time tasks waited on a resource')}
      <div class="table-responsive"><table class="table table-sm mb-0">
        <thead><tr><th>Resource</th><th></th><th class="text-end">10s</th><th class="text-end">60s</th><th class="text-end">5m</th></tr></thead>
        <tbody>${Object.entries(d.pressure).flatMap(([res, kinds]) => Object.entries(kinds).map(([kind, v]) => `
          <tr><td>${_esc(res)}</td><td class="text-muted">${kind === 'some' ? 'some tasks' : 'all tasks'}</td>
          ${['avg10', 'avg60', 'avg300'].map(k => `<td class="text-end ${v[k] >= 10 ? 'text-danger fw-bold' : v[k] >= 1 ? 'text-warning' : ''}">${v[k]?.toFixed(2) ?? '—'}</td>`).join('')}</tr>`)).join('')}
        </tbody></table></div>` : ''}
    ${_section('What is using the CPU', '100% = one full core')}
    ${_procTable(d.processes)}`;
}

// SHARED PIECES

function _procTable(rows) {
    return `<div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <thead><tr><th>PID</th><th>Name</th><th class="text-end">CPU</th><th class="text-end">RSS</th></tr></thead>
      <tbody>${rows.map(r => `<tr class="${r.self ? 'table-primary' : ''}">
        <td>${r.pid}</td><td>${_esc(r.name)}${r.self ? ' <span class="badge bg-primary">manager</span>' : ''}</td>
        <td class="text-end ${r.cpu_percent >= 50 ? 'fw-bold' : ''}">${r.cpu_percent.toFixed(1)}%</td>
        <td class="text-end">${_bytes(r.rss)}</td></tr>`).join('')}
      </tbody></table></div>`;
}

function _threadTable(rows) {
    if (!rows.length) return '<div class="text-muted">No threads match.</div>';
    return `<div class="table-responsive"><table class="table table-sm table-hover mb-0">
      <thead><tr><th>TID</th><th>Name</th><th>Started by</th><th>Running</th><th class="text-end">CPU</th><th class="text-end">CPU time</th></tr></thead>
      <tbody>${rows.map(t => {
        const label = t.name || t.os_name;
        const badges = (t.main ? ' <span class="badge bg-primary">main</span>' : '')
            + (!t.python ? ' <span class="badge bg-secondary">native</span>' : '')
            + (t.daemon ? ' <span class="badge bg-light text-muted border">daemon</span>' : '');
        return `<tr>
          <td class="text-muted">${t.tid}</td>
          <td class="text-nowrap">${_esc(label)}${badges}${t.name && t.os_name !== t.name.slice(0, 15) ? `<div class="text-muted" style="font-size:0.65rem">os: ${_esc(t.os_name)}</div>` : ''}</td>
          <td class="text-break"><code>${_esc(t.target || '—')}</code></td>
          <td class="text-break"><code>${_esc(t.current || '—')}</code></td>
          <td class="text-end ${t.cpu_percent >= 50 ? 'text-danger fw-bold' : t.cpu_percent >= 10 ? 'text-warning' : ''}">${t.cpu_percent != null ? t.cpu_percent.toFixed(1) + '%' : '—'}</td>
          <td class="text-end text-muted">${_duration(t.cpu_total_secs)}</td></tr>`;
      }).join('')}
      </tbody></table></div>`;
}

function _pressureLine(label, p) {
    const s = p.some || {};
    return `<div class="text-muted mt-1">${label}: stalled ${s.avg10?.toFixed(2) ?? '—'}% (10s) · ${s.avg60?.toFixed(2) ?? '—'}% (60s) · ${s.avg300?.toFixed(2) ?? '—'}% (5m)</div>`;
}

function _section(title, note = '') {
    return `<h6 class="mt-3 mb-2 small text-uppercase text-muted fw-bold">${title}${note ? ` <span class="fw-normal text-lowercase">— ${note}</span>` : ''}</h6>`;
}

// values are pre-escaped/trusted HTML (callers escape any server strings)
function _kv(pairs, html = false) {
    return `<div class="row g-2 mb-2">${pairs.map(([k, v]) => `
      <div class="col-lg-3 col-md-4 col-6"><div class="border rounded px-2 py-1 h-100">
        <div class="text-muted" style="font-size:0.68rem">${k}</div>
        <div class="fw-semibold text-break">${html ? v : _esc(v)}</div></div></div>`).join('')}</div>`;
}

function _bar(value, warn, crit) {
    const cls = value >= crit ? 'bg-danger' : value >= warn ? 'bg-warning' : 'bg-success';
    return `<div class="progress flex-grow-1" style="height:4px"><div class="progress-bar ${cls}" style="width:${Math.min(Math.max(value, 0), 100)}%"></div></div>`;
}

function _stackBar(parts, total) {
    return `<div class="progress mb-1" style="height:10px">${parts.map(([label, v, cls]) =>
        `<div class="progress-bar ${cls}" style="width:${(v / total * 100).toFixed(1)}%" title="${label}: ${_bytes(v)}"></div>`).join('')}</div>
      <div class="d-flex flex-wrap gap-2 text-muted" style="font-size:0.68rem">${parts.map(([label, v, cls]) =>
        `<span><span class="d-inline-block rounded ${cls}" style="width:.6rem;height:.6rem"></span> ${label} ${_bytes(v)}</span>`).join('')}</div>`;
}

function _bytes(n) {
    if (n == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

function _pct(a, b) {
    return b ? `${(a / b * 100).toFixed(0)}%` : '—';
}

function _duration(secs) {
    if (secs == null) return '—';
    if (secs < 60) return `${Number(secs).toFixed(secs < 10 ? 1 : 0)}s`;
    const d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
