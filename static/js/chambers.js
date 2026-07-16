/**
 * Chambers — the Frames-side room registry.
 *
 * A "chamber" is a room. The name follows the beehive metaphor used by Frames
 * (Hive = the install, Frame = a dashboard, Chamber = a room, Cell = a tile).
 *
 * Heating and the floor-plan editor say "room" internally and deliberately keep
 * doing so; the backend adopts their rooms into this registry by id. Entries
 * with `editable: false` exist only because another subsystem defines them.
 *
 * A device's current chamber ships with the device itself as
 * `device.settings.chamber` (from get_device_list), so nothing here needs a
 * per-device fetch.
 *
 * Zigbee-only for now: the assign API validates against the zigbee service, so
 * Matter/AC/media devices are not assignable yet.
 */

import { state } from './state.js';

const log = zmmLog('chambers');

let chambers = [];
let levels = [];
let firstLoad = null;

const SOURCE_LABELS = {
    config: { label: 'Frames', cls: 'bg-primary' },
    heating: { label: 'Heating', cls: 'bg-danger' },
    floor_plan: { label: 'Floor plan', cls: 'bg-info' },
};

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

/** True for devices the assign API will accept (Zigbee only, for now). */
export function isAssignable(device) {
    return !!device && (!device.protocol || device.protocol === 'zigbee');
}

export function getChambers() {
    return chambers;
}

export function chamberName(id) {
    if (!id) return null;
    const c = chambers.find(x => x.id === id);
    return c ? c.name : id;
}

export async function loadChambers() {
    try {
        const res = await fetch('/api/chambers');
        const data = await res.json();
        if (data.success) {
            chambers = data.chambers || [];
            levels = data.levels || [];
        } else {
            log.warn('chamber registry error:', data.error);
        }
    } catch (e) {
        log.warn('failed to load chambers:', e.message);
    }
    return chambers;
}

/**
 * Resolve once the registry is available, reusing the in-flight initial load.
 *
 * renderChamberPicker() is synchronous, so without this a modal opened before
 * the first fetch lands would render an assigned device as "living (unknown)".
 */
export function ensureChambers() {
    if (!firstLoad) firstLoad = loadChambers();
    return firstLoad;
}

/** Devices currently assigned to a chamber, from the cache we already hold. */
function devicesInChamber(id) {
    return Object.values(state.deviceCache || {})
        .filter(d => d.settings?.chamber === id);
}

// ── device picker (device modal header) ─────────────────────────────

/**
 * Chamber <select> for the device modal header.
 *
 * Deliberately has NO `name` attribute and lives outside `#configForm`:
 * saveConfig() sweeps every named field into /api/device/configure, which would
 * try to configure a Zigbee attribute called "chamber". It saves on change
 * instead, like rename does.
 */
export function renderChamberPicker(device) {
    if (!isAssignable(device)) return '';

    const current = device.settings?.chamber || '';
    const grouped = new Map();
    for (const c of chambers) {
        const key = c.level || '';
        if (!grouped.has(key)) grouped.set(key, []);
        grouped.get(key).push(c);
    }

    const optionsFor = list => list.map(c =>
        `<option value="${esc(c.id)}" ${c.id === current ? 'selected' : ''}>${esc(c.name)}</option>`
    ).join('');

    // Marked selected explicitly rather than relying on it being first: the
    // browser's default-to-first only holds while nothing else is selected.
    let opts = `<option value="" ${current ? '' : 'selected'}>— Unassigned —</option>`;
    for (const level of levels) {
        const list = grouped.get(level.id);
        if (list?.length) {
            opts += `<optgroup label="${esc(level.name)}">${optionsFor(list)}</optgroup>`;
            grouped.delete(level.id);
        }
    }
    // Chambers on no known level (or a level the floor plan doesn't describe)
    const rest = [...grouped.values()].flat();
    if (rest.length) {
        opts += levels.length
            ? `<optgroup label="Other">${optionsFor(rest)}</optgroup>`
            : optionsFor(rest);
    }

    // An assignment pointing at a chamber that no longer exists shouldn't be
    // silently dropped from the list — show it, flagged, so it can be fixed.
    if (current && !chambers.some(c => c.id === current)) {
        opts += `<option value="${esc(current)}" selected>${esc(current)} (unknown)</option>`;
    }

    return `
        <div class="d-flex align-items-center gap-2 mt-2">
            <label class="text-muted small mb-0" for="deviceChamberSelect">
                <i class="fas fa-location-dot"></i> Chamber
            </label>
            <select id="deviceChamberSelect" class="form-select form-select-sm" style="max-width: 200px;"
                    onchange="window.assignDeviceChamber('${esc(device.ieee)}', this.value)">
                ${opts}
            </select>
            <button type="button" class="btn btn-sm btn-link p-0 text-muted"
                    onclick="window.openChamberManager()" title="Manage chambers">
                <i class="fas fa-gear"></i>
            </button>
        </div>
    `;
}

