/**
 * Device Control Tab
 * Location: static/js/modal/control.js
 */

import { state } from '../state.js';
import { hasCluster } from './config.js';

export function renderControlTab(device) {
    const s = device.state || {};
    let html = '<div class="row g-3">';
    let controlsFound = false;

    // --- Window Covering (0x0102) ---
    const hasCover = hasCluster(device, 0x0102);
    if (hasCover) {
        controlsFound = true;
        const position = s.position !== undefined ? s.position : 50;
        const isClosed = s.is_closed;

        html += `
        <div class="col-12">
            <div class="card">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-blinds"></i> Window Covering</strong>
                    ${isClosed !== undefined ? (isClosed ? '<span class="badge bg-secondary">Closed</span>' : '<span class="badge bg-success">Open</span>') : ''}
                </div>
                <div class="card-body">
                    <div class="row g-3">
                        <div class="col-12">
                            <label class="form-label small text-muted">Actions</label>
                            <div class="btn-group w-100">
                                <button type="button" class="btn btn-outline-success" onclick="window.sendCommand('${device.ieee}', 'open')"><i class="fas fa-arrow-up"></i> Open</button>
                                <button type="button" class="btn btn-outline-danger" onclick="window.sendCommand('${device.ieee}', 'stop')"><i class="fas fa-stop"></i> Stop</button>
                                <button type="button" class="btn btn-outline-secondary" onclick="window.sendCommand('${device.ieee}', 'close')"><i class="fas fa-arrow-down"></i> Close</button>
                            </div>
                        </div>
                        <div class="col-12">
                            <label class="form-label small text-muted">Position: ${position}%</label>
                            <input type="range" class="form-range" min="0" max="100" value="${position}"
                                   onchange="window.sendCommand('${device.ieee}', 'position', this.value)">
                            <div class="d-flex justify-content-between small text-muted">
                                <span>Closed (0%)</span>
                                <span>Open (100%)</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    }

    // --- Thermostat (0x0201) ---
    const hasThermostat = hasCluster(device, 0x0201);
    if (hasThermostat) {
        controlsFound = true;
        const tempKeys = ['internal_temperature', 'temperature', 'local_temperature'];
        let validTemp = null;

        for (const key of tempKeys) {
            const val = s[key];
            if (val !== undefined && val !== null && Number(val) !== 0) {
                validTemp = val;
                break;
            }
        }
        if (validTemp === null) {
             for (const key of tempKeys) {
                if (s[key] !== undefined && s[key] !== null) {
                    validTemp = s[key];
                    break;
                }
            }
        }

        const currentTemp = (validTemp !== null && Number(validTemp) !== 0) ? Number(validTemp).toFixed(1) : "--";
        const rawTarget = s.occupied_heating_setpoint || s.heating_setpoint || 20;
        const targetTemp = Number(rawTarget).toFixed(1);
        const systemMode = s.system_mode || 'off';
        const runningState = s.running_state || 0;
        const piDemand = s.pi_heating_demand || 0;
        const battery = s.battery || 0;

        const modeMap = {
            0: 'Off', 1: 'Auto', 3: 'Cool', 4: 'Heat',
            'off': 'Off', 'auto': 'Auto', 'cool': 'Cool', 'heat': 'Heat'
        };
        const currentModeName = modeMap[systemMode] || systemMode;
        const isHeating = (runningState & 0x0001) || (String(runningState).includes("heat"));

        html += `
        <div class="col-12">
            <div class="card">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <strong><i class="fas fa-thermometer-half"></i> Thermostat</strong>
                    <div>
                        ${isHeating
                            ? '<span class="badge bg-danger"><i class="fas fa-fire"></i> Heating</span>'
                            : '<span class="badge bg-secondary"><i class="fas fa-pause"></i> Idle</span>'}
                        ${battery > 0 && battery < 20
                            ? `<span class="badge bg-warning text-dark ms-1"><i class="fas fa-battery-quarter"></i> ${battery}%</span>`
                            : ''}
                    </div>
                </div>
                <div class="card-body">
                    <div class="row g-3">
                        <div class="col-md-6">
                            <div class="text-center p-3 bg-light rounded">
                                <small class="text-muted d-block mb-1">Current</small>
                                <h2 class="mb-0">${currentTemp}°C</h2>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <div class="text-center p-3 bg-primary bg-opacity-10 rounded">
                                <small class="text-muted d-block mb-1">Target</small>
                                <h2 class="mb-0 text-primary">${targetTemp}°C</h2>
                            </div>
                        </div>
                        <div class="col-12">
                            <label class="form-label fw-bold"><i class="fas fa-cog"></i> Mode</label>
                            <select id="hvac-mode-${device.ieee}" class="form-select"
                                    onchange="window.setHvacMode('${device.ieee}', this.value)">
                                <option value="off" ${String(systemMode).toLowerCase() === 'off' ? 'selected' : ''}>Off</option>
                                <option value="heat" ${String(systemMode).toLowerCase() === 'heat' ? 'selected' : ''}>Heat</option>
                                <option value="auto" ${String(systemMode).toLowerCase() === 'auto' ? 'selected' : ''}>Auto</option>
                            </select>
                        </div>
                        <div class="col-12">
                            <label class="form-label fw-bold"><i class="fas fa-temperature-high"></i> Setpoint</label>
                            <div class="input-group">
                                <button class="btn btn-outline-secondary" type="button" onclick="window.adjustThermostat('${device.ieee}', -0.5)"><i class="fas fa-minus"></i></button>
                                <input type="number" id="thermostat-setpoint-${device.ieee}" class="form-control text-center fw-bold" value="${targetTemp}" min="5" max="35" step="0.5" style="font-size: 1.1rem;">
                                <span class="input-group-text">°C</span>
                                <button class="btn btn-outline-secondary" type="button" onclick="window.adjustThermostat('${device.ieee}', 0.5)"><i class="fas fa-plus"></i></button>
                                <button class="btn btn-primary" type="button" onclick="window.setThermostatTemp('${device.ieee}')"><i class="fas fa-check"></i> Set</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    }

    // --- On/Off, Level, Color Clusters ---
    if (device.capabilities && Array.isArray(device.capabilities)) {
        device.capabilities.forEach(ep => {
            const epId = ep.id;
            const clusters = (ep.inputs || []).concat(ep.outputs || []);
            const hasOnOff = clusters.some(c => c.id === 0x0006);
            const hasLevel = clusters.some(c => c.id === 0x0008);
            const hasColor = clusters.some(c => c.id === 0x0300);

            if (hasOnOff || hasLevel || hasColor) {
                controlsFound = true;
                let isOn = s[`on_${epId}`] !== undefined ? s[`on_${epId}`] : (epId === 1 ? s.on : false);
                let brightness = s[`brightness_${epId}`] !== undefined ? s[`brightness_${epId}`] : (epId === 1 ? s.brightness : 0);
                let colorTemp = s[`color_temp_${epId}`] || (epId === 1 ? s.color_temp : 370);
                let kelvin = colorTemp ? Math.round(1000000 / colorTemp) : 2700;

                html += `
                <div class="col-12 col-md-6 mb-3">
                    <div class="card h-100">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <strong><i class="fas fa-lightbulb text-warning"></i> Light (EP${epId})</strong>
                            ${isOn ? '<span class="badge bg-success">ON</span>' : '<span class="badge bg-secondary">OFF</span>'}
                        </div>
                        <div class="card-body">`;

                if (hasOnOff) {
                    html += `
                        <div class="mb-3">
                            <label class="form-label small text-muted">Power</label>
                            <div class="btn-group w-100">
                                <button type="button" class="btn btn-success" onclick="window.sendCommand('${device.ieee}', 'on', null, ${epId})">On</button>
                                <button type="button" class="btn btn-secondary" onclick="window.sendCommand('${device.ieee}', 'off', null, ${epId})">Off</button>
                                <button type="button" class="btn btn-outline-primary" onclick="window.sendCommand('${device.ieee}', 'toggle', null, ${epId})">Toggle</button>
                            </div>
                        </div>`;
                }

                if (hasLevel) {
                    html += `
                        <div class="mb-3">
                            <label class="form-label small text-muted">Brightness: ${brightness}%</label>
                            <input type="range" class="form-range" min="0" max="100" value="${brightness}"
                                   onchange="window.sendCommand('${device.ieee}', 'brightness', this.value, ${epId})">
                        </div>`;
                }

                if (hasColor) {
                    html += `
                        <div class="mb-3">
                            <label class="form-label small text-muted">Color Temp: ${kelvin}K</label>
                            <input type="range" class="form-range" min="2000" max="6500" value="${kelvin}"
                                   style="background: linear-gradient(to right, #ffae00, #ffead1, #fff, #d1eaff, #99ccff);"
                                   onchange="window.sendCommand('${device.ieee}', 'color_temp', this.value, ${epId})">
                        </div>`;
                }

                html += `</div></div></div>`;
            }
        });
    }

    if (!controlsFound) {
        if (s.state !== undefined || s.on !== undefined) {
             html += `
                <div class="col-12"><div class="card"><div class="card-body">
                    <h6>Legacy Power Control</h6>
                    <button class="btn btn-success" onclick="window.sendCommand('${device.ieee}', 'on')">On</button>
                    <button class="btn btn-secondary" onclick="window.sendCommand('${device.ieee}', 'off')">Off</button>
                </div></div></div>
             `;
        } else {
            html += `<div class="col-12"><div class="alert alert-info">No interactive controls found for this device.</div></div>`;
        }
    }
    html += '</div>';
    return html;
}

