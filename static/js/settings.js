/**
 * settings.js
 * Rich Settings Panel - Config / Security / Spectrum tabs
 * Replaces the raw YAML textarea with a structured form UI.
 */
const log = zmmLog('settings');


 // ============================================================================
 // IMPORTS
 // ============================================================================

 import { loadSSLStatus } from './system.js';
 import { createChart } from './chart-utils.js';
 import { confirmDialog } from './dialogs.js';

// ============================================================================
// STATE
// ============================================================================

let _spectrumChart = null;   // ECharts: live channel-energy bar chart
let _historyChart = null;    // ECharts: live spectrum box-plot
let _spectrumData = {};
let _currentConfig = {};

// ============================================================================
// INIT
// ============================================================================

export function initSettings() {
    const tab = document.querySelector('[data-bs-target="#settings"]');
    if (tab) {
        tab.addEventListener('shown.bs.tab', () => loadSettingsPanel());
    }
}

export async function loadSettingsPanel() {
    await loadStructuredConfig();
    await loadSSLStatus();
    renderBackupRestoreSection();
}

// ============================================================================
// STRUCTURED CONFIG LOAD / SAVE
// ============================================================================

async function loadStructuredConfig() {
    try {
        const res = await fetch('/api/config/structured');
        const data = await res.json();
        if (!data.success) {
            showSettingsAlert('danger', 'Failed to load config: ' + data.error);
            return;
        }
        _currentConfig = data.config;
        renderConfigTab(data.config);
        renderSecurityTab(data.config);
        renderApisTab(data.config);
    } catch (e) {
        showSettingsAlert('danger', 'Error loading config: ' + e.message);
    }
}


async function saveStructuredConfig(silent = false) {
    const config = collectFormValues();
    try {
        const res = await fetch('/api/config/structured', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });
        const data = await res.json();
        if (data.success) {
            if (!silent) showSettingsAlert('success', 'Configuration saved. Restart the service to apply.');
            return true;
        }
        showSettingsAlert('danger', 'Save failed: ' + data.error);
        return false;
    } catch (e) {
        showSettingsAlert('danger', 'Error saving: ' + e.message);
        return false;
    }
}

// ============================================================================
// CONFIG TAB RENDER
// ============================================================================

function renderConfigTab(config) {
    const z = config.zigbee || {};
    const m = config.mqtt || {};
    const w = config.web || {};
    const l = config.logging || {};
    const o = config.ota || {};
    const ha = config.homeassistant || {};

    const el = document.getElementById('configFormBody');
    if (!el) return;

    // Internal tabs — one pane per section instead of one long scroll.
    // All panes stay in the DOM, so Save (collectFormValues) sees every field.
    el.innerHTML = `
    <ul class="nav nav-tabs mb-3" role="tablist">
      <li class="nav-item">
        <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#cfgPaneZigbee" type="button">
          <i class="fas fa-broadcast-tower me-1"></i> Zigbee Radio
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneMqtt" type="button">
          <i class="fas fa-network-wired me-1"></i> MQTT
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneHa" type="button">
          <i class="fas fa-home me-1"></i> Home Assistant
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneWeb" type="button">
          <i class="fas fa-globe me-1"></i> Web
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneOta" type="button">
          <i class="fas fa-cloud-arrow-down me-1"></i> OTA
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneSsl" type="button">
          <i class="fas fa-lock me-1"></i> HTTPS / SSL
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#cfgPaneBackup" type="button">
          <i class="fas fa-database me-1"></i> Backup
        </button>
      </li>
    </ul>
    <div class="tab-content">

    <!-- ZIGBEE PANE -->
    <div class="tab-pane fade show active" id="cfgPaneZigbee">
    <div class="row g-3 mb-4">
      <div class="col-md-5">
        <label class="form-label small fw-semibold">Serial Port</label>
        <input type="text" class="form-control" id="cfg_port" value="${z.port || ''}" placeholder="/dev/ttyACM0">
        <div class="form-text">Serial path or socket URI (e.g. <code>socket://127.0.0.1:9999</code> for MultiPAN)</div>
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Radio Type</label>
        <select class="form-select" id="cfg_radio_type">
          ${['auto','ezsp','znp','deconz'].map(t =>
            `<option value="${t}" ${z.radio_type === t ? 'selected' : ''}>${t}</option>`
          ).join('')}
        </select>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Channel</label>
        <select class="form-select" id="cfg_channel">
          ${Array.from({length: 16}, (_, i) => i + 11).map(ch =>
            `<option value="${ch}" ${z.channel == ch ? 'selected' : ''}>${ch}</option>`
          ).join('')}
        </select>
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Topology Scan (s)</label>
        <input type="number" class="form-control" id="cfg_topology_scan_interval"
               value="${z.topology_scan_interval || 120}" min="0">
      </div>
    </div>
    </div>

    <!-- MQTT PANE -->
    <div class="tab-pane fade" id="cfgPaneMqtt">
    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="fw-semibold"><i class="fas fa-network-wired me-1"></i> MQTT Broker</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" id="cfg_mqtt_enabled"
               ${m.enabled !== false ? 'checked' : ''}>
        <label class="form-check-label small text-muted" for="cfg_mqtt_enabled">Enable</label>
      </div>
    </div>
    <p class="text-muted small mb-2">
      Publishes device state and accepts commands over MQTT. Disabling also turns off
      everything carried over MQTT — Home Assistant discovery, presence and zone
      publishing. Requires a service restart to apply.
    </p>
    <div class="row g-3 mb-4">
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Broker Host</label>
        <input type="text" class="form-control" id="cfg_mqtt_host" value="${m.broker_host || ''}">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Port</label>
        <input type="number" class="form-control" id="cfg_mqtt_port" value="${m.broker_port || 1883}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Username</label>
        <input type="text" class="form-control" id="cfg_mqtt_username" value="${m.username || ''}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Password</label>
        <input type="password" class="form-control" id="cfg_mqtt_password" value="${m.password || ''}"
               placeholder="(unchanged)">
      </div>
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Base Topic</label>
        <input type="text" class="form-control" id="cfg_mqtt_base_topic" value="${m.base_topic || 'zigbee_manager'}">
      </div>
    </div>
    </div>

    <!-- HOME ASSISTANT PANE -->
    <div class="tab-pane fade" id="cfgPaneHa">
    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="fw-semibold"><i class="fas fa-home me-1"></i> Home Assistant</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" id="cfg_ha_enabled"
               ${ha.enabled !== false ? 'checked' : ''}>
        <label class="form-check-label small text-muted" for="cfg_ha_enabled">Enable</label>
      </div>
    </div>
    <p class="text-muted small mb-2">
      Exposes devices, groups, zones and presence to Home Assistant via MQTT
      discovery (<code>homeassistant/…</code> topics) and listens for HA commands
      and its birth message. Needs MQTT enabled to do anything. When disabled,
      nothing is published to or read from the <code>homeassistant/</code> topics —
      plain state publishing on the base topic keeps working.
      Requires a service restart to apply.
    </p>
    ${m.enabled === false ? `
    <div class="alert alert-warning py-2 small mb-0">
      <i class="fas fa-exclamation-triangle me-1"></i>
      MQTT is currently disabled — Home Assistant integration is inactive
      regardless of this switch.
    </div>` : ''}
    </div>

    <!-- WEB PANE -->
    <div class="tab-pane fade" id="cfgPaneWeb">
    <div class="row g-3 mb-4">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Host</label>
        <input type="text" class="form-control" id="cfg_web_host" value="${w.host || '0.0.0.0'}">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Port</label>
        <input type="number" class="form-control" id="cfg_web_port" value="${w.port || 8000}">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Log Level</label>
        <select class="form-select" id="cfg_log_level">
          ${['DEBUG','INFO','WARNING','ERROR'].map(lv =>
            `<option value="${lv}" ${l.level === lv ? 'selected' : ''}>${lv}</option>`
          ).join('')}
        </select>
      </div>
    </div>
    </div>

    <!-- OTA PANE -->
    <div class="tab-pane fade" id="cfgPaneOta">
    <p class="text-muted small mb-2">
      Extra firmware providers consulted by zigpy when checking for updates.
      The built-in defaults (IKEA, LEDVANCE, Sonoff, Inovelli, etc.) are always
      active unless explicitly disabled. The local <code>./data/ota_firmware</code>
      directory is auto-attached at runtime — don't add it here.
    </p>
    <div class="row g-3 mb-3">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">OTA Enabled</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_ota_enabled"
                 ${o.enabled !== false ? 'checked' : ''}>
        </div>
      </div>
      <div class="col-md-9">
        <label class="form-label small fw-semibold">Disable Default Providers</label>
        <input type="text" class="form-control" id="cfg_ota_disable_defaults"
               value="${(o.disable_default_providers || []).join(', ')}"
               placeholder="comma-separated, e.g. ledvance, inovelli">
        <div class="form-text">Names of zigpy's built-in providers to suppress. Leave blank for none.</div>
      </div>
    </div>

    <div class="card border-secondary-subtle mb-2">
      <div class="card-header py-2 d-flex justify-content-between align-items-center bg-light">
        <span class="small fw-semibold">
          <i class="fas fa-list me-1"></i> Extra Providers
        </span>
        <button type="button" class="btn btn-outline-primary btn-sm" onclick="addOtaProviderRow()">
          <i class="fas fa-plus me-1"></i> Add Provider
        </button>
      </div>
      <div class="card-body p-2" id="otaProvidersBody">
        <!-- rows injected by renderOtaProviderRows() -->
      </div>
    </div>
    <div class="form-text mb-4">
      Common <code>type</code> values: <code>z2m</code> (Zigbee2MQTT remote index),
      <code>zigpy</code> (legacy provider name), <code>advanced</code>
      (local directory — point <code>path</code> at it), <code>salus</code>,
      <code>thirdreality</code>, <code>sonoff</code>. Each entry is passed
      verbatim to zigpy.
    </div>
    </div>

    <!-- SSL PANE -->
    <div class="tab-pane fade" id="cfgPaneSsl">
      <div class="d-flex align-items-center gap-3">
        <div class="form-check form-switch fs-5 mb-0">
          <input class="form-check-input" type="checkbox" id="sslToggle" onchange="toggleSSL(this.checked)">
          <label class="form-check-label" for="sslToggle">Enable HTTPS (self-signed cert)</label>
        </div>
        <small class="text-muted">Applies immediately on toggle — requires service restart to take effect</small>
      </div>
    </div>

    <!-- BACKUP PANE (filled by renderBackupRestoreSection) -->
    <div class="tab-pane fade" id="cfgPaneBackup">
      <div id="backupRestoreBody"></div>
    </div>

    </div>
    `;

    // Now that the OTA section markup is in the DOM, populate the rows.
    renderOtaProviderRows(o.extra_providers || []);
}

// ============================================================================
// OTA PROVIDER ROWS
// ============================================================================