export async function assignDeviceChamber(ieee, chamberId) {
    const select = document.getElementById('deviceChamberSelect');
    if (select) select.disabled = true;
    try {
        const res = await fetch('/api/chambers/assign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ieee, chamber: chamberId || null }),
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'assign failed');

        // Keep the cache in step so chamber counts and the picker stay correct
        // without a full device refetch.
        const dev = state.deviceCache?.[ieee];
        if (dev) {
            dev.settings = dev.settings || {};
            if (data.chamber) dev.settings.chamber = data.chamber;
            else delete dev.settings.chamber;
        }

        window.toast.success(
            data.chamber ? `Moved to ${chamberName(data.chamber)}` : 'Chamber cleared'
        );
    } catch (e) {
        window.toast.error('Could not set chamber: ' + e.message);
        // Put the control back where it was rather than leaving it showing a
        // chamber the device isn't actually in.
        const dev = state.deviceCache?.[ieee];
        if (select) select.value = dev?.settings?.chamber || '';
    } finally {
        if (select) select.disabled = false;
    }
}

// ── chamber manager (modal) ─────────────────────────────────────────

export async function openChamberManager() {
    await loadChambers();

    document.getElementById('chamberManagerModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="chamberManagerModal" tabindex="-1" aria-labelledby="chamberManagerTitle" aria-hidden="true">
            <div class="modal-dialog modal-lg modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="chamberManagerTitle">
                            <i class="fas fa-location-dot"></i> Chambers
                        </h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <div class="text-muted small">
                                Rooms already defined by heating or the floor plan are listed
                                automatically — you don't need to re-create them.
                            </div>
                            <button class="btn btn-sm btn-success flex-shrink-0 ms-2" onclick="window.createChamber()">
                                <i class="fas fa-plus"></i> New
                            </button>
                        </div>
                        <div id="chamberList"></div>
                    </div>
                </div>
            </div>
        </div>
    `);

    renderChamberList();
    new bootstrap.Modal(document.getElementById('chamberManagerModal')).show();
}

function renderChamberList() {
    const el = document.getElementById('chamberList');
    if (!el) return;

    if (!chambers.length) {
        el.innerHTML = `
            <div class="alert alert-light text-center small mb-0">
                No chambers yet. Create one, or define rooms in Heating or the floor plan.
            </div>`;
        return;
    }

    const levelName = id => levels.find(l => l.id === id)?.name || id;

    el.innerHTML = chambers.map(c => {
        const src = SOURCE_LABELS[c.source] || { label: c.source, cls: 'bg-secondary' };
        const count = devicesInChamber(c.id).length;
        // Only Frames-owned chambers can truly be deleted — an adopted room
        // would just reappear from heating / the floor plan on next read.
        const canDelete = c.source === 'config';

        return `
        <div class="card mb-2">
            <div class="card-body py-2">
                <div class="d-flex justify-content-between align-items-center">
                    <div class="min-w-0">
                        <div class="fw-bold text-truncate">
                            <i class="fas ${esc(c.icon || 'fa-cube')} text-muted me-1"></i>
                            ${esc(c.name)}
                        </div>
                        <div class="small text-muted">
                            <span class="badge ${src.cls}">${src.label}</span>
                            ${c.adopted && c.source === 'config'
                                ? '<span class="badge bg-secondary" title="Heating or the floor plan also defines this room, so its id is shared">shared</span>'
                                : ''}
                            ${c.level ? `<span class="ms-1">${esc(levelName(c.level))}</span>` : ''}
                            <span class="ms-1">${count} device${count === 1 ? '' : 's'}</span>
                        </div>
                    </div>
                    <div class="btn-group btn-group-sm flex-shrink-0 ms-2">
                        <button class="btn btn-outline-primary" onclick="window.assignChamberDevices('${esc(c.id)}')" title="Assign devices">
                            <i class="fas fa-plug"></i>
                        </button>
                        <button class="btn btn-outline-secondary" onclick="window.renameChamber('${esc(c.id)}')" title="Rename">
                            <i class="fas fa-pen"></i>
                        </button>
                        <button class="btn btn-outline-danger" ${canDelete ? '' : 'disabled'}
                                onclick="window.deleteChamber('${esc(c.id)}')"
                                title="${canDelete ? 'Delete' : `Defined by ${src.label} — remove it there`}">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
            </div>
        </div>`;
    }).join('');
}

export async function createChamber() {
    const name = await window.zbmPrompt({
        title: 'New chamber',
        label: 'Room name (e.g. Kitchen, Master Bedroom):',
        confirmText: 'Create',
    });
    if (!name) return;
    await saveChamber({ name });
}

export async function renameChamber(id) {
    const existing = chambers.find(c => c.id === id);
    const name = await window.zbmPrompt({
        title: 'Rename chamber',
        label: 'Room name:',
        value: existing?.name || '',
        confirmText: 'Save',
    });
    if (!name) return;
    // Send the id so this renames rather than creating a second chamber. For an
    // adopted room this writes a Frames-side override; heating keeps its own name.
    await saveChamber({ id, name });
}

async function saveChamber(body) {
    try {
        const res = await fetch('/api/chambers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'save failed');
        chambers = data.chambers || chambers;
        renderChamberList();
        window.toast.success(`Chamber "${data.chamber.name}" saved`);
    } catch (e) {
        window.toast.error('Could not save chamber: ' + e.message);
    }
}

export async function deleteChamber(id) {
    const c = chambers.find(x => x.id === id);
    const count = devicesInChamber(id).length;
    const ok = await window.zbmConfirm({
        title: 'Delete chamber',
        message: count
            ? `Delete "${c?.name || id}"? ${count} device${count === 1 ? '' : 's'} will be unassigned.`
            : `Delete "${c?.name || id}"?`,
        confirmText: 'Delete',
        variant: 'danger',
    });
    if (!ok) return;

    try {
        const res = await fetch(`/api/chambers/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const data = await res.json();

        // A chamber that heating or the floor plan also defines survives the
        // delete; the backend says so rather than pretending it worked.
        if (!data.success) {
            if (data.chambers) {
                chambers = data.chambers;
                renderChamberList();
                window.toast.info(data.error);
                return;
            }
            throw new Error(data.error || 'delete failed');
        }

        for (const d of devicesInChamber(id)) delete d.settings.chamber;
        await loadChambers();
        renderChamberList();
        window.toast.success(
            data.unassigned
                ? `Chamber deleted — ${data.unassigned} device${data.unassigned === 1 ? '' : 's'} unassigned`
                : 'Chamber deleted'
        );
    } catch (e) {
        window.toast.error('Could not delete chamber: ' + e.message);
    }
}

