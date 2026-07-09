/**
 * Device Profile Tab
 * ------------------
 * The single per-device mapping + profile surface. Three sub-views:
 *
 *   1. DISCOVER — full-spectrum live cluster introspection. Shows the
 *      device's endpoints / clusters / attributes / commands with
 *      readable/writable/reportable badges, and per-attribute live values.
 *
 *   2. SIGNALS  — the Signal Inspector component (see modal/signals.js):
 *      live raw signals, learn-by-demonstration mapping (values incl. Tuya
 *      datapoints, command→action), the mapped-signals manager and
 *      "promote to model profile". This replaced the old "Map" sub-view.
 *
 *   3. ASSEMBLE — pick a device type, capabilities, actions, reporting.
 *      Save as a profile that auto-applies to any device of this model.
 *
 * The same tab handles Zigbee and Matter devices — the backend hides the
 * difference behind ``/api/profiles/device/{ieee}``.
 *
 * Location: static/js/modal/profile.js
 */

import { state } from '../state.js';
import { createSignalInspector } from './signals.js';

// ---------------------------------------------------------------------------
// Module-level cache for the currently-open device
// ---------------------------------------------------------------------------

let _data = null;               // last response from /api/profiles/device/{ieee}
let _draft = null;              // profile draft being edited in Assemble view
let _activeSubview = 'signals'; // 'discover' | 'signals' | 'assemble'
let _deviceTypes = null;        // cached /api/profiles/device_types result
let _inspector = null;          // Signal Inspector instance mounted in Signals subview

/** Tear down the mounted Signal Inspector (stops its live stream). */
export function cleanupProfileInspector() {
    if (_inspector) { _inspector.destroy(); _inspector = null; }
}

// ---------------------------------------------------------------------------
// Render entry points (called from device-modal.js)
// ---------------------------------------------------------------------------

export function renderProfileTab(device) {
    return `
        <div id="profileTabContent" data-ieee="${device?.ieee || ''}">
            <div class="text-center text-muted p-3">
                <i class="fas fa-spinner fa-spin"></i> Loading device profile…
            </div>
        </div>
    `;
}

export async function initProfileTab(ieee) {
    const root = document.getElementById('profileTabContent');
    if (!root) return;
    root.dataset.ieee = ieee;

    try {
        if (!_deviceTypes) {
            const r = await fetch('/api/profiles/device_types');
            const d = await r.json();
            _deviceTypes = (d?.types || []);
        }
        const r = await fetch(`/api/profiles/device/${encodeURIComponent(ieee)}`);
        _data = await r.json();
        if (!_data?.success) {
            root.innerHTML = `<div class="alert alert-danger">${_data?.error || 'Failed to load'}</div>`;
            return;
        }
        _draft = _data.profile ? _cloneDraft(_data.profile) : _newDraft(_data);
        _render(root, ieee);
    } catch (e) {
        root.innerHTML = `<div class="alert alert-danger">Error: ${_esc(e.message)}</div>`;
    }
}

// ---------------------------------------------------------------------------
// Drafts
// ---------------------------------------------------------------------------

function _newDraft(data) {
    const ident = data.identity || {};
    return {
        id:           _slug(ident.model || ident.product_id || 'new_profile'),
        protocol:     ident.protocol || 'zigbee',
        match: {
            model:        ident.model || '',
            manufacturer: ident.manufacturer || '',
            vendor_id:    ident.vendor_id || null,
            product_id:   ident.product_id || null,
        },
        device_type:  'generic',
        capabilities: [],
        endpoints:    {},
        actions:      [],
        reporting:    [],
        meta:         { source: 'user' },
    };
}

function _cloneDraft(p) {
    return JSON.parse(JSON.stringify(p));
}

// ---------------------------------------------------------------------------
// Top-level render
// ---------------------------------------------------------------------------