// Known zigpy provider types. Free-text input is still allowed so future
// types in newer zigpy versions don't require a UI release.
const OTA_PROVIDER_TYPES = [
    'z2m',
    'zigpy',
    'advanced',
    'salus',
    'sonoff',
    'thirdreality',
    'inovelli',
    'ikea',
    'ledvance',
];

function renderOtaProviderRows(providers) {
    const body = document.getElementById('otaProvidersBody');
    if (!body) return;
    ensureOtaTypeDatalist();
    if (!providers || providers.length === 0) {
        body.innerHTML = `<div class="text-muted small fst-italic px-2 py-1">
            No extra providers configured. Click "Add Provider" to add one.
        </div>`;
        return;
    }
    body.innerHTML = providers.map((p, idx) => otaProviderRowHtml(p, idx)).join('');
}

function otaProviderRowHtml(p, idx) {
    const type = (p && p.type) || '';
    // Known top-level fields we render with dedicated inputs
    const known = new Set(['type', 'url', 'path']);
    const url = (p && p.url) || '';
    const path = (p && p.path) || '';
    // Anything else (warning, manuf_id, etc.) goes into the JSON extras box
    const extras = {};
    if (p && typeof p === 'object') {
        for (const [k, v] of Object.entries(p)) {
            if (!known.has(k)) extras[k] = v;
        }
    }
    const extrasJson = Object.keys(extras).length > 0
        ? JSON.stringify(extras, null, 0)
        : '';

    // Type input uses a datalist (#otaProviderTypeList) for autocomplete
    // while still allowing free text for forward-compat with new zigpy types.

    return `
    <div class="row g-2 align-items-start ota-provider-row mb-2 pb-2 border-bottom"
         data-ota-idx="${idx}">
      <div class="col-md-2">
        <label class="form-label x-small mb-0 fw-semibold">Type</label>
        <input type="text" class="form-control form-control-sm ota-prov-type"
               list="otaProviderTypeList"
               value="${escapeAttr(type)}"
               placeholder="z2m">
      </div>
      <div class="col-md-4">
        <label class="form-label x-small mb-0 fw-semibold">URL</label>
        <input type="text" class="form-control form-control-sm ota-prov-url"
               value="${escapeAttr(url)}"
               placeholder="(optional, for index-based providers)">
      </div>
      <div class="col-md-3">
        <label class="form-label x-small mb-0 fw-semibold">Path</label>
        <input type="text" class="form-control form-control-sm ota-prov-path"
               value="${escapeAttr(path)}"
               placeholder="(optional, for advanced provider)">
      </div>
      <div class="col-md-2">
        <label class="form-label x-small mb-0 fw-semibold">Extra (JSON)</label>
        <input type="text" class="form-control form-control-sm font-monospace ota-prov-extras"
               value="${escapeAttr(extrasJson)}"
               placeholder='{"manuf_id":4476}'>
      </div>
      <div class="col-md-1 d-flex align-items-end">
        <button type="button"
                class="btn btn-outline-danger btn-sm w-100"
                onclick="removeOtaProviderRow(${idx})"
                title="Remove this provider">
          <i class="fas fa-trash"></i>
        </button>
      </div>
    </div>
    `;
}

function escapeAttr(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

window.addOtaProviderRow = function() {
    const current = collectOtaProviderRows();
    current.push({ type: '' });
    renderOtaProviderRows(current);
    // Datalist injected on first call
    ensureOtaTypeDatalist();
};

window.removeOtaProviderRow = function(idx) {
    const current = collectOtaProviderRows();
    current.splice(idx, 1);
    renderOtaProviderRows(current);
};

function ensureOtaTypeDatalist() {
    if (document.getElementById('otaProviderTypeList')) return;
    const dl = document.createElement('datalist');
    dl.id = 'otaProviderTypeList';
    dl.innerHTML = OTA_PROVIDER_TYPES.map(t => `<option value="${t}">`).join('');
    document.body.appendChild(dl);
}

function collectOtaProviderRows() {
    const rows = document.querySelectorAll('#otaProvidersBody .ota-provider-row');
    const out = [];
    rows.forEach(row => {
        const type = row.querySelector('.ota-prov-type')?.value?.trim() || '';
        const url = row.querySelector('.ota-prov-url')?.value?.trim() || '';
        const path = row.querySelector('.ota-prov-path')?.value?.trim() || '';
        const extrasRaw = row.querySelector('.ota-prov-extras')?.value?.trim() || '';

        // Skip wholly-empty rows so a stray click on Add doesn't pollute the YAML
        if (!type && !url && !path && !extrasRaw) return;

        const entry = {};
        if (type) entry.type = type;
        if (url) entry.url = url;
        if (path) entry.path = path;

        if (extrasRaw) {
            try {
                const parsed = JSON.parse(extrasRaw);
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                    Object.assign(entry, parsed);
                }
            } catch (e) {
                // Surface the parse error inline rather than silently dropping
                log.warn('OTA provider extras JSON parse failed:', extrasRaw, e);
                row.querySelector('.ota-prov-extras')?.classList.add('is-invalid');
            }
        }
        out.push(entry);
    });
    return out;
}

// ============================================================================
// SECURITY TAB RENDER
// ============================================================================

function renderSecurityTab(config) {
    const z = config.zigbee || {};
    const el = document.getElementById('securityFormBody');
    if (!el) return;

    el.innerHTML = `
    <div class="alert alert-warning small mb-4">
      <i class="fas fa-exclamation-triangle me-1"></i>
      <strong>Warning:</strong> Changing PAN ID or Network Key will disconnect all paired devices.
      They will need to be re-paired.
    </div>

    <!-- PAN ID -->
    <div class="mb-4">
      <label class="form-label fw-semibold">PAN ID (16-bit hex)</label>
      <div class="input-group">
        <span class="input-group-text text-muted">0x</span>
        <input type="text" class="form-control font-monospace" id="cfg_pan_id"
               value="${z.pan_id || ''}" maxlength="4" placeholder="A1B2"
               pattern="[0-9A-Fa-f]{4}">
        <button class="btn btn-outline-secondary btn-sm" onclick="regenCredential('pan_id')"
                title="Generate random PAN ID">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">4-character hex. Must be unique on your 2.4GHz network.</div>
    </div>

    <!-- EXTENDED PAN ID -->
    <div class="mb-4">
      <label class="form-label fw-semibold">Extended PAN ID (64-bit)</label>
      <div class="input-group">
        <input type="text" class="form-control font-monospace" id="cfg_extended_pan_id"
               value="${z.extended_pan_id_hex || ''}" maxlength="16" placeholder="16 hex characters">
        <button class="btn btn-outline-secondary btn-sm" onclick="regenCredential('extended_pan_id')"
                title="Generate random Extended PAN ID">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">16-character hex (8 bytes). Stored as byte array in config.</div>
    </div>

    <!-- NETWORK KEY -->
    <div class="mb-4">
      <label class="form-label fw-semibold">Network Key (128-bit)</label>
      <div class="input-group">
        <input type="password" class="form-control font-monospace" id="cfg_network_key"
               value="${z.network_key_hex || ''}" maxlength="32"
               placeholder="32 hex characters (leave blank to keep current)">
        <button class="btn btn-outline-secondary btn-sm"
                onclick="document.getElementById('cfg_network_key').type === 'password'
                  ? (document.getElementById('cfg_network_key').type='text', this.innerHTML='<i class=\\'fas fa-eye-slash\\'></i>')
                  : (document.getElementById('cfg_network_key').type='password', this.innerHTML='<i class=\\'fas fa-eye\\'></i>')"
                title="Toggle visibility">
          <i class="fas fa-eye"></i>
        </button>
        <button class="btn btn-outline-danger btn-sm" onclick="regenCredential('network_key')"
                title="Generate new random network key">
          <i class="fas fa-sync-alt"></i> Regenerate
        </button>
      </div>
      <div class="form-text">32-character hex (16 bytes). Changing this requires re-pairing ALL devices.</div>
    </div>

    <div id="regenResult" class="mt-2"></div>
    `;
}


// ============================================================================
// WEATHER TAB RENDER
// ============================================================================

function renderApisTab(config) {
    const el = document.getElementById('apisFormBody');
    if (!el) return;

    // Internal tabs so each provider group is its own pane (no long scroll).
    el.innerHTML = `
    <ul class="nav nav-tabs mb-3" role="tablist">
      <li class="nav-item">
        <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#apiPaneWeather" type="button">
          <i class="fas fa-cloud-sun me-1"></i> Weather
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#apiPaneMedia" type="button">
          <i class="fas fa-volume-up me-1"></i> Media
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#apiPaneTidal" type="button">
          <i class="fas fa-music me-1"></i> Tidal
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#apiPaneAc" type="button">
          <i class="fas fa-snowflake me-1"></i> Air Con
        </button>
      </li>
      <li class="nav-item">
        <button class="nav-link" data-bs-toggle="tab" data-bs-target="#apiPaneSecurity" type="button">
          <i class="fas fa-shield-halved me-1"></i> Security
        </button>
      </li>
    </ul>
    <div class="tab-content">
      <div class="tab-pane fade show active" id="apiPaneWeather">${renderWeatherSection(config)}</div>
      <div class="tab-pane fade" id="apiPaneMedia">${renderMediaSection(config)}</div>
      <div class="tab-pane fade" id="apiPaneTidal">${renderTidalSection(config)}</div>
      <div class="tab-pane fade" id="apiPaneAc">${renderAcSection()}</div>
      <div class="tab-pane fade" id="apiPaneSecurity">${renderSecuritySection(config)}</div>
    </div>
    `;

    loadWeatherStatus();
    loadTidalStatus();
    loadAcUnits();
    SECURITY_PROVIDERS.forEach(p => p.onShow?.());
}

function renderWeatherSection(config) {
    const w = config.weather || {};
    return `
    <p class="text-muted small mb-3">
      Free weather API — no account or API key required.
      Provides outdoor temperature, humidity, wind and forecast for heating automation.
      <a href="https://open-meteo.com" target="_blank" rel="noopener" class="ms-1">open-meteo.com <i class="fas fa-external-link-alt fa-xs"></i></a>
    </p>
    <div class="row g-3 mb-4">
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Enabled</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_weather_enabled"
                 ${w.enabled ? 'checked' : ''}>
        </div>
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Latitude</label>
        <input type="number" step="0.0001" class="form-control" id="cfg_weather_lat"
               value="${w.latitude ?? ''}" placeholder="51.5074">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Longitude</label>
        <input type="number" step="0.0001" class="form-control" id="cfg_weather_lon"
               value="${w.longitude ?? ''}" placeholder="-0.1278">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Poll Interval (min)</label>
        <input type="number" class="form-control" id="cfg_weather_interval"
               value="${w.poll_interval_minutes ?? 30}" min="5">
      </div>
      <div class="col-md-2">
        <label class="form-label small fw-semibold">MQTT Publish</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_weather_mqtt"
                 ${w.mqtt_publish ? 'checked' : ''}>
          <label class="form-check-label small text-muted">HA sensors</label>
        </div>
      </div>
    </div>

    <div id="weatherStatusRow" class="mb-2"></div>
    <button class="btn btn-outline-secondary btn-sm" onclick="window.refreshWeatherNow()">
      <i class="fas fa-sync-alt me-1"></i> Fetch Now
    </button>
    `;
}

