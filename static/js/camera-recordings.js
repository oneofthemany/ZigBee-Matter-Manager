/* Cameras -> Recordings: clips of events and continuous footage, with what is
   recording and how much room it takes. Backend: routes/recording_routes.py,
   the recorder sidecar — docs/recordings.md. */

import { escapeHtml } from './utils.js';

const LABELS = { motion: ['fa-person-running', 'Motion'], person: ['fa-person', 'Person'],
                 vehicle: ['fa-car-side', 'Vehicle'], animal: ['fa-paw', 'Animal'] };
const PAGE = 24;
const SLOT_S = 300;

async function api(method, url, body) {
    const res = await fetch(url, {
        method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    return data;
}
const isAdmin = () => !!window.zmmAuth?.hasScope?.('admin');
const gb = n => (n == null ? '?' : (n / 1073741824).toFixed(n < 10737418240 ? 1 : 0));
const when = t => new Date(t * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const hhmm = t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

export async function renderRecordings(host, cameras) {
    const name = id => cameras.find(c => c.id === id)?.name || id;
    let clips = [], more = false, filter = '', playing = null;

    host.innerHTML = `
        <div id="rec_status" class="small mb-2"></div>
        <div id="rec_player" class="mb-3 d-none">
            <video id="rec_video" class="w-100 rounded bg-black" style="max-height:70vh" controls playsinline></video>
            <div class="d-flex flex-wrap gap-2 align-items-center mt-1">
                <span id="rec_title" class="small me-auto"></span>
                <a id="rec_download" class="btn btn-sm btn-outline-secondary d-none"><i class="fas fa-download me-1"></i>Download</a>
                <button id="rec_delete" class="btn btn-sm btn-outline-danger d-none">Delete</button>
                <button id="rec_close" class="btn btn-sm btn-outline-secondary">Close</button>
            </div>
        </div>
        <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
            <strong>Clips</strong>
            <select id="rec_filter" class="form-select form-select-sm w-auto" aria-label="Camera">
                <option value="">All cameras</option>
                ${cameras.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('')}
            </select>
        </div>
        <div class="row g-2" id="rec_clips"></div>
        <button id="rec_more" class="btn btn-sm btn-outline-secondary mt-2 d-none">Older clips</button>
        <div class="card shadow-sm mt-3"><div class="card-body">
            <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
                <strong>Footage</strong>
                <select id="rec_fcam" class="form-select form-select-sm w-auto" aria-label="Camera">
                    ${cameras.filter(c => c.record?.mode === 'continuous').map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('')}
                </select>
                <input id="rec_fday" type="date" class="form-control form-control-sm w-auto" aria-label="Day">
            </div>
            <div id="rec_hours" class="d-flex flex-wrap gap-1"></div>
            <div id="rec_slots" class="d-flex flex-wrap gap-1 mt-2"></div>
            <div id="rec_fmsg" class="small text-muted mt-1"></div>
        </div></div>`;
    const $ = id => host.querySelector('#' + id);
    const video = $('rec_video');

    function play(src, title, clip) {
        playing = clip || null;
        $('rec_player').classList.remove('d-none');
        video.src = src;
        video.play().catch(() => { /* autoplay refused: the controls are there */ });
        $('rec_title').textContent = title;
        $('rec_download').classList.toggle('d-none', !clip);
        if (clip) $('rec_download').href = `${src}?download=1`;
        $('rec_delete').classList.toggle('d-none', !(clip && isAdmin()));
        $('rec_player').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    function close() {
        video.pause(); video.removeAttribute('src'); video.load();
        $('rec_player').classList.add('d-none');
        playing = null;
    }
    $('rec_close').addEventListener('click', close);
    video.addEventListener('error', () => {
        if (video.getAttribute('src')) $('rec_title').textContent = "This browser can't play that recording (the camera may use H.265). Download it instead.";
    });
    $('rec_delete').addEventListener('click', async () => {
        if (!playing || !window.confirm('Delete this clip?')) return;
        try {
            await api('DELETE', `/api/recordings/clips/${encodeURIComponent(playing.camera)}/${encodeURIComponent(playing.id)}`);
            clips = clips.filter(c => c !== playing);
            close(); paintClips();
        } catch (e) { window.toast?.error(e.message); }
    });

    function paintClips() {
        $('rec_clips').innerHTML = clips.length ? clips.map((c, i) => {
            const base = `/api/recordings/clips/${encodeURIComponent(c.camera)}/${encodeURIComponent(c.id)}`;
            return `<div class="col-6 col-md-4 col-xl-3"><button type="button" class="card h-100 w-100 p-0 text-start rec-clip" data-i="${i}">
                <div class="ratio ratio-16x9 bg-black rounded-top overflow-hidden">
                    ${c.thumb ? `<img src="${base}.jpg" alt="" loading="lazy" style="object-fit:cover">` : ''}</div>
                <div class="p-2 small">
                    <div class="fw-semibold text-truncate">${escapeHtml(name(c.camera))}</div>
                    <div class="text-muted">${escapeHtml(when(c.start))} · ${Math.max(1, Math.round(c.end - c.start))} s</div>
                    <div>${c.labels.map(l => `<span class="badge bg-secondary me-1"><i class="fas ${(LABELS[l] || ['fa-circle'])[0]}"></i> ${escapeHtml((LABELS[l] || [0, l])[1])}</span>`).join('')}</div>
                </div></button></div>`;
        }).join('') : '<div class="col-12 text-muted small">No clips yet. Switch recording on for a camera under Manage.</div>';
        $('rec_more').classList.toggle('d-none', !more);
        host.querySelectorAll('.rec-clip').forEach(b => b.addEventListener('click', () => {
            const c = clips[Number(b.dataset.i)];
            play(`/api/recordings/clips/${encodeURIComponent(c.camera)}/${encodeURIComponent(c.id)}.mp4`,
                 `${name(c.camera)} — ${when(c.start)}`, c);
        }));
    }
    async function loadClips(append) {
        const before = append && clips.length ? `&before=${clips[clips.length - 1].start}` : '';
        try {
            const r = await api('GET', `/api/recordings?limit=${PAGE}${filter ? `&camera=${encodeURIComponent(filter)}` : ''}${before}`);
            clips = append ? clips.concat(r.clips) : r.clips;
            more = r.more;
            paintClips();
        } catch (e) { $('rec_clips').innerHTML = `<div class="col-12 text-danger small">${escapeHtml(e.message)}</div>`; }
    }
    $('rec_filter').addEventListener('change', ev => { filter = ev.target.value; loadClips(false); });
    $('rec_more').addEventListener('click', () => loadClips(true));

    // Footage: a day's hours, then five-minute stretches of the hour picked.
    const day = $('rec_fday');
    const today = new Date();
    day.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    let ranges = [];
    const covered = (a, b) => ranges.some(([s, e]) => s < b && e > a);
    async function loadDay() {
        const cam = $('rec_fcam').value;
        $('rec_slots').innerHTML = '';
        if (!cam) {
            $('rec_hours').innerHTML = '';
            $('rec_fmsg').textContent = 'No camera is set to record all the time. Clips are cut either way.';
            return;
        }
        const [y, m, d] = day.value.split('-').map(Number);
        const start = new Date(y, m - 1, d).getTime() / 1000;
        try {
            ranges = (await api('GET', `/api/recordings/footage/${encodeURIComponent(cam)}?start=${start}&end=${start + 86400}`)).ranges;
        } catch (e) { $('rec_fmsg').textContent = e.message; return; }
        $('rec_fmsg').textContent = ranges.length ? '' : 'Nothing recorded on this day.';
        $('rec_hours').innerHTML = Array.from({ length: 24 }, (_, h) => {
            const a = start + h * 3600;
            return `<button type="button" class="btn btn-sm ${covered(a, a + 3600) ? 'btn-outline-primary' : 'btn-outline-secondary'} rec-hour"
                data-t="${a}" ${covered(a, a + 3600) ? '' : 'disabled'} style="min-width:3.2rem">${String(h).padStart(2, '0')}:00</button>`;
        }).join('');
        host.querySelectorAll('.rec-hour').forEach(b => b.addEventListener('click', () => {
            host.querySelectorAll('.rec-hour').forEach(x => x.classList.toggle('active', x === b));
            const t0 = Number(b.dataset.t);
            $('rec_slots').innerHTML = Array.from({ length: 12 }, (_, i) => {
                const a = t0 + i * SLOT_S;
                return covered(a, a + SLOT_S) ? `<button type="button" class="btn btn-sm btn-outline-primary rec-slot" data-t="${a}">${escapeHtml(hhmm(a))}</button>` : '';
            }).join('');
            host.querySelectorAll('.rec-slot').forEach(s => s.addEventListener('click', () => {
                const a = Number(s.dataset.t);
                play(`/api/recordings/footage/${encodeURIComponent(cam)}/play?start=${a}&seconds=${SLOT_S}`,
                     `${name(cam)} — ${when(a)} (5 minutes)`, null);
            }));
        }));
    }
    $('rec_fcam').addEventListener('change', loadDay);
    day.addEventListener('change', loadDay);

    async function loadStatus() {
        const el = $('rec_status');
        let s;
        try { s = await api('GET', '/api/recordings/status'); }
        catch (e) { el.innerHTML = `<span class="text-danger">${escapeHtml(e.message)}</span>`; return; }
        const u = s.usage;
        const cams = s.wanted.map(id => {
            const c = s.cameras[id];
            const ok = c?.recording;
            return `<div class="text-break"><i class="fas fa-circle ${ok ? 'text-danger' : 'text-secondary'} me-1" style="font-size:.5rem;vertical-align:middle"></i>${escapeHtml(name(id))}${
                ok ? ' <span class="text-muted">recording</span>' : ` <span class="text-danger">${escapeHtml(c?.error || (s.reachable ? 'starting…' : 'not recording'))}</span>`}</div>`;
        }).join('');
        const paused = (s.paused || []).map(id => `<div class="text-break"><i class="fas fa-pause me-1 text-secondary" style="font-size:.6rem"></i>${escapeHtml(name(id))}
            <span class="text-muted">paused — someone is home</span></div>`).join('');
        const awayOnly = s.settings.away_only;
        el.innerHTML = `
            <div class="form-check form-switch mb-1">
                <input class="form-check-input" type="checkbox" id="rec_away" ${awayOnly ? 'checked' : ''} ${isAdmin() ? '' : 'disabled'}>
                <label class="form-check-label" for="rec_away">Only record while everyone is away</label>
                <div class="text-muted">${awayOnly ? 'House mode away or holiday. At home, detection still runs but nothing is saved.'
                    : 'Cameras record whenever recording is on for them, whoever is home.'}</div>
            </div>
            ${awayOnly && s.away == null && (s.paused?.length || s.wanted.length) ? `<div class="small text-warning-emphasis mb-1">House mode isn't set up,
                so cameras are recording all the time. Set it up under Settings → Security.</div>` : ''}
            ${!s.reachable && s.wanted.length ? `<div class="alert alert-warning py-2 mb-2">Nothing is being recorded: the recorder isn't running.
                ${isAdmin() ? '<a href="#" data-zmm-manager>Enable it in the ZMM Manager</a> (Services → Cameras → Recording).' : 'Ask an admin to enable it.'}</div>` : ''}
            <div>${cams}${paused}${cams || paused ? '' : '<span class="text-muted">No camera is set to record.</span>'}</div>
            <div class="text-muted">Using ${gb(u.footage_bytes + u.clip_bytes)} of ${gb(u.max_bytes)} GB
                (${gb(u.clip_bytes)} GB clips) · ${gb(u.free_bytes)} GB free on the disk
                ${isAdmin() ? `· <a href="#" id="rec_limit">Change limit</a>` : ''}</div>`;
        $('rec_away')?.addEventListener('change', async ev => {
            try { await api('PUT', '/api/recordings/settings', { away_only: ev.target.checked }); }
            catch (e) { window.toast?.error(e.message); }
            setTimeout(loadStatus, 1500);                 // the recorder takes the new list on its next check
        });
        $('rec_limit')?.addEventListener('click', async ev => {
            ev.preventDefault();
            const v = window.prompt('Most space recordings may use, in GB. The oldest footage is deleted first when it fills.', s.settings.max_gb);
            if (v == null) return;
            try { await api('PUT', '/api/recordings/settings', { max_gb: Number(v) }); loadStatus(); }
            catch (e) { window.toast?.error(e.message); }
        });
    }

    await Promise.all([loadStatus(), loadClips(false), loadDay()]);
    return { stop: close };
}
