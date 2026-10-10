/* Zone editor for a camera's object detection: polygons drawn on the camera's
   own picture. Points are stored 0-1 of it; the detector's frame is the same
   picture stretched to a fixed size, so the fractions line up. Backend: modules/cameras.py, vision/zones.py —
   docs/vision.md §Zones. */

import { escapeHtml } from './utils.js';

const COLOURS = ['#ffd400', '#4dc3ff', '#7be07b', '#ff8a5c', '#d59bff', '#ff6fae', '#9be3d6', '#c9c9c9'];
const MAX_ZONES = 8, MAX_POINTS = 24;
const GRAB_PX = 18;            // a fingertip, not a mouse pointer
const LABELS = { person: 'Person', vehicle: 'Vehicle', animal: 'Animal' };

function segmentDistance(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const t = dx || dy ? Math.min(Math.max(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0), 1) : 0;
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/* Mounts into `host`; returns { value() } giving the zones to save. */
export function mountZoneEditor(host, cameraId, initial, cameraLabels) {
    // id: fixed when a zone is first saved, and sent back so a rename keeps its signals.
    const zones = (initial || []).map(z => ({ id: z.id, name: z.name, labels: [...z.labels], points: z.points.map(p => [...p]) }));
    let active = -1, drag = -1, img = null;

    host.innerHTML = `
        <div class="small fw-semibold mt-3 mb-1">Zones <span class="text-muted fw-normal">— only count what is standing inside a shape</span></div>
        <div class="small text-muted mb-1">Draw them on the camera's picture. They take effect while object detection is on for this camera.</div>
        <div id="cz_list"></div>
        <button type="button" class="btn btn-sm btn-outline-primary mt-1" id="cz_add"><i class="fas fa-draw-polygon me-1"></i>Add zone</button>
        <div id="cz_draw" class="mt-2 d-none">
            <div class="position-relative" style="touch-action:none">
                <canvas id="cz_canvas" class="w-100 rounded d-block" style="touch-action:none;cursor:crosshair"></canvas>
            </div>
            <div class="d-flex flex-wrap gap-2 mt-2 align-items-center">
                <button type="button" class="btn btn-sm btn-outline-secondary" id="cz_undo">Undo point</button>
                <button type="button" class="btn btn-sm btn-outline-secondary" id="cz_clear">Clear shape</button>
                <button type="button" class="btn btn-sm btn-outline-secondary" id="cz_reload">Refresh picture</button>
                <button type="button" class="btn btn-sm btn-primary" id="cz_done">Done</button>
                <span class="small text-muted" id="cz_hint"></span>
            </div>
        </div>
        <div id="cz_msg" class="small text-muted mt-1"></div>`;

    const $ = id => host.querySelector('#' + id);
    const canvas = $('cz_canvas'), ctx = canvas.getContext('2d');

    function list() {
        $('cz_list').innerHTML = zones.map((z, i) => `
            <div class="border rounded p-2 mb-2" style="border-left:4px solid ${COLOURS[i % COLOURS.length]} !important">
                <div class="d-flex flex-wrap gap-2 align-items-center">
                    <input class="form-control form-control-sm cz-name" style="max-width:14rem" maxlength="40" data-i="${i}"
                           value="${escapeHtml(z.name)}" placeholder="Name, e.g. Drive" aria-label="Zone name">
                    <button type="button" class="btn btn-sm ${i === active ? 'btn-primary' : 'btn-outline-secondary'} cz-shape" data-i="${i}">
                        ${z.points.length >= 3 ? 'Edit shape' : 'Draw shape'}</button>
                    <button type="button" class="btn btn-sm btn-outline-danger cz-del" data-i="${i}" aria-label="Remove zone ${escapeHtml(z.name)}">Remove</button>
                    <span class="small ${z.points.length >= 3 ? 'text-muted' : 'text-warning-emphasis'}">${z.points.length >= 3 ? `${z.points.length} points` : 'no shape yet'}</span>
                </div>
                <div class="mt-1">${cameraLabels.map(g => `<div class="form-check form-check-inline">
                    <input class="form-check-input cz-label" type="checkbox" id="cz_l_${i}_${g}" data-i="${i}" value="${g}" ${z.labels.includes(g) ? 'checked' : ''}>
                    <label class="form-check-label small" for="cz_l_${i}_${g}">${LABELS[g] || g}</label></div>`).join('')}</div>
            </div>`).join('');
        $('cz_add').disabled = zones.length >= MAX_ZONES;
        host.querySelectorAll('.cz-name').forEach(el => el.addEventListener('input', () => { zones[el.dataset.i].name = el.value; }));
        host.querySelectorAll('.cz-label').forEach(el => el.addEventListener('change', () => {
            const z = zones[el.dataset.i];
            z.labels = cameraLabels.filter(g => host.querySelector(`#cz_l_${el.dataset.i}_${g}`).checked);
        }));
        host.querySelectorAll('.cz-shape').forEach(el => el.addEventListener('click', () => edit(Number(el.dataset.i))));
        host.querySelectorAll('.cz-del').forEach(el => el.addEventListener('click', () => {
            zones.splice(Number(el.dataset.i), 1);
            if (active >= zones.length) active = -1;
            if (active < 0) $('cz_draw').classList.add('d-none');
            list(); paint();
        }));
    }

    function paint() {
        if (!img) return;
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const W = canvas.width, H = canvas.height;
        zones.forEach((z, i) => {
            if (!z.points.length) return;
            const on = i === active, colour = COLOURS[i % COLOURS.length];
            ctx.beginPath();
            z.points.forEach(([x, y], k) => (k ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
            if (z.points.length >= 3) ctx.closePath();
            ctx.lineWidth = on ? 3 : 2;
            ctx.strokeStyle = colour;
            ctx.globalAlpha = on ? 0.3 : 0.15;
            ctx.fillStyle = colour;
            if (z.points.length >= 3) ctx.fill();
            ctx.globalAlpha = 1;
            ctx.stroke();
            if (on) {
                for (const [x, y] of z.points) {
                    ctx.beginPath();
                    ctx.arc(x * W, y * H, 6, 0, Math.PI * 2);
                    ctx.fillStyle = '#fff'; ctx.fill();
                    ctx.lineWidth = 2; ctx.stroke();
                }
            }
        });
        const z = zones[active];
        $('cz_hint').textContent = !z ? '' : z.points.length < 3 ? `Tap the picture to add corners (${z.points.length}/3 so far).`
            : 'Tap near an edge to add a corner there; drag a corner to move it.';
    }

    async function loadFrame() {
        $('cz_msg').textContent = 'Getting the picture…';
        try {
            // The camera's live picture; the detector's frame if go2rtc can't give one.
            let res = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/snapshot?t=${Date.now()}`);
            if (!res.ok) res = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/detection?view=frame&t=${Date.now()}`);
            if (!res.ok) throw new Error();
            const next = new Image();
            const url = URL.createObjectURL(await res.blob());
            await new Promise((ok, bad) => { next.onload = ok; next.onerror = bad; next.src = url; });
            img = next;
            canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
            $('cz_msg').textContent = '';
            paint();
            URL.revokeObjectURL(url);
            return true;
        } catch (e) {
            $('cz_msg').innerHTML = `<span class="text-warning-emphasis">Couldn't get a picture from this camera.
                Zones are drawn on its live view, which needs go2rtc running and the camera reachable.</span>`;
            return false;
        }
    }

    async function edit(i) {
        active = i;
        list();
        $('cz_draw').classList.remove('d-none');
        if (!img && !await loadFrame()) { $('cz_draw').classList.add('d-none'); active = -1; list(); return; }
        paint();
        canvas.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // Pointer position in canvas pixels and 0-1 of the picture.
    function at(ev) {
        const r = canvas.getBoundingClientRect();
        const nx = Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1);
        const ny = Math.min(Math.max((ev.clientY - r.top) / r.height, 0), 1);
        return { nx, ny, scale: r.width };
    }

    canvas.addEventListener('pointerdown', ev => {
        const z = zones[active];
        if (!z) return;
        ev.preventDefault();
        const { nx, ny, scale } = at(ev);
        const r = canvas.getBoundingClientRect();
        // Nearest existing corner within a fingertip, measured on screen.
        let best = -1, bestD = GRAB_PX;
        z.points.forEach(([x, y], k) => {
            const d = Math.hypot((x - nx) * scale, (y - ny) * r.height);
            if (d < bestD) { best = k; bestD = d; }
        });
        if (best >= 0) drag = best;
        else if (z.points.length < MAX_POINTS) {
            // Into the nearest edge, not onto the end: appending would cross the shape over itself.
            let after = z.points.length - 1, near = Infinity;
            if (z.points.length >= 3) {
                z.points.forEach(([ax, ay], k) => {
                    const [bx, by] = z.points[(k + 1) % z.points.length];
                    const d = segmentDistance(nx * scale, ny * r.height, ax * scale, ay * r.height, bx * scale, by * r.height);
                    if (d < near) { near = d; after = k; }
                });
            }
            z.points.splice(after + 1, 0, [nx, ny]);
            drag = after + 1;
        }
        canvas.setPointerCapture(ev.pointerId);
        paint();
    });
    canvas.addEventListener('pointermove', ev => {
        if (drag < 0 || !zones[active]) return;
        const { nx, ny } = at(ev);
        zones[active].points[drag] = [nx, ny];
        paint();
    });
    const drop = () => { if (drag >= 0) { drag = -1; list(); } };
    canvas.addEventListener('pointerup', drop);
    canvas.addEventListener('pointercancel', drop);

    $('cz_add').addEventListener('click', () => {
        zones.push({ name: `Zone ${zones.length + 1}`, labels: [...cameraLabels], points: [] });
        edit(zones.length - 1);
    });
    $('cz_undo').addEventListener('click', () => { zones[active]?.points.pop(); list(); paint(); });
    $('cz_clear').addEventListener('click', () => { if (zones[active]) zones[active].points = []; list(); paint(); });
    $('cz_reload').addEventListener('click', loadFrame);
    $('cz_done').addEventListener('click', () => { active = -1; $('cz_draw').classList.add('d-none'); list(); });

    list();
    return {
        value: () => zones.map(z => ({ id: z.id, name: z.name.trim(), labels: z.labels,
                                       points: z.points.map(([x, y]) => [Number(x.toFixed(4)), Number(y.toFixed(4))]) })),
    };
}