// ============================================================================
// MEDIA SECTION (Cast / WiiM / Radio) — lives in the External APIs tab
// ============================================================================

function renderMediaSection(config) {
    const m = config.media || {};
    const cast = m.cast || {};
    const wiim = m.wiim || {};
    const rb = m.radio_browser || {};
    const tts = m.tts || {};
    const devices = (wiim.devices || []).join('\n');
    return `
    <p class="text-muted small mb-3">
      Multi-room audio for Google Cast (Nest/Home) and WiiM players, plus internet radio.
      Self-contained — no Home Assistant required. Changes take effect after a service restart.
    </p>
    <div class="row g-3 mb-3">
      <div class="col-md-2">
        <label class="form-label small fw-semibold">Enabled</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_media_enabled" ${m.enabled ? 'checked' : ''}>
        </div>
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Poll Interval (s)</label>
        <input type="number" class="form-control" id="cfg_media_poll" value="${m.poll_interval_seconds ?? 10}" min="3">
      </div>
      <div class="col-md-5">
        <label class="form-label small fw-semibold">Adopt casting sessions</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_media_adopt" ${m.adopt_sessions !== false ? 'checked' : ''}>
          <label class="form-check-label small text-muted">Keep controlling devices after a reboot/upgrade</label>
        </div>
      </div>
    </div>

    <div class="row g-3 mb-3">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Google Cast</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_media_cast_enabled" ${cast.enabled !== false ? 'checked' : ''}>
          <label class="form-check-label small text-muted">Enable discovery</label>
        </div>
      </div>
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Cast App ID</label>
        <input type="text" class="form-control" id="cfg_media_cast_appid"
               value="${w_escape(cast.app_id || 'CC1AD845')}" placeholder="CC1AD845">
        <small class="text-muted">Default media receiver. Swap for a custom Web Receiver later.</small>
      </div>
      <div class="col-md-5">
        <label class="form-label small fw-semibold">Lyrics Receiver App ID</label>
        <input type="text" class="form-control" id="cfg_media_cast_lyrics_appid"
               value="${w_escape(cast.lyrics_app_id || '')}" placeholder="e.g. ABCD1234 (blank = off)">
        <small class="text-muted">Custom Cast receiver for album art + synced lyrics on screened
          devices (Nest Hub). Host <code>static/cast/receiver.html</code> on HTTPS, register it at
          cast.google.com/publish (one-time $5), put the App ID here. Karaoke on/off is a live
          toggle in the Media tab.
          <br><strong>No registration?</strong> Use the <em>Lyrics</em> button in the Media tab — it opens a
          full-screen page you Chrome-Cast-tab to the Hub for free (leave this blank).</small>
      </div>
    </div>

    <div class="row g-3 mb-3">
      <div class="col-md-3">
        <label class="form-label small fw-semibold">WiiM</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_media_wiim_enabled" ${wiim.enabled !== false ? 'checked' : ''}>
          <label class="form-check-label small text-muted">Enable</label>
        </div>
      </div>
      <div class="col-md-9">
        <label class="form-label small fw-semibold">WiiM Device IPs</label>
        <textarea class="form-control" id="cfg_media_wiim_devices" rows="3"
                  placeholder="One IP per line, e.g.&#10;192.168.1.50&#10;192.168.1.51">${w_escape(devices)}</textarea>
        <small class="text-muted">Manual list for now; mDNS auto-discovery comes later.</small>
      </div>
    </div>

    <div class="row g-3 mb-3">
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Radio-Browser</label>
        <div class="form-check form-switch mt-1">
          <input class="form-check-input" type="checkbox" id="cfg_media_rb_enabled" ${rb.enabled !== false ? 'checked' : ''}>
          <label class="form-check-label small text-muted">Enable station directory</label>
        </div>
      </div>
    </div>

    <hr class="my-3">
    <div class="fw-semibold mb-2"><i class="fas fa-bullhorn me-1"></i> Announcements (TTS)</div>
    <p class="text-muted small mb-2">Text-to-speech for automation announcements. Default is the
      keyless Google Translate endpoint, which returns a plain MP3 that Cast/WiiM play directly.</p>
    <div class="row g-3 mb-3">
      <div class="col-md-7">
        <label class="form-label small fw-semibold">TTS Base URL</label>
        <input type="text" class="form-control" id="cfg_media_tts_base"
               value="${w_escape(tts.base_url || 'https://translate.google.com/translate_tts')}"
               placeholder="https://translate.google.com/translate_tts">
      </div>
      <div class="col-md-3">
        <label class="form-label small fw-semibold">Language</label>
        <input type="text" class="form-control" id="cfg_media_tts_lang"
               value="${w_escape(tts.lang || 'en')}" placeholder="en">
      </div>
    </div>
    `;
}

// ============================================================================
// AIR CON SECTION (Gree / Midea local LAN) — lives in the External APIs tab
// ============================================================================
// Unlike the other panes, AC units are managed live through /api/ac/* and
// persist immediately — no Save / restart cycle.

function _acEsc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderAcSection() {
    return `
    <p class="text-muted small mb-3">
      Local-LAN air conditioning — <strong>Gree</strong> protocol (EcoAir) and
      <strong>Midea</strong> protocol (Comfee). Units are controlled directly on your
      network; changes apply immediately, no restart needed. Midea units need a one-time
      <em>Bind</em> to fetch their token/key (uses the library's anonymous preset
      account — after that everything stays local).
    </p>
    <div class="d-flex gap-2 mb-3">
      <button class="btn btn-primary btn-sm" id="acDiscoverBtn" onclick="window.acDiscover()">
        <i class="fas fa-search me-1"></i> Discover on LAN
      </button>
      <button class="btn btn-outline-secondary btn-sm" onclick="window.loadAcUnits()">
        <i class="fas fa-sync-alt me-1"></i> Refresh
      </button>
    </div>
    <div id="acDiscoverResults" class="mb-3"></div>
    <div id="acUnitsList">
      <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading units…</div>
    </div>
    `;
}

