/**
 * Shared dialog layer — replaces native confirm()/prompt().
 *
 * One lazily-created Bootstrap modal serves every request; calls are serialised
 * so overlapping requests queue instead of clobbering each other.
 *
 *   import { confirmDialog, promptDialog } from './dialogs.js';
 *   await confirmDialog({ title, message, variant });   // -> true/false
 *   await promptDialog({ title, label, value });        // -> string or null
 *
 * Classic scripts use window.zbmConfirm / window.zbmPrompt. Both resolve on the
 * same contract as the natives.
 */

let modalEl = null;
let bsModal = null;
let queue = Promise.resolve();

function ensureModal() {
    if (modalEl && document.body.contains(modalEl)) {
        // Re-append so the dialog is last in <body>: Bootstrap gives every
        // modal the same z-index, so paint order follows DOM order. Without
        // this, a dialog launched from inside a modal created later (e.g. the
        // editor's batch-deploy confirm) renders *underneath* it.
        document.body.appendChild(modalEl);
        return modalEl;
    }

    modalEl = document.createElement('div');
    modalEl.className = 'modal fade';
    modalEl.tabIndex = -1;
    modalEl.setAttribute('aria-hidden', 'true');
    modalEl.innerHTML = `
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header py-2">
                    <h5 class="modal-title fs-6" id="zbmDialogTitle"></h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <div id="zbmDialogMessage" class="mb-0" style="white-space: pre-line;"></div>
                    <div id="zbmDialogDetail" class="text-muted small mt-2" style="white-space: pre-line;"></div>
                    <div id="zbmDialogInputWrap" class="mt-2">
                        <label id="zbmDialogInputLabel" class="form-label small" for="zbmDialogInput"></label>
                        <input id="zbmDialogInput" class="form-control" autocomplete="off">
                    </div>
                </div>
                <div class="modal-footer py-2">
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal" id="zbmDialogCancel"></button>
                    <button type="button" class="btn btn-sm" id="zbmDialogOk"></button>
                </div>
            </div>
        </div>`;
    document.body.appendChild(modalEl);
    bsModal = new bootstrap.Modal(modalEl, { backdrop: true, keyboard: true });
    return modalEl;
}

function runDialog({ title, message, detail, confirmText, cancelText, variant, input }) {
    return new Promise((resolve) => {
        ensureModal();

        const $ = (id) => modalEl.querySelector('#' + id);
        $('zbmDialogTitle').textContent = title || (input ? 'Input required' : 'Are you sure?');
        $('zbmDialogMessage').textContent = message || '';
        $('zbmDialogMessage').style.display = message ? '' : 'none';
        $('zbmDialogDetail').textContent = detail || '';
        $('zbmDialogDetail').style.display = detail ? '' : 'none';

        const okBtn = $('zbmDialogOk');
        okBtn.className = 'btn btn-sm ' + (variant === 'danger' ? 'btn-danger' : 'btn-primary');
        okBtn.textContent = confirmText || (input ? 'OK' : 'Confirm');
        $('zbmDialogCancel').textContent = cancelText || 'Cancel';

        const wrap = $('zbmDialogInputWrap');
        const field = $('zbmDialogInput');
        if (input) {
            wrap.style.display = '';
            $('zbmDialogInputLabel').textContent = input.label || '';
            $('zbmDialogInputLabel').style.display = input.label ? '' : 'none';
            field.type = input.type || 'text';
            field.value = input.value != null ? String(input.value) : '';
            field.placeholder = input.placeholder || '';
        } else {
            wrap.style.display = 'none';
        }

        // Resolve ONLY after the modal has fully finished hiding. If we
        // resolved on the OK click, the queue would advance and the next
        // chained dialog's show() would collide with this one's close
        // animation — Bootstrap drops it and the flow silently stalls
        // (e.g. Upgrade's discard→swap double confirm). Record the outcome
        // here, resolve in onHidden.
        let confirmed = false;
        let enteredValue = null;

        const onOk = () => {
            confirmed = true;
            if (input) enteredValue = field.value;
            bsModal.hide();
        };
        const onKeydown = (e) => {
            if (e.key === 'Enter' && input) {
                e.preventDefault();
                onOk();
            }
        };
        const onHidden = () => {
            okBtn.removeEventListener('click', onOk);
            field.removeEventListener('keydown', onKeydown);
            modalEl.removeEventListener('hidden.bs.modal', onHidden);
            modalEl.style.zIndex = '';
            // Bootstrap drops body.modal-open whenever ANY modal hides;
            // restore it if an underlying modal is still open so its
            // scrolling keeps working.
            if (document.querySelector('.modal.show')) {
                document.body.classList.add('modal-open');
            }
            if (input) resolve(confirmed ? enteredValue : null);
            else resolve(confirmed);
        };

        okBtn.addEventListener('click', onOk);
        field.addEventListener('keydown', onKeydown);
        modalEl.addEventListener('hidden.bs.modal', onHidden);
        modalEl.addEventListener('shown.bs.modal', () => {
            (input ? field : okBtn).focus();
            if (input) field.select();
        }, { once: true });

        // If another modal is already open, lift this dialog above it (all
        // Bootstrap modals share z-index 1055) and, once shown, raise its
        // backdrop to sit just beneath so the underlying modal is dimmed.
        if (document.querySelector('.modal.show')) {
            modalEl.style.zIndex = '1080';
            modalEl.addEventListener('shown.bs.modal', () => {
                const bd = document.querySelectorAll('.modal-backdrop');
                const last = bd[bd.length - 1];
                if (last) last.style.zIndex = '1079';
            }, { once: true });
        }

        bsModal.show();
    });
}

/** Ask a yes/no question. Resolves true (confirm) or false (cancel/Esc). */
export function confirmDialog(opts = {}) {
    if (typeof opts === 'string') opts = { message: opts };
    const run = () => runDialog(opts);
    const p = queue.then(run, run);
    queue = p.then(() => {}, () => {});
    return p;
}

/** Ask for a text value. Resolves the string, or null on cancel/Esc. */
export function promptDialog(opts = {}) {
    if (typeof opts === 'string') opts = { message: opts };
    const { label, value, placeholder, type, ...rest } = opts;
    const run = () => runDialog({ ...rest, input: { label, value, placeholder, type } });
    const p = queue.then(run, run);
    queue = p.then(() => {}, () => {});
    return p;
}

// Globals for classic (non-module) scripts and inline onclick handlers
window.zbmConfirm = confirmDialog;
window.zbmPrompt = promptDialog;