// ── bulk assign ─────────────────────────────────────────────────────

export function assignChamberDevices(chamberId) {
    const chamber = chambers.find(c => c.id === chamberId);
    if (!chamber) return;

    const devices = Object.values(state.deviceCache || {})
        .filter(isAssignable)
        .sort((a, b) => (a.friendly_name || a.ieee).localeCompare(b.friendly_name || b.ieee));

    document.getElementById('chamberDevicesModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="chamberDevicesModal" tabindex="-1" aria-labelledby="chamberDevicesTitle" aria-hidden="true">
            <div class="modal-dialog modal-lg modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="chamberDevicesTitle">Devices in "${esc(chamber.name)}"</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        ${devices.length ? `
                        <input type="search" id="chamberDeviceSearch" class="form-control form-control-sm mb-2"
                               placeholder="Filter devices…" aria-label="Filter devices">
                        <div class="list-group" id="chamberDeviceList"></div>
                        ` : `<div class="alert alert-light text-center small mb-0">No assignable devices.</div>`}
                    </div>
                    <div class="modal-footer">
                        <span class="text-muted small me-auto" id="chamberAssignHint"></span>
                        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button class="btn btn-sm btn-primary" id="chamberAssignSave"
                                onclick="window.saveChamberDevices('${esc(chamberId)}')">Save</button>
                    </div>
                </div>
            </div>
        </div>
    `);

    const list = document.getElementById('chamberDeviceList');
    if (list) {
        list.innerHTML = devices.map(d => {
            const inThis = d.settings?.chamber === chamberId;
            const elsewhere = d.settings?.chamber && !inThis ? d.settings.chamber : null;
            return `
            <label class="list-group-item d-flex align-items-center gap-2" data-name="${esc((d.friendly_name || d.ieee).toLowerCase())}">
                <input class="form-check-input m-0" type="checkbox" value="${esc(d.ieee)}" ${inThis ? 'checked' : ''}>
                <span class="flex-grow-1 text-truncate">${esc(d.friendly_name || d.ieee)}</span>
                ${elsewhere
                    // Say where it currently is — ticking this box moves it, and
                    // that shouldn't be a surprise.
                    ? `<span class="badge bg-light text-dark border" title="Currently assigned elsewhere; ticking this will move it">${esc(chamberName(elsewhere))}</span>`
                    : ''}
            </label>`;
        }).join('');
    }

    const search = document.getElementById('chamberDeviceSearch');
    if (search) {
        search.addEventListener('input', () => {
            const q = search.value.trim().toLowerCase();
            list.querySelectorAll('label').forEach(el => {
                el.classList.toggle('d-none', q && !el.dataset.name.includes(q));
            });
        });
    }

    new bootstrap.Modal(document.getElementById('chamberDevicesModal')).show();
}

