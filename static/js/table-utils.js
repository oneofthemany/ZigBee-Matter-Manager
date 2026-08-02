/**
 * Shared table layer — every table goes through here (the analogue of
 * chart-utils.js).
 *
 * One comparator reused by click-sort and the data-level sorts, delegated
 * click-to-sort that survives re-renders, a delegated text filter, and
 * consistent sticky headers via table.css. Markup contract: docs/structure.md.
 */

// Comparators — single source of truth.

/**
 * Compare two raw values under a column type. Blanks always sort last.
 * @param {*} a @param {*} b
 * @param {'string'|'number'|'date'|'boolean'} [type='string']
 * @returns {number} negative / 0 / positive
 */
export function compareValues(a, b, type = 'string') {
    if (type === 'number') {
        const na = parseFloat(a), nb = parseFloat(b);
        const aok = !Number.isNaN(na), bok = !Number.isNaN(nb);
        if (!aok && !bok) return 0;
        if (!aok) return 1;
        if (!bok) return -1;
        return na - nb;
    }
    if (type === 'boolean') {
        const tb = (v) => (v === true || v === 'true' || v === 1 || v === '1') ? 1 : 0;
        return tb(a) - tb(b);
    }
    if (type === 'date') {
        const da = Date.parse(a), db = Date.parse(b);
        const aok = !Number.isNaN(da), bok = !Number.isNaN(db);
        if (!aok && !bok) return 0;
        if (!aok) return 1;
        if (!bok) return -1;
        return da - db;
    }
    // string — case-insensitive natural sort ("dev2" < "dev10")
    return String(a ?? '').localeCompare(String(b ?? ''), undefined, {
        numeric: true,
        sensitivity: 'base',
    });
}

/**
 * Build a row/object comparator from a value getter.
 * @param {(item:any)=>any} getter
 * @param {string} type @param {'asc'|'desc'} [dir='asc']
 */
export function makeComparator(getter, type, dir = 'asc') {
    const sign = dir === 'desc' ? -1 : 1;
    return (x, y) => sign * compareValues(getter(x), getter(y), type);
}

// DOM click-to-sort

function headerCells(table) {
    const headRow = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
    return headRow ? Array.from(headRow.cells) : [];
}

function cellValue(row, idx) {
    const cell = row.cells[idx];
    if (!cell) return '';
    return cell.dataset.sortValue !== undefined ? cell.dataset.sortValue : cell.textContent.trim();
}

function sortTableByIndex(table, idx, dir) {
    const tbody = table.tBodies[table.tBodies.length - 1];
    if (!tbody) return;

    const heads = headerCells(table);
    const headCount = heads.length;
    const type = heads[idx]?.dataset.sortType || 'string';

    // Only sort plain data rows: skip placeholder/colspan rows ("No data"),
    // expandable detail rows, and anything tagged data-no-sort.
    const rows = Array.from(tbody.rows).filter(r =>
        r.dataset.noSort === undefined &&
        r.cells.length === headCount &&
        !r.querySelector('[colspan]')
    );
    rows.sort((a, b) => {
        const r = compareValues(cellValue(a, idx), cellValue(b, idx), type);
        return dir === 'desc' ? -r : r;
    });
    rows.forEach(r => tbody.appendChild(r));   // re-append in order (moves nodes)

    table.dataset.sortIndex = String(idx);
    table.dataset.sortDir = dir;
    heads.forEach((c, i) => {
        c.classList.toggle('tbl-sort-asc', i === idx && dir === 'asc');
        c.classList.toggle('tbl-sort-desc', i === idx && dir === 'desc');
    });
}

/**
 * Re-apply a table's current sort. Call this after a table that re-renders its
 * own rows (e.g. a polling stats table) rebuilds its <tbody>, so the user's
 * chosen sort survives the refresh.
 * @param {HTMLTableElement} table
 */
export function reapplySort(table) {
    if (!table || table.dataset.sortIndex === undefined) return;
    sortTableByIndex(table, parseInt(table.dataset.sortIndex, 10), table.dataset.sortDir || 'asc');
}

function onHeaderClick(e) {
    const th = e.target.closest('table.tbl-sortable > thead th');
    if (!th || th.dataset.noSort !== undefined) return;
    const table = th.closest('table');
    const idx = th.cellIndex;
    const same = parseInt(table.dataset.sortIndex ?? '-1', 10) === idx;
    const dir = (same && table.dataset.sortDir === 'asc') ? 'desc' : 'asc';
    sortTableByIndex(table, idx, dir);
}

// Delegated text filter

function filterTable(table, query) {
    const tbody = table && table.tBodies[table.tBodies.length - 1];
    if (!tbody) return;
    const q = query.trim().toLowerCase();
    Array.from(tbody.rows).forEach(r => {
        if (r.dataset.noSort !== undefined) return;
        r.style.display = (!q || r.textContent.toLowerCase().includes(q)) ? '' : 'none';
    });
}

function onFilterInput(e) {
    const input = e.target.closest('input.tbl-filter');
    if (!input) return;
    const sel = input.dataset.tblTarget;
    const table = sel
        ? document.querySelector(sel)
        : input.closest('.card, .tab-pane, body')?.querySelector('table.tbl');
    if (table) filterTable(table, input.value);
}

// Init (attach the two delegated listeners once)

let _initialised = false;

export function initTables() {
    if (_initialised) return;
    _initialised = true;
    document.addEventListener('click', onHeaderClick);
    document.addEventListener('input', onFilterInput);
}
