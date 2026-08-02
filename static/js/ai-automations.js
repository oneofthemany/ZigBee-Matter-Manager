/**
 * AI Automations - Natural Language Rule Generation
 *
 * Adds an "AI Assist" panel to the global Automations tab.
 * User types natural language, AI generates a rule, user reviews
 * the pre-filled form, then saves via the existing automation API.
 */

import { initAutomationTab } from './modal/automation.js';

let _aiConfigured = false;
let _aiHost = null;            // last host assessment (for Ollama gating)

// INIT — called once from automations-page.js

export async function initAIAutomations() {
    try {
        const res = await fetch('/api/ai/status');
        const data = await res.json();
        _aiConfigured = data.configured;
    } catch {
        _aiConfigured = false;
    }
}

export function isAIConfigured() { return _aiConfigured; }

// RENDER — returns the AI panel HTML for injection into automations page

export function renderAIPanel() {
    return `
    <div id="ai-auto-panel" class="card mb-3 border-info">
        <div class="card-header bg-info bg-opacity-10 d-flex justify-content-between align-items-center py-2">
            <strong><i class="fas fa-brain me-1"></i> AI Automation Builder</strong>
            <div class="d-flex gap-2 align-items-center">
                <span class="badge bg-info text-dark small" title="Works offline — no AI model required"><i class="fas fa-bolt"></i> Local</span>
                <span id="ai-status-badge" class="badge ${_aiConfigured ? 'bg-success' : 'bg-secondary'} small"
                      title="Optional AI model for free-form requests">
                    AI: ${_aiConfigured ? 'connected' : 'optional'}
                </span>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._aiToggleChat()"
                        title="Ask about your devices &amp; automations"><i class="fas fa-comments"></i> Chat</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="window._aiOpenSettingsTab()"
                        title="AI provider &amp; host settings (Settings → AI)"><i class="fas fa-cog"></i></button>
            </div>
        </div>
        <div class="card-body">
            <!-- Provider & host config now live in Settings → AI (the cog above). -->

            <!-- Prompt input -->
            <div class="input-group">
                <input type="text" class="form-control" id="ai-prompt"
                       placeholder="Describe it in plain English, e.g. 'turn on the hall light when the hallway sensor detects motion'"
                       onkeydown="if(event.key==='Enter')window._aiGenerate()">
                <button class="btn btn-info text-white" onclick="window._aiGenerate()" id="ai-gen-btn">
                    <i class="fas fa-magic me-1"></i> Generate
                </button>
            </div>
            <div class="small text-muted mt-1">
                Built instantly on-device. <a href="#" role="button" onclick="window._aiShowHelp(event)">What can I say?</a>
            </div>
            <div id="ai-help" class="small mt-2" style="display:none"></div>

            <!-- Result area -->
            <div id="ai-result" class="mt-2" style="display:none"></div>

            <!-- Domain-aware chat (hidden by default) -->
            <div id="ai-chat" class="mt-3 border-top pt-2" style="display:none">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span class="small fw-bold"><i class="fas fa-comments me-1"></i> Ask about your devices &amp; automations</span>
                    <button class="btn btn-sm btn-outline-danger py-0" onclick="window._aiChatClear()" title="Clear history">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
                <div id="ai-chat-log" class="border rounded p-2 mb-2 bg-white"
                     style="height:260px;overflow-y:auto;font-size:.85rem"></div>
                <div class="input-group input-group-sm">
                    <input type="text" class="form-control" id="ai-chat-input"
                           placeholder="e.g. why didn't my hallway light come on? which sensors are low battery?"
                           onkeydown="if(event.key==='Enter')window._aiChatSend()">
                    <button class="btn btn-info text-white" id="ai-chat-send" onclick="window._aiChatSend()">
                        <i class="fas fa-paper-plane"></i>
                    </button>
                </div>
            </div>
        </div>
    </div>`;
}

// SETTINGS → AI  (host assessment · provider config · local Ollama)
// The infrastructure controls live here (not in the Automations builder) so
// there's one home for "what model/provider does the AI use, and can this host
// run one locally". Reuses the same element ids + window handlers.
export function renderAISettingsPanel() {
    return `
    <div class="card border-info">
        <div class="card-header bg-info bg-opacity-10 d-flex justify-content-between align-items-center py-2">
            <strong><i class="fas fa-brain me-1"></i> AI provider &amp; local model</strong>
            <span id="ai-status-badge" class="badge ${_aiConfigured ? 'bg-success' : 'bg-secondary'} small">
                AI: ${_aiConfigured ? 'connected' : 'optional'}
            </span>
        </div>
        <div class="card-body small">
            <div id="ai-settings-alert"></div>
            <p class="text-muted">
                The deterministic on-device parser handles most automations with no model.
                A local (or remote) LLM is an optional fallback for free-form requests
                and the device chat.
            </p>
            <!-- Host capability check -->
            <div class="mb-2 d-flex justify-content-between align-items-center">
                <span class="fw-bold"><i class="fas fa-microchip me-1"></i> Host check</span>
                <button class="btn btn-sm btn-outline-secondary py-0" onclick="window._aiCheckHost()">
                    <i class="fas fa-gauge-high me-1"></i> Assess this host
                </button>
            </div>
            <div id="ai-host" class="mb-3"></div>
            <hr class="my-2">
            <div class="fw-bold mb-2"><i class="fas fa-sliders-h me-1"></i> Provider</div>
            <div class="row g-2 mb-2">
                <div class="col-md-3">
                    <label class="form-label small mb-0">Provider</label>
                    <select class="form-select form-select-sm" id="ai-provider" onchange="window._aiProviderChanged(this.value)">
                        <option value="ollama">Ollama (local)</option>
                        <option value="sglang" id="ai-provider-sglang" disabled>SGLang (assess host first)</option>
                        <option value="openai">OpenAI</option>
                        <option value="anthropic">Anthropic</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                <div class="col-md-3">
                    <label class="form-label small mb-0">Model</label>
                    <input type="text" class="form-control form-control-sm" id="ai-model" placeholder="e.g. llama3.1:8b-instruct-q4_K_M">
                </div>
                <div class="col-md-4">
                    <label class="form-label small mb-0">API Key <span class="text-muted" id="ai-key-hint">(not required for Ollama)</span></label>
                    <div class="input-group input-group-sm">
                        <input type="password" class="form-control form-control-sm" id="ai-apikey" placeholder="Leave blank for Ollama">
                        <span class="input-group-text" id="ai-key-badge"></span>
                    </div>
                </div>
                <div class="col-md-2 d-flex align-items-end">
                    <button class="btn btn-sm btn-primary w-100" onclick="window._aiSaveSettings()">
                        <i class="fas fa-save me-1"></i>Save
                    </button>
                </div>
            </div>
            <div class="row g-2">
                <div class="col-md-5">
                    <label class="form-label small mb-0">Base URL <span class="text-muted">(auto-detected per provider)</span></label>
                    <input type="text" class="form-control form-control-sm" id="ai-baseurl" placeholder="http://localhost:11434/v1">
                </div>
                <div class="col-md-2">
                    <label class="form-label small mb-0">Temperature</label>
                    <input type="number" class="form-control form-control-sm" id="ai-temp" value="0.3" min="0" max="2" step="0.1">
                </div>
                <div class="col-md-2">
                    <label class="form-label small mb-0">Max Tokens</label>
                    <input type="number" class="form-control form-control-sm" id="ai-maxtokens" value="2000" min="500" max="8000" step="100">
                </div>
                <div class="col-md-3 d-flex align-items-end">
                    <button class="btn btn-sm btn-outline-info w-100" onclick="window._aiTestConnection()">
                        <i class="fas fa-plug me-1"></i>Test Connection
                    </button>
                </div>
            </div>
        </div>
    </div>`;
}