window.loadAcUnits = async function () {
    const el = document.getElementById('acUnitsList');
    if (!el) return;
    try {
        const res = await fetch('/api/ac/units').then(r => r.json());
        if (!res.success) throw new Error(res.error || 'failed');
        const units = res.units || [];
        if (!units.length) {
            el.innerHTML = `<div class="text-muted small fst-italic">
                No AC units configured yet — run <strong>Discover on LAN</strong> above.</div>`;
            return;
        }
        const rows = units.map(u => {
            const online = u.online;
            const status = online
                ? `<span class="badge bg-success">online</span>
                   <span class="ms-1 small">${u.power ? '⏻ on' : '⏻ off'}${u.mode ? ' · ' + _acEsc(u.mode) : ''}
                   ${u.current_c != null ? ' · ' + Number(u.current_c).toFixed(1) + '°C' : ''}
                   ${u.target_c != null ? ' → ' + Number(u.target_c).toFixed(1) + '°C' : ''}</span>`
                : `<span class="badge bg-secondary" title="${_acEsc(u.error || '')}">offline</span>
                   <span class="ms-1 small text-muted">${_acEsc((u.error || '').slice(0, 60))}</span>`;
            const controls = online ? `
                <button class="btn btn-sm btn-outline-${u.power ? 'danger' : 'success'} py-0"
                        onclick="window.acPower('${_acEsc(u.id)}', ${u.power ? 'false' : 'true'})">
                  ${u.power ? 'Off' : 'On'}</button>
                <select class="form-select form-select-sm d-inline-block py-0 ms-1" style="width:auto;font-size:0.78rem"
                        onchange="window.acSetMode('${_acEsc(u.id)}', this.value)">
                  ${['', 'auto', 'cool', 'dry', 'fan', 'heat'].map(m =>
                    `<option value="${m}" ${m === (u.mode || '') ? 'selected' : ''}>${m || 'mode…'}</option>`).join('')}
                </select>
                <input type="number" step="0.5"
                       min="${u.capabilities?.min_c ?? 16}" max="${u.capabilities?.max_c ?? 31}"
                       class="form-control form-control-sm d-inline-block ms-1 py-0"
                       style="width:70px;font-size:0.78rem" value="${u.target_c ?? 22}"
                       id="acTemp_${_acEsc(u.id)}">
                <button class="btn btn-sm btn-outline-primary py-0 ms-1"
                        onclick="window.acSetTemp('${_acEsc(u.id)}')">Set</button>
                <button class="btn btn-sm btn-outline-secondary py-0 ms-1" title="All controls (fan, swing, timer…)"
                        onclick="window.openAcModal('${_acEsc(u.id)}')">
                  <i class="fas fa-sliders-h"></i></button>` : `
                <button class="btn btn-sm btn-outline-warning py-0"
                        onclick="window.acBind('${_acEsc(u.id)}')" title="Fetch key/token and reconnect">
                  <i class="fas fa-key me-1"></i>Bind</button>`;
            return `
              <tr>
                <td class="fw-semibold">${_acEsc(u.name || u.id)}
                    <div class="small text-muted">${_acEsc(u.brand)}${u.room_id ? ' · room: ' + _acEsc(u.room_id) : ''}</div></td>
                <td>${status}</td>
                <td class="text-nowrap">${controls}</td>
                <td class="text-end">
                  ${online ? `<button class="btn btn-sm btn-link text-warning py-0" title="Re-bind"
                        onclick="window.acBind('${_acEsc(u.id)}')"><i class="fas fa-key"></i></button>` : ''}
                  <button class="btn btn-sm btn-link text-danger py-0" title="Remove"
                        onclick="window.acDelete('${_acEsc(u.id)}')"><i class="fas fa-trash"></i></button>
                </td>
              </tr>`;
        }).join('');
        el.innerHTML = `
          <table class="table table-sm align-middle mb-0">
            <thead><tr class="small text-muted">
              <th>Unit</th><th>Status</th><th>Control</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
    } catch (e) {
        el.innerHTML = `<div class="text-danger small">Failed to load AC units: ${_acEsc(e.message)}</div>`;
    }
};

window.acDiscover = async function () {
    const btn = document.getElementById('acDiscoverBtn');
    const out = document.getElementById('acDiscoverResults');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Scanning…'; }
    try {
        const res = await fetch('/api/ac/discover', { method: 'POST' }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'discovery failed');
        const f = res.found || {};
        const cands = [...(f.gree || []), ...(f.midea || [])];
        const errs = [...(f.gree_error || []).map(e => 'Gree: ' + e),
                      ...(f.midea_error || []).map(e => 'Midea: ' + e)];
        if (!cands.length) {
            out.innerHTML = `<div class="alert alert-warning py-2 small mb-0">
                No units answered the scan. Check they're powered on and on this network.
                ${errs.length ? '<br>' + _acEsc(errs.join(' · ')) : ''}</div>`;
            return;
        }
        out.innerHTML = `
          <div class="border rounded p-2">
            <div class="small fw-semibold mb-1">Found on the network:</div>
            ${cands.map((c, i) => `
              <div class="d-flex align-items-center gap-2 py-1 small">
                <span class="badge bg-${c.brand === 'gree' ? 'info' : 'primary'}">${_acEsc(c.brand)}</span>
                <span class="font-monospace">${_acEsc(c.host)}</span>
                <span class="text-muted">${_acEsc(c.name || c.model || c.sn || '')}</span>
                ${c.already_configured
                    ? '<span class="badge bg-secondary">configured</span>'
                    : `<button class="btn btn-sm btn-outline-success py-0 ms-auto"
                         onclick='window.acAddCandidate(${JSON.stringify(JSON.stringify(c))})'>
                         <i class="fas fa-plus me-1"></i>Add</button>`}
              </div>`).join('')}
          </div>`;
    } catch (e) {
        out.innerHTML = `<div class="text-danger small">${_acEsc(e.message)}</div>`;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-search me-1"></i> Discover on LAN'; }
    }
};

window.acAddCandidate = async function (candJson) {
    try {
        const c = JSON.parse(candJson);
        const suggested = c.name || (c.brand === 'gree' ? 'EcoAir' : 'Comfee');
        const name = window.zbmPrompt
            ? await window.zbmPrompt({ title: 'Add AC unit', label: 'Name for this unit:',
                                       value: suggested, confirmText: 'Add' })
            : prompt('Name for this unit:', suggested);
        if (name === null) return;
        const body = {
            name: name || c.brand, brand: c.brand, host: c.host, port: c.port,
            mac: c.mac, device_id: c.device_id, protocol: c.protocol,
            model: c.model,
        };
        const res = await fetch('/api/ac/units', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'add failed');
        window.toast?.success?.('AC added', `${name} saved${c.brand === 'midea' ? ' — now click Bind to fetch its key' : ''}`);
        document.getElementById('acDiscoverResults').innerHTML = '';
        await window.loadAcUnits();
        if (c.brand === 'midea' && res.unit?.id) await window.acBind(res.unit.id);
    } catch (e) {
        window.toast?.error?.('Add failed', e.message);
    }
};

window.acBind = async function (unitId) {
    try {
        window.toast?.info?.('Binding…', 'Fetching device key — a few seconds.');
        const res = await fetch(`/api/ac/units/${encodeURIComponent(unitId)}/bind`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
            .then(r => r.json());
        if (!res.success) throw new Error(res.error || 'bind failed');
        window.toast?.success?.('Bound', 'Unit is ready.');
    } catch (e) {
        window.toast?.error?.('Bind failed', e.message);
    }
    await window.loadAcUnits();
};

window.acDelete = async function (unitId) {
    const ok = window.zbmConfirm
        ? await window.zbmConfirm({ title: 'Remove AC unit',
                                    message: `Remove AC unit "${unitId}"?`,
                                    confirmText: 'Remove', variant: 'danger' })
        : confirm(`Remove AC unit "${unitId}"?`);
    if (!ok) return;
    try {
        const res = await fetch(`/api/ac/units/${encodeURIComponent(unitId)}`,
            { method: 'DELETE' }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'delete failed');
    } catch (e) {
        window.toast?.error?.('Remove failed', e.message);
    }
    await window.loadAcUnits();
};

async function _acControl(unitId, changes) {
    try {
        const res = await fetch(`/api/ac/units/${encodeURIComponent(unitId)}/control`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes),
        }).then(r => r.json());
        if (!res.success) throw new Error(res.error || 'control failed');
    } catch (e) {
        window.toast?.error?.('AC control failed', e.message);
    }
    await window.loadAcUnits();
}

window.acPower = (id, on) => _acControl(id, { power: on === true || on === 'true' });
window.acSetMode = (id, mode) => mode ? _acControl(id, { mode }) : null;
window.acSetTemp = (id) => {
    const inp = document.getElementById(`acTemp_${id}`);
    const v = parseFloat(inp?.value);
    if (Number.isFinite(v)) return _acControl(id, { target_c: v });
};

// ============================================================================
// SECURITY SECTION (smart locks) — lives in the External APIs tab
// ============================================================================
// Providers are registry-driven: to add one (e.g. Yale) append an entry
// here with its own render function — the sub-tabs build themselves.
// Config fields save through the normal Save button (collectFormValues);
// locks are read/controlled live through /api/security/*.

const SECURITY_PROVIDERS = [
    {
        id: 'nuki',
        label: 'Nuki',
        icon: 'fa-lock',
        render: (config) => renderNukiSection(config),
        onShow: () => { loadNukiStatus(); window.loadNukiLocks(); },
    },
];

// fetch → JSON with a readable failure when the server answers non-JSON
// (e.g. a 500 error page), instead of "Unexpected token ... is not valid JSON".
async function _secFetch(url, opts) {
    const r = await fetch(url, opts);
    try {
        return await r.json();
    } catch {
        throw new Error(`HTTP ${r.status} ${r.statusText || ''} from ${url}`.trim());
    }
}

function renderSecuritySection(config) {
    const tabs = SECURITY_PROVIDERS.map((p, i) => `
      <li class="nav-item">
        <button class="nav-link ${i === 0 ? 'active' : ''}" data-bs-toggle="tab"
                data-bs-target="#secPane_${_acEsc(p.id)}" type="button">
          <i class="fas ${_acEsc(p.icon)} me-1"></i> ${_acEsc(p.label)}
        </button>
      </li>`).join('');
    const panes = SECURITY_PROVIDERS.map((p, i) => `
      <div class="tab-pane fade ${i === 0 ? 'show active' : ''}" id="secPane_${_acEsc(p.id)}">
        ${p.render(config)}
      </div>`).join('');
    return `
    <ul class="nav nav-pills mb-3" role="tablist">${tabs}</ul>
    <div class="tab-content">${panes}</div>
    `;
}

// ── Nuki ────────────────────────────────────────────────────────────────

function renderNukiSection(config) {
    const nuki = (config.security || {}).nuki || {};
    const bridge = nuki.bridge || {};
    const matter = nuki.matter || {};
    return `
    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="fw-semibold"><i class="fas fa-lock me-1"></i> Nuki Smart Lock</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" id="cfg_nuki_enabled" ${nuki.enabled ? 'checked' : ''}>
        <label class="form-check-label small text-muted">Enable</label>
      </div>
    </div>
    <p class="text-muted small mb-3">
      Two ways in: locks paired to a <strong>Nuki Bridge</strong> are controlled over its
      local HTTP API; bridge-less locks (Smart Lock 3.0 Pro / 4th gen) are commissioned as
      <strong>Matter</strong> devices via the Matter tab and picked up here automatically.
      Config changes need Save; lock control below is live.
    </p>

    <div class="row g-3 mb-3">
      <div class="col-lg-7">
        <div class="card h-100">
          <div class="card-header py-2 d-flex justify-content-between align-items-center">
            <span class="small fw-bold"><i class="fas fa-tower-broadcast me-1"></i> Bridge (local HTTP API)</span>
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" id="cfg_nuki_bridge_enabled"
                     ${bridge.enabled !== false ? 'checked' : ''}>
            </div>
          </div>
          <div class="card-body">
            <div class="row g-2 mb-2">
              <div class="col-md-6">
                <label class="form-label small fw-semibold">Bridge IP</label>
                <input type="text" class="form-control form-control-sm" id="cfg_nuki_bridge_host"
                       value="${_acEsc(bridge.host || '')}" placeholder="192.168.1.50">
              </div>
              <div class="col-md-2">
                <label class="form-label small fw-semibold">Port</label>
                <input type="number" class="form-control form-control-sm" id="cfg_nuki_bridge_port"
                       value="${bridge.port ?? 8080}">
              </div>
              <div class="col-md-4">
                <label class="form-label small fw-semibold">API Token</label>
                <input type="password" class="form-control form-control-sm" id="cfg_nuki_bridge_token"
                       value="" placeholder="${bridge.token ? '•••••• (saved — blank keeps it)' : 'token'}"
                       autocomplete="new-password">
              </div>
            </div>
            <div class="form-check form-switch mb-2">
              <input class="form-check-input" type="checkbox" id="cfg_nuki_bridge_hashed"
                     ${bridge.hashed_token !== false ? 'checked' : ''}>
              <label class="form-check-label small text-muted">
                Hashed token (recommended — plain sends the token unencrypted on the LAN)</label>
            </div>
            <div class="d-flex gap-2 flex-wrap">
              <button class="btn btn-primary btn-sm" id="nukiDiscoverBtn" onclick="window.nukiDiscoverBridges()">
                <i class="fas fa-search me-1"></i> Discover Bridges
              </button>
              <button class="btn btn-outline-primary btn-sm" onclick="window.nukiBridgeAuth()"
                      title="Press the button on the bridge, then click within 30 seconds">
                <i class="fas fa-key me-1"></i> Get Token
              </button>
              <button class="btn btn-outline-secondary btn-sm" onclick="window.nukiTestBridge()">
                <i class="fas fa-stethoscope me-1"></i> Test
              </button>
            </div>
            <div id="nukiBridgeResults" class="small mt-2"></div>
          </div>
        </div>
      </div>
      <div class="col-lg-5">
        <div class="card h-100">
          <div class="card-header py-2 d-flex justify-content-between align-items-center">
            <span class="small fw-bold"><i class="fas fa-atom me-1"></i> Matter (no bridge)</span>
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" id="cfg_nuki_matter_enabled"
                     ${matter.enabled !== false ? 'checked' : ''}>
            </div>
          </div>
          <div class="card-body small text-muted">
            Enable Matter in the Nuki app (Features &amp; Configuration → Matter), then commission
            the lock from this app's <strong>Matter</strong> tab with the pairing code.
            Commissioned locks appear in the list below with a
            <span class="badge bg-info">matter</span> badge — no credentials needed here.
            <div id="nukiMatterStatus" class="mt-2"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="fw-semibold small"><i class="fas fa-list me-1"></i> Locks</span>
      <button class="btn btn-outline-secondary btn-sm" onclick="window.loadNukiLocks()">
        <i class="fas fa-sync-alt me-1"></i> Refresh
      </button>
    </div>
    <div id="nukiLocksList">
      <div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Loading locks…</div>
    </div>
    `;
}

async function loadNukiStatus() {
    const el = document.getElementById('nukiMatterStatus');
    if (!el) return;
    try {
        const res = await _secFetch('/api/security/nuki/status');
        const m = res.matter;
        if (!m) { el.innerHTML = ''; return; }
        el.innerHTML = m.ok
            ? `<span class="badge bg-success">Matter server connected</span>
               <span class="ms-1">${m.locks} lock${m.locks === 1 ? '' : 's'} found</span>`
            : `<span class="badge bg-secondary">Matter server not running</span>`;
    } catch { el.innerHTML = ''; }
}

window.loadNukiLocks = async function () {
    const el = document.getElementById('nukiLocksList');
    if (!el) return;
    try {
        const res = await _secFetch('/api/security/nuki/locks');
        if (!res.success) throw new Error(res.error || 'failed');
        if (res.enabled === false) {
            el.innerHTML = `<div class="text-muted small fst-italic">
                Nuki is disabled — switch it on above and Save.</div>`;
            return;
        }
        const locks = res.locks || [];
        const errs = (res.errors || []).map(e =>
            `<div class="text-warning small mb-1"><i class="fas fa-triangle-exclamation me-1"></i>${_acEsc(e)}</div>`).join('');
        if (!locks.length) {
            el.innerHTML = errs + `<div class="text-muted small fst-italic">
                No locks found yet — configure the bridge above, or commission a
                Matter lock from the Matter tab.</div>`;
            return;
        }
        const stateBadge = (l) => {
            const s = (l.state_name || 'unknown').toLowerCase();
            const cls = s === 'locked' ? 'bg-success'
                : s.includes('unlock') || s.includes('unlatch') ? 'bg-warning text-dark'
                : s === 'motor blocked' ? 'bg-danger' : 'bg-secondary';
            return `<span class="badge ${cls}">${_acEsc(l.state_name || 'unknown')}</span>`;
        };
        const rows = locks.map(l => `
          <tr>
            <td class="fw-semibold">${_acEsc(l.name)}
              <div class="small text-muted">
                ${_acEsc(l.device_type_name || l.model || '')}
                ${l.firmware ? ' · fw ' + _acEsc(l.firmware) : ''}
              </div>
            </td>
            <td><span class="badge ${l.via === 'bridge' ? 'bg-primary' : 'bg-info'}">${_acEsc(l.via)}</span></td>
            <td>${stateBadge(l)}
                ${l.battery_critical ? '<span class="badge bg-danger ms-1">battery!</span>' : ''}
                ${l.battery != null ? `<span class="small text-muted ms-1">🔋 ${l.battery}%</span>` : ''}
                ${l.door_state ? `<span class="small text-muted ms-1">door: ${_acEsc(l.door_state)}</span>` : ''}
            </td>
            <td class="text-nowrap text-end">
              <button class="btn btn-sm btn-outline-success py-0" ${l.available === false ? 'disabled' : ''}
                      onclick="window.nukiAction('${_acEsc(l.id)}', 'lock')">
                <i class="fas fa-lock me-1"></i>Lock</button>
              <button class="btn btn-sm btn-outline-warning py-0 ms-1" ${l.available === false ? 'disabled' : ''}
                      onclick="window.nukiAction('${_acEsc(l.id)}', 'unlock')">
                <i class="fas fa-lock-open me-1"></i>Unlock</button>
              <button class="btn btn-sm btn-outline-danger py-0 ms-1" ${l.available === false ? 'disabled' : ''}
                      title="Also pulls the latch — the door swings open"
                      onclick="window.nukiAction('${_acEsc(l.id)}', 'unlatch')">
                <i class="fas fa-door-open me-1"></i>Unlatch</button>
            </td>
          </tr>`).join('');
        el.innerHTML = errs + `
          <table class="table table-sm align-middle mb-0">
            <thead><tr class="small text-muted">
              <th>Lock</th><th>Via</th><th>Status</th><th class="text-end">Control</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
    } catch (e) {
        el.innerHTML = `<div class="text-danger small">Failed to load locks: ${_acEsc(e.message)}</div>`;
    }
};