function _render(root, ieee) {
    const { identity, profile, ieee_pin } = _data;
    const friendlyType = profile
        ? `${profile.device_type} · ${profile.protocol}`
        : 'No profile applied';
    const matchLine = profile
        ? `Matched <code>${_esc(profile.id)}</code> ${profile.meta?.source === 'bundled' ? '(bundled)' : '(user)'}`
        : 'This device is using built-in handlers only. Map attributes below or build a profile.';

    root.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <h6 class="mb-0">${_esc(identity.model || '?')} <small class="text-muted">·</small> <small class="text-muted">${_esc(identity.manufacturer || '?')}</small></h6>
                <div class="small text-muted">${matchLine}</div>
            </div>
            <div>
                <span class="badge bg-secondary">${_esc(friendlyType)}</span>
                ${ieee_pin ? '<span class="badge bg-warning text-dark ms-1" title="This device is pinned to a specific profile, overriding model match">PINNED</span>' : ''}
            </div>
        </div>

        <ul class="nav nav-pills nav-justified mb-3" id="profileSubNav" role="tablist">
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'discover' ? 'active' : ''}" data-sub="discover"><i class="fas fa-search"></i> Discover</button></li>
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'signals' ? 'active' : ''}"  data-sub="signals"><i class="fas fa-wave-square"></i> Signals</button></li>
            <li class="nav-item"><button class="nav-link ${_activeSubview === 'assemble' ? 'active' : ''}" data-sub="assemble"><i class="fas fa-cube"></i> Assemble</button></li>
        </ul>

        <div id="profileSubBody"></div>
    `;

    // Any re-render rebuilds #profileSubBody, so drop a mounted inspector first.
    cleanupProfileInspector();

    root.querySelectorAll('#profileSubNav button').forEach(btn => {
        btn.onclick = () => {
            _activeSubview = btn.dataset.sub;
            _render(root, ieee);
        };
    });

    const body = root.querySelector('#profileSubBody');
    if (_activeSubview === 'discover') _renderDiscover(body, ieee);
    if (_activeSubview === 'signals')  _renderSignals(body, ieee);
    if (_activeSubview === 'assemble') _renderAssemble(body, ieee);
}

// ===========================================================================
// SUBVIEW: SIGNALS — the unified live-signal + learn + mapped surface.
// Replaces the old "Map" subview; the Signal Inspector component does all
// per-device mapping (values, Tuya DPs, command→action, promote to profile).
// ===========================================================================

function _renderSignals(container, ieee) {
    container.innerHTML = '<div class="signal-inspector-mount"></div>';
    const mount = container.querySelector('.signal-inspector-mount');
    cleanupProfileInspector();
    _inspector = createSignalInspector(mount, { ieee, showPicker: false });
}

// ===========================================================================
// SUBVIEW 1: DISCOVER
// ===========================================================================

function _renderDiscover(container, ieee) {
    const topo = _data.topology || { endpoints: {} };
    const epIds = Object.keys(topo.endpoints || {}).sort((a, b) => parseInt(a) - parseInt(b));

    let html = `
        <div class="d-flex justify-content-between align-items-center mb-2">
            <div class="small text-muted">Live tree of the device's endpoints, clusters, attributes and commands.</div>
            <button class="btn btn-sm btn-outline-primary" id="profileIntrospectBtn">
                <i class="fas fa-sync"></i> Full introspection
            </button>
        </div>
        <div id="profileIntrospectStatus" class="mb-2"></div>
    `;

    if (!epIds.length) {
        html += `<div class="alert alert-warning">No topology cached yet for this device. Hit "Full introspection" to interrogate it.</div>`;
    } else {
        html += '<div class="accordion" id="profileEpAccordion">';
        for (const epId of epIds) {
            const ep = topo.endpoints[epId];
            const clusterIds = Object.keys(ep.clusters || {}).sort();
            html += `
                <div class="accordion-item">
                    <h2 class="accordion-header">
                        <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#profileEp${epId}">
                            <strong>Endpoint ${_esc(epId)}</strong>
                            <span class="badge bg-secondary ms-2">${clusterIds.length} clusters</span>
                        </button>
                    </h2>
                    <div id="profileEp${epId}" class="accordion-collapse collapse" data-bs-parent="#profileEpAccordion">
                        <div class="accordion-body p-2">
                            ${_renderClusterList(ep, epId)}
                        </div>
                    </div>
                </div>
            `;
        }
        html += '</div>';
    }

    container.innerHTML = html;
    const btn = container.querySelector('#profileIntrospectBtn');
    if (btn) btn.onclick = () => _runIntrospection(ieee);
}

function _renderClusterList(ep, epId) {
    const clusterIds = Object.keys(ep.clusters || {}).sort();
    if (!clusterIds.length) return '<div class="small text-muted">No clusters cached.</div>';
    let out = '<div class="table-responsive"><table class="table table-sm mb-0 tbl tbl-sortable"><thead><tr><th>Cluster</th><th>Attrs</th><th>Dir</th></tr></thead><tbody>';
    for (const cid of clusterIds) {
        const cl = ep.clusters[cid];
        const attrCount = Object.keys(cl.attributes || {}).length;
        out += `
            <tr style="cursor:pointer" data-ep="${epId}" data-cluster="${cid}" class="profile-cluster-row">
                <td><code>${_esc(cid)}</code> <small class="text-muted">${_esc(cl.name || '')}</small></td>
                <td>${attrCount}</td>
                <td>${_esc(cl.direction || 'in')}</td>
            </tr>
            <tr class="profile-cluster-detail d-none" data-detail-ep="${epId}" data-detail-cluster="${cid}">
                <td colspan="3" class="bg-light">${_renderAttrTable(cl, epId, cid)}</td>
            </tr>
        `;
    }
    out += '</tbody></table></div>';
    // Defer click binding (innerHTML hasn't been written yet)
    setTimeout(() => {
        document.querySelectorAll('.profile-cluster-row').forEach(r => {
            r.onclick = () => {
                const detail = document.querySelector(
                    `.profile-cluster-detail[data-detail-ep="${r.dataset.ep}"][data-detail-cluster="${r.dataset.cluster}"]`
                );
                if (detail) detail.classList.toggle('d-none');
            };
        });
    }, 0);
    return out;
}

function _renderAttrTable(cluster, epId, clusterId) {
    const attrIds = Object.keys(cluster.attributes || {}).sort();
    if (!attrIds.length) return '<div class="small text-muted p-2">No attributes cached on this cluster.</div>';
    let out = '<table class="table table-sm table-borderless mb-0 tbl tbl-sortable"><thead><tr><th>Attr</th><th>Name</th><th>Type</th><th>Value</th><th class="text-end">Access</th></tr></thead><tbody>';
    for (const aid of attrIds) {
        const a = cluster.attributes[aid];
        const access = [
            a.readable ? '<span class="badge bg-success">R</span>' : '',
            a.writable ? '<span class="badge bg-primary">W</span>' : '',
            a.reportable === true ? '<span class="badge bg-info">Report</span>' : '',
        ].filter(Boolean).join(' ');
        out += `
            <tr>
                <td><code>${_esc(aid)}</code></td>
                <td>${_esc(a.name || '')}</td>
                <td><small>${_esc(a.type || '')}</small></td>
                <td><small><code>${_esc(_fmtVal(a.value))}</code></small></td>
                <td class="text-end">${access || '<small class="text-muted">?</small>'}</td>
            </tr>
        `;
    }
    out += '</tbody></table>';
    return out;
}

async function _runIntrospection(ieee) {
    const status = document.getElementById('profileIntrospectStatus');
    const btn = document.getElementById('profileIntrospectBtn');
    if (btn) { btn.disabled = true; }
    if (status) status.innerHTML = '<div class="alert alert-info py-2 small"><i class="fas fa-spinner fa-spin"></i> Walking every cluster — about 1 second per cluster, plus per-attribute reads. Battery devices may take a minute or two.</div>';
    try {
        const r = await fetch(`/api/profiles/introspect/${encodeURIComponent(ieee)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pace_seconds: 1.0 }),
        });
        const d = await r.json();
        const okCount = d.ok_count ?? (d.results?.length || 0);
        const errCount = d.error_count ?? (d.errors?.length || 0);

        let html;
        if (d.success) {
            html = `<div class="alert alert-success py-2 small">
                Introspection finished — ${okCount} cluster${okCount === 1 ? '' : 's'} interrogated.
            </div>`;
        } else if (okCount > 0) {
            html = `<div class="alert alert-warning py-2 small">
                <strong>Partial success:</strong> ${okCount} OK, ${errCount} failed.
                ${_renderIntrospectErrors(d.errors || [])}
            </div>`;
        } else {
            html = `<div class="alert alert-danger py-2 small">
                <strong>Introspection failed for all ${errCount} clusters.</strong>
                Most likely the device is asleep (battery sensors / TRVs) or out of range.
                Wake it (press a button / open the cover) and try again, or use the
                tree below to introspect one cluster at a time.
                ${_renderIntrospectErrors(d.errors || [])}
            </div>`;
        }
        if (status) status.innerHTML = html;
        // Always reload the topology — even partial results are worth showing
        await initProfileTab(ieee);
    } catch (e) {
        if (status) status.innerHTML = `<div class="alert alert-danger py-2 small">${_esc(e.message)}</div>`;
    } finally {
        if (btn) btn.disabled = false;
    }
}

