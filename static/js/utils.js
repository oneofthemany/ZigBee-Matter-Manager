/**
 * Utility Functions
 * General-purpose helpers used across the application
 */

/**
 * Get icon for device type
 */
export function getTypeIcon(type) {
    if (type === 'Coordinator') return '<i class="fas fa-network-wired text-primary" title="Coordinator"></i>';
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
 * Update all last-seen time displays
 */
export function updateLastSeenTimes() {
    document.querySelectorAll('.last-seen').forEach(cell => {
        const ts = parseInt(cell.getAttribute('data-ts'));
        if (ts) {
            cell.innerText = timeAgo(ts);
        }
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