window.nukiAction = async function (lockId, action) {
    try {
        const res = await _secFetch(`/api/security/nuki/locks/${encodeURIComponent(lockId)}/action`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
        });
        if (!res.success) throw new Error(res.error || `${action} failed`);
        window.toast?.success?.('Nuki', `${action} sent`);
    } catch (e) {
        window.toast?.error?.('Nuki action failed', e.message);
    }
    await window.loadNukiLocks();
};

window.nukiDiscoverBridges = async function () {
    const btn = document.getElementById('nukiDiscoverBtn');
    const out = document.getElementById('nukiBridgeResults');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Searching…'; }
    try {
        const res = await _secFetch('/api/security/nuki/bridge/discover', { method: 'POST' });
        if (!res.success) throw new Error(res.error || 'discovery failed');
        const bridges = res.bridges || [];
        if (!bridges.length) {
            out.innerHTML = `<div class="alert alert-warning py-2 small mb-0">
                No bridges reported for this network — the bridge must be online
                and have phoned home to the Nuki cloud recently.</div>`;
            return;
        }
        out.innerHTML = bridges.map(b => `
          <div class="d-flex align-items-center gap-2 py-1">
            <span class="badge bg-primary">bridge</span>
            <span class="font-monospace">${_acEsc(b.ip)}:${_acEsc(b.port)}</span>
            <span class="text-muted">id ${_acEsc(b.bridgeId)}</span>
            <button class="btn btn-sm btn-outline-success py-0 ms-auto"
                    onclick="document.getElementById('cfg_nuki_bridge_host').value='${_acEsc(b.ip)}';
                             document.getElementById('cfg_nuki_bridge_port').value='${_acEsc(b.port)}';">
              Use</button>
          </div>`).join('');
    } catch (e) {
        out.innerHTML = `<div class="text-danger">${_acEsc(e.message)}</div>`;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-search me-1"></i> Discover Bridges'; }
    }
};

window.nukiBridgeAuth = async function () {
    const out = document.getElementById('nukiBridgeResults');
    out.innerHTML = `<div class="text-muted"><i class="fas fa-spinner fa-spin me-1"></i>
        Waiting for the bridge — press its button if you haven't…</div>`;
    try {
        const res = await _secFetch('/api/security/nuki/bridge/auth', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                host: document.getElementById('cfg_nuki_bridge_host')?.value?.trim(),
                port: Number(document.getElementById('cfg_nuki_bridge_port')?.value) || 8080,
            }),
        });
        if (!res.success) throw new Error(res.error || 'auth failed');
        document.getElementById('cfg_nuki_bridge_token').value = res.token;
        out.innerHTML = `<div class="text-success"><i class="fas fa-check me-1"></i>
            Token received — click Save to store it.</div>`;
    } catch (e) {
        out.innerHTML = `<div class="text-danger">${_acEsc(e.message)}</div>`;
    }
};

window.nukiTestBridge = async function () {
    const out = document.getElementById('nukiBridgeResults');
    out.innerHTML = `<div class="text-muted"><i class="fas fa-spinner fa-spin me-1"></i> Testing…</div>`;
    try {
        const res = await _secFetch('/api/security/nuki/status');
        const b = res.bridge;
        if (!b) { out.innerHTML = `<div class="text-muted">Bridge channel disabled.</div>`; return; }
        out.innerHTML = b.ok
            ? `<div class="text-success"><i class="fas fa-check me-1"></i>
                 Bridge OK — firmware ${_acEsc(b.firmware || '?')}${b.server_connected ? ', cloud-connected' : ''}.
                 <span class="text-muted">Uses the saved token — Save first after changes.</span></div>`
            : `<div class="text-danger"><i class="fas fa-xmark me-1"></i> ${_acEsc(b.error || 'unreachable')}</div>`;
    } catch (e) {
        out.innerHTML = `<div class="text-danger">${_acEsc(e.message)}</div>`;
    }
};

function renderTidalSection(config) {
    const tidal = (config.media || {}).tidal || {};
    return `
    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="fw-semibold"><i class="fas fa-music me-1"></i> Tidal</span>
      <div class="form-check form-switch mb-0">
        <input class="form-check-input" type="checkbox" id="cfg_media_tidal_enabled" ${tidal.enabled ? 'checked' : ''}>
        <label class="form-check-label small text-muted">Enable</label>
      </div>
    </div>
    <p class="text-muted small mb-2">
      Stream your Tidal HiFi library. Uses an unofficial client — personal use only.
      Enable, save &amp; restart, then log in.
    </p>
    <div class="row g-3 mb-3">
      <div class="col-md-4">
        <label class="form-label small fw-semibold">Quality</label>
        <select class="form-select" id="cfg_media_tidal_quality">
          <option value="high" ${(tidal.quality || 'high') === 'high' ? 'selected' : ''}>High (320k AAC — Cast + WiiM)</option>
          <option value="lossless" ${tidal.quality === 'lossless' ? 'selected' : ''}>Lossless (FLAC/DASH — Cast only)</option>
        </select>
        <small class="text-muted">Lossless needs the Manifest URL below; WiiM auto-falls back to AAC.</small>
      </div>
      <div class="col-md-8">
        <label class="form-label small fw-semibold">Manifest Base URL</label>
        <input type="text" class="form-control" id="cfg_media_tidal_manifest"
               value="${w_escape(tidal.manifest_base_url || '')}" placeholder="https://192.168.1.1:8000">
        <small class="text-muted">Public URL of THIS app, reachable by Cast on the LAN (needs valid
          TLS or http). Required for lossless so Cast can fetch the DASH manifest.</small>
      </div>
    </div>
    <div id="tidalStatusRow" class="mb-2"></div>
    <div class="d-flex gap-2">
      <button type="button" class="btn btn-outline-primary btn-sm" onclick="window.tidalLogin()">
        <i class="fas fa-right-to-bracket me-1"></i> Log in to Tidal
      </button>
      <button type="button" class="btn btn-outline-danger btn-sm" onclick="window.tidalLogout()">
        <i class="fas fa-right-from-bracket me-1"></i> Log out
      </button>
    </div>
    <div id="tidalLoginLink" class="mt-2"></div>
    `;
}

