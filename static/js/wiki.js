/* ============================================================================
   Docs / Wiki tab — renders the bundled docs/*.md served by /api/wiki.
   Lazy-loads the first time the tab is shown; server renders the markdown.
   ============================================================================ */
(function () {
    'use strict';

    let loaded = false;
    let currentSlug = null;

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    }

    async function loadList() {
        const listEl = document.getElementById('wikiList');
        if (!listEl) return;
        try {
            const res = await fetch('/api/wiki', { credentials: 'same-origin' });
            const data = await res.json();
            const docs = data.docs || [];
            if (!docs.length) {
                listEl.innerHTML =
                    '<div class="text-muted small p-3">No documents are bundled in this build.</div>';
                return;
            }
            listEl.innerHTML = docs.map(d =>
                `<button type="button"
                         class="list-group-item list-group-item-action wiki-doc-link"
                         data-slug="${escapeHtml(d.slug)}">${escapeHtml(d.title)}</button>`
            ).join('');
            listEl.querySelectorAll('.wiki-doc-link').forEach(btn => {
                btn.addEventListener('click', () => openDoc(btn.dataset.slug));
            });
            // Open the first doc by default.
            openDoc(docs[0].slug);
        } catch (e) {
            listEl.innerHTML =
                '<div class="text-danger small p-3">Failed to load the document list.</div>';
        }
    }

    async function openDoc(slug) {
        const contentEl = document.getElementById('wikiContent');
        if (!contentEl) return;
        currentSlug = slug;

        document.querySelectorAll('#wikiList .wiki-doc-link').forEach(b => {
            b.classList.toggle('active', b.dataset.slug === slug);
        });

        contentEl.innerHTML =
            '<div class="text-muted p-4"><span class="scan-spinner"></span> Loading…</div>';
        try {
            const res = await fetch('/api/wiki/' + encodeURIComponent(slug),
                { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            // Server renders markdown with raw-HTML disabled; docs are shipped
            // in the image (trusted), so injecting the rendered HTML is safe.
            contentEl.innerHTML = data.html || '<p class="text-muted">Empty document.</p>';
            // docs/images screenshots aren't bundled in the image, so those refs
            // 404 — hide any image that fails to load rather than showing a
            // broken-image icon.
            contentEl.querySelectorAll('img').forEach(function (img) {
                img.addEventListener('error', function () { img.style.display = 'none'; });
            });
            contentEl.scrollTop = 0;
        } catch (e) {
            contentEl.innerHTML =
                '<div class="text-danger p-4">Could not load this document.</div>';
        }
    }

    // Lazy-load the list the first time the Docs tab is shown.
    document.addEventListener('DOMContentLoaded', () => {
        const tabBtn = document.querySelector('[data-bs-target="#wiki"]');
        if (!tabBtn) return;
        tabBtn.addEventListener('shown.bs.tab', () => {
            if (!loaded) { loaded = true; loadList(); }
        });
    });

    window.zmmWiki = { reload: () => { loaded = true; loadList(); }, openDoc };
})();
