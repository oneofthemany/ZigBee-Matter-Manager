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

/** The entry's description of an attribute: its setting (writable) or its reading. */
function specOf(entry, a) {
    const st = (entry.zmm?.settings || []).find(x => x.cluster === a.cluster && x.attr === a.attr
        && (x.ep === a.ep || x.ep === 'each'));
    if (st) return { as: 'setting', spec: st, name: st.id };
    const m = entry.endpoints?.[String(a.ep)]?.clusters?.[a.cluster]?.attributes?.[a.attr];
    return m?.name ? { as: 'reading', spec: m, name: m.name } : { as: '', spec: null, name: '' };
}

const ONOFF_SETTING = { 0: 'Off', 1: 'On' };

/** number, choice (named values) or onoff, as the spec now reads. */
function kindOf(found, a) {
    const { as, spec } = found;
    if (!spec) return 'number';
    if (as === 'setting') {
        if (spec.type === 'bool') return 'onoff';
        const v = spec.values || {};
        const keys = Object.keys(v).sort();
        if (keys.join() === '0,1' && v['0'] === 'Off' && v['1'] === 'On') return 'onoff';
        return keys.length ? 'choice' : 'number';
    }
    const vm = spec.value_map || {};
    if (Object.keys(vm).length === 2 && vm['1'] === true && vm['0'] === false) return 'onoff';
    return Object.keys(vm).length ? 'choice' : 'number';
}

function attrKey(a) { return `${a.ep}|${a.cluster}|${a.attr}`; }

/** Shown value: the raw one through the spec's labels, or divided by its scale. */
function shownValue(found, a) {
    const { as, spec } = found;
    if (!spec) return null;
    const labels = as === 'setting' ? spec.values : spec.value_map;
    if (labels && String(a.value) in labels) return String(labels[String(a.value)]);
    const n = Number(a.value);
    return spec.scale && spec.scale !== 1 && Number.isFinite(n) ? String(+(n / spec.scale).toFixed(3)) : null;
}

function field(label, f, key, value, type = 'text', extra = '') {
    return `<div class="col-6 col-md-3"><label class="form-label x-small mb-0">${label}</label>
        <input class="form-control form-control-sm" type="${type}" ${type === 'number' ? 'step="any"' : ''}
            data-qe="attr-f" data-f="${f}" data-key="${esc(key)}" value="${esc(value ?? '')}" ${extra}></div>`;
}

/** The details of one named attribute: what kind of value it is and how to show it. */
function detailsHtml(entry, a) {
    const found = specOf(entry, a);
    const { as, spec } = found;
    const key = attrKey(a);
    const kind = kindOf(found, a);
    const opt = (v, label) => `<option value="${v}" ${v === kind ? 'selected' : ''}>${label}</option>`;
    let html = `<div class="row g-2 align-items-end">
        <div class="col-6 col-md-3"><label class="form-label x-small mb-0">Kind of value</label>
            <select class="form-select form-select-sm" data-qe="attr-f" data-f="kind" data-key="${esc(key)}">
            ${opt('number', 'A number')}${opt('choice', 'One of a list')}${opt('onoff', 'On / off')}</select></div>`;
    if (as === 'setting') html += field('Label', 'label', key, spec.label);
    if (kind === 'number') {
        html += field('Unit', 'unit', key, spec.unit, 'text', 'placeholder="sec, °C, %"')
            + field('Divide by', 'scale', key, spec.scale, 'number', 'min="0" placeholder="1"');
        if (as === 'setting') {
            html += field('Min', 'min', key, spec.min, 'number') + field('Max', 'max', key, spec.max, 'number')
                + field('Step', 'step', key, spec.step, 'number', 'min="0"');
        }
    }
    if (as === 'reading') html += field('Home Assistant class', 'device_class', key, spec.device_class, 'text',
                                         'placeholder="temperature, occupancy…"');
    if (as === 'setting') html += `<div class="col-12">${field('Description', 'description', key, spec.description)
        .replace('col-6 col-md-3', 'w-100')}</div>`;
    html += '</div>';
    if (kind === 'choice') {
        const labels = (as === 'setting' ? spec.values : spec.value_map) || {};
        const rows = Object.entries(labels).sort((x, y) => Number(x[0]) - Number(y[0])).map(([raw, label]) => `<tr>
            <td><code>${esc(raw)}</code></td>
            <td><input class="form-control form-control-sm" data-qe="attr-v" data-key="${esc(key)}" data-raw="${esc(raw)}"
                value="${esc(label)}"></td>
            <td class="text-end"><button class="btn btn-outline-danger btn-sm" data-qe-del="attr-value"
                data-key="${esc(key)}" data-raw="${esc(raw)}">×</button></td></tr>`).join('');
        html += `<div class="small fw-bold mt-2">Values</div>
            <table class="table table-sm small align-middle mb-0"><thead><tr><th>Device sends</th><th>Means</th><th></th></tr></thead>
            <tbody>${rows}<tr>
            <td><input type="number" class="form-control form-control-sm" style="width:6rem"
                data-qe-new="attr-raw-${esc(key)}" placeholder="${esc(a.value)}"></td>
            <td><input class="form-control form-control-sm" data-qe-new="attr-label-${esc(key)}" placeholder="e.g. low"></td>
            <td class="text-end"><button class="btn btn-outline-secondary btn-sm" data-qe-add="attr-value"
                data-key="${esc(key)}">Add</button></td></tr></tbody></table>`;
    }
    return html;
}

