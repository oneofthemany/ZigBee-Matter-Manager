/**
 * Utility Functions
 * General-purpose helpers used across the application
 */
const log = zmmLog('utils');


/**
 * Get icon for device type
 */
export function getTypeIcon(type) {
    if (type === 'Coordinator') return '<i class="fas fa-network-wired text-primary" title="Coordinator"></i>';
    if (type === 'Matter') return '<i class="fas fa-atom text-info"></i>';
    if (type === 'AirConditioner') return '<i class="fas fa-snowflake text-info" title="Air conditioner (WiFi)"></i>';
    // Changed fa-wifi to fa-plug to better represent mains-powered devices
    if (type === 'Router') return '<i class="fas fa-plug text-success" title="Router (Mains)"></i>';
    return '<i class="fas fa-battery-three-quarters text-warning" title="End Device (Battery)"></i>';
}

/**
 * Get colored badge for LQI value
 */
export function getLqiBadge(lqi) {
    let color = 'bg-secondary';
    if (lqi > 150) color = 'bg-success';
    else if (lqi > 80) color = 'bg-warning text-dark';
    else if (lqi > 0) color = 'bg-danger';
    return `<span class="badge ${color}">${lqi}</span>`;
}

/**
 * Format timestamp as relative time
 */
export function timeAgo(ts) {
    if (!ts) return "Never";
    const seconds = Math.floor((Date.now() - ts) / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
}

/**
 * Format timestamp as a readable date string
 */
export function formatTime(ts) {
    if (!ts) return "Never";
    return new Date(ts).toLocaleString();
}

/**
 * Update all last-seen displays. Runs once a second, so it stays cheap and
 * non-destructive: bails while the page is hidden, uses textContent (reading
 * innerText forces a synchronous layout — a full reflow every second on a large
 * table), and writes only when the rendered string actually changed, so a cell
 * the user is selecting is not rebuilt under them.
 */
export function updateLastSeenTimes() {
    if (document.hidden) return;
    document.querySelectorAll('.last-seen').forEach(cell => {
        const ts = parseInt(cell.getAttribute('data-ts'));
        if (!ts) return;
        const next = timeAgo(ts);
        if (cell.textContent !== next) cell.textContent = next;
    });
}

/**
 * Get formatted current timestamp string [HH:MM:SS.mmm]
 */
export function getTimestamp() {
    const now = new Date();
    const pad = (n, width = 2) => n.toString().padStart(width, '0');
    return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}.${pad(now.getMilliseconds(), 3)}`;
}


/**
 * Run an async action with the triggering button in a "busy" state:
 * disabled + spinner, restored in finally. Guards against double-submit
 * while a dialog/request is in flight.
 *
 * @param {HTMLButtonElement|null} btn - button to disable (null = just run fn)
 * @param {Function} fn - sync or async action
 * @returns {Promise<*>} resolves with fn's result
 */
export function withBusy(btn, fn) {
    if (!btn) return Promise.resolve().then(fn);
    if (btn.dataset.zbmBusy) return Promise.resolve();  // already in flight
    btn.dataset.zbmBusy = '1';
    const saved = btn.innerHTML;
    const wasDisabled = btn.disabled;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    // Reuse the button's own markup (minus a leading icon) so responsive
    // label spans keep working — textContent would surface hidden spans too
    const busyLabel = saved.replace(/^\s*<i\b[^>]*><\/i>/, '').trim();
    const busyHtml = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> '
        + (busyLabel || '…');
    btn.innerHTML = busyHtml;
    return Promise.resolve()
        .then(fn)
        .finally(() => {
            delete btn.dataset.zbmBusy;
            btn.disabled = wasDisabled;
            btn.removeAttribute('aria-busy');
            // Only restore if nothing else re-rendered the button meanwhile
            if (btn.innerHTML === busyHtml) btn.innerHTML = saved;
        });
}

// Classic (non-module) scripts and inline onclick handlers
window.withBusy = withBusy;

/**
 * Show a toast notification — thin shim over the shared toast system
 * (toasts.js). Kept for the many call sites that use the
 * `showToast(message, type)` argument order.
 * @param {string} message - The message to display
 * @param {string} type - 'success', 'danger'/'error', 'info', 'warning' (default: info)
 */
export function showToast(message, type = 'info') {
    const typeMap = { danger: 'error', error: 'error', success: 'success', warning: 'warning', info: 'info' };
    const fn = window.toast && window.toast[typeMap[type] || 'info'];
    if (fn) fn(message);
    else log.log(`[toast:${type}]`, message);
}

// Several classic scripts and modules feature-detect window.showToast
window.showToast = showToast;

// Color mode toggle
window.showColorMode = function(ieee, epId, mode) {
    const tempPanel = document.getElementById(`colorTempPanel_${epId}`);
    const colorPanel = document.getElementById(`colorPickerPanel_${epId}`);
    if (mode === 'temp') {
        if (tempPanel) tempPanel.style.display = '';
        if (colorPanel) colorPanel.style.display = 'none';
    } else {
        if (tempPanel) tempPanel.style.display = 'none';
        if (colorPanel) colorPanel.style.display = '';
    }
};

// Convert HSL to hex for color picker
window.hslToHex = function(h, s, l) {
    s /= 100;
    l /= 100;
    const a = s * Math.min(l, 1 - l);
    const f = n => {
        const k = (n + h / 30) % 12;
        const color = l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
        return Math.round(255 * color).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
};

// Convert hex to HS
window.hexToHS = function(hex) {
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s;
    const d = max - min;
    s = max === 0 ? 0 : d / max;
    if (max === min) {
        h = 0;
    } else {
        switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            case b: h = (r - g) / d + 4; break;
        }
        h /= 6;
    }
    return { h: Math.round(h * 360), s: Math.round(s * 100) };
};

// Interaction debounce for color controls (prevents modal refresh during use)
let colorInteractionTimeout = null;
function setColorInteractionActive() {
    // Use the shared state if available, otherwise create local flag
    if (window.state) {
        window.state.controlInteractionActive = true;
    }
    if (colorInteractionTimeout) clearTimeout(colorInteractionTimeout);
    colorInteractionTimeout = setTimeout(() => {
        if (window.state) {
            window.state.controlInteractionActive = false;
        }
    }, 1000); // Longer timeout for color picking
}

// Send color from picker (converts hex to HS)
window.sendColorFromPicker = function(ieee, hexColor, epId) {
    setColorInteractionActive();

    const hs = window.hexToHS(hexColor);

    // Optimistic UI update - update saturation slider to match picked color
    const satSlider = document.getElementById(`satSlider_${ieee}_${epId}`);
    if (satSlider) {
        satSlider.value = hs.s;
    }

    // Send command
    window.sendCommand(ieee, 'hs_color', [hs.h, hs.s], epId);
};

// Send HS color command
window.sendHSColor = function(ieee, hue, sat, epId) {
    setColorInteractionActive();

    // Get current values if one is null
    if (hue === null || hue === undefined) {
        const picker = document.getElementById(`colorPicker_${ieee}_${epId}`);
        if (picker) {
            const hs = window.hexToHS(picker.value);
            hue = hs.h;
        } else {
            hue = 0;
        }
    }
    if (sat === null || sat === undefined) {
        const slider = document.getElementById(`satSlider_${ieee}_${epId}`);
        if (slider) {
            sat = parseInt(slider.value);
        } else {
            sat = 100;
        }
    }

    // Optimistic UI update - update color picker to reflect new HS values
    const picker = document.getElementById(`colorPicker_${ieee}_${epId}`);
    if (picker) {
        const newHex = window.hslToHex(hue, sat, 50);
        picker.value = newHex;
    }

    // Send via REST API
    window.sendCommand(ieee, 'hs_color', [hue, sat], epId);
};