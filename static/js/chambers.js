/**
 * Chambers — the one room registry.
 *
 * A heating room and a floor-plan room are the chamber with the same id
 * (`heated` / `mapped`). A device's chamber ships with the device as
 * device.settings.chamber, so no per-device fetch is needed. Zigbee-only for
 * now. See docs/chambers.md.
 */

import { state } from './state.js';

const log = zmmLog('chambers');

let chambers = [];
let levels = [];
let duplicates = [];
let firstLoad = null;

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

function adopt(data) {
    chambers = data.chambers || chambers;
    levels = data.levels || levels;
    duplicates = data.duplicates || [];
}

export async function loadChambers() {
    try {
        const res = await fetch('/api/chambers');
        const data = await res.json();
        if (data.success) adopt(data); else {
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

// device picker (device modal header)

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
    opts += `<option value="__new__">+ New chamber…</option>`;

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
    if (chamberId === '__new__') {
        const created = await promptNewChamber();
        if (!created) {
            if (select) select.value = state.deviceCache?.[ieee]?.settings?.chamber || '';
            return;
        }
        chamberId = created.id;
        if (select) {
            select.insertAdjacentHTML('beforeend',
                `<option value="${esc(created.id)}">${esc(created.name)}</option>`);
            select.value = created.id;
        }
    }
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

// chamber manager (modal)

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
                                One list of rooms for the whole app. Rooms added in Heating or drawn
                                on the floor plan appear here, and a rename here renames them there.
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
                No chambers yet. Create one here, add a room in Heating, or draw one on the floor plan.
            </div>`;
        return;
    }

    const levelName = id => levels.find(l => l.id === id)?.name || id;

    el.innerHTML = renderDuplicates() + chambers.map(c => {
        const count = devicesInChamber(c.id).length;
        const uses = [
            c.heated ? '<span class="badge bg-danger">Heating</span>' : '',
            c.mapped ? '<span class="badge bg-info">Floor plan</span>' : '',
            c.zoned ? '<span class="badge bg-warning text-dark">Zone</span>' : '',
        ].join(' ');
        const why = [c.heated && 'heating', c.mapped && 'the floor plan'].filter(Boolean).join(' and ');

        return `
        <div class="card mb-2" data-chamber="${esc(c.id)}">
            <div class="card-body py-2">
                <div class="d-flex justify-content-between align-items-center">
                    <div class="min-w-0">
                        <div class="fw-bold text-truncate">
                            <i class="fas ${esc(c.icon || 'fa-cube')} text-muted me-1"></i>
                            ${esc(c.name)}
                        </div>
                        <div class="small text-muted">
                            ${uses}
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
                        <button class="btn btn-outline-secondary" onclick="window.showChamberMerge('${esc(c.id)}')" title="Merge into another chamber">
                            <i class="fas fa-code-merge"></i>
                        </button>
                        <button class="btn btn-outline-danger" ${c.in_use ? 'disabled' : ''}
                                onclick="window.deleteChamber('${esc(c.id)}')"
                                title="${c.in_use ? `Used by ${why} — remove it there, or merge it` : 'Delete'}">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
                <div class="chamber-merge-row d-none mt-2"></div>
            </div>
        </div>`;
    }).join('');
}

/** Pairs the backend thinks are one room, each with a one-tap merge. */
function renderDuplicates() {
    const byId = id => chambers.find(c => c.id === id);
    const rows = duplicates.map(d => {
        const a = byId(d.a), b = byId(d.b);
        if (!a || !b) return '';
        // Keep the name of the one in use: that's the one heating and the plan show.
        const [from, into] = a.in_use ? [b, a] : [a, b];
        return `
            <div class="d-flex flex-wrap align-items-center gap-2 py-1">
                <span class="flex-grow-1 min-w-0">
                    <strong>${esc(from.name)}</strong> and <strong>${esc(into.name)}</strong>
                    look like the same room
                </span>
                <button class="btn btn-sm btn-warning flex-shrink-0"
                        onclick="window.mergeChambers('${esc(from.id)}', '${esc(into.id)}')">
                    Merge as "${esc(into.name)}"
                </button>
            </div>`;
    }).join('');
    return rows ? `<div class="alert alert-warning small py-2 mb-3">${rows}</div>` : '';
}

export function showChamberMerge(id) {
    const row = document.querySelector(`#chamberList [data-chamber="${CSS.escape(id)}"] .chamber-merge-row`);
    if (!row) return;
    const others = chambers.filter(c => c.id !== id);
    row.innerHTML = others.length ? `
        <div class="d-flex flex-wrap gap-2 align-items-center">
            <label class="small text-muted mb-0" for="chamberMergeInto">Merge into</label>
            <select class="form-select form-select-sm flex-grow-1" id="chamberMergeInto" style="min-width: 0; max-width: 240px;">
                ${others.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}
            </select>
            <button class="btn btn-sm btn-primary"
                    onclick="window.mergeChambers('${esc(id)}', document.getElementById('chamberMergeInto').value)">Merge</button>
        </div>` : '<span class="small text-muted">No other chamber to merge into.</span>';
    row.classList.toggle('d-none');
}

export async function mergeChambers(source, into) {
    const a = chambers.find(c => c.id === source), b = chambers.find(c => c.id === into);
    const ok = await window.zbmConfirm({
        title: 'Merge chambers',
        message: `Merge "${a?.name || source}" into "${b?.name || into}"?`,
        detail: 'Its devices, groups and dashboards move across, and the merged room keeps the name '
            + `"${b?.name || into}".`,
        confirmText: 'Merge',
    });
    if (!ok) return;
    try {
        const res = await fetch('/api/chambers/merge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source, into }),
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'merge failed');
        for (const d of Object.values(state.deviceCache || {})) {
            if (d.settings?.chamber === data.removed) d.settings.chamber = data.chamber;
        }
        adopt(data);
        renderChamberList();
        window.toast.success(`Merged into "${chamberName(data.chamber)}"`);
    } catch (e) {
        window.toast.error('Could not merge: ' + e.message);
    }
}

export async function createChamber() {
    if (await promptNewChamber()) renderChamberList();
}

/** Ask for a name and create the chamber; the new entry, or null. */
async function promptNewChamber(value = '') {
    const name = await window.zbmPrompt({
        title: 'New chamber',
        label: 'Room name (e.g. Kitchen, Master Bedroom):',
        value,
        confirmText: 'Create',
    });
    if (!name?.trim()) return null;
    return saveChamber({ name: name.trim() });
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
    // The id makes it a rename; heating and the floor plan take the new name too.
    if (await saveChamber({ ...existing, id, name })) renderChamberList();
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
        adopt(data);
        window.toast.success(`Chamber "${data.chamber.name}" saved`);
        return data.chamber;
    } catch (e) {
        window.toast.error('Could not save chamber: ' + e.message);
        return null;
    }
}

export async function deleteChamber(id) {
    const c = chambers.find(x => x.id === id);
    const count = devicesInChamber(id).length;
    const ok = await window.zbmConfirm({
        title: 'Delete chamber',
        message: `Delete "${c?.name || id}"?`
            + (count ? ` ${count} device${count === 1 ? '' : 's'} will be unassigned.` : '')
            + (c?.zoned ? ' Its heating zone is removed too.' : ''),
        confirmText: 'Delete',
        variant: 'danger',
    });
    if (!ok) return;

    try {
        const res = await fetch(`/api/chambers/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const data = await res.json();

        if (!data.success) throw new Error(data.error || 'delete failed');

        for (const d of devicesInChamber(id)) delete d.settings.chamber;
        adopt(data);
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

// bulk assign

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


// pick or create (heating "Add room", drawing a room on the floor plan)

/**
 * Choose the chamber a new heating or plan room is, or name a new one.
 *
 * `exclude(c)` drops chambers that side already has, since one chamber is one
 * heating room and one outline. Resolves `{id, name}` or null on cancel.
 */
export async function chooseChamber({ title = 'Which room is this?', exclude = () => false,
                                      suggestedName = '' } = {}) {
    await loadChambers();
    const free = chambers.filter(c => !exclude(c));
    document.getElementById('chamberChooseModal')?.remove();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="chamberChooseModal" tabindex="-1" aria-labelledby="chamberChooseTitle" aria-hidden="true">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="chamberChooseTitle"><i class="fas fa-location-dot"></i> ${esc(title)}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        ${free.length ? `
                        <label class="form-label small" for="chamberChooseSelect">Existing chamber</label>
                        <select class="form-select mb-3" id="chamberChooseSelect">
                            <option value="">— New chamber —</option>
                            ${free.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}
                        </select>` : ''}
                        <div id="chamberChooseNewWrap">
                            <label class="form-label small" for="chamberChooseName">New chamber name</label>
                            <input type="text" class="form-control" id="chamberChooseName"
                                   value="${esc(suggestedName)}" placeholder="e.g. Kitchen">
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button class="btn btn-sm btn-primary" id="chamberChooseOk">OK</button>
                    </div>
                </div>
            </div>
        </div>`);
    const modalEl = document.getElementById('chamberChooseModal');
    const select = document.getElementById('chamberChooseSelect');
    const nameEl = document.getElementById('chamberChooseName');
    const wrap = document.getElementById('chamberChooseNewWrap');
    select?.addEventListener('change', () => wrap.classList.toggle('d-none', !!select.value));

    // Stacked over the heating modal: lift above it, as dialogs.js does.
    if (document.querySelector('.modal.show')) {
        modalEl.style.zIndex = '1080';
        modalEl.addEventListener('shown.bs.modal', () => {
            const bd = document.querySelectorAll('.modal-backdrop');
            if (bd.length) bd[bd.length - 1].style.zIndex = '1079';
        }, { once: true });
    }

    return new Promise(resolve => {
        let result = null;
        const modal = new bootstrap.Modal(modalEl);
        const ok = async () => {
            if (select?.value) {
                result = chambers.find(c => c.id === select.value) || null;
            } else {
                const name = nameEl.value.trim();
                if (!name) { nameEl.focus(); return; }
                // An existing chamber of that name is the answer, not a duplicate.
                result = chambers.find(c => c.name.trim().toLowerCase() === name.toLowerCase())
                    || await saveChamber({ name });
                if (!result) return;
                if (exclude(result)) {
                    window.toast.error(`"${result.name}" is already here`);
                    result = null;
                    return;
                }
            }
            modal.hide();
        };
        document.getElementById('chamberChooseOk').addEventListener('click', ok);
        nameEl.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); ok(); } });
        modalEl.addEventListener('shown.bs.modal', () => (select || nameEl).focus(), { once: true });
        modalEl.addEventListener('hidden.bs.modal', () => {
            if (document.querySelector('.modal.show')) document.body.classList.add('modal-open');
            modalEl.remove();
            resolve(result ? { id: result.id, name: result.name } : null);
        }, { once: true });
        modal.show();
    });
}

export function initChambers() {
    ensureChambers();
}