function attrRows(entry, device) {
    // The device's own values outside the blob: name one and it is published
    // under that name, as a setting where it can be written, else as a reading.
    const all = device.attributes || [];
    const shown = device.show_all_attributes ? all : all.filter(a => a.reported);
    const open = device.open_attributes || new Set();
    const rows = shown.map(a => {
        const found = specOf(entry, a);
        const pretty = shownValue(found, a);
        const access = a.writable === true ? '<span class="badge bg-success">settable</span>'
            : a.writable === false ? '<span class="badge bg-secondary">read-only</span>'
            : '<span class="badge bg-light text-dark border">unknown</span>';
        const key = attrKey(a);
        const isOpen = found.name && open.has(key);
        const toggle = found.name ? `<input type="checkbox" class="btn-check" id="qe-open-${esc(key)}"
            data-qe="attr-open" data-key="${esc(key)}" ${isOpen ? 'checked' : ''}>
            <label class="btn btn-outline-secondary btn-sm" for="qe-open-${esc(key)}">Details</label>` : '';
        return `<tr>
        <td><code>${esc(a.attr)}</code></td>
        <td class="text-nowrap">${esc(a.value)}${pretty !== null ? ` <span class="text-muted">→ ${esc(pretty)}</span>` : ''}</td>
        <td>${access}</td>
        <td><input class="form-control form-control-sm" style="min-width:8rem" data-qe="attr-name"
            data-key="${esc(key)}" value="${esc(found.name)}" placeholder="unnamed"></td>
        <td class="text-end position-relative">${toggle}</td>
    </tr>` + (isOpen ? `<tr><td colspan="5" class="bg-light">
        <div style="max-width:calc(100vw - 3rem)">${detailsHtml(entry, a)}</div></td></tr>` : '');
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

function humanise(name) {
    return name.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}

/** Name, rename or unname an attribute, keeping what its details already say. */
function nameAttr(entry, a, name) {
    const found = specOf(entry, a);
    if (found.spec && name) {
        if (found.as === 'setting') {
            if (!found.spec.label || found.spec.label === humanise(found.spec.id)) found.spec.label = humanise(name);
            found.spec.id = name;
        } else {
            found.spec.name = name;
        }
        return;
    }
    if (found.as === 'setting') {
        entry.zmm.settings = entry.zmm.settings.filter(x => x !== found.spec);
        if (!entry.zmm.settings.length) delete entry.zmm.settings;
    } else if (found.as === 'reading') {
        delete entry.endpoints[String(a.ep)].clusters[a.cluster].attributes[a.attr];
    }
    if (!name) return;
    if (a.writable && SETTING_TYPES.includes(a.type)) {
        const st = { id: name, label: humanise(name), type: a.type, ep: a.ep, cluster: a.cluster,
                     attr: a.attr, values: {} };
        if (a.mfr) st.mfr = a.mfr;
        zmm(entry, 'settings', []).push(st);
    } else {
        const c = ((ep(entry, a.ep).clusters ??= {})[a.cluster] ??= {});
        (c.attributes ??= {})[a.attr] = { name };
    }
}

/** One details field into the spec; returns an error message, or null. */
function setDetail(entry, a, f, raw) {
    const found = specOf(entry, a);
    if (!found.spec) return 'name it first';
    const { as, spec } = found;
    const value = String(raw ?? '').trim();
    if (f === 'kind') {
        const labelsKey = as === 'setting' ? 'values' : 'value_map';
        if (value === 'number') {
            if (as === 'setting') spec.values = {}; else delete spec.value_map;
        } else if (value === 'onoff') {
            delete spec.scale; delete spec.unit; delete spec.min; delete spec.max; delete spec.step;
            if (as === 'setting') spec.values = spec.type === 'bool' ? {} : { ...ONOFF_SETTING };
            else spec.value_map = { 0: false, 1: true };
        } else {
            delete spec.scale; delete spec.unit; delete spec.min; delete spec.max; delete spec.step;
            if (!Object.keys(spec[labelsKey] || {}).length || kindOf(found, a) === 'onoff') {
                spec[labelsKey] = Number.isInteger(Number(a.value)) ? { [String(a.value)]: String(a.value) } : {};
            }
        }
        return null;
    }
    if (['scale', 'min', 'max', 'step'].includes(f)) {
        if (value === '') { delete spec[f]; return null; }
        const n = Number(value);
        if (!Number.isFinite(n)) return `${f} must be a number`;
        if ((f === 'scale' || f === 'step') && n <= 0) return `${f === 'scale' ? 'divide by' : 'step'} must be above 0`;
        if (f === 'scale' && n === 1) delete spec.scale; else spec[f] = n;
        if (spec.min !== undefined && spec.max !== undefined && spec.min > spec.max) return 'min is above max';
        return null;
    }
    if (f === 'unit' && value.length > 8) return 'a unit is at most 8 characters';
    if (value === '') delete spec[f]; else spec[f] = value;
    return null;
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
        + section('Attributes', ['Attribute', 'Last value', 'Can be set', 'Name', ''],
                  attrRows(entry, device),
                  'The device\'s own values. Name one to publish it under that name: as a setting where it '
                  + 'can be set, otherwise as a reading. Then open Details to say what kind of value it is: '
                  + 'a number (with its unit, range and scale), one of a list (with what each value means), '
                  + 'or on / off.')
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
    case 'attr-open': {
        const open = (device.open_attributes ??= new Set());
        if (el.checked) open.add(d.key); else open.delete(d.key);
        break;
    }
    case 'attr-name': {
        const a = findAttr(device, d.key);
        const name = el.value.trim();
        if (!a) return 'that attribute is no longer known';
        if (name && !NAME_RE.test(name)) return 'a name is lower case letters, digits and _';
        nameAttr(entry, a, name);
        if (name) (device.open_attributes ??= new Set()).add(d.key);   // what it is comes next
        break;
    }
    case 'attr-f': {
        const a = findAttr(device, d.key);
        if (!a) return 'that attribute is no longer known';
        return setDetail(entry, a, d.f, el.value);
    }
    case 'attr-v': {
        const a = findAttr(device, d.key);
        const { as, spec } = a ? specOf(entry, a) : {};
        if (!spec) return 'that attribute is no longer known';
        const label = el.value.trim();
        if (!label) return 'a value needs a meaning; remove it with × instead';
        (as === 'setting' ? (spec.values ??= {}) : (spec.value_map ??= {}))[d.raw] = label;
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

/** A named value of an attribute ("the device sends 2: it means medium"). */
function specByKey(entry, key) {
    const [epId, cluster, attr] = key.split('|');
    const st = (entry.zmm?.settings || []).find(x => x.cluster === cluster && x.attr === attr
        && String(x.ep) === epId);
    if (st) return { labels: (st.values ??= {}) };
    const m = entry.endpoints?.[epId]?.clusters?.[cluster]?.attributes?.[attr];
    return m ? { labels: (m.value_map ??= {}) } : null;
}

function valueButton(entry, root, d) {
    const found = specByKey(entry, d.key);
    if (!found) return 'name it first';
    if (d.qeDel) { delete found.labels[d.raw]; return null; }
    const rawEl = root.querySelector(`[data-qe-new="attr-raw-${CSS.escape(d.key)}"]`);
    const labelEl = root.querySelector(`[data-qe-new="attr-label-${CSS.escape(d.key)}"]`);
    const raw = Number((rawEl?.value || '').trim());
    const label = (labelEl?.value || '').trim();
    if (!Number.isInteger(raw) || (rawEl?.value || '').trim() === '') return 'what the device sends is a whole number';
    if (!label) return 'say what that value means';
    found.labels[String(raw)] = label;
    return null;
}

/** The "Add" and "×" buttons; returns an error message, or null. */
export function applyButton(entry, root, btn) {
    const d = btn.dataset;
    if (d.qeAdd === 'attr-value' || d.qeDel === 'attr-value') return valueButton(entry, root, d);
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
