/**
 * Device Clusters Tab
 * Location: static/js/modal/clusters.js
 */

import { getClusterName } from './config.js';

export function renderCapsTab(device) {
    if (!device.capabilities) return '<div class="alert alert-warning">No capability data.</div>';
    let html = `<div class="accordion" id="epAccordion">`;
    device.capabilities.forEach((ep, idx) => {
        const inputs = (ep.inputs||[]).map(c => {
            const name = getClusterName(c.id, c.name);
            return `<span class="badge bg-light text-dark border m-1" title="0x${c.id.toString(16)}">${name} (0x${c.id.toString(16)})</span>`;
        }).join('');

        const outputs = (ep.outputs||[]).map(c => {
            const name = getClusterName(c.id, c.name);
            return `<span class="badge bg-light text-dark border m-1" title="0x${c.id.toString(16)}">${name} (0x${c.id.toString(16)})</span>`;
        }).join('');

        html += `
            <div class="accordion-item">
                <h2 class="accordion-header">
                    <button class="accordion-button ${idx !== 0 ? 'collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${ep.id}">
                        Endpoint ${ep.id} <span class="ms-2 badge bg-primary">${ep.profile || '?'}</span>
                    </button>
                </h2>
                <div id="collapse${ep.id}" class="accordion-collapse collapse ${idx === 0 ? 'show' : ''}" data-bs-parent="#epAccordion">
                    <div class="accordion-body">
                        <small class="text-muted d-block mb-2">Input Clusters:</small><div class="d-flex flex-wrap mb-3">${inputs}</div>
                        <small class="text-muted d-block mb-2">Output Clusters:</small><div class="d-flex flex-wrap">${outputs}</div>
                    </div>
                </div>
            </div>`;
    });
    html += `</div>`;
    return html;
}
