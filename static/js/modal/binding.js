/**
 * Device Binding Tab
 * Location: static/js/modal/binding.js
 */

import { state } from '../state.js';

export function renderBindingTab(device) {
    const outputClusters = [];
    if (device.capabilities) {
        device.capabilities.forEach(ep => {
            if (ep.outputs) {
                ep.outputs.forEach(c => {
                    outputClusters.push({
                        id: c.id,
                        name: `${c.name} (0x${c.id.toString(16)})`,
                        ep: ep.id
                    });
                });
            }
        });
    }

    if (outputClusters.length === 0) {
        return `<div class="alert alert-warning">This device has no output clusters to bind. It cannot control other devices directly.</div>`;
    }

    const targets = Object.values(state.deviceCache)
        .filter(d => d.ieee !== device.ieee) // Exclude self
        .sort((a, b) => (a.friendly_name || a.ieee).localeCompare(b.friendly_name || b.ieee));

    const targetOptions = targets.map(t =>
        `<option value="${t.ieee}">${t.friendly_name} (${t.ieee})</option>`
    ).join('');

    const clusterOptions = outputClusters.map(c =>
        `<option value="${c.id}">EP${c.ep}: ${c.name}</option>`
    ).join('');

    return `
        <div class="card">
            <div class="card-header bg-light">
                <i class="fas fa-link"></i> Bind to another device
            </div>
            <div class="card-body">
                <p class="small text-muted">
                    Binding allows this device (Source) to control another device (Target) directly,
                    without needing the hub. e.g., Thermostat -> Receiver.
                </p>
                <form onsubmit="event.preventDefault(); window.bindDevices('${device.ieee}', this.target_ieee.value, this.cluster_id.value)">
                    <div class="mb-3">
                        <label class="form-label fw-bold small">1. Source Cluster (From this device)</label>
                        <select class="form-select" name="cluster_id" required>
                            ${clusterOptions}
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label fw-bold small">2. Target Device</label>
                        <select class="form-select" name="target_ieee" required>
                            <option value="">Select a device...</option>
                            ${targetOptions}
                        </select>
                    </div>
                    <div class="text-end">
                        <button type="submit" id="bindBtn" class="btn btn-primary">
                            <i class="fas fa-link"></i> Bind Devices
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
}