// Small HTML-attribute escaper (settings.js has no shared esc helper).
function w_escape(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

// ---- Tidal login (lives in the Media settings section) ----
window.tidalLogin = async function () {
    const link = document.getElementById('tidalLoginLink');
    if (link) link.innerHTML = '<span class="small text-muted"><i class="fas fa-spinner fa-spin"></i> Requesting link…</span>';
    try {
        const res = await fetch('/api/media/tidal/login', { method: 'POST' });
        const data = await res.json();
        if (!data.success) {
            if (link) link.innerHTML = `<div class="alert alert-warning small py-2 mb-0">${w_escape(data.error || 'Login failed')}</div>`;
            return;
        }
        const url = data.link;
        if (link) link.innerHTML = `
          <div class="alert alert-info small py-2 mb-0">
            Open this link and authorise, then come back:
            <a href="${w_escape(url)}" target="_blank" rel="noopener" class="d-block mt-1">${w_escape(url)}</a>
          </div>`;
        pollTidalStatus(40);
    } catch (e) {
        if (link) link.innerHTML = `<div class="alert alert-danger small py-2 mb-0">${w_escape(e.message)}</div>`;
    }
};

window.tidalLogout = async function () {
    await fetch('/api/media/tidal/logout', { method: 'POST' });
    document.getElementById('tidalLoginLink').innerHTML = '';
    loadTidalStatus();
};

async function pollTidalStatus(tries) {
    for (let i = 0; i < tries; i++) {
        await new Promise(r => setTimeout(r, 3000));
        const state = await loadTidalStatus();
        if (state === 'logged_in') {
            document.getElementById('tidalLoginLink').innerHTML =
                '<div class="alert alert-success small py-2 mb-0">Logged in to Tidal.</div>';
            return;
        }
    }
}

async function loadTidalStatus() {
    const row = document.getElementById('tidalStatusRow');
    try {
        const res = await fetch('/api/media/tidal/status');
        const data = await res.json();
        const st = data.status || { state: 'unavailable' };
        const badge = {
            logged_in: `<span class="badge bg-success">Logged in${st.user ? ' · ' + w_escape(st.user) : ''}</span>`,
            logged_out: '<span class="badge bg-secondary">Logged out</span>',
            pending: '<span class="badge bg-warning text-dark">Login pending…</span>',
            unavailable: '<span class="badge bg-light text-muted">Unavailable (enable + restart)</span>',
        }[st.state] || '<span class="badge bg-light text-muted">Unknown</span>';
        if (row) row.innerHTML = `Status: ${badge}`;
        return st.state;
    } catch (e) {
        if (row) row.innerHTML = '<span class="text-muted small">Status unavailable</span>';
        return 'unavailable';
    }
}

async function loadWeatherStatus() {
    const el = document.getElementById('weatherStatusRow');
    if (!el) return;
    try {
        const res = await fetch('/api/weather/current');
        const data = await res.json();
        if (data.success) {
            const d = data.data;
            el.innerHTML = `
              <div class="alert alert-info py-2 small mb-2">
                <i class="fas fa-thermometer-half me-1"></i>
                <strong>${d.temperature_2m}°C</strong> &nbsp;·&nbsp;
                ${d.weather_description} &nbsp;·&nbsp;
                Humidity ${d.relative_humidity_2m}% &nbsp;·&nbsp;
                Wind ${d.wind_speed_10m} mph &nbsp;·&nbsp;
                Feels like ${d.apparent_temperature}°C
              </div>`;
        } else {
            el.innerHTML = `<div class="text-muted small">${data.error}</div>`;
        }
    } catch (_) {
        el.innerHTML = `<div class="text-muted small">Weather status unavailable</div>`;
    }
}

window.refreshWeatherNow = async function() {
    const el = document.getElementById('weatherStatusRow');
    if (el) el.innerHTML = '<span class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i> Fetching…</span>';
    try {
        await fetch('/api/weather/refresh', { method: 'POST' });
        await loadWeatherStatus();
    } catch (e) {
        if (el) el.innerHTML = `<span class="text-danger small">Error: ${e.message}</span>`;
    }
};

// ============================================================================
// CREDENTIAL REGENERATION
// ============================================================================

window.regenCredential = async function(type) {
    const labels = {
        pan_id: 'PAN ID',
        extended_pan_id: 'Extended PAN ID',
        network_key: 'Network Key'
    };
    const warn = type === 'network_key'
        ? '\n\nWARNING: All devices will need to be re-paired!'
        : '';

    if (!await confirmDialog({
        title: 'Regenerate credentials',
        message: `Regenerate ${labels[type]}?`,
        detail: warn.trim() || undefined,
        confirmText: 'Regenerate',
        variant: 'danger'
    })) return;

    try {
        const res = await fetch('/api/zigbee/credentials/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [type]: true })
        });
        const data = await res.json();
        if (data.success) {
            const r = data.regenerated;
            if (type === 'pan_id' && r.pan_id) {
                document.getElementById('cfg_pan_id').value = r.pan_id;
            }
            if (type === 'extended_pan_id' && r.extended_pan_id_hex) {
                document.getElementById('cfg_extended_pan_id').value = r.extended_pan_id_hex;
            }
            if (type === 'network_key' && r.network_key_hex) {
                document.getElementById('cfg_network_key').value = r.network_key_hex;
                document.getElementById('cfg_network_key').type = 'text';
            }
            document.getElementById('regenResult').innerHTML =
                `<div class="alert alert-success small">${labels[type]} regenerated and saved. Restart to apply.</div>`;
        } else {
            document.getElementById('regenResult').innerHTML =
                `<div class="alert alert-danger small">Error: ${data.error}</div>`;
        }
    } catch (e) {
        document.getElementById('regenResult').innerHTML =
            `<div class="alert alert-danger small">Request failed: ${e.message}</div>`;
    }
};

// ============================================================================
// SPECTRUM ANALYSIS
// ============================================================================

export async function runSpectrumScan() {
    const btn = document.getElementById('spectrumScanBtn');
    const statusEl = document.getElementById('spectrumStatus');
    const autoBtn = document.getElementById('autoChannelBtn');

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Scanning...'; }
    if (statusEl) statusEl.textContent = 'Running energy scan across channels 11-26...';
    if (autoBtn) autoBtn.disabled = true;

    try {
        const res = await fetch('/api/zigbee/spectrum');
        const data = await res.json();

        if (!data.success) {
            if (statusEl) statusEl.textContent = 'Scan failed: ' + data.error;
            return;
        }

        _spectrumData = data.spectrum;
        renderSpectrumChart(data);

        if (statusEl) {
            statusEl.textContent =
                `Scan complete. Best channel: ${data.best_channel}` +
                (data.current_channel ? ` (current: ${data.current_channel})` : '');
        }

        if (autoBtn) autoBtn.disabled = false;

    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-satellite-dish me-1"></i> Scan Spectrum'; }
    }
}

function renderSpectrumChart(data) {
    const container = document.getElementById('spectrumChart');
    if (!container) return;

    const channels = Object.keys(data.spectrum).map(Number).sort((a,b) => a-b);
    const energies = channels.map(ch => data.spectrum[ch]);
    const best = data.best_channel;
    const current = data.current_channel;

    // Bar colour: best=blue, current=purple, else green/amber/red by energy.
    const barColor = (ch) => {
        if (ch === best) return '#0d6efd';
        if (ch === current) return '#6f42c1';
        const e = data.spectrum[ch];
        if (e < 80) return '#198754';
        if (e < 150) return '#ffc107';
        return '#dc3545';
    };

    // Wi-Fi overlap annotations (typical) — shown above each bar.
    const wifiOverlap = {
        11: '1,2',          12: '1,2,3',        13: '1,2,3,4',      14: '1,2,3,4,5',
        15: '2,3,4,5,6',    16: '3,4,5,6,7',    17: '4,5,6,7,8',    18: '5,6,7,8,9',
        19: '6,7,8,9,10',   20: '7,8,9,10,11',  21: '8,9,10,11,12', 22: '9,10,11,12,13',
        23: '10,11,12,13',  24: '11,12,13,14',  25: '12,13,14',     26: '13,14'
    };

    if (_spectrumChart) { _spectrumChart.dispose(); _spectrumChart = null; }
    container.innerHTML = `
      <div id="spectrumChartCanvas" style="height:260px"></div>
      <div class="mt-2 d-flex gap-3 flex-wrap small">
        <span><span style="display:inline-block;width:12px;height:12px;background:#198754;border-radius:2px;"></span> Low interference</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#ffc107;border-radius:2px;"></span> Medium</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#dc3545;border-radius:2px;"></span> High interference</span>
        <span><span style="display:inline-block;width:12px;height:12px;background:#0d6efd;border-radius:2px;"></span> Best ★</span>
        ${current ? `<span><span style="display:inline-block;width:12px;height:12px;background:#6f42c1;border-radius:2px;"></span> Current ●</span>` : ''}
      </div>`;
    _spectrumChart = createChart(document.getElementById('spectrumChartCanvas'));

    _spectrumChart.setOption({
        grid: { top: 28, right: 16, bottom: 40, left: 44 },
        tooltip: {
            trigger: 'axis',
            formatter: (params) => {
                const p = params[0];
                const ch = channels[p.dataIndex];
                const tag = ch === best ? ' ★ best' : ch === current ? ' ● current' : '';
                const wifi = wifiOverlap[ch] ? `<br/>WiFi overlap: ${wifiOverlap[ch]}` : '';
                return `<strong>Channel ${ch}${tag}</strong><br/>Energy: ${p.value}${wifi}`;
            },
        },
        xAxis: {
            type: 'category',
            data: channels.map(String),
            name: 'ZigBee Channel',
            nameLocation: 'middle',
            nameGap: 26,
            axisTick: { alignWithLabel: true },
            axisLabel: {
                formatter: (v) => {
                    const ch = Number(v);
                    return ch === best ? `${ch}★` : ch === current ? `${ch}●` : `${ch}`;
                },
            },
        },
        yAxis: { type: 'value', min: 0, max: 255, name: 'Energy', splitNumber: 4 },
        series: [{
            type: 'bar',
            barWidth: '70%',
            data: channels.map((ch, i) => ({
                value: energies[i],
                itemStyle: { color: barColor(ch), borderRadius: [2, 2, 0, 0] },
            })),
            label: {
                show: true,
                position: 'top',
                fontSize: 8,
                color: '#888',
                formatter: (p) => wifiOverlap[channels[p.dataIndex]] || '',
            },
        }],
    });
}

window.autoSelectChannel = async function() {
    if (!await confirmDialog({
        title: 'Auto-select channel',
        message: 'Run spectrum scan and automatically set the best channel in config?',
        detail: 'A service restart will be required to apply.',
        confirmText: 'Scan & select'
    })) return;

    const btn = document.getElementById('autoChannelBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Selecting...'; }

    try {
        const res = await fetch('/api/zigbee/channel/auto', { method: 'POST' });
        const data = await res.json();
        const statusEl = document.getElementById('spectrumStatus');

        if (data.success) {
            // Update the channel dropdown in config tab
            const sel = document.getElementById('cfg_channel');
            if (sel) sel.value = data.selected_channel;

            if (statusEl) statusEl.innerHTML =
                `<span class="text-success fw-semibold"><i class="fas fa-check me-1"></i>Channel ${data.selected_channel} selected and saved. ${data.message}</span>`;

            // Re-render chart with new data
            if (data.spectrum) {
                renderSpectrumChart({ spectrum: data.spectrum, best_channel: data.selected_channel });
            }
        } else {
            if (statusEl) statusEl.innerHTML = `<span class="text-danger">Error: ${data.error}</span>`;
        }
    } catch (e) {
        document.getElementById('spectrumStatus').textContent = 'Request failed: ' + e.message;
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-magic me-1"></i> Auto Select Best Channel'; }
    }
};


// ============================================================================
// HISTORY CHART
// ============================================================================