export async function saveChamberDevices(chamberId) {
    const list = document.getElementById('chamberDeviceList');
    if (!list) return;

    const checked = new Set(
        [...list.querySelectorAll('input[type="checkbox"]:checked')].map(cb => cb.value)
    );

    // Diff against current state: assign the newly ticked, clear the unticked
    // that were in this chamber. Devices in other chambers are left alone.
    const toAssign = [];
    const toClear = [];
    for (const d of Object.values(state.deviceCache || {})) {
        if (!isAssignable(d)) continue;
        const was = d.settings?.chamber === chamberId;
        const now = checked.has(d.ieee);
        if (now && !was) toAssign.push(d.ieee);
        else if (!now && was) toClear.push(d.ieee);
    }

    if (!toAssign.length && !toClear.length) {
        bootstrap.Modal.getInstance(document.getElementById('chamberDevicesModal'))?.hide();
        return;
    }

    const btn = document.getElementById('chamberAssignSave');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…'; }

    try {
        const calls = [];
        if (toAssign.length) calls.push(bulkAssign(toAssign, chamberId));
        if (toClear.length) calls.push(bulkAssign(toClear, null));
        const results = await Promise.all(calls);

        const skipped = results.flatMap(r => r.skipped || []);
        for (const ieee of toAssign) {
            const d = state.deviceCache[ieee];
            if (d && !skipped.includes(ieee)) {
                d.settings = d.settings || {};
                d.settings.chamber = chamberId;
            }
        }
        for (const ieee of toClear) {
            const d = state.deviceCache[ieee];
            if (d && !skipped.includes(ieee)) delete d.settings?.chamber;
        }

        bootstrap.Modal.getInstance(document.getElementById('chamberDevicesModal'))?.hide();
        renderChamberList();

        const changed = toAssign.length + toClear.length - skipped.length;
        if (skipped.length) {
            window.toast.warning(`${changed} device(s) updated, ${skipped.length} skipped`);
        } else {
            window.toast.success(`${changed} device${changed === 1 ? '' : 's'} updated`);
        }
    } catch (e) {
        window.toast.error('Could not update devices: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = 'Save'; }
    }
}

async function bulkAssign(ieees, chamberId) {
    const res = await fetch('/api/chambers/assign/bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ieees, chamber: chamberId }),
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'bulk assign failed');
    return data;
}

// ── init ────────────────────────────────────────────────────────────

export function initChambers() {
    ensureChambers();
}