// Mount the panel into Settings → AI on first reveal, then load current config
// and auto-assess the host so the Ollama controls surface without a manual click.
export function initAISettingsTab() {
    const btn = document.querySelector('[data-bs-target="#settingsAI"]');
    if (!btn || btn.dataset.aiInit) return;
    btn.dataset.aiInit = '1';
    btn.addEventListener('shown.bs.tab', () => {
        const host = document.getElementById('settingsAI');
        if (host && !host.dataset.mounted) {
            host.innerHTML = renderAISettingsPanel();
            host.dataset.mounted = '1';
        }
        _aiLoadSettings();
        _aiCheckHost();
    });
}

function _aiOpenSettingsTab() {
    const show = sel => {
        const el = document.querySelector(sel);
        if (el && window.bootstrap?.Tab) window.bootstrap.Tab.getOrCreateInstance(el).show();
    };
    show('[data-bs-target="#settings"]');
    setTimeout(() => show('[data-bs-target="#settingsAI"]'), 200);
}

// GENERATE

async function _aiGenerate() {
    const input = document.getElementById('ai-prompt');
    const btn = document.getElementById('ai-gen-btn');
    const result = document.getElementById('ai-result');
    if (!input || !input.value.trim()) return;

    const prompt = input.value.trim();

    // Show loading
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Generating...';
    result.style.display = 'block';
    result.innerHTML = '<div class="text-muted small"><i class="fas fa-spinner fa-spin"></i> Analysing your request and generating automation rule...</div>';

    try {
        const res = await fetch('/api/ai/automation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt })
        });
        const data = await res.json();

        if (data.success) {
            _showGeneratedRule(data.rule, data.explanation, data.source);
        } else {
            const partial = data.partial && Object.keys(data.partial).length
                ? `<div class="mt-1">Understood so far: ${Object.entries(data.partial)
                    .map(([k, v]) => `<span class="badge bg-secondary">${_esc(k)}: ${_esc(String(v))}</span>`).join(' ')}</div>`
                : '';
            const examples = (data.examples || []).length
                ? `<div class="mt-2"><div class="small fw-bold mb-1">Try phrasing it like:</div>${
                    data.examples.map(e => `<div><code class="small" role="button" onclick="window._aiUseExample(this)">${_esc(e)}</code></div>`).join('')}</div>`
                : '';
            result.innerHTML = `<div class="alert alert-warning mb-0 py-2 small">
                <i class="fas fa-exclamation-triangle me-1"></i> ${_esc(data.error || 'Failed to generate rule')}
                ${partial}${examples}
                ${data.raw_response ? `<details class="mt-1"><summary>Raw response</summary><pre class="mb-0 small">${_esc(data.raw_response)}</pre></details>` : ''}
            </div>`;
        }
    } catch (e) {
        result.innerHTML = `<div class="alert alert-danger mb-0 py-2 small">
            <i class="fas fa-times-circle me-1"></i> ${e.message}
        </div>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-magic me-1"></i> Generate';
    }
}

// DISPLAY GENERATED RULE

function _showGeneratedRule(rule, explanation, source) {
    const result = document.getElementById('ai-result');
    const srcBadge = source === 'local'
        ? '<span class="badge bg-info text-dark ms-1" title="Built locally — no AI model used"><i class="fas fa-bolt"></i> Local</span>'
        : source === 'llm'
            ? '<span class="badge bg-primary ms-1" title="Generated by the AI model"><i class="fas fa-brain"></i> AI</span>'
            : '';

    // Conditions summary
    const condHtml = (rule.conditions || []).map(c => {
        if (c.type === 'time_window') return `<span class="badge bg-info">🕐 ${c.time_from}-${c.time_to}</span>`;
        if (c.type === 'sun') return `<span class="badge bg-info">🌅 ${c.from}→${c.to}</span>`;
        if (c.type === 'zone') return `<span class="badge bg-primary">${c.event === 'leave' ? '🚶 leaves' : '📍 enters'} ${c.place}</span>`;
        return `<span class="badge bg-primary">${c.attribute} ${c.operator} ${c.value}</span>`;
    }).join(rule.condition_logic === 'or'
        ? ' <small style="color:#6f42c1;font-weight:600">OR</small> '
        : ' <small class="text-muted">AND</small> ');

    // Steps summary
    const stepsHtml = s => (s || []).map(step => {
        if (step.type === 'command') return `<span class="badge bg-success">${step.command}${step.value != null ? '=' + step.value : ''}</span> <small>${step.target_ieee || '?'}</small>`;
        if (step.type === 'delay') return `<span class="badge bg-warning text-dark">wait ${step.seconds}s</span>`;
        if (step.type === 'if_then_else') return `<span class="badge bg-purple" style="background:#6f42c1">IF/THEN/ELSE</span>`;
        if (step.type === 'parallel') return `<span class="badge bg-dark">parallel</span>`;
        return `<span class="badge bg-secondary">${step.type}</span>`;
    }).join(' → ');

    result.innerHTML = `
    <div class="card border-success">
        <div class="card-header bg-success bg-opacity-10 py-2 d-flex justify-content-between align-items-center">
            <strong class="small"><i class="fas fa-check-circle text-success me-1"></i> Generated Rule Preview${srcBadge}</strong>
            <div class="d-flex gap-2">
                <button class="btn btn-sm btn-outline-success" onclick="window._aiSaveRule()">
                    <i class="fas fa-save me-1"></i> Save Directly
                </button>
                <button class="btn btn-sm btn-outline-primary" onclick="window._aiEditRule()">
                    <i class="fas fa-edit me-1"></i> Review & Edit
                </button>
            </div>
        </div>
        <div class="card-body py-2 small">
            <p class="mb-1 text-muted"><i class="fas fa-info-circle me-1"></i> ${_esc(explanation)}</p>
            <div class="mb-1"><strong>Name:</strong> ${_esc(rule.name || '(unnamed)')}</div>
            <div class="mb-1"><strong>Source:</strong> <code>${rule.source_ieee}</code></div>
            <div class="mb-1"><strong>IF:</strong> ${condHtml}</div>
            ${(rule.prerequisites || []).length ? `<div class="mb-1"><strong>CHECK:</strong> ${rule.prerequisites.map(p => {
                if (p.type === 'time_window') return `<span class="badge bg-info">🕐 ${p.time_from}-${p.time_to}</span>`;
                if (p.type === 'sun') return `<span class="badge bg-info">🌅 ${p.from}→${p.to}</span>`;
                return `<span class="badge bg-secondary">${p.negate ? 'NOT ' : ''}${p.ieee} ${p.attribute} ${p.operator} ${p.value}</span>`;
            }).join(' ')}</div>` : ''}
            <div class="mb-1"><strong>THEN:</strong> ${stepsHtml(rule.then_sequence)}</div>
            ${(rule.else_sequence || []).length ? `<div class="mb-1"><strong>ELSE:</strong> ${stepsHtml(rule.else_sequence)}</div>` : ''}
            <details class="mt-1"><summary class="text-muted">Raw JSON</summary><pre class="mb-0 small bg-light p-2 rounded" style="max-height:200px;overflow:auto">${_esc(JSON.stringify(rule, null, 2))}</pre></details>
        </div>
    </div>`;

    // Store rule for save/edit
    window._aiGeneratedRule = rule;
}

// SAVE / EDIT ACTIONS

async function _aiSaveRule() {
    const rule = window._aiGeneratedRule;
    if (!rule) return;

    try {
        const res = await fetch('/api/automations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(rule)
        });
        const data = await res.json();

        const result = document.getElementById('ai-result');
        if (data.success) {
            result.innerHTML = `<div class="alert alert-success mb-0 py-2 small">
                <i class="fas fa-check-circle me-1"></i> Rule saved: <strong>${data.rule?.name || data.rule?.id}</strong>
            </div>`;
            // Clear the prompt
            const input = document.getElementById('ai-prompt');
            if (input) input.value = '';
            window._aiGeneratedRule = null;
            // Refresh the rules list if the page refresh function exists
            if (typeof window._apRefresh === 'function') window._apRefresh();
        } else {
            result.innerHTML = `<div class="alert alert-danger mb-0 py-2 small">
                <i class="fas fa-times-circle me-1"></i> Save failed: ${data.error || data.detail || 'Unknown error'}
            </div>`;
        }
    } catch (e) {
        document.getElementById('ai-result').innerHTML = `<div class="alert alert-danger mb-0 py-2 small">${e.message}</div>`;
    }
}

async function _aiEditRule() {
    const rule = window._aiGeneratedRule;
    if (!rule || !rule.source_ieee) return;

    // Open the create panel with the source device pre-selected, then populate the form
    const createPanel = document.getElementById('ap-create-panel');
    const srcSelect = document.getElementById('ap-source-select');
    if (!createPanel || !srcSelect) return;

    createPanel.style.display = 'block';
    srcSelect.value = rule.source_ieee;

    // Trigger source selection to init the form (builds the DOM scaffold).
    if (typeof window._apSourceSelected === 'function') {
        await window._apSourceSelected(rule.source_ieee);
    }

    // Load the FULL generated rule (conditions, prerequisites, then/else steps)
    // into the visual builder, treated as a new unsaved rule.
    if (typeof window._aShowFormWith === 'function') {
        window._aShowFormWith(rule);
    }

    // Scroll to form
    createPanel.scrollIntoView({ behavior: 'smooth' });
}

// SETTINGS

// Provider defaults — mirrors PROVIDER_DEFAULTS in ai_assistant.py
const PROVIDER_DEFAULTS = {
    ollama:    { base_url: 'http://localhost:11434/v1', model: 'llama3.1:8b-instruct-q4_K_M', requires_key: false },
    sglang:    { base_url: 'http://localhost:30000/v1', model: '', requires_key: false },
    openai:    { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', requires_key: true },
    anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-20250514', requires_key: true },
    custom:    { base_url: '', model: '', requires_key: false },
};

// SGLang is GPU-class — only selectable once the host assessor confirms it.
function _aiGateSglang(host) {
    const opt = document.getElementById('ai-provider-sglang');
    if (!opt) return;
    const sg = host && host.backends && host.backends.sglang;
    if (sg && sg.viable) {
        opt.disabled = false;
        opt.textContent = 'SGLang (local GPU)';
        opt.title = '';
    } else {
        opt.disabled = true;
        opt.textContent = 'SGLang (needs ≥12 GB GPU)';
        opt.title = (sg && sg.reason) || 'Run the host check first';
    }
}

async function _aiLoadSettings() {
    try {
        const res = await fetch('/api/ai/status');
        const data = await res.json();

        const set = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined) el.value = val; };
        set('ai-provider', data.provider || 'ollama');
        set('ai-model', data.model || '');
        set('ai-baseurl', data.base_url || '');
        set('ai-temp', data.temperature ?? 0.3);
        set('ai-maxtokens', data.max_tokens ?? 2000);

        // API key indicator
        const badge = document.getElementById('ai-key-badge');
        if (badge) {
            badge.innerHTML = data.has_api_key
                ? '<i class="fas fa-check text-success"></i>'
                : '<i class="fas fa-times text-muted"></i>';
        }

        // Update key hint based on provider
        _updateKeyHint(data.provider || 'ollama');

        // Gate the SGLang option: use cached assessment, else fetch once.
        if (_aiHost) {
            _aiGateSglang(_aiHost);
        } else {
            try { _aiHost = await (await fetch('/api/ai/host')).json(); _aiGateSglang(_aiHost); }
            catch { /* leave disabled */ }
        }
    } catch { /* ignore */ }
}

function _aiProviderChanged(provider) {
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;

    // Auto-fill model and base_url placeholders/values
    const modelEl = document.getElementById('ai-model');
    const urlEl = document.getElementById('ai-baseurl');
    const keyEl = document.getElementById('ai-apikey');

    if (modelEl) {
        modelEl.value = defaults.model;
        modelEl.placeholder = defaults.model || 'Model name';
    }
    if (urlEl) {
        urlEl.value = defaults.base_url;
        urlEl.placeholder = defaults.base_url || 'Base URL';
    }
    // Clear API key field when switching provider
    if (keyEl) keyEl.value = '';

    _updateKeyHint(provider);
}

function _updateKeyHint(provider) {
    const hint = document.getElementById('ai-key-hint');
    if (!hint) return;
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;
    hint.textContent = defaults.requires_key
        ? `(required for ${provider})`
        : `(not required for ${provider})`;
}

async function _aiSaveSettings() {
    const get = id => document.getElementById(id)?.value?.trim() || '';
    const alert = document.getElementById('ai-settings-alert');
    const config = {};

    // Always send provider
    config.provider = get('ai-provider') || 'ollama';

    // Only send non-empty optional fields
    const model = get('ai-model');
    if (model) config.model = model;

    const baseUrl = get('ai-baseurl');
    if (baseUrl) config.base_url = baseUrl;

    const apiKey = get('ai-apikey');
    if (apiKey) config.api_key = apiKey;

    const temp = parseFloat(get('ai-temp'));
    if (!isNaN(temp)) config.temperature = temp;

    const maxTokens = parseInt(get('ai-maxtokens'));
    if (!isNaN(maxTokens)) config.max_tokens = maxTokens;

    try {
        const res = await fetch('/api/ai/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        const data = await res.json();

        if (data.success || data.configured !== undefined) {
            _aiConfigured = data.configured;

            // Update header badge
            const badge = document.getElementById('ai-status-badge');
            if (badge) {
                badge.className = `badge ${_aiConfigured ? 'bg-success' : 'bg-warning text-dark'} small`;
                badge.textContent = _aiConfigured ? 'Connected' : 'Not Configured';
            }

            // Show feedback
            if (alert) {
                alert.innerHTML = `<div class="alert alert-success py-1 mb-2 small">
                    <i class="fas fa-check-circle me-1"></i> Settings saved to config.yaml
                    — ${data.provider}/${data.model}
                </div>`;
                setTimeout(() => { alert.innerHTML = ''; }, 4000);
            }

            // Refresh key indicator
            _aiLoadSettings();
        } else {
            if (alert) alert.innerHTML = `<div class="alert alert-danger py-1 mb-2 small">
                <i class="fas fa-times-circle me-1"></i> Save failed: ${data.error || 'Unknown error'}
            </div>`;
        }
    } catch (e) {
        if (alert) alert.innerHTML = `<div class="alert alert-danger py-1 mb-2 small">${e.message}</div>`;
    }
}

const _AI_TEST_STAGES = [
    { stage: 'connectivity', label: 'Provider reachable',
      hint: 'tiny prompt, no device context' },
    { stage: 'json', label: 'Strict JSON output',
      hint: 'can the model follow a format instruction' },
    { stage: 'rule', label: 'Automation rule generation',
      hint: 'real rule against your devices — slow on CPU' },
];

// Progressive self-test: stages run one at a time against /api/ai/test with a
// live elapsed counter, and every failure shows the provider's actual error.
// 'quick' = connectivity + JSON; 'full' adds a grounded rule generation.
async function _aiTestConnection(level = 'quick') {
    const alert = document.getElementById('ai-settings-alert');
    if (!alert) return;
    const stages = level === 'full' ? _AI_TEST_STAGES : _AI_TEST_STAGES.slice(0, 2);

    alert.innerHTML = `<div class="alert alert-info py-2 mb-2 small" id="ai-test-panel">
        <div class="fw-bold mb-1" id="ai-test-head"><i class="fas fa-plug me-1"></i>Testing AI provider…</div>
        ${stages.map(s => `<div id="ai-test-${s.stage}" class="mb-1">
            <i class="far fa-circle text-muted"></i> ${s.label}
            <span class="text-muted">— ${s.hint}</span></div>`).join('')}
        <div id="ai-test-extra" class="mt-1"></div>
    </div>`;

    let overall = true;
    for (const s of stages) {
        const row = document.getElementById(`ai-test-${s.stage}`);
        if (!row) return;                          // panel replaced meanwhile
        const t0 = Date.now();
        row.innerHTML = `<i class="fas fa-spinner fa-spin text-info"></i> ${s.label}
            <span class="text-muted">— ${s.hint} · <span id="ai-test-${s.stage}-t">0s</span></span>`;
        const timer = setInterval(() => {
            const el = document.getElementById(`ai-test-${s.stage}-t`);
            if (el) el.textContent = `${Math.round((Date.now() - t0) / 1000)}s`;
        }, 500);

        let d;
        try {
            const res = await fetch('/api/ai/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stage: s.stage })
            });
            d = await res.json();
        } catch (e) {
            d = { success: false, error: e.message };
        }
        clearInterval(timer);

        const head = document.getElementById('ai-test-head');
        if (head && d.provider) {
            head.innerHTML = `<i class="fas fa-plug me-1"></i>${_esc(d.provider)}/${_esc(d.model || '?')}
                <span class="text-muted fw-normal">@ ${_esc(d.base_url || '')}</span>`;
        }
        const secs = d.latency_ms != null
            ? `${(d.latency_ms / 1000).toFixed(1)}s`
            : `${Math.round((Date.now() - t0) / 1000)}s`;

        if (d.success === true) {
            let extra = '';
            if (s.stage === 'rule') {
                extra = `
                    <div class="ms-3 text-muted">Asked: &ldquo;${_esc(d.prompt || '')}&rdquo;</div>
                    ${d.explanation ? `<div class="ms-3">Got: ${_esc(d.explanation)}</div>` : ''}
                    <div class="ms-3 text-muted">Context sent: ${d.context_devices ?? '?'} device(s),
                        ${d.prompt_chars ?? '?'} chars —
                        <a href="#" onclick="window._aiShowTestContext(event, '${encodeURIComponent(d.prompt || '')}')">view</a></div>`;
            }
            row.innerHTML = `<i class="fas fa-check-circle text-success"></i> ${s.label}
                <span class="text-muted">(${secs})</span>${extra}`;
        } else if (d.success === false) {
            overall = false;
            row.innerHTML = `<i class="fas fa-times-circle text-danger"></i> ${s.label}
                <span class="text-muted">(${secs})</span>
                <div class="ms-3 text-danger" style="word-break:break-word">${_esc(d.error || 'failed')}</div>
                ${d.prompt ? `<div class="ms-3 text-muted">Asked: &ldquo;${_esc(d.prompt)}&rdquo;</div>` : ''}
                ${d.raw_response ? `<div class="ms-3 text-muted">Model said: <code>${_esc(d.raw_response)}</code></div>` : ''}`;
            break;                                  // later stages depend on this one
        } else {
            row.innerHTML = `<i class="fas fa-minus-circle text-muted"></i> ${s.label}
                <div class="ms-3 text-muted">${_esc(d.error || 'skipped')}</div>`;
        }
    }

    const panel = document.getElementById('ai-test-panel');
    if (panel) panel.className = `alert alert-${overall ? 'success' : 'warning'} py-2 mb-2 small`;
    const extraBox = document.getElementById('ai-test-extra');
    if (extraBox && overall && level === 'quick') {
        extraBox.innerHTML = `<button class="btn btn-sm btn-outline-primary mt-1" onclick="window._aiTestConnection('full')">
            <i class="fas fa-vial me-1"></i>Run full capability test</button>`;
    }
}

// Show the exact device context the rule test sent (prefiltered by intent).
async function _aiShowTestContext(ev, encPrompt) {
    ev.preventDefault();
    const box = document.getElementById('ai-test-extra');
    if (!box) return;
    box.innerHTML = '<i class="fas fa-spinner fa-spin"></i> loading context…';
    try {
        const d = await (await fetch('/api/ai/context?intent=' + encPrompt)).json();
        box.innerHTML = `
            <div class="fw-bold mt-1">Device context sent to the model
                (${d.device_count} device(s), ${d.prompt_chars} prompt chars):</div>
            <pre class="bg-dark text-light p-2 rounded" style="max-height:220px;overflow:auto;font-size:.72rem">${_esc(d.device_context)}</pre>`;
    } catch (e) {
        box.innerHTML = `<div class="text-danger">${_esc(e.message)}</div>`;
    }
}

// DOMAIN-AWARE CHAT

function _aiToggleChat() {
    const el = document.getElementById('ai-chat');
    if (!el) return;
    const showing = el.style.display !== 'none';
    el.style.display = showing ? 'none' : 'block';
    if (!showing) {
        _aiChatLoad();
        const inp = document.getElementById('ai-chat-input');
        if (inp) inp.focus();
    }
}

function _aiRenderChat(messages) {
    const log = document.getElementById('ai-chat-log');
    if (!log) return;
    if (!messages || !messages.length) {
        log.innerHTML = `<div class="text-muted">Ask anything about your setup — device states, why a rule fired, or for an automation idea. Grounded in your live devices and rules.</div>`;
        return;
    }
    log.innerHTML = messages.map(m => {
        const me = m.role === 'user';
        return `<div class="mb-2 d-flex ${me ? 'justify-content-end' : 'justify-content-start'}">
            <div class="px-2 py-1 rounded ${me ? 'bg-primary text-white' : 'bg-light border'}" style="max-width:85%;white-space:pre-wrap">${_esc(m.content)}</div>
        </div>`;
    }).join('');
    log.scrollTop = log.scrollHeight;
}

async function _aiChatLoad() {
    try {
        const data = await (await fetch('/api/ai/chat/history')).json();
        _aiRenderChat(data.messages || []);
    } catch (e) {
        _aiRenderChat([]);
    }
}

async function _aiChatSend() {
    const inp = document.getElementById('ai-chat-input');
    const btn = document.getElementById('ai-chat-send');
    const log = document.getElementById('ai-chat-log');
    if (!inp || !inp.value.trim()) return;
    const msg = inp.value.trim();
    inp.value = '';

    // Optimistic echo + typing indicator.
    if (log) {
        if (log.querySelector('.text-muted') && log.children.length === 1) log.innerHTML = '';
        log.insertAdjacentHTML('beforeend',
            `<div class="mb-2 d-flex justify-content-end"><div class="px-2 py-1 rounded bg-primary text-white" style="max-width:85%;white-space:pre-wrap">${_esc(msg)}</div></div>
             <div id="ai-chat-typing" class="mb-2 text-muted small"><i class="fas fa-spinner fa-spin"></i> thinking…</div>`);
        log.scrollTop = log.scrollHeight;
    }
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: msg })
        });
        const data = await res.json();
        document.getElementById('ai-chat-typing')?.remove();
        if (data.success) {
            await _aiChatLoad();
        } else if (log) {
            log.insertAdjacentHTML('beforeend',
                `<div class="mb-2 text-danger small"><i class="fas fa-exclamation-triangle me-1"></i>${_esc(data.error || 'Chat failed')}</div>`);
            log.scrollTop = log.scrollHeight;
        }
    } catch (e) {
        document.getElementById('ai-chat-typing')?.remove();
        if (log) log.insertAdjacentHTML('beforeend', `<div class="mb-2 text-danger small">${_esc(e.message)}</div>`);
    } finally {
        if (btn) btn.disabled = false;
        if (inp) inp.focus();
    }
}

async function _aiChatClear() {
    if (!await window.zbmConfirm({
        title: 'Clear chat',
        message: 'Clear chat history?',
        confirmText: 'Clear',
        variant: 'danger'
    })) return;
    try {
        await fetch('/api/ai/chat/history', { method: 'DELETE' });
        _aiRenderChat([]);
    } catch (e) { /* ignore */ }
}

// HOST CAPABILITY CHECK

async function _aiCheckHost() {
    const box = document.getElementById('ai-host');
    if (!box) return;
    box.innerHTML = '<span class="text-muted"><i class="fas fa-spinner fa-spin"></i> Measuring CPU / RAM / GPU…</span>';
    try {
        const h = await (await fetch('/api/ai/host')).json();
        _aiHost = h;
        const v = h.verdict || {};
        const cls = !v.llm_viable ? 'secondary' : v.tier === 'gpu' ? 'success'
            : v.tier === 'cpu' ? 'warning' : 'info';
        const gpu = h.gpu && h.gpu.present
            ? `${h.gpu.name}${h.gpu.vram_total_gb ? ` · ${h.gpu.vram_free_gb ?? h.gpu.vram_total_gb}/${h.gpu.vram_total_gb} GB VRAM` : ''}`
            : 'none detected';
        const fits = (h.recommendations || []).filter(r => r.fits)
            .map(r => `<span class="badge bg-light text-dark border" title="~${r.est_gb} GB ${r.where}">${_esc(r.name)}</span>`).join(' ');
        const ollama = h.backends?.ollama || {};
        const ollamaBadge = ollama.running
            ? '<span class="badge bg-success">Ollama running</span>'
            : ollama.installed
                ? '<span class="badge bg-info text-dark">Ollama installed (stopped)</span>'
                : ollama.installable
                    ? '<span class="badge bg-secondary">Ollama not installed</span>'
                    : '';
        const sglang = h.backends?.sglang?.viable
            ? '<span class="badge bg-success">SGLang viable</span>'
            : `<span class="badge bg-secondary" title="${_esc(h.backends?.sglang?.reason || '')}">SGLang n/a</span>`;
        box.innerHTML = `
            <div class="alert alert-${cls} py-2 mb-2 small">
                <div class="fw-bold">${_esc(v.headline || 'Assessment complete')}</div>
                <div>${_esc(v.detail || '')}</div>
            </div>
            <div class="row g-2 small">
                <div class="col-md-4"><strong>CPU:</strong> ${_esc(h.cpu?.model || '?')}
                    <span class="text-muted">(${h.cpu?.cores_logical} threads)</span></div>
                <div class="col-md-4"><strong>RAM:</strong> ${h.ram?.available_gb}/${h.ram?.total_gb} GB free</div>
                <div class="col-md-4"><strong>GPU:</strong> ${_esc(gpu)}</div>
            </div>
            <div class="mt-2 small"><strong>Backends:</strong> ${ollamaBadge} ${sglang}
                ${h.backends?.container_runtime ? `<span class="badge bg-light text-dark border">${h.backends.container_runtime}</span>` : ''}</div>
            ${fits ? `<div class="mt-2 small"><strong>Fits here:</strong> ${fits}</div>` : ''}
            ${v.recommended_model ? `<div class="mt-1 small text-muted">Recommended: <code>${_esc(v.recommended_model)}</code></div>` : ''}
            <div id="ai-ollama" class="mt-2"></div>
            <div id="ai-sglang" class="mt-2"></div>
        `;
        _aiGateSglang(h);
        _aiOllamaRender();
        _aiSglangRender();
    } catch (e) {
        box.innerHTML = `<div class="text-danger small">${_esc(e.message)}</div>`;
    }
}

// Ollama enablement (privileged actions behind explicit confirm)

async function _aiOllamaRender() {
    const box = document.getElementById('ai-ollama');
    if (!box) return;
    const h = _aiHost || {};
    if (!h.verdict || !h.verdict.llm_viable) { box.innerHTML = ''; return; }
    try {
        const s = await (await fetch('/api/ai/ollama/status')).json();
        if (s.job && s.job.status === 'running') { _aiOllamaRenderJob(s.job); _aiOllamaPoll(); return; }

        let html = '<div class="border rounded p-2 small"><div class="fw-bold mb-1"><i class="fas fa-server me-1"></i>Local Ollama</div>';
        if (!s.mode) {
            box.innerHTML = html + '<div class="text-muted">No container runtime reachable yet. The deploy/upgrade path enables and mounts the host podman socket automatically — run an <strong>Upgrade</strong> (or redeploy) and local AI will light up here. (If it persists, the host likely lacks systemd or podman socket support — use a remote provider instead.)</div></div>';
            return;
        }
        const via = s.mode === 'socket'
            ? `via host socket <code>${_esc(s.runtime)}</code>`
            : `via <code>${_esc(s.runtime)}</code>`;
        const rec = h.verdict.recommended_model;
        const haveModel = rec && (s.models || []).some(m => m.name === rec || (m.name || '').startsWith(rec));
        if (!s.running) {
            const label = s.container_exists ? 'Start Ollama' : 'Install Ollama';
            html += `<div class="mb-1">Status: <span class="badge bg-secondary">not running</span></div>`;
            html += `<button class="btn btn-sm btn-success" onclick="window._aiOllamaInstall(${s.container_exists})"><i class="fas fa-download me-1"></i>${label}</button>`;
            html += `<div class="text-muted mt-1">Starts the <code>ollama</code> container ${via}${s.gpu_passthrough ? ' with GPU passthrough' : ' (CPU only)'}. ZMM will reach it at <code>${_esc(s.base_url || '')}</code>.</div>`;
        } else {
            html += `<div class="mb-1">Status: <span class="badge bg-success">running</span> · ${(s.models || []).length} model(s)</div>`;
            if (s.models && s.models.length) {
                html += '<div class="mb-1">' + s.models.map(m =>
                    `<span class="badge bg-light text-dark border">${_esc(m.name)}${m.size_gb ? ` (${m.size_gb} GB)` : ''}</span>`).join(' ') + '</div>';
            }
            if (rec && !haveModel) {
                html += `<button class="btn btn-sm btn-primary me-1" onclick="window._aiOllamaPull('${_esc(rec)}')"><i class="fas fa-cloud-download-alt me-1"></i>Pull ${_esc(rec)}</button>`;
            }
            if (haveModel) {
                html += `<button class="btn btn-sm btn-success" onclick="window._aiOllamaUse('${_esc(rec)}')"><i class="fas fa-plug me-1"></i>Use ${_esc(rec)} for AI</button>`;
            }
        }
        box.innerHTML = html + '</div>';
    } catch (e) {
        box.innerHTML = `<div class="text-danger small">${_esc(e.message)}</div>`;
    }
}

function _aiOllamaRenderJob(j) {
    const box = document.getElementById('ai-ollama');
    if (!box) return;
    const tail = (j.log || []).slice(-6).map(_esc).join('\n');
    const st = j.status === 'running'
        ? `<i class="fas fa-spinner fa-spin"></i> ${_esc(j.action || 'working')}…`
        : j.status === 'done' ? '<span class="text-success">done</span>'
            : '<span class="text-danger">error</span>';
    box.innerHTML = `<div class="border rounded p-2 small">
        <div class="fw-bold mb-1">Ollama ${_esc(j.action || '')} ${st}</div>
        <pre class="mb-0 bg-dark text-light p-2 rounded" style="max-height:140px;overflow:auto;font-size:.72rem">${tail || '…'}</pre>
    </div>`;
}

function _aiOllamaPoll() {
    const tick = async () => {
        try {
            const j = await (await fetch('/api/ai/ollama/job')).json();
            _aiOllamaRenderJob(j);
            if (j.status === 'running') { setTimeout(tick, 2000); }
            else { setTimeout(_aiOllamaRender, 600); }
        } catch (e) { setTimeout(tick, 3000); }
    };
    tick();
}

async function _aiOllamaInstall(exists) {
    if (!await window.zbmConfirm({
        title: 'Start Ollama',
        message: exists
            ? 'Start the existing Ollama container on this host?'
            : 'Start the Ollama container via podman/docker on this host?',
        detail: exists ? undefined : 'This downloads the Ollama image (~hundreds of MB) the first time.',
        confirmText: 'Start'
    })) return;
    try {
        const r = await fetch('/api/ai/ollama/install', { method: 'POST' });
        const d = await r.json();
        if (!r.ok) { window.toast.error(d.detail || d.error || 'Install failed'); return; }
        _aiOllamaPoll();
    } catch (e) { window.toast.error(e.message); }
}

async function _aiOllamaPull(model) {
    if (!await window.zbmConfirm({
        title: 'Download model',
        message: `Download "${model}" into Ollama?`,
        detail: 'This can be several GB and may take a while.',
        confirmText: 'Download'
    })) return;
    try {
        const r = await fetch('/api/ai/ollama/pull', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model })
        });
        const d = await r.json();
        if (!r.ok) { window.toast.error(d.detail || d.error || 'Pull failed'); return; }
        _aiOllamaPoll();
    } catch (e) { window.toast.error(e.message); }
}

async function _aiOllamaUse(model) {
    try {
        // Use the base URL the manager reports (localhost natively, the slirp
        // host gateway when ZMM is containerised).
        let base = 'http://localhost:11434/v1';
        try { const st = await (await fetch('/api/ai/ollama/status')).json(); if (st.base_url) base = st.base_url; } catch { /* default */ }
        const r = await fetch('/api/ai/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: 'ollama', base_url: base, model })
        });
        const d = await r.json();
        _aiConfigured = !!d.configured;
        const badge = document.getElementById('ai-status-badge');
        if (badge) {
            badge.className = `badge ${_aiConfigured ? 'bg-success' : 'bg-secondary'} small`;
            badge.textContent = `AI: ${_aiConfigured ? 'connected' : 'optional'}`;
        }
        // Reflect the applied provider/model/base URL in the settings form —
        // without this the form keeps showing the previous values.
        await _aiLoadSettings();
        window.toast?.success?.(`AI now using ${d.provider}/${d.model}`);
        _aiOllamaRender();
    } catch (e) { window.toast.error(e.message); }
}

// SGLang enablement (GPU-class; only rendered when the assessor says so)

async function _aiSglangRender() {
    const box = document.getElementById('ai-sglang');
    if (!box) return;
    const sg = (_aiHost || {}).backends?.sglang;
    if (!sg || !sg.viable) { box.innerHTML = ''; return; }
    try {
        const s = await (await fetch('/api/ai/sglang/status')).json();
        if (s.job && s.job.status === 'running') { _aiSglangRenderJob(s.job); _aiSglangPoll(); return; }

        let html = `<div class="border rounded p-2 small">
            <div class="fw-bold mb-1"><i class="fas fa-bolt me-1"></i>Local SGLang
                <span class="text-muted fw-normal">(high-throughput GPU serving)</span></div>`;
        if (s.running) {
            html += `<div class="mb-1">Status: <span class="badge bg-success">running</span>
                ${s.model ? ` · <code>${_esc(s.model)}</code>` : ' · <span class="text-muted">starting / loading weights…</span>'}</div>`;
            if (s.model) {
                html += `<button class="btn btn-sm btn-success" onclick="window._aiSglangUse('${_esc(s.model)}')">
                    <i class="fas fa-plug me-1"></i>Use for AI</button>`;
            }
        } else {
            html += `<div class="mb-1">Status: <span class="badge bg-secondary">not running</span></div>
                <div class="input-group input-group-sm mb-1" style="max-width:440px">
                    <input type="text" class="form-control" id="ai-sglang-model"
                        value="Qwen/Qwen2.5-7B-Instruct" placeholder="HF model path, e.g. Qwen/Qwen2.5-7B-Instruct">
                    <button class="btn btn-success" onclick="window._aiSglangInstall()">
                        <i class="fas fa-download me-1"></i>Install &amp; start</button>
                </div>
                <div class="text-muted">Pulls the SGLang image (several GB), then serves the model on :30000
                    (weights download on first boot). ZMM will reach it at <code>${_esc(s.base_url || '')}</code>.</div>`;
        }
        box.innerHTML = html + '</div>';
    } catch (e) {
        box.innerHTML = `<div class="text-danger small">${_esc(e.message)}</div>`;
    }
}

function _aiSglangRenderJob(j) {
    const box = document.getElementById('ai-sglang');
    if (!box) return;
    const tail = (j.log || []).slice(-6).map(_esc).join('\n');
    const st = j.status === 'running'
        ? `<i class="fas fa-spinner fa-spin"></i> ${_esc(j.action || 'working')}…`
        : j.status === 'done' ? '<span class="text-success">done</span>'
            : '<span class="text-danger">error</span>';
    box.innerHTML = `<div class="border rounded p-2 small">
        <div class="fw-bold mb-1">SGLang ${_esc(j.action || '')} ${st}</div>
        <pre class="mb-0 bg-dark text-light p-2 rounded" style="max-height:140px;overflow:auto;font-size:.72rem">${tail || '…'}</pre>
    </div>`;
}

function _aiSglangPoll() {
    const tick = async () => {
        try {
            const j = await (await fetch('/api/ai/sglang/job')).json();
            _aiSglangRenderJob(j);
            if (j.status === 'running') { setTimeout(tick, 2000); }
            else { setTimeout(_aiSglangRender, 600); }
        } catch (e) { setTimeout(tick, 3000); }
    };
    tick();
}

async function _aiSglangInstall() {
    const model = document.getElementById('ai-sglang-model')?.value?.trim();
    if (!model) { window.toast.error('Enter a HuggingFace model path first'); return; }
    if (!await window.zbmConfirm({
        title: 'Start SGLang',
        message: `Start an SGLang container serving "${model}" on this host's GPU?`,
        detail: 'Downloads the SGLang image (several GB) plus the model weights on first boot. This can take a long while.',
        confirmText: 'Start'
    })) return;
    try {
        const r = await fetch('/api/ai/sglang/install', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model })
        });
        const d = await r.json();
        if (!r.ok) { window.toast.error(d.detail || d.error || 'Install failed'); return; }
        _aiSglangPoll();
    } catch (e) { window.toast.error(e.message); }
}