export async function loadSpectrumHistory() {
    const hours = parseInt(document.getElementById('historyHours')?.value || 24);
    const container = document.getElementById('spectrumHistory');
    const meta = document.getElementById('spectrumHistoryMeta');
    if (!container) return;

    // Any content swap orphans the canvas — drop the live chart first.
    if (_historyChart) { _historyChart.dispose(); _historyChart = null; }
    container.innerHTML = '<div class="text-center text-muted py-3 small"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        const [histRes, statsRes] = await Promise.all([
            fetch(`/api/zigbee/spectrum/history?hours=${hours}`).then(r => r.json()),
            fetch(`/api/zigbee/spectrum/stats?hours=${hours}`).then(r => r.json())
        ]);

        if (!histRes.success || !histRes.records.length) {
            container.innerHTML = '<div class="text-center text-muted py-4 small">No history yet — background scans run hourly.</div>';
            return;
        }

        if (statsRes.success && statsRes.stats) {
            renderHistoryChart(statsRes.stats, hours, container);
        } else {
            // Fallback: compute stats client-side from raw records
            const stats = computeStatsFromRecords(histRes.records);
            renderHistoryChart(stats, hours, container);
        }

        if (meta) {
            const count = histRes.records.length / 16;
            meta.textContent = `${Math.round(count)} scans over the last ${hours}h`;
        }

    } catch (e) {
        container.innerHTML = `<div class="text-center text-danger py-3 small">Error: ${e.message}</div>`;
    }
}

function computeStatsFromRecords(records) {
    const byChannel = {};
    for (const r of records) {
        if (!byChannel[r.channel]) byChannel[r.channel] = [];
        byChannel[r.channel].push(r.energy);
    }
    const stats = {};
    for (const [ch, vals] of Object.entries(byChannel)) {
        vals.sort((a, b) => a - b);
        const n = vals.length;
        const mean = vals.reduce((a, b) => a + b, 0) / n;
        const variance = vals.reduce((a, b) => a + (b - mean) ** 2, 0) / n;
        stats[ch] = {
            min: vals[0], max: vals[n - 1],
            mean: Math.round(mean * 10) / 10,
            stddev: Math.round(Math.sqrt(variance) * 10) / 10,
            median: vals[Math.floor(n / 2)],
            p25: vals[Math.max(0, Math.floor(n * 0.25) - 1)],
            p75: vals[Math.min(n - 1, Math.floor(n * 0.75))],
            count: n
        };
    }
    return stats;
}

function renderHistoryChart(stats, hours, container) {
    const channels = Object.keys(stats).map(Number).sort((a, b) => a - b);
    if (!channels.length) return;

    // Find best/worst by mean
    let bestCh = channels[0], worstCh = channels[0];
    for (const ch of channels) {
        if (stats[ch].mean < stats[bestCh].mean) bestCh = ch;
        if (stats[ch].mean > stats[worstCh].mean) worstCh = ch;
    }

    // WiFi ↔ Zigbee overlap mapping (all 14 channels, 22MHz bandwidth each)
    const zbToWifi = {
        11: [1,2],       12: [1,2,3],     13: [1,2,3,4],   14: [1,2,3,4,5],
        15: [2,3,4,5,6], 16: [3,4,5,6,7], 17: [4,5,6,7,8], 18: [5,6,7,8,9],
        19: [6,7,8,9,10],20: [7,8,9,10,11],21: [8,9,10,11,12],22: [9,10,11,12,13],
        23: [10,11,12,13],24: [11,12,13,14],25: [12,13,14],  26: [13,14]
    };
    const commonWifi = new Set([1, 6, 11]); // Non-overlapping config
    const maxOverlap = 5;

    // Per-channel colour by mean energy (best channel always blue).
    const strokeFor = (ch) => {
        const m = stats[ch].mean;
        if (ch === bestCh) return '#0d6efd';
        if (m < 80) return '#198754';
        if (m < 150) return '#e0a800';
        return '#dc3545';
    };
    const fillFor = (ch) => {
        const m = stats[ch].mean;
        if (ch === bestCh) return 'rgba(13,110,253,0.15)';
        if (m < 80) return 'rgba(25,135,84,0.12)';
        if (m < 150) return 'rgba(255,193,7,0.12)';
        return 'rgba(220,53,69,0.12)';
    };

    const summary = `
      <div class="d-flex gap-3 mt-2 flex-wrap small align-items-center">
        <span class="d-flex align-items-center gap-1">
          <svg width="14" height="14"><rect x="1" y="2" width="12" height="10" fill="rgba(25,135,84,0.15)" stroke="#198754" stroke-width="1.5" rx="2"/></svg>
          IQR (P25–P75)
        </span>
        <span class="d-flex align-items-center gap-1">
          <svg width="14" height="14"><line x1="1" y1="7" x2="13" y2="7" stroke="#666" stroke-width="2" stroke-dasharray="3,2"/></svg>
          Median
        </span>
        <span class="d-flex align-items-center gap-1">
          <svg width="14" height="14"><polygon points="7,2 11,7 7,12 3,7" fill="#666"/></svg>
          Mean
        </span>
        <span class="d-flex align-items-center gap-1">
          <svg width="14" height="14"><line x1="7" y1="1" x2="7" y2="13" stroke="rgba(0,0,0,0.2)" stroke-width="1"/><line x1="4" y1="1" x2="10" y2="1" stroke="rgba(0,0,0,0.2)" stroke-width="1.5"/><line x1="4" y1="13" x2="10" y2="13" stroke="rgba(0,0,0,0.2)" stroke-width="1.5"/></svg>
          Min–Max
        </span>
        <span class="d-flex align-items-center gap-1">
          <svg width="14" height="14"><rect x="1" y="2" width="12" height="10" fill="rgba(220,53,69,0.08)" rx="1"/></svg>
          WiFi density
        </span>
      </div>
      <div class="d-flex gap-3 mt-1 flex-wrap small">
        <span class="text-primary"><i class="fas fa-trophy me-1"></i>Best: ch ${bestCh} (μ=${stats[bestCh].mean}, σ=${stats[bestCh].stddev})</span>
        <span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>Noisiest: ch ${worstCh} (μ=${stats[worstCh].mean}, σ=${stats[worstCh].stddev})</span>
        <span class="text-muted">${stats[channels[0]].count} samples/ch over ${hours}h</span>
      </div>
      <div class="mt-1 small text-muted">
        <strong>WiFi row:</strong> Numbers = overlapping WiFi channels. <strong>*</strong> = common non-overlapping config (1, 6, 11).
        Background tint = WiFi density (more overlap = darker).
      </div>`;

    if (_historyChart) { _historyChart.dispose(); _historyChart = null; }
    container.innerHTML = `<div id="spectrumHistoryCanvas" style="height:320px"></div>${summary}`;
    _historyChart = createChart(document.getElementById('spectrumHistoryCanvas'));

    _historyChart.setOption({
        grid: { top: 16, right: 16, bottom: 78, left: 46 },
        tooltip: {
            trigger: 'item',
            formatter: (p) => {
                const s = stats[channels[p.dataIndex]];
                if (!s) return '';
                return `<strong>Channel ${channels[p.dataIndex]}</strong><br/>`
                    + `Mean: <b>${s.mean}</b> · Median: ${s.median}<br/>`
                    + `Std Dev: ${s.stddev}<br/>`
                    + `Min: ${s.min} · Max: ${s.max}<br/>`
                    + `P25: ${s.p25} · P75: ${s.p75}<br/>`
                    + `<span style="opacity:.7">${s.count} samples</span>`;
            },
        },
        xAxis: {
            type: 'category',
            data: channels.map(String),
            axisTick: { alignWithLabel: true },
            axisLabel: {
                interval: 0,
                lineHeight: 11,
                formatter: (v) => {
                    const ch = Number(v);
                    const s = stats[ch];
                    const star = ch === bestCh ? '★' : '';
                    const wifi = (zbToWifi[ch] || []).map(w => commonWifi.has(w) ? w + '*' : w).join(',');
                    return `{ch|${ch}${star}}\n{mu|μ${s.mean}}\n{sig|σ${s.stddev}}\n{wifi|${wifi}}`;
                },
                rich: {
                    ch:   { fontSize: 10, fontWeight: 'bold', color: '#666' },
                    mu:   { fontSize: 8, color: '#999' },
                    sig:  { fontSize: 7, color: '#aaa' },
                    wifi: { fontSize: 6.5, color: '#bbb' },
                },
            },
        },
        yAxis: { type: 'value', min: 0, max: 255, name: 'Energy (0–255)', nameTextStyle: { fontSize: 9 } },
        series: [
            // WiFi-density column background.
            {
                type: 'bar',
                barWidth: '100%',
                silent: true,
                z: 0,
                tooltip: { show: false },
                data: channels.map(ch => {
                    const intensity = (zbToWifi[ch] || []).length / maxOverlap;
                    const alpha = (0.03 + intensity * 0.06).toFixed(3);
                    return { value: 255, itemStyle: { color: `rgba(220,53,69,${alpha})` } };
                }),
            },
            // Box plot: [min, Q1(p25), median, Q3(p75), max].
            {
                type: 'boxplot',
                z: 2,
                boxWidth: ['25%', '55%'],
                data: channels.map(ch => {
                    const s = stats[ch];
                    return {
                        value: [s.min, s.p25, s.median, s.p75, s.max],
                        itemStyle: { color: fillFor(ch), borderColor: strokeFor(ch), borderWidth: 1.5 },
                    };
                }),
            },
            // Mean marker (diamond) on top.
            {
                type: 'scatter',
                z: 3,
                symbol: 'diamond',
                symbolSize: 9,
                tooltip: { show: false },
                data: channels.map(ch => ({
                    value: stats[ch].mean,
                    itemStyle: { color: strokeFor(ch) },
                })),
            },
        ],
    });
}


// ============================================================================
// FORM VALUE COLLECTION
// ============================================================================