// Global Helpers for Control Tab
window.adjustThermostat = function(ieee, delta) {
    const input = document.getElementById(`thermostat-setpoint-${ieee}`);
    if (input) {
        const currentVal = parseFloat(input.value) || 20;
        const newVal = currentVal + delta;
        input.value = Math.max(5, Math.min(35, newVal)).toFixed(1);
    }
};

window.setThermostatTemp = async function(ieee) {
    const input = document.getElementById(`thermostat-setpoint-${ieee}`);
    if (!input) {
        console.error('Thermostat input not found');
        return;
    }
    const temp = parseFloat(input.value);
    if (isNaN(temp) || temp < 5 || temp > 35) {
        alert('Invalid temperature. Must be between 5°C and 35°C');
        return;
    }
    try {
        await window.sendCommand(ieee, 'temperature', temp);
        console.log(`✓ Temperature set to ${temp}°C`);
    } catch (error) {
        console.error('Failed to set temperature:', error);
        alert('Failed to set temperature: ' + error.message);
    }
};

window.setHvacMode = async function(ieee, mode) {
    try {
        await window.sendCommand(ieee, 'system_mode', mode);
        console.log(`✓ HVAC mode set to ${mode}`);
    } catch (error) {
        console.error('Failed to set HVAC mode:', error);
        alert('Failed to set HVAC mode: ' + error.message);
    }
};