function _renderIntrospectErrors(errors) {
    if (!errors || !errors.length) return '';
    // Group errors by message so a wall of timeouts collapses to a single row
    const groups = new Map();
    for (const e of errors) {
        const key = e.error || 'unknown';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(`EP${e.ep}/${e.cluster}`);
    }
    const rows = [...groups.entries()].map(([msg, where]) => `
        <li><strong>${_esc(msg)}</strong> — ${_esc(where.join(', '))}</li>
    `).join('');
    return `
        <details class="mt-2">
            <summary class="small">Show ${errors.length} error${errors.length === 1 ? '' : 's'}</summary>
            <ul class="mt-1 small mb-0">${rows}</ul>
        </details>
    `;
}


// ===========================================================================
// SUBVIEW 3: ASSEMBLE
// ===========================================================================

function _renderAssemble(container, ieee) {
    const types = _deviceTypes || [];
    const d = _draft;

    container.innerHTML = `
        <div class="card mb-3">
            <div class="card-header bg-light"><i class="fas fa-id-card"></i> <strong>Profile</strong></div>
            <div class="card-body">
                <div class="row g-2">
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Profile ID</label>
                        <input class="form-control form-control-sm" id="pdId" value="${_esc(d.id)}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Device type</label>
                        <select class="form-select form-select-sm" id="pdType">
                            ${types.map(t => `<option value="${t.id}" ${t.id === d.device_type ? 'selected' : ''}>${_esc(t.label)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Match model</label>
                        <input class="form-control form-control-sm" id="pdMatchModel" value="${_esc(d.match?.model || '')}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label small">Match manufacturer</label>
                        <input class="form-control form-control-sm" id="pdMatchManuf" value="${_esc(d.match?.manufacturer || '')}">
                    </div>
                    ${d.protocol === 'matter' ? `
                        <div class="col-6">
                            <label class="form-label small">Vendor ID</label>
                            <input type="number" class="form-control form-control-sm" id="pdMatchVendor" value="${d.match?.vendor_id ?? ''}">
                        </div>
                        <div class="col-6">
                            <label class="form-label small">Product ID</label>
                            <input class="form-control form-control-sm" id="pdMatchProd" value="${_esc(d.match?.product_id || '')}">
                        </div>
                    ` : ''}
                    <div class="col-12">
                        <label class="form-label small">Capabilities (comma-separated)</label>
                        <input class="form-control form-control-sm" id="pdCaps" value="${_esc((d.capabilities || []).join(', '))}">
                        <div class="form-text">Suggestions for the chosen type appear automatically when type changes.</div>
                    </div>
                </div>
            </div>
        </div>

        ${_renderAssembleActions(d)}
        ${_renderAssembleReporting(d)}
        ${_renderAssembleAttributes(d)}

        <div class="d-flex justify-content-between mt-3">
            <div>
                <button class="btn btn-outline-secondary btn-sm" id="pdExport">
                    <i class="fas fa-download"></i> Export JSON
                </button>
                <button class="btn btn-outline-secondary btn-sm" id="pdImport">
                    <i class="fas fa-upload"></i> Import JSON
                </button>
            </div>
            <div>
                <button class="btn btn-outline-danger btn-sm" id="pdDelete">
                    <i class="fas fa-trash"></i> Delete profile
                </button>
                <button class="btn btn-primary" id="pdSave">
                    <i class="fas fa-save"></i> Save &amp; apply
                </button>
            </div>
        </div>
    `;

    // Bind events
    container.querySelector('#pdType').onchange = (e) => {
        d.device_type = e.target.value;
        const t = (_deviceTypes || []).find(t => t.id === d.device_type);
        if (t && (!d.capabilities || !d.capabilities.length)) {
            d.capabilities = [...(t.capabilities || [])];
        }
        _renderAssemble(container, ieee);
    };
    container.querySelector('#pdSave').onclick = () => _saveAssemble(ieee, container);
    container.querySelector('#pdDelete').onclick = () => _deleteProfile(ieee);
    container.querySelector('#pdExport').onclick = () => _exportProfile();
    container.querySelector('#pdImport').onclick = () => _importProfile(ieee);

    // Cluster & attribute edit, action add, reporting add, etc.
    _bindAssembleControls(container, ieee);
}

function _renderAssembleActions(d) {
    const rows = (d.actions || []).map((a, i) => `
        <tr>
            <td><input class="form-control form-control-sm pd-act-id" value="${_esc(a.id)}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-act-label" value="${_esc(a.label || '')}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-act-ep" value="${a.ep ?? 1}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-act-cluster" value="${_esc(a.cluster || '')}" data-i="${i}" placeholder="0x0006"></td>
            <td><input class="form-control form-control-sm pd-act-cmd" value="${_esc(a.command || '')}" data-i="${i}" placeholder="0x02"></td>
            <td class="text-end"><button class="btn btn-sm btn-outline-danger pd-act-del" data-i="${i}"><i class="fas fa-times"></i></button></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light d-flex justify-content-between">
                <span><i class="fas fa-bolt"></i> <strong>Actions</strong> <small class="text-muted">— shown on Control tab</small></span>
                <button class="btn btn-sm btn-outline-primary" id="pdActAdd"><i class="fas fa-plus"></i> Add</button>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0 tbl">
                    <thead><tr><th>ID</th><th>Label</th><th>EP</th><th>Cluster</th><th>Command</th><th></th></tr></thead>
                    <tbody id="pdActRows">${rows || '<tr><td colspan="6" class="text-center text-muted small">No actions yet.</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    `;
}

function _renderAssembleReporting(d) {
    const rows = (d.reporting || []).map((r, i) => `
        <tr>
            <td><input type="number" class="form-control form-control-sm pd-rep-ep" value="${r.ep ?? 1}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-rep-cluster" value="${_esc(r.cluster || '')}" data-i="${i}"></td>
            <td><input class="form-control form-control-sm pd-rep-attr" value="${_esc(r.attr || '')}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-rep-min" value="${r.min}" data-i="${i}"></td>
            <td><input type="number" class="form-control form-control-sm pd-rep-max" value="${r.max}" data-i="${i}"></td>
            <td><input type="number" step="any" class="form-control form-control-sm pd-rep-delta" value="${r.delta}" data-i="${i}"></td>
            <td class="text-end"><button class="btn btn-sm btn-outline-danger pd-rep-del" data-i="${i}"><i class="fas fa-times"></i></button></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light d-flex justify-content-between">
                <span><i class="fas fa-broadcast-tower"></i> <strong>Reporting</strong> <small class="text-muted">— applied on save and on each interview</small></span>
                <button class="btn btn-sm btn-outline-primary" id="pdRepAdd"><i class="fas fa-plus"></i> Add</button>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0 tbl">
                    <thead><tr><th>EP</th><th>Cluster</th><th>Attr</th><th>Min (s)</th><th>Max (s)</th><th>Δ</th><th></th></tr></thead>
                    <tbody id="pdRepRows">${rows || '<tr><td colspan="7" class="text-center text-muted small">No reporting configured.</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    `;
}

function _renderAssembleAttributes(d) {
    // Render the cluster/attribute matrix as a flat editable table
    const rows = [];
    for (const [epId, ep] of Object.entries(d.endpoints || {})) {
        for (const [cid, cluster] of Object.entries(ep.clusters || {})) {
            for (const [aid, attr] of Object.entries(cluster.attributes || {})) {
                rows.push({ ep: epId, cluster: cid, attr: aid, ...attr });
            }
        }
    }
    const html = rows.map((r, i) => `
        <tr>
            <td><small><code>EP${r.ep} / ${r.cluster} / ${r.attr}</code></small></td>
            <td><input class="form-control form-control-sm pd-attr-name" data-i="${i}" value="${_esc(r.name || '')}"></td>
            <td><input class="form-control form-control-sm pd-attr-scale" data-i="${i}" value="${r.scale ?? ''}" placeholder="1"></td>
            <td><input class="form-control form-control-sm pd-attr-unit" data-i="${i}" value="${_esc(r.unit || '')}"></td>
            <td><input class="form-control form-control-sm pd-attr-class" data-i="${i}" value="${_esc(r.device_class || '')}"></td>
            <td class="text-center"><input type="checkbox" class="form-check-input pd-attr-inv" data-i="${i}" ${r.invert ? 'checked' : ''}></td>
        </tr>
    `).join('');
    return `
        <div class="card mb-3">
            <div class="card-header bg-light">
                <i class="fas fa-tags"></i> <strong>Attribute mappings</strong>
            </div>
            <div class="table-responsive">
                <table class="table table-sm mb-0 tbl">
                    <thead><tr><th>Location</th><th>Name</th><th>Scale</th><th>Unit</th><th>Class</th><th class="text-center">Inv</th></tr></thead>
                    <tbody id="pdAttrRows" data-rows='${JSON.stringify(rows).replace(/'/g, "&#39;")}'>
                        ${html || '<tr><td colspan="6" class="text-center text-muted small">No attributes mapped. Use the Map tab first.</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function _bindAssembleControls(container, ieee) {
    container.querySelector('#pdActAdd')?.addEventListener('click', () => {
        _draft.actions = _draft.actions || [];
        _draft.actions.push({ id: 'new_action', label: 'New action', ep: 1, cluster: '0x0006', command: '0x02' });
        _renderAssemble(container, ieee);
    });
    container.querySelector('#pdRepAdd')?.addEventListener('click', () => {
        _draft.reporting = _draft.reporting || [];
        _draft.reporting.push({ ep: 1, cluster: '0x0402', attr: '0x0000', min: 60, max: 300, delta: 10 });
        _renderAssemble(container, ieee);
    });
    container.querySelectorAll('.pd-act-del').forEach(btn => {
        btn.onclick = () => { _draft.actions.splice(+btn.dataset.i, 1); _renderAssemble(container, ieee); };
    });
    container.querySelectorAll('.pd-rep-del').forEach(btn => {
        btn.onclick = () => { _draft.reporting.splice(+btn.dataset.i, 1); _renderAssemble(container, ieee); };
    });
}

async function _saveAssemble(ieee, container) {
    // Pull all form state back into _draft before saving
    _draft.id          = container.querySelector('#pdId').value.trim();
    _draft.device_type = container.querySelector('#pdType').value;
    _draft.match.model        = container.querySelector('#pdMatchModel').value.trim();
    _draft.match.manufacturer = container.querySelector('#pdMatchManuf').value.trim();
    if (_draft.protocol === 'matter') {
        const v = container.querySelector('#pdMatchVendor')?.value;
        _draft.match.vendor_id  = v ? parseInt(v) : null;
        _draft.match.product_id = container.querySelector('#pdMatchProd')?.value.trim() || null;
    }
    _draft.capabilities = container.querySelector('#pdCaps').value.split(',').map(s => s.trim()).filter(Boolean);

    // Actions
    const actRows = container.querySelectorAll('#pdActRows tr');
    const actions = [];
    actRows.forEach(row => {
        const idInput = row.querySelector('.pd-act-id'); if (!idInput) return;
        actions.push({
            id:      idInput.value.trim(),
            label:   row.querySelector('.pd-act-label').value.trim(),
            ep:      parseInt(row.querySelector('.pd-act-ep').value) || 1,
            cluster: row.querySelector('.pd-act-cluster').value.trim(),
            command: row.querySelector('.pd-act-cmd').value.trim(),
        });
    });
    _draft.actions = actions;

    // Reporting
    const repRows = container.querySelectorAll('#pdRepRows tr');
    const reporting = [];
    repRows.forEach(row => {
        const ep = row.querySelector('.pd-rep-ep'); if (!ep) return;
        reporting.push({
            ep:      parseInt(ep.value) || 1,
            cluster: row.querySelector('.pd-rep-cluster').value.trim(),
            attr:    row.querySelector('.pd-rep-attr').value.trim(),
            min:     parseInt(row.querySelector('.pd-rep-min').value) || 30,
            max:     parseInt(row.querySelector('.pd-rep-max').value) || 600,
            delta:   parseFloat(row.querySelector('.pd-rep-delta').value) || 1,
        });
    });
    _draft.reporting = reporting;

    // Attribute mappings — pull back into endpoints structure
    const rowsJson = container.querySelector('#pdAttrRows')?.dataset.rows;
    if (rowsJson) {
        let baseRows = [];
        try { baseRows = JSON.parse(rowsJson); } catch {}
        const newEndpoints = {};
        baseRows.forEach((r, i) => {
            const name  = container.querySelector(`.pd-attr-name[data-i="${i}"]`)?.value.trim() || '';
            if (!name) return;
            const scale = parseFloat(container.querySelector(`.pd-attr-scale[data-i="${i}"]`)?.value) || 1;
            const unit  = container.querySelector(`.pd-attr-unit[data-i="${i}"]`)?.value.trim() || '';
            const cls   = container.querySelector(`.pd-attr-class[data-i="${i}"]`)?.value.trim() || '';
            const inv   = container.querySelector(`.pd-attr-inv[data-i="${i}"]`)?.checked || false;
            const ep    = newEndpoints[r.ep] ||= { role: 'primary', clusters: {} };
            const cl    = ep.clusters[r.cluster] ||= { attributes: {} };
            const m     = { name };
            if (scale !== 1) m.scale = scale;
            if (unit)        m.unit  = unit;
            if (cls)         m.device_class = cls;
            if (inv)         m.invert = true;
            cl.attributes[r.attr] = m;
        });
        if (Object.keys(newEndpoints).length) _draft.endpoints = newEndpoints;
    }

    try {
        const r = await fetch('/api/profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_draft),
        });
        const d = await r.json();
        if (!d.success) { window.toast.error(d.error || 'Save failed'); return; }
        // Apply to this device immediately
        await fetch(`/api/profiles/apply/${encodeURIComponent(ieee)}`, { method: 'POST' });
        await initProfileTab(ieee);
    } catch (e) { window.toast.error(e.message); }
}

async function _deleteProfile(ieee) {
    if (!_draft.id) return;
    if (!await window.zbmConfirm({
        title: 'Delete profile',
        message: `Delete profile "${_draft.id}"?`,
        detail: 'Devices using it will fall back to built-in handlers.',
        confirmText: 'Delete',
        variant: 'danger'
    })) return;
    const r = await fetch(`/api/profiles/${encodeURIComponent(_draft.id)}`, { method: 'DELETE' });
    const d = await r.json();
    if (d.success) await initProfileTab(ieee);
    else window.toast.error(d.error || 'Delete failed');
}

async function _exportProfile() {
    if (!_draft?.id) { window.toast.warning('Save the profile first.'); return; }
    const r = await fetch(`/api/profiles/export/${encodeURIComponent(_draft.id)}`);
    const d = await r.json();
    if (!d.success) { window.toast.error(d.error || 'Export failed'); return; }
    const blob = new Blob([JSON.stringify(d.profile, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${d.profile.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
}

function _importProfile(ieee) {
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = '.json,application/json';
    inp.onchange = async () => {
        const f = inp.files?.[0]; if (!f) return;
        const text = await f.text();
        let profile;
        try { profile = JSON.parse(text); } catch (e) { window.toast.error('Invalid JSON'); return; }
        const r = await fetch('/api/profiles/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile }),
        });
        const d = await r.json();
        if (!d.success) { window.toast.error(d.error || 'Import failed'); return; }
        await initProfileTab(ieee);
    };
    inp.click();
}

// ===========================================================================
// HELPERS
// ===========================================================================

function _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function _fmtVal(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
}

function _slug(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '');
}

