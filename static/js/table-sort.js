/**
 * Device Table Sorting
 * The devices table re-renders its rows from a live socket, so it sorts the
 * data array (rather than DOM click-sort) and re-applies on every render.
 * Comparison itself is delegated to the shared compareValues() in
 * table-utils.js so all tables order values identically.
 */

import { compareValues } from './table-utils.js';

/**
 * Sort state management
 */
const sortState = {
    column: null,
    direction: 'asc', // 'asc' or 'desc'
    type: 'string'    // 'string', 'number', 'boolean', 'date'
};

/**
 * Extract sortable value from device object based on column name
 */
function extractValue(device, column) {
    switch(column) {
        case 'type':
            return device.type || '';
        case 'friendly_name':
            return device.friendly_name || '';
        case 'ieee':
            return device.ieee || '';
        case 'manufacturer':
            return device.manufacturer || '';
        case 'model':
            return device.model || '';
        case 'lqi':
            // Handle LQI which might be undefined
            return device.lqi !== undefined ? device.lqi : -1;
        case 'last_seen_ts':
            return device.last_seen_ts || 0;
        case 'available':
            return device.available !== false; // Default to true if undefined
        default:
            return '';
    }
}

/**
 * Sort devices array by specified column and direction
 */
export function sortDevices(devices, column, type, direction) {
    if (!devices || devices.length === 0) return devices;

    const sign = direction === 'desc' ? -1 : 1;
    return [...devices].sort((a, b) =>
        sign * compareValues(extractValue(a, column), extractValue(b, column), type)
    );
}

/**
 * Update sort indicators in table headers
 */
function updateSortIndicators(clickedHeader) {
    // Remove all sort classes from headers
    document.querySelectorAll('.sortable-header').forEach(header => {
        header.classList.remove('sort-asc', 'sort-desc');
    });

    // Add appropriate class to clicked header
    if (sortState.direction === 'asc') {
        clickedHeader.classList.add('sort-asc');
    } else {
        clickedHeader.classList.add('sort-desc');
    }
}

/**
 * Initialize table sorting
 * Attaches click handlers to sortable headers
 */
export function initTableSort(onSortCallback) {
    console.log("Initialising table sort functionality...");

    const headers = document.querySelectorAll('.sortable-header');

    headers.forEach(header => {
        header.addEventListener('click', () => {
            const column = header.getAttribute('data-column');
            const type = header.getAttribute('data-type') || 'string';

            // Toggle direction if clicking same column, otherwise reset to ascending
            if (sortState.column === column) {
                sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
            } else {
                sortState.column = column;
                sortState.type = type;
                sortState.direction = 'asc';
            }

            console.log(`Sorting by ${column} (${type}) in ${sortState.direction} order`);

            // Update visual indicators
            updateSortIndicators(header);

            // Trigger callback to re-render table with sorted data
            if (onSortCallback) {
                onSortCallback(column, type, sortState.direction);
            }
        });
    });

    console.log(`Table sort initialised for ${headers.length} columns`);
}

/**
 * Get current sort state
 */
export function getSortState() {
    return { ...sortState };
}

/**
 * Reset sort state to default
 */
export function resetSortState() {
    sortState.column = null;
    sortState.direction = 'asc';
    sortState.type = 'string';

    // Remove all sort classes
    document.querySelectorAll('.sortable-header').forEach(header => {
        header.classList.remove('sort-asc', 'sort-desc');
    });
}

/**
 * Apply sorting with current state
 * Useful for re-applying sort after data update
 */
export function applySortState(devices) {
    if (!sortState.column) {
        return devices; // No sorting applied
    }

    return sortDevices(devices, sortState.column, sortState.type, sortState.direction);
}