async function _aiSglangUse(model) {
    try {
        let base = 'http://localhost:30000/v1';
        try { const st = await (await fetch('/api/ai/sglang/status')).json(); if (st.base_url) base = st.base_url; } catch { /* default */ }
        const r = await fetch('/api/ai/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: 'sglang', base_url: base, model })
        });
        const d = await r.json();
        _aiConfigured = !!d.configured;
        const badge = document.getElementById('ai-status-badge');
        if (badge) {
            badge.className = `badge ${_aiConfigured ? 'bg-success' : 'bg-secondary'} small`;
            badge.textContent = `AI: ${_aiConfigured ? 'connected' : 'optional'}`;
        }
        await _aiLoadSettings();
        window.toast?.success?.(`AI now using ${d.provider}/${d.model}`);
        _aiSglangRender();
    } catch (e) { window.toast.error(e.message); }
}

// HELP / EXAMPLES

function _aiUseExample(el) {
    const input = document.getElementById('ai-prompt');
    if (input && el) { input.value = el.textContent; input.focus(); }
}

async function _aiShowHelp(ev) {
    if (ev) ev.preventDefault();
    const box = document.getElementById('ai-help');
    if (!box) return;
    if (box.style.display !== 'none') { box.style.display = 'none'; return; }
    box.style.display = 'block';
    box.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Loading…';
    try {
        const data = await (await fetch('/api/ai/nl-help')).json();
        const ex = (data.examples || []).map(e =>
            `<div><code role="button" onclick="window._aiUseExample(this)">${_esc(e)}</code></div>`).join('');
        const devs = (data.devices || []).map(d => `<span class="badge bg-light text-dark border">${_esc(d)}</span>`).join(' ');
        box.innerHTML = `
            <div class="p-2 bg-light rounded">
                <div class="fw-bold mb-1">Example phrasings <span class="text-muted fw-normal">(click to use)</span></div>
                ${ex}
                <div class="fw-bold mt-2 mb-1">Your devices</div>
                <div>${devs || '<span class="text-muted">none</span>'}</div>
                <div class="text-muted mt-2">Patterns: <code>when &lt;device&gt; &lt;state&gt;</code>,
                <code>turn on/off/toggle &lt;device&gt;</code>, <code>set &lt;light&gt; to N%</code>,
                <code>after N minutes</code>, <code>between HH:MM and HH:MM</code>, <code>only if &lt;device&gt; is …</code>,
                <code>otherwise …</code></div>
            </div>`;
    } catch (e) {
        box.innerHTML = `<div class="text-danger small">${_esc(e.message)}</div>`;
    }
}


function _esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

// WINDOW HANDLERS

window._aiGenerate = _aiGenerate;
window._aiSaveRule = _aiSaveRule;
window._aiEditRule = _aiEditRule;
window._aiSaveSettings = _aiSaveSettings;
window._aiProviderChanged = _aiProviderChanged;
window._aiTestConnection = _aiTestConnection;
window._aiShowTestContext = _aiShowTestContext;
window._aiUseExample = _aiUseExample;
window._aiShowHelp = _aiShowHelp;
window._aiCheckHost = _aiCheckHost;
window._aiOpenSettingsTab = _aiOpenSettingsTab;
window._aiOllamaInstall = _aiOllamaInstall;
window._aiOllamaPull = _aiOllamaPull;
window._aiOllamaUse = _aiOllamaUse;
window._aiSglangInstall = _aiSglangInstall;
window._aiSglangUse = _aiSglangUse;
window._aiToggleChat = _aiToggleChat;
window._aiChatSend = _aiChatSend;
window._aiChatClear = _aiChatClear;
window._aiGeneratedRule = null;