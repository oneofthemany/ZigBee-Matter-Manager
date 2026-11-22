/**
 * The device Overview form's rules, free of imports so node can test them
 * (tests/devices/js/test_form_guard.js). docs/device-profiles.md §4, "Accepted".
 */

/** Battery devices sleep: a write reaches them only while they are awake. */
export function isSleepy(device) {
    return !!device && (device.power_source === 'Battery' || device.type === 'EndDevice');
}

/** The QoS the hub stores for the device; 0 where none was ever saved. */
export function storedQos(device) {
    const q = device && device.settings ? device.settings.qos : undefined;
    return q === undefined || q === null ? 0 : q;
}

/** What a save sends: only controls changed from the value they showed
 *  (data-initial), so saving one setting never rewrites QoS or the rest.
 *  `entries` are [name, value] pairs; `elements` maps a name to its control. */
export function collectChanges(entries, elements) {
    const updates = {};
    let qos = null, pollingInterval = null;
    for (const [key, value] of entries) {
        const field = elements ? elements[key] : undefined;
        // A saved reporting interval cleared: back to the device's default reporting.
        if (key === 'reporting_max' && value === '' && field?.dataset?.initial) {
            updates.reporting_max = 'default';
            continue;
        }
        if (value === '') continue;
        if (field?.dataset?.initial !== undefined && field.dataset.initial === value) continue;
        if (key === 'qos') { qos = parseInt(value); continue; }
        if (key === 'polling_interval') { pollingInterval = parseInt(value); continue; }
        if (key === 'reporting_max') { updates.reporting_max = parseInt(value); continue; }
        if (key.startsWith('tuya_')) updates[key.replace('tuya_', '')] = parseFloat(value);
        else updates[key] = !isNaN(value) && String(value).trim() !== '' ? parseFloat(value) : value;
    }
    return { updates, qos, pollingInterval };
}

/** True while the form holds something a re-render would lose: focus, an
 *  edit not yet saved, or a save in flight. */
export function formIsBusy(form, active) {
    if (!form) return false;
    if (form.dataset && form.dataset.saving === '1') return true;
    if (active && typeof form.contains === 'function' && form.contains(active)) return true;
    for (const el of Array.from(form.elements || [])) {
        if (!el || !el.name || !el.dataset || el.dataset.initial === undefined) continue;
        if (String(el.value) !== String(el.dataset.initial)) return true;
    }
    return false;
}

/** The value a control shows. Live state wins: the schema's value is as old as
 *  the device list. A waiting setting shows what was asked for. */
export function shownValue(def, s) {
    const live = s ? s[def.name] : undefined;
    if (!def.pending && def.type !== 'zones' && live !== undefined && live !== null) return live;
    if (def.current_value !== undefined && def.current_value !== null) return def.current_value;
    return '';
}

/** Put what the device took into the cached device, so the next render
 *  shows it before the device list is fetched again. */
export function seedApplied(device, applied, qos) {
    if (!device) return;
    device.state = device.state || {};
    for (const [name, value] of Object.entries(applied || {})) {
        device.state[name] = value;
        const def = (device.config_schema || []).find(d => d.name === name);
        if (def) { def.current_value = value; def.pending = false; }
    }
    if (qos !== null && qos !== undefined) device.settings = { ...(device.settings || {}), qos };
}

/** After a save: what was taken, or waits, becomes the form's baseline; what
 *  was refused goes back to the value the device holds. */
export function settleFields(form, data) {
    const failed = new Set(data.failed || []);
    for (const el of Array.from(form.elements || [])) {
        if (!el || !el.name || !el.dataset || el.dataset.initial === undefined) continue;
        if (failed.has(el.name)) el.value = el.dataset.initial;
        else el.dataset.initial = el.value;
    }
}

/** {name: verdict} from a save's answer: set | unconfirmed | queued | failed. */
export function verdicts(data) {
    const out = {};
    for (const n of Object.keys(data.applied || {})) out[n] = 'set';
    for (const n of data.unconfirmed || []) out[n] = 'unconfirmed';
    for (const n of data.queued || []) out[n] = 'queued';
    for (const n of data.failed || []) out[n] = 'failed';
    return out;
}
