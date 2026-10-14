/**
 * The entry editor: a form over the fields a user most often has to correct
 * (per-EP type, name, metering and actions; measurement scaling; the device's
 * own attributes; blob tags; press names). It edits the same entry object the JSON box shows, so either
 * can be used; saving goes through the normal profile save.
 */

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const NAME_RE = /^[a-z][a-z0-9_]{0,39}$/;
const MEASURE_LABEL = { active_power: 'Power', rms_voltage: 'Voltage', rms_current: 'Current', energy: 'Energy' };

function measured(v) {
    const m = /^measures:([\d,]+)$/.exec(v || '');
    return m ? m[1].split(',').map(Number) : null;
}

function epName(device, ep) {
    const e = device.endpoints.find(x => x.ep === ep);
    return e?.label ? `EP${ep} ${e.label}` : `EP${ep}`;
}

function endpointRows(entry, device) {
    const eps = entry.endpoints || {};
    return device.endpoints.map(d => {
        const e = eps[String(d.ep)] || {};
        const targets = measured(e.metering);
        const scope = targets ? 'measures' : (e.metering || '');
        const hasPower = d.clusters.includes('0x0B04') || d.clusters.includes('0x0702');
        const opt = (v, label, cur) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${label}</option>`;
        const picks = targets ? `<div class="d-flex flex-wrap gap-2 mt-1">${device.endpoints.map(o => `
            <label class="form-check-label small"><input type="checkbox" class="form-check-input me-1"
                data-qe="ep-measures" data-ep="${d.ep}" data-target="${o.ep}"
                ${targets.includes(o.ep) ? 'checked' : ''}>${esc(epName(device, o.ep))}</label>`).join('')}</div>`
            : '';
        return `<tr>
            <td class="text-nowrap">EP${d.ep}<div class="text-muted" style="font-size:.7rem">now ${esc(d.kind || '?')}</div></td>
            <td><select class="form-select form-select-sm" data-qe="ep-kind" data-ep="${d.ep}">
                ${opt('', 'decided by evidence', e.kind || '')}${opt('switch', 'switch', e.kind)}${opt('light', 'light', e.kind)}
            </select></td>
            <td><input class="form-control form-control-sm" data-qe="ep-label" data-ep="${d.ep}"
                value="${esc(e.label || '')}" placeholder="${esc(d.label || 'name')}"></td>
            <td>${hasPower ? `<select class="form-select form-select-sm" data-qe="ep-metering" data-ep="${d.ep}">
                ${opt('', 'decided by evidence', scope)}${opt('self', 'this EP', scope)}
                ${opt('measures', 'other EPs…', scope)}${opt('device_total', 'the whole device', scope)}
                ${opt('none', 'nothing (ignore)', scope)}</select>${picks}` : '<span class="text-muted">—</span>'}</td>
            <td class="text-center"><input type="checkbox" class="form-check-input" data-qe="ep-actions"
                data-ep="${d.ep}" ${e.actions === 'multistate' ? 'checked' : ''}
                ${d.clusters.includes('0x0012') ? '' : 'disabled'}></td></tr>`;
    }).join('');
}

function measurementRows(entry, device) {
    const ms = entry.zmm?.measurements || {};
    return Object.entries(device.measurements).map(([name, spec]) => {
        const m = ms[name];
        const mode = !(name in ms) ? '' : m === null ? 'absent' : 'scaled';
        const opt = (v, label) => `<option value="${v}" ${v === mode ? 'selected' : ''}>${label}</option>`;
        const num = k => `<input type="number" min="1" step="1" class="form-control form-control-sm d-inline-block"
            style="width:6rem" data-qe="m-num" data-m="${name}" data-k="${k}" value="${esc(m[k] ?? 1)}" title="${k}">`;
        const nums = mode === 'scaled' ? `raw × ${num('multiplier')} ÷ ${num('divisor')}` : '';
        return `<tr><td>${esc(MEASURE_LABEL[name] || name)}<div class="text-muted" style="font-size:.7rem">
                ${esc(spec.cluster)}/${esc(spec.attr)}</div></td>
            <td><select class="form-select form-select-sm" data-qe="m-mode" data-m="${name}">
                ${opt('', 'as the device reports')}${opt('scaled', 'scale it')}${opt('absent', 'not supported (hide)')}
            </select></td><td>${nums}</td></tr>`;
    }).join('');
}

function tagRows(entry, device) {
    // Every tag the device has sent, with its value, beside those the entry names:
    // the value is what a person matches against what they know.
    const tags = entry.zmm?.struct_tags || {};
    const seen = Object.fromEntries((device.blob_tags || []).map(t => [t.tag, t]));
    const keys = [...new Set([...Object.keys(seen), ...Object.keys(tags)])]
        .sort((a, b) => parseInt(a, 16) - parseInt(b, 16));
    const shown = (t, spec) => {
        if (!t) return '<span class="text-muted">not seen</span>';
        const v = esc(t.value);
        const n = Number(t.value);
        return spec?.scale && spec.scale !== 1 && Number.isFinite(n)
            ? `${v} <span class="text-muted">→ ${esc(+(n * spec.scale).toFixed(3))}</span>` : v;
    };
    return keys.map(tag => {
        const spec = tags[tag];
        const named = tag in tags;
        const hint = named ? 'dropped' : (seen[tag]?.map ? `unnamed (map: ${seen[tag].map})` : 'unnamed');
        return `<tr>
        <td><code>${esc(tag)}</code></td>
        <td class="text-nowrap">${shown(seen[tag], spec)}</td>
        <td><input class="form-control form-control-sm" style="min-width:10rem" data-qe="tag-name"
            data-key="${esc(tag)}" value="${esc(spec?.name || '')}" placeholder="${esc(hint)}"></td>
        <td><input type="number" step="any" class="form-control form-control-sm" style="width:6rem"
            data-qe="tag-scale" data-key="${esc(tag)}" value="${esc(spec?.scale ?? '')}" ${spec ? '' : 'disabled'}></td>
        <td class="text-end">${named ? `<button class="btn btn-outline-danger btn-sm" data-qe-del="tag"
            data-key="${esc(tag)}" title="forget what the entry says of it">×</button>` : ''}</td>
    </tr>`;
    }).join('') + `<tr><td><input class="form-control form-control-sm" style="width:5rem"
        data-qe-new="tag" placeholder="0x97"></td><td colspan="3" class="text-muted small">a tag not seen yet;
        an empty name drops it</td>
        <td class="text-end"><button class="btn btn-outline-secondary btn-sm" data-qe-add="tag">Add</button></td></tr>`;
}

const SETTING_TYPES = ['bool', 'uint8', 'uint16', 'uint32', 'int8', 'int16', 'enum8'];

/** What the entry calls an attribute now: a setting (writable) or a reading. */
function attrName(entry, a) {
    const st = (entry.zmm?.settings || []).find(x => x.cluster === a.cluster && x.attr === a.attr
        && (x.ep === a.ep || x.ep === 'each'));
    if (st) return { name: st.id, scale: st.scale, as: 'setting' };
    const m = entry.endpoints?.[String(a.ep)]?.clusters?.[a.cluster]?.attributes?.[a.attr];
    return m?.name ? { name: m.name, scale: m.scale, as: 'reading' } : { name: '', scale: undefined, as: '' };
}

function attrRows(entry, device) {
    // The device's own values outside the blob: name one and it is published
    // under that name, as a setting where it can be written, else as a reading.
    const all = device.attributes || [];
    const shown = device.show_all_attributes ? all : all.filter(a => a.reported);
    const rows = shown.map(a => {
        const now = attrName(entry, a);
        const n = Number(a.value);
        const scaled = now.scale && now.scale !== 1 && Number.isFinite(n)
            ? ` <span class="text-muted">→ ${esc(+(n / now.scale).toFixed(3))}</span>` : '';
        const access = a.writable === true ? '<span class="badge bg-success">settable</span>'
            : a.writable === false ? '<span class="badge bg-secondary">read-only</span>'
            : '<span class="badge bg-light text-dark border">unknown</span>';
        const key = `${a.ep}|${a.cluster}|${a.attr}`;
        return `<tr>
        <td><code>${esc(a.attr)}</code></td>
        <td class="text-nowrap">${esc(a.value)}${scaled}</td>
        <td>${access}</td>
        <td><input class="form-control form-control-sm" style="min-width:10rem" data-qe="attr-name"
            data-key="${esc(key)}" value="${esc(now.name)}" placeholder="unnamed"></td>
        <td><input type="number" step="any" min="0" class="form-control form-control-sm" style="width:6rem"
            data-qe="attr-scale" data-key="${esc(key)}" value="${esc(now.scale ?? '')}" placeholder="1"
            ${now.name ? '' : 'disabled'} title="what the raw value is divided by: 1000 for ms → s"></td>
    </tr>`;
    }).join('');
    const hidden = all.length - shown.length;
    const toggle = all.some(a => !a.reported) ? `<tr><td colspan="5" class="small">
        <label class="form-check-label"><input type="checkbox" class="form-check-input me-1" data-qe="attr-all"
        ${device.show_all_attributes ? 'checked' : ''}>Also show the ${all.length - all.filter(a => a.reported).length}
        it lists but never sends by itself (usually the maker's internals)</label></td></tr>` : '';
    return (rows || `<tr><td colspan="5" class="text-muted small">${hidden ? 'None it sends by itself.'
        : 'None known yet: run Find everything first.'}</td></tr>`) + toggle;
}

function findAttr(device, key) {
    const [ep, cluster, attr] = key.split('|');
    return (device.attributes || []).find(a => String(a.ep) === ep && a.cluster === cluster && a.attr === attr);
}

/** Name (or unname) an attribute: one place in the entry, the right kind for it. */
function nameAttr(entry, a, name, scale) {
    entry.zmm ??= {};
    entry.zmm.settings = (entry.zmm.settings || []).filter(x => !(x.cluster === a.cluster && x.attr === a.attr
        && x.ep === a.ep));
    const cl = entry.endpoints?.[String(a.ep)]?.clusters?.[a.cluster];
    if (cl?.attributes) delete cl.attributes[a.attr];
    if (!entry.zmm.settings.length) delete entry.zmm.settings;
    if (!name) return;
    if (a.writable && SETTING_TYPES.includes(a.type)) {
        const st = { id: name, label: name.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()),
                     type: a.type, ep: a.ep, cluster: a.cluster, attr: a.attr, values: {} };
        if (a.mfr) st.mfr = a.mfr;
        if (scale && scale !== 1) st.scale = scale;
        zmm(entry, 'settings', []).push(st);
    } else {
        const c = ((ep(entry, a.ep).clusters ??= {})[a.cluster] ??= {});
        const m = { name };
        if (scale && scale !== 1) m.scale = scale;
        (c.attributes ??= {})[a.attr] = m;
    }
}

function pressRows(entry) {
    const presses = entry.zmm?.press_names || {};
    return Object.entries(presses).map(([value, name]) => `<tr>
        <td><code>${esc(value)}</code></td>
        <td><input class="form-control form-control-sm" data-qe="press-name" data-key="${esc(value)}" value="${esc(name)}"></td>
        <td class="text-end"><button class="btn btn-outline-danger btn-sm" data-qe-del="press" data-key="${esc(value)}">×</button></td>
    </tr>`).join('') + `<tr><td><input type="number" class="form-control form-control-sm" style="width:5rem"
        data-qe-new="press" placeholder="value"></td><td class="text-muted small">then name it above</td>
        <td class="text-end"><button class="btn btn-outline-secondary btn-sm" data-qe-add="press">Add</button></td></tr>`;
}

export function editorHtml(entry, device) {
    const section = (title, head, rows, hint) => `<div class="fw-bold small mt-2">${title}</div>
        ${hint ? `<div class="text-muted small">${hint}</div>` : ''}
        <div class="table-responsive"><table class="table table-sm small align-middle mb-1">
        ${head ? `<thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead>` : ''}<tbody>${rows}</tbody></table></div>`;
    const settings = entry.zmm?.settings || [];
    return section('Endpoints', ['EP', 'Type', 'Name', 'Its power reading measures', 'Button actions'],
                   endpointRows(entry, device),
                   'Where a reading covers other sockets (or the whole device), say so here: '
                   + 'ZMM then files it under what it really measures.')
        + (Object.keys(device.measurements).length
            ? section('Measurements', ['Reading', 'Handling', 'Scaling'], measurementRows(entry, device)) : '')
        + section('Attributes', ['Attribute', 'Last value', 'Can be set', 'Name', 'Divide by'],
                  attrRows(entry, device),
                  'The device\'s own values. Name one to publish it under that name: as a setting where it '
                  + 'can be set, otherwise as a reading.')
        + section('Blob tags', ['Tag', 'Last value', 'Name', 'Scale', ''], tagRows(entry, device),
                  'Values packed in the maker\'s status report (Aqara 0xF7/0xDF). Name a tag to publish it; '
                  + 'the scale turns the raw value into its unit (0.001 for mV → V).')
        + section('Press names', null, pressRows(entry), 'Button value → action name.')
        + (settings.length ? `<div class="small text-muted">Settings: ${settings.map(s => esc(s.label || s.id)).join(', ')}
            (edit in the JSON below)</div>` : '');
}

function ep(entry, id) {
    entry.endpoints ??= {};
    return (entry.endpoints[String(id)] ??= {});
}

function zmm(entry, key, empty) {
    entry.zmm ??= {};
    return (entry.zmm[key] ??= empty);
}

function setOrDrop(obj, key, value) {
    if (value === '' || value === undefined || value === null) delete obj[key];
    else obj[key] = value;
}

/** Apply one form control to `entry`; returns an error message, or null. */
export function applyField(entry, el, device) {
    const d = el.dataset;
    switch (d.qe) {
    case 'ep-kind': setOrDrop(ep(entry, d.ep), 'kind', el.value); break;
    case 'ep-label': setOrDrop(ep(entry, d.ep), 'label', el.value.trim()); break;
    case 'ep-actions': setOrDrop(ep(entry, d.ep), 'actions', el.checked ? 'multistate' : ''); break;
    case 'ep-metering':
        // "other EPs" starts from this EP alone; the user then ticks what it really covers
        setOrDrop(ep(entry, d.ep), 'metering', el.value === 'measures' ? `measures:${d.ep}` : el.value);
        break;
    case 'ep-measures': {
        const e = ep(entry, d.ep);
        const set = new Set(measured(e.metering) || []);
        if (el.checked) set.add(Number(d.target)); else set.delete(Number(d.target));
        if (!set.size) return 'a reading must measure at least one EP';
        e.metering = `measures:${[...set].sort((a, b) => a - b).join(',')}`;
        break;
    }
    case 'm-mode': {
        const ms = zmm(entry, 'measurements', {});
        if (el.value === '') delete ms[d.m];
        else if (el.value === 'absent') ms[d.m] = null;
        else {
            const spec = device.measurements[d.m];
            ms[d.m] = { cluster: spec.cluster, attr: spec.attr, multiplier: 1, divisor: 1,
                        ...(d.m === 'energy' ? { ep: spec.ep } : {}) };
        }
        break;
    }
    case 'm-num': {
        const n = Number(el.value);
        if (!Number.isInteger(n) || n < 1) return `${d.k} must be a whole number of 1 or more`;
        zmm(entry, 'measurements', {})[d.m][d.k] = n;
        break;
    }
    case 'tag-name': {
        const name = el.value.trim();
        if (name && !NAME_RE.test(name)) return 'a tag name is lower case letters, digits and _';
        const tags = zmm(entry, 'struct_tags', {});
        if (!name && !(d.key in tags)) break;       // a seen tag left unnamed stays as it was
        tags[d.key] = name ? { name, scale: tags[d.key]?.scale ?? 1 } : null;
        break;
    }
    case 'tag-scale': {
        const n = Number(el.value);
        if (!Number.isFinite(n) || n === 0) return 'scale must be a non-zero number';
        const tags = zmm(entry, 'struct_tags', {});
        if (tags[d.key]) tags[d.key].scale = n;
        break;
    }
    case 'attr-all': device.show_all_attributes = el.checked; break;
    case 'attr-name': {
        const a = findAttr(device, d.key);
        const name = el.value.trim();
        if (!a) return 'that attribute is no longer known';
        if (name && !NAME_RE.test(name)) return 'a name is lower case letters, digits and _';
        nameAttr(entry, a, name, attrName(entry, a).scale);
        break;
    }
    case 'attr-scale': {
        const a = findAttr(device, d.key);
        const n = el.value === '' ? 1 : Number(el.value);
        if (!a) return 'that attribute is no longer known';
        if (!Number.isFinite(n) || n <= 0) return 'divide by a number above 0';
        const now = attrName(entry, a);
        if (now.name) nameAttr(entry, a, now.name, n);
        break;
    }
    case 'press-name': {
        const name = el.value.trim();
        if (!NAME_RE.test(name)) return 'a press name is lower case letters, digits and _';
        zmm(entry, 'press_names', {})[d.key] = name;
        break;
    }
    default: return null;
    }
    return null;
}

/** The "Add" and "×" buttons; returns an error message, or null. */
export function applyButton(entry, root, btn) {
    const d = btn.dataset;
    if (d.qeDel) {
        const key = d.qeDel === 'tag' ? 'struct_tags' : 'press_names';
        delete (entry.zmm?.[key] || {})[d.key];
        return null;
    }
    const input = root.querySelector(`[data-qe-new="${d.qeAdd}"]`);
    const raw = (input?.value || '').trim();
    if (d.qeAdd === 'tag') {
        const n = Number(raw);
        if (!raw || !Number.isInteger(n) || n < 0 || n > 0xFFFF) return 'a tag is a number such as 0x97';
        const tag = `0x${n.toString(16).toUpperCase().padStart(2, '0')}`;
        zmm(entry, 'struct_tags', {})[tag] ??= null;
    } else {
        const n = Number(raw);
        if (!raw || !Number.isInteger(n)) return 'a press value is a whole number';
        zmm(entry, 'press_names', {})[String(n)] ??= 'press';
    }
    return null;
}