function collectFormValues() {
    const get = id => document.getElementById(id)?.value?.trim() ?? null;
    const getNum = id => { const v = get(id); return v !== null ? Number(v) : null; };

    // Parse the comma-separated "Disable Default Providers" input
    const disableDefaultsRaw = get('cfg_ota_disable_defaults') || '';
    const disableDefaults = disableDefaultsRaw
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);

    return {
        zigbee: {
            port: get('cfg_port'),
            radio_type: get('cfg_radio_type'),
            channel: getNum('cfg_channel'),
            topology_scan_interval: getNum('cfg_topology_scan_interval'),
            pan_id: get('cfg_pan_id'),
            extended_pan_id_hex: get('cfg_extended_pan_id'),
            network_key_hex: get('cfg_network_key') || null,  // null = don't overwrite
        },
        mqtt: {
            enabled: document.getElementById('cfg_mqtt_enabled')?.checked ?? true,
            broker_host: get('cfg_mqtt_host'),
            broker_port: getNum('cfg_mqtt_port'),
            username: get('cfg_mqtt_username'),
            password: get('cfg_mqtt_password') || undefined,
            base_topic: get('cfg_mqtt_base_topic'),
        },
        homeassistant: {
            enabled: document.getElementById('cfg_ha_enabled')?.checked ?? true,
        },
        web: {
            host: get('cfg_web_host'),
            port: getNum('cfg_web_port'),
        },
        logging: {
            level: get('cfg_log_level'),
        },
        weather: {
            enabled: document.getElementById('cfg_weather_enabled')?.checked ?? false,
            latitude: parseFloat(document.getElementById('cfg_weather_lat')?.value) || null,
            longitude: parseFloat(document.getElementById('cfg_weather_lon')?.value) || null,
            poll_interval_minutes: Number(document.getElementById('cfg_weather_interval')?.value) || 30,
            mqtt_publish: document.getElementById('cfg_weather_mqtt')?.checked ?? false,
        },
        media: {
            enabled: document.getElementById('cfg_media_enabled')?.checked ?? false,
            poll_interval_seconds: Number(document.getElementById('cfg_media_poll')?.value) || 10,
            adopt_sessions: document.getElementById('cfg_media_adopt')?.checked ?? true,
            cast: {
                enabled: document.getElementById('cfg_media_cast_enabled')?.checked ?? true,
                app_id: document.getElementById('cfg_media_cast_appid')?.value?.trim() || 'CC1AD845',
                lyrics_app_id: document.getElementById('cfg_media_cast_lyrics_appid')?.value?.trim() || '',
            },
            wiim: {
                enabled: document.getElementById('cfg_media_wiim_enabled')?.checked ?? true,
                devices: (document.getElementById('cfg_media_wiim_devices')?.value || '')
                    .split('\n').map(s => s.trim()).filter(Boolean),
            },
            radio_browser: {
                enabled: document.getElementById('cfg_media_rb_enabled')?.checked ?? true,
            },
            tts: {
                base_url: document.getElementById('cfg_media_tts_base')?.value?.trim()
                    || 'https://translate.google.com/translate_tts',
                lang: document.getElementById('cfg_media_tts_lang')?.value?.trim() || 'en',
            },
            tidal: {
                enabled: document.getElementById('cfg_media_tidal_enabled')?.checked ?? false,
                quality: document.getElementById('cfg_media_tidal_quality')?.value || 'high',
                manifest_base_url: document.getElementById('cfg_media_tidal_manifest')?.value?.trim() || '',
            },
        },
        ota: {
            enabled: document.getElementById('cfg_ota_enabled')?.checked ?? true,
            extra_providers: collectOtaProviderRows(),
            disable_default_providers: disableDefaults,
        },
        security: {
            nuki: {
                enabled: document.getElementById('cfg_nuki_enabled')?.checked ?? false,
                bridge: {
                    enabled: document.getElementById('cfg_nuki_bridge_enabled')?.checked ?? true,
                    host: get('cfg_nuki_bridge_host') ?? '',
                    port: getNum('cfg_nuki_bridge_port') || 8080,
                    // Blank = keep the stored token (backend skips falsy)
                    token: get('cfg_nuki_bridge_token') || '',
                    hashed_token: document.getElementById('cfg_nuki_bridge_hashed')?.checked ?? true,
                },
                matter: {
                    enabled: document.getElementById('cfg_nuki_matter_enabled')?.checked ?? true,
                },
            },
        },
    };
}

// ============================================================================
// BACKUP & RESTORE
// ============================================================================

let _pendingRestoreFile = null;

function renderBackupRestoreSection() {
    let el = document.getElementById('backupRestoreBody');
    if (!el) {
        const configTab = document.getElementById('settingsConfig');
        if (!configTab) return;
        const wrapper = document.createElement('div');
        wrapper.id = 'backupRestoreBody';
        wrapper.className = 'mt-3';
        configTab.appendChild(wrapper);
        el = wrapper;
    }

    el.innerHTML = `
    <h6 class="text-uppercase text-muted fw-bold mb-3 mt-2 small">
      <i class="fas fa-database me-1"></i> Network Backup
    </h6>
    <p class="text-muted small mb-3">
      Download a full backup of your Zigbee network: config, paired devices (zigbee.db),
      friendly names, automations, groups, zones, and device state cache.
      Use this to migrate to a new container or recover from failure.
    </p>

    <div class="row g-3 mb-4">

      <!-- CREATE -->
      <div class="col-md-6">
        <div class="card border-primary h-100">
          <div class="card-body text-center py-4">
            <i class="fas fa-download fa-2x text-primary mb-2"></i>
            <h6 class="fw-semibold">Create Backup</h6>
            <p class="text-muted small mb-3">Download a .zip containing the full network state</p>
            <button class="btn btn-primary" onclick="window.createNetworkBackup()" id="btnCreateBackup">
              <i class="fas fa-file-archive me-1"></i> Download Backup
            </button>
            <div id="backupStatus" class="small mt-2"></div>
          </div>
        </div>
      </div>

      <!-- RESTORE -->
      <div class="col-md-6">
        <div class="card border-warning h-100">
          <div class="card-body text-center py-4">
            <i class="fas fa-upload fa-2x text-warning mb-2"></i>
            <h6 class="fw-semibold">Restore Backup</h6>
            <p class="text-muted small mb-3">Upload a previously downloaded backup .zip</p>
            <input type="file" id="restoreFileInput" accept=".zip" class="d-none"
                   onchange="window.handleRestoreFile(this)">
            <button class="btn btn-outline-warning"
                    onclick="document.getElementById('restoreFileInput').click()">
              <i class="fas fa-file-upload me-1"></i> Select Backup File
            </button>
            <div id="restoreStatus" class="small mt-2"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Confirm panel -->
    <div id="restoreConfirmPanel" class="d-none mb-3">
      <div class="alert alert-danger mb-0">
        <i class="fas fa-exclamation-triangle me-1"></i>
        <strong>Warning:</strong> Restoring will overwrite your current configuration,
        device database, automations, groups, and all settings.
        The service must be restarted afterwards.
        <div class="mt-2">
          <span id="restoreFileName" class="fw-semibold"></span>
          <span id="restoreFileSize" class="text-muted ms-2"></span>
        </div>
        <div class="mt-3 d-flex gap-2">
          <button class="btn btn-danger btn-sm" onclick="window.confirmRestore()">
            <i class="fas fa-check me-1"></i> Confirm Restore
          </button>
          <button class="btn btn-outline-secondary btn-sm" onclick="window.cancelRestore()">
            Cancel
          </button>
        </div>
      </div>
    </div>
    `;

    loadBackupInfo();
}

async function loadBackupInfo() {
    try {
        const res = await fetch('/api/backup/info');
        const data = await res.json();
        if (data.success) {
            const el = document.getElementById('backupStatus');
            if (el) {
                const count = data.files.filter(f => f.exists).length;
                el.innerHTML = `<span class="text-muted">${count} files · ${data.total_size_mb} MB</span>`;
            }
        }
    } catch (_) { /* silent */ }
}

async function createNetworkBackup() {
    const btn = document.getElementById('btnCreateBackup');
    const status = document.getElementById('backupStatus');

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Creating...';

    try {
        const res = await fetch('/api/backup/create');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const blob = await res.blob();
        const disposition = res.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^"]+)"?/);
        const filename = match ? match[1] : `zmm_backup_${Date.now()}.zip`;

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>${filename}</span>`;
    } catch (e) {
        status.innerHTML = `<span class="text-danger">Backup failed: ${e.message}</span>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-file-archive me-1"></i> Download Backup';
    }
}

function handleRestoreFile(input) {
    const file = input.files[0];
    if (!file) return;

    _pendingRestoreFile = file;
    document.getElementById('restoreFileName').textContent = file.name;
    document.getElementById('restoreFileSize').textContent =
        `(${(file.size / (1024 * 1024)).toFixed(2)} MB)`;
    document.getElementById('restoreConfirmPanel').classList.remove('d-none');
}

async function confirmRestore() {
    if (!_pendingRestoreFile) {
        showSettingsAlert('danger', 'Select a backup file first.');
        return;
    }

    const formData = new FormData();
    formData.append('file', _pendingRestoreFile);

    const status = document.getElementById('restoreStatus');
    if (status) status.innerHTML = '<span class="text-muted"><i class="fas fa-spinner fa-spin me-1"></i> Restoring…</span>';

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);

    try {
        const resp = await fetch('/api/backup/restore', {
            method: 'POST',
            body: formData,
            signal: controller.signal,
        });
        clearTimeout(timer);

        const result = await resp.json();
        if (result.success) {
            showSettingsAlert('success', `Restored ${result.restored_count} files. Waiting for restart…`);
            if (status) status.innerHTML = '';
            pollForRestart(window.location.origin);
        } else {
            showSettingsAlert('danger', `Restore failed: ${result.error}`);
            if (status) status.innerHTML = '';
        }

    } catch (e) {
        clearTimeout(timer);
        if (e.name === 'AbortError' || e.message.toLowerCase().includes('network')) {
            showSettingsAlert('warning', 'Connection lost — restore likely succeeded. Waiting for server…');
            if (status) status.innerHTML = '';
            pollForRestart(window.location.origin);
        } else {
            showSettingsAlert('danger', `Restore failed: ${e.message}`);
            if (status) status.innerHTML = '';
        }
    }
}

function pollForRestart(baseUrl, attempts = 20) {
    let tries = 0;
    const iv = setInterval(async () => {
        try {
            const r = await fetch(`${baseUrl}/api/system/health`);
            if (r.ok) {
                clearInterval(iv);
                window.location.href = baseUrl;
            }
        } catch (_) {}
        if (++tries >= attempts) {
            clearInterval(iv);
            showSettingsAlert('danger', 'Server did not come back. Check the address manually.');
        }
    }, 2000);
}

function cancelRestore() {
    _pendingRestoreFile = null;
    document.getElementById('restoreConfirmPanel').classList.add('d-none');
    document.getElementById('restoreFileInput').value = '';
    document.getElementById('restoreStatus').innerHTML = '';
}

// ============================================================================
// EXPORT GLOBALS FOR HTML INLINE ONCLICK
// ============================================================================

export async function saveSettingsConfig() {
    await saveStructuredConfig();
}

export async function saveSettingsConfigAndRestart() {
    const ok = await saveStructuredConfig(true);
    if (!ok) return;
    showSettingsAlert('warning', 'Configuration saved — restarting the service. This page will reconnect shortly…');
    try {
        await fetch('/api/system/restart', { method: 'POST' });
    } catch (e) {
        // The connection drops as the process restarts — that's expected.
    }
    // Give the process time to come back, then reload.
    setTimeout(() => window.location.reload(), 8000);
}

window.runSpectrumScan = runSpectrumScan;
window.saveSettingsConfig = saveSettingsConfig;
window.saveSettingsConfigAndRestart = saveSettingsConfigAndRestart;
window.loadSpectrumHistory = loadSpectrumHistory;
window.createNetworkBackup = createNetworkBackup;
window.handleRestoreFile = handleRestoreFile;
window.confirmRestore = confirmRestore;
window.cancelRestore = cancelRestore;

// ============================================================================
// UTILITIES
// ============================================================================

function showSettingsAlert(type, msg) {
    const el = document.getElementById('settingsAlert');
    if (!el) return;
    el.className = `alert alert-${type} small`;
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 6000);
}