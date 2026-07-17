/* ============================================================
   ZMM Auth — Users, Groups, Tokens settings panel
   ============================================================
   Renders into an element with id="auth-settings-host".
   Call window.initAuthSettings() when the Settings tab is shown.
   ============================================================ */

(function () {
    'use strict';

    var HOST_ID = 'auth-settings-host';
    var state = {
        users: [],
        groups: [],
        scopes: [],
        tokens: [],
        view: 'users',     // 'users' | 'groups' | 'tokens'
    };

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function refresh() {
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');
        try {
            if (isAdmin) {
                var [u, g, s, t] = await Promise.all([
                    fetch('/api/auth/users').then(r => r.json()),
                    fetch('/api/auth/groups').then(r => r.json()),
                    fetch('/api/auth/scopes').then(r => r.json()),
                    fetch('/api/auth/tokens').then(r => r.json()),
                ]);
                state.users = u.users || [];
                state.groups = g.groups || [];
                state.scopes = s.scopes || [];
                state.tokens = t.tokens || [];
                // Presence lives in its own registry; we need it to show whether
                // each user's phone reporting is wired up.
                await loadPresenceUsers();
            } else {
                // Non-admins see only their own tokens + scope reference
                var [s2, t2] = await Promise.all([
                    fetch('/api/auth/scopes').then(r => r.json()),
                    fetch('/api/auth/tokens').then(r => r.json()),
                ]);
                state.scopes = s2.scopes || [];
                state.tokens = t2.tokens || [];
                state.users = [];
                state.groups = [];
            }
        } catch (e) {
            zmmLog('auth-settings').error('[auth-settings] refresh failed', e);
        }
        render();
    }

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');

        var tabs = isAdmin
            ? ['users', 'groups', 'tokens']
            : ['tokens'];

        host.innerHTML =
        '<div class="card mb-3">' +
          '<div class="card-header d-flex justify-content-between align-items-center">' +
            '<strong><i class="fas fa-users-cog"></i> Users, Groups & Tokens</strong>' +
            (auth && auth.whoami() ?
              '<span class="text-muted small">Signed in as <code>' + escape(auth.whoami().username) + '</code> ' +
              '<button class="btn btn-link btn-sm p-0 ms-2" id="auth-logout-link">log out</button></span>' : '') +
          '</div>' +
          '<div class="card-body">' +
            '<ul class="nav nav-tabs mb-3">' +
              tabs.map(function (t) {
                  return '<li class="nav-item">' +
                    '<a class="nav-link' + (state.view === t ? ' active' : '') + '" ' +
                    'data-tab="' + t + '" href="#">' +
                    t.charAt(0).toUpperCase() + t.slice(1) + '</a></li>';
              }).join('') +
            '</ul>' +
            '<div id="auth-tab-body"></div>' +
          '</div>' +
        '</div>';

        var ll = document.getElementById('auth-logout-link');
        if (ll) ll.onclick = function (e) { e.preventDefault(); auth.logout(); };

        document.querySelectorAll('[data-tab]').forEach(function (a) {
            a.onclick = function (e) {
                e.preventDefault();
                state.view = a.getAttribute('data-tab');
                render();
            };
        });

        var body = document.getElementById('auth-tab-body');
        if (state.view === 'users') body.innerHTML = renderUsers();
        else if (state.view === 'groups') body.innerHTML = renderGroups();
        else body.innerHTML = renderTokens();

        bindActions();
    }

    // ----------------------------------------------------------
    // Users tab
    // ----------------------------------------------------------
    function renderUsers() {
        var rows = state.users.map(function (u) {
            return '<tr>' +
              '<td><strong>' + escape(u.username) + '</strong>' +
                (u.disabled ? ' <span class="badge bg-secondary">disabled</span>' : '') +
                (u.has_password ? '' : ' <span class="badge bg-warning text-dark">no password</span>') +
                (u.landing === 'frames'
                    ? ' <span class="badge bg-primary" title="Opens on the Frames UI">' +
                      '<i class="fas fa-mobile-screen"></i> Frames</span>'
                    : '') +
                (hasPresenceUser(u.username)
                    ? ' <span class="badge bg-success" title="Phone reports presence">' +
                      '<i class="fas fa-location-arrow"></i> Presence</span>'
                    : '') +
              '</td>' +
              '<td>' + (u.groups || []).map(function (g) {
                  return '<span class="badge bg-info me-1">' + escape(g) + '</span>';
              }).join('') + '</td>' +
              '<td><small>' + (u.effective_scopes || []).slice(0, 4).map(escape).join(', ') +
                  ((u.effective_scopes || []).length > 4 ? ' …' : '') +
              '</small></td>' +
              '<td><small class="text-muted">' + escape(u.description || '') + '</small></td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-primary me-1" data-action="user-edit" data-id="' + escape(u.username) + '">' +
                  '<i class="fas fa-edit"></i></button>' +
                '<button class="btn btn-sm btn-outline-danger" data-action="user-delete" data-id="' + escape(u.username) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="user-new">' +
            '<i class="fas fa-plus"></i> New User</button></div>' +
            '<table class="table table-sm align-middle tbl tbl-sortable">' +
            '<thead class="table-light"><tr><th>Username</th><th>Groups</th>' +
            '<th>Scopes (effective)</th><th>Description</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    // ----------------------------------------------------------
    // Groups tab
    // ----------------------------------------------------------
    function renderGroups() {
        var rows = state.groups.map(function (g) {
            return '<tr>' +
              '<td><strong>' + escape(g.name) + '</strong></td>' +
              '<td>' + (g.scopes || []).map(function (s) {
                  return '<span class="badge bg-secondary me-1">' + escape(s) + '</span>';
              }).join('') + '</td>' +
              '<td><small class="text-muted">' + escape(g.description || '') + '</small></td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-primary me-1" data-action="group-edit" data-id="' + escape(g.name) + '">' +
                  '<i class="fas fa-edit"></i></button>' +
                '<button class="btn btn-sm btn-outline-danger" data-action="group-delete" data-id="' + escape(g.name) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="group-new">' +
            '<i class="fas fa-plus"></i> New Group</button></div>' +
            '<table class="table table-sm align-middle tbl tbl-sortable">' +
            '<thead class="table-light"><tr><th>Name</th><th>Scopes</th>' +
            '<th>Description</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    // ----------------------------------------------------------
    // Tokens tab
    // ----------------------------------------------------------
    function renderTokens() {
        var rows = state.tokens.map(function (t) {
            var status = t.revoked
                ? '<span class="badge bg-danger">revoked</span>'
                : (t.expires_at && t.expires_at * 1000 < Date.now()
                    ? '<span class="badge bg-secondary">expired</span>'
                    : '<span class="badge bg-success">active</span>');
            var lastUsed = t.last_used_at
                ? new Date(t.last_used_at * 1000).toLocaleString()
                : '—';
            var expires = t.expires_at
                ? new Date(t.expires_at * 1000).toLocaleDateString()
                : 'never';
            return '<tr>' +
              '<td><strong>' + escape(t.label) + '</strong>' +
                (t.device_id ? '<br><small class="text-muted">device: ' + escape(t.device_id) + '</small>' : '') +
              '</td>' +
              '<td>' + escape(t.user) + '</td>' +
              '<td>' + (t.scopes || []).map(function (s) {
                  return '<span class="badge bg-secondary me-1">' + escape(s) + '</span>';
              }).join('') + '</td>' +
              '<td><small>' + lastUsed + '</small></td>' +
              '<td><small>' + expires + '</small></td>' +
              '<td>' + status + '</td>' +
              '<td class="text-end">' +
                '<button class="btn btn-sm btn-outline-danger" data-action="token-revoke" data-id="' + escape(t.id) + '">' +
                  '<i class="fas fa-trash"></i></button>' +
              '</td>' +
            '</tr>';
        }).join('');

        return '<div class="d-flex justify-content-end mb-2">' +
            '<button class="btn btn-sm btn-primary" data-action="token-new">' +
            '<i class="fas fa-plus"></i> Issue Token</button></div>' +
            '<table class="table table-sm align-middle tbl tbl-sortable">' +
            '<thead class="table-light"><tr><th>Label</th><th>User</th>' +
            '<th>Scopes</th><th>Last Used</th><th>Expires</th><th>Status</th><th></th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>' +
            (state.tokens.length === 0
              ? '<p class="text-muted text-center">No tokens issued yet.</p>'
              : '');
    }

    // ----------------------------------------------------------
    // Actions
    // ----------------------------------------------------------
    function bindActions() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;
        host.querySelectorAll('[data-action]').forEach(function (el) {
            el.onclick = function () {
                var a = el.getAttribute('data-action');
                var id = el.getAttribute('data-id');
                if (a === 'user-new') openUserModal(null);
                else if (a === 'user-edit') openUserModal(id);
                else if (a === 'user-delete') deleteUser(id);
                else if (a === 'group-new') openGroupModal(null);
                else if (a === 'group-edit') openGroupModal(id);
                else if (a === 'group-delete') deleteGroup(id);
                else if (a === 'token-new') openTokenModal();
                else if (a === 'token-revoke') revokeToken(id);
            };
        });
    }

    async function deleteUser(username) {
        if (!await window.zbmConfirm({
            title: 'Delete user',
            message: 'Delete user "' + username + '"?',
            detail: 'This revokes all their tokens.',
            confirmText: 'Delete',
            variant: 'danger'
        })) return;
        var r = await fetch('/api/auth/users/' + encodeURIComponent(username),
            { method: 'DELETE' });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            window.toast.error('Delete failed: ' + (e.detail || r.status));
            return;
        }
        await refresh();
    }

    async function deleteGroup(name) {
        if (!await window.zbmConfirm({
            title: 'Delete group',
            message: 'Delete group "' + name + '"?',
            detail: 'Members will lose its scopes.',
            confirmText: 'Delete',
            variant: 'danger'
        })) return;
        var r = await fetch('/api/auth/groups/' + encodeURIComponent(name),
            { method: 'DELETE' });
        if (!r.ok) { window.toast.error('Delete failed'); return; }
        await refresh();
    }

    async function revokeToken(id) {
        if (!await window.zbmConfirm({
            title: 'Revoke token',
            message: 'Revoke this token?',
            detail: 'The device using it will lose access immediately.',
            confirmText: 'Revoke',
            variant: 'danger'
        })) return;
        var r = await fetch('/api/auth/tokens/' + encodeURIComponent(id),
            { method: 'DELETE' });
        if (!r.ok) { window.toast.error('Revoke failed'); return; }
        await refresh();
    }

    // ----------------------------------------------------------
    // User modal
    // ----------------------------------------------------------
    // Presence users are a SEPARATE registry (/api/presence/users) keyed by
    // user_id. An auth user and a presence user are different records that
    // happen to share a name; this is the only place we link them.
    var presenceUsers = [];

    // Both per-user scopes a phone needs. BOTH matter: the companion app fetches
    // its own home (read) to arm the geofence AND reports fixes (write). And they
    // must be granted EXPLICITLY on the user, because the built-in "users" group
    // grants blanket "presence:read" (which does not scope-match the per-user
    // "presence:read:<u>") and "presence:write:*" — so without these a minimal
    // per-user token can't even be issued.
    function presenceScopes(username) {
        return ['presence:read:' + username, 'presence:write:' + username];
    }

    function hasPresenceUser(username) {
        return presenceUsers.some(function (p) { return p.user_id === username; });
    }

    // presence_users.py requires user_id to be alphanumeric/underscore, but auth
    // usernames also allow '-'. Rather than silently mangle the id (and create a
    // presence record that never matches), refuse and say why.
    function presenceIdOk(username) {
        return /^[A-Za-z0-9_]+$/.test(username || '');
    }

    async function loadPresenceUsers() {
        try {
            var r = await fetch('/api/presence/users', { credentials: 'same-origin' });
            presenceUsers = r.ok ? (await r.json()) : [];
            if (!Array.isArray(presenceUsers)) presenceUsers = presenceUsers.users || [];
        } catch (e) {
            presenceUsers = [];
        }
    }

    function openUserModal(username) {
        var existing = username
            ? state.users.find(function (u) { return u.username === username; })
            : null;
        var u = existing || {
            username: '', groups: [], extra_scopes: [],
            disabled: false, description: '', has_password: false,
            landing: 'manager',
        };

        var groupCheckboxes = state.groups.map(function (g) {
            var checked = (u.groups || []).indexOf(g.name) !== -1;
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="ugrp-' + escape(g.name) + '" value="' + escape(g.name) + '"' +
                (checked ? ' checked' : '') + '>' +
              '<label class="form-check-label" for="ugrp-' + escape(g.name) + '">' +
                escape(g.name) + ' <small class="text-muted">' + escape(g.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="userEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header">' +
                '<h5 class="modal-title">' + (existing ? 'Edit' : 'New') + ' User</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button>' +
              '</div>' +
              '<div class="modal-body">' +
                '<div class="mb-3"><label class="form-label">Username</label>' +
                  '<input id="uname" class="form-control" value="' + escape(u.username) + '"' +
                    (existing ? ' readonly' : '') + '></div>' +
                '<div class="mb-3"><label class="form-label">Password ' +
                  (existing ? '(leave blank to keep)' : '') + '</label>' +
                  '<input id="upass" type="password" class="form-control" autocomplete="new-password"></div>' +
                '<div class="mb-3"><label class="form-label">Groups</label>' +
                  '<div>' + groupCheckboxes + '</div></div>' +
                '<div class="mb-3"><label class="form-label">Extra scopes (comma-separated)</label>' +
                  '<input id="uscopes" class="form-control" value="' +
                    escape((u.extra_scopes || []).join(', ')) + '">' +
                  '<div class="form-text">Direct grants beyond group membership.</div></div>' +
                '<div class="mb-3"><label class="form-label">Description</label>' +
                  '<input id="udesc" class="form-control" value="' + escape(u.description || '') + '"></div>' +
                '<div class="mb-3"><label class="form-label" for="ulanding">Opens on</label>' +
                  '<select id="ulanding" class="form-select">' +
                    '<option value="manager"' + (u.landing !== 'frames' ? ' selected' : '') + '>' +
                      'Manager — the full dashboard</option>' +
                    '<option value="frames"' + (u.landing === 'frames' ? ' selected' : '') + '>' +
                      'Frames — the mobile UI</option>' +
                  '</select>' +
                  '<div class="form-text">Where this user lands when they open the app. ' +
                    'A convenience, not a restriction — both stay reachable.</div></div>' +
                '<div class="mb-3">' +
                  '<div class="form-check">' +
                    '<input id="upresence" type="checkbox" class="form-check-input"' +
                      (existing && hasPresenceUser(u.username) ? ' checked' : '') +
                      (existing ? '' : ' disabled') + '>' +
                    '<label class="form-check-label" for="upresence">Mobile presence</label>' +
                  '</div>' +
                  '<div class="form-text">' +
                    (existing
                      ? 'Lets this user\'s phone report its location. Creates a presence user and ' +
                        'grants <code>' + escape(presenceScopes(u.username).join('</code> + <code>')) +
                        '</code> — issue the phone\'s token with exactly those two scopes. ' +
                        'Home location and radius are set in the Presence tab.'
                      : 'Save the user first, then re-open to enable presence.') +
                  '</div>' +
                '</div>' +
                (existing ? '<div class="form-check"><input id="udisabled" type="checkbox" class="form-check-input"' +
                    (u.disabled ? ' checked' : '') + '><label class="form-check-label">Disabled</label></div>' : '') +
                '<div id="user-edit-error" class="alert alert-danger mt-3" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-primary" id="user-save-btn">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('userEditModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('userEditModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('user-save-btn').onclick = async function () {
            var groups = [];
            document.querySelectorAll('#userEditModal input[type=checkbox][id^=ugrp-]').forEach(function (cb) {
                if (cb.checked) groups.push(cb.value);
            });
            var scopes = document.getElementById('uscopes').value
                .split(',').map(function (s) { return s.trim(); })
                .filter(Boolean);

            // Mobile presence: keep the per-user presence scope in step with the
            // tick, without disturbing any other scope the admin typed in.
            var presenceBox = document.getElementById('upresence');
            var wantPresence = !!(presenceBox && presenceBox.checked && existing);
            if (existing) {
                var pScopes = presenceScopes(u.username);
                scopes = scopes.filter(function (s) { return pScopes.indexOf(s) === -1; });
                if (wantPresence) {
                    if (!presenceIdOk(u.username)) {
                        showErr('user-edit-error',
                            'Presence needs a username of letters, numbers or underscores — ' +
                            '"' + u.username + '" can\'t be used as a presence id.');
                        return;
                    }
                    pScopes.forEach(function (s) { scopes.push(s); });
                }
            }

            var body = {
                groups: groups,
                extra_scopes: scopes,
                description: document.getElementById('udesc').value,
                landing: document.getElementById('ulanding').value,
            };
            var pw = document.getElementById('upass').value;
            if (pw) body.password = pw;
            if (existing) {
                var dis = document.getElementById('udisabled');
                if (dis) body.disabled = dis.checked;
                var r = await fetch('/api/auth/users/' + encodeURIComponent(u.username),
                    { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r.ok) {
                    var e = await r.json().catch(function () { return {}; });
                    showErr('user-edit-error', e.detail || 'Update failed');
                    return;
                }

                var hadPresence = hasPresenceUser(u.username);
                if (wantPresence && !hadPresence) {
                    // Create ONLY when missing. upsert_user replaces the whole
                    // config from the payload, so re-sending {user_id, name} for
                    // an existing user would wipe their home location/radius.
                    var pr = await fetch('/api/presence/users', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        credentials: 'same-origin',
                        body: JSON.stringify({
                            user_id: u.username,
                            display_name: u.username,
                            enabled: true,
                        }),
                    });
                    if (!pr.ok) {
                        var pe = await pr.json().catch(function () { return {}; });
                        showErr('user-edit-error',
                            'User saved, but presence setup failed: ' +
                            (pe.detail || 'unknown error') +
                            '. The scope was granted; add the presence user in the Presence tab.');
                        await loadPresenceUsers();
                        return;
                    }
                } else if (!wantPresence && hadPresence) {
                    var dr = await fetch('/api/presence/users/' + encodeURIComponent(u.username),
                        { method: 'DELETE', credentials: 'same-origin' });
                    if (!dr.ok) {
                        showErr('user-edit-error',
                            'User saved, but removing the presence user failed. ' +
                            'Remove it in the Presence tab.');
                        await loadPresenceUsers();
                        return;
                    }
                }
                await loadPresenceUsers();
            } else {
                body.username = document.getElementById('uname').value.trim();
                if (pw) body.password = pw;
                var r2 = await fetch('/api/auth/users',
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r2.ok) {
                    var e2 = await r2.json().catch(function () { return {}; });
                    showErr('user-edit-error', e2.detail || 'Create failed');
                    return;
                }
            }
            modal.hide();
            await refresh();
        };
    }

    // ----------------------------------------------------------
    // Group modal
    // ----------------------------------------------------------
    function openGroupModal(name) {
        var existing = name
            ? state.groups.find(function (g) { return g.name === name; })
            : null;
        var g = existing || { name: '', scopes: [], description: '' };

        var scopeOpts = state.scopes.map(function (s) {
            var checked = (g.scopes || []).indexOf(s.name) !== -1;
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="gscp-' + escape(s.name) + '" value="' + escape(s.name) + '"' +
                (checked ? ' checked' : '') + '>' +
              '<label class="form-check-label" for="gscp-' + escape(s.name) + '">' +
                '<code>' + escape(s.name) + '</code> ' +
                '<small class="text-muted">' + escape(s.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="groupEditModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">' +
                (existing ? 'Edit' : 'New') + ' Group</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<div class="mb-3"><label class="form-label">Name</label>' +
                  '<input id="gname" class="form-control" value="' + escape(g.name) + '"' +
                    (existing ? ' readonly' : '') + '></div>' +
                '<div class="mb-3"><label class="form-label">Description</label>' +
                  '<input id="gdesc" class="form-control" value="' + escape(g.description || '') + '"></div>' +
                '<div class="mb-3"><label class="form-label">Scopes</label>' +
                  '<div>' + scopeOpts + '</div></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>' +
                '<button class="btn btn-primary" id="group-save-btn">Save</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('groupEditModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('groupEditModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        document.getElementById('group-save-btn').onclick = async function () {
            var scopes = [];
            document.querySelectorAll('#groupEditModal input[id^=gscp-]').forEach(function (cb) {
                if (cb.checked) scopes.push(cb.value);
            });
            var body = {
                scopes: scopes,
                description: document.getElementById('gdesc').value,
            };
            if (existing) {
                var r = await fetch('/api/auth/groups/' + encodeURIComponent(g.name),
                    { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r.ok) { window.toast.error('Update failed'); return; }
            } else {
                body.name = document.getElementById('gname').value.trim();
                var r2 = await fetch('/api/auth/groups',
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body) });
                if (!r2.ok) { window.toast.error('Create failed'); return; }
            }
            modal.hide();
            await refresh();
        };
    }

    // ----------------------------------------------------------
    // Token issue modal
    // ----------------------------------------------------------
    function openTokenModal() {
        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');
        var meUsername = auth && auth.whoami() ? auth.whoami().username : '';

        var userOpts = isAdmin
            ? state.users.map(function (u) {
                  return '<option value="' + escape(u.username) + '"' +
                      (u.username === meUsername ? ' selected' : '') + '>' +
                      escape(u.username) + '</option>';
              }).join('')
            : '<option value="' + escape(meUsername) + '" selected>' + escape(meUsername) + '</option>';

        var scopeOpts = state.scopes.map(function (s) {
            return '<div class="form-check">' +
              '<input type="checkbox" class="form-check-input" ' +
                'id="tscp-' + escape(s.name) + '" value="' + escape(s.name) + '">' +
              '<label class="form-check-label" for="tscp-' + escape(s.name) + '">' +
                '<code>' + escape(s.name) + '</code> ' +
                '<small class="text-muted">' + escape(s.description) + '</small>' +
              '</label></div>';
        }).join('');

        var html =
        '<div class="modal fade" id="tokenIssueModal" tabindex="-1">' +
          '<div class="modal-dialog modal-lg">' +
            '<div class="modal-content">' +
              '<div class="modal-header"><h5 class="modal-title">Issue Token</h5>' +
                '<button class="btn-close" data-bs-dismiss="modal"></button></div>' +
              '<div class="modal-body">' +
                '<div class="row g-3">' +
                  '<div class="col-md-6"><label class="form-label">For user</label>' +
                    '<select id="tuser" class="form-select">' + userOpts + '</select></div>' +
                  '<div class="col-md-6"><label class="form-label">Label</label>' +
                    '<input id="tlabel" class="form-control" placeholder="e.g. Sean\'s Pixel"></div>' +
                  '<div class="col-md-6"><label class="form-label">Device ID (optional)</label>' +
                    '<input id="tdevice" class="form-control" placeholder="opaque identifier"></div>' +
                  '<div class="col-md-6"><label class="form-label">Expires in (days)</label>' +
                    '<input id="texp" type="number" min="1" max="3650" class="form-control" placeholder="leave blank for no expiry"></div>' +
                  '<div class="col-12"><label class="form-label">Scopes</label>' +
                    '<div class="form-text">Leave all unchecked to inherit the user\'s full scope set. ' +
                      'A token can never hold a scope its user lacks.</div>' +
                    '<div>' + scopeOpts + '</div>' +
                    '<div class="mt-2 alert alert-info small mb-1">' +
                      '<strong>Mobile presence app:</strong> don\'t tick the boxes above — ' +
                      'use the button for a token scoped to just this one user\'s presence.' +
                    '</div>' +
                    '<button type="button" class="btn btn-sm btn-outline-primary mb-2" id="tmobile">' +
                      '<i class="fas fa-mobile-screen"></i> Fill for mobile presence</button>' +
                    '<input id="tcustom" class="form-control" ' +
                      'placeholder="custom scopes, comma-separated (e.g. presence:read:sean, presence:write:sean)">' +
                  '</div>' +
                '</div>' +
                '<div id="token-issue-error" class="alert alert-danger mt-3" style="display:none;"></div>' +
                '<div id="token-issue-result" class="mt-3" style="display:none;"></div>' +
              '</div>' +
              '<div class="modal-footer">' +
                '<button class="btn btn-secondary" data-bs-dismiss="modal">Close</button>' +
                '<button class="btn btn-primary" id="token-issue-btn">Issue</button>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</div>';

        var prev = document.getElementById('tokenIssueModal');
        if (prev) prev.remove();
        document.body.insertAdjacentHTML('beforeend', html);
        var modalEl = document.getElementById('tokenIssueModal');
        var modal = new bootstrap.Modal(modalEl);
        modal.show();

        // One click fills exactly the two per-user scopes the phone needs, for
        // whichever user is selected, and clears any ticked boxes so the token
        // stays minimal. Requires that user to have the matching presence scopes
        // (tick Mobile presence on the user first).
        document.getElementById('tmobile').onclick = function () {
            var u = document.getElementById('tuser').value;
            document.querySelectorAll('#tokenIssueModal input[id^=tscp-]').forEach(function (cb) {
                cb.checked = false;
            });
            document.getElementById('tcustom').value =
                'presence:read:' + u + ', presence:write:' + u;
            if (!document.getElementById('tlabel').value.trim()) {
                document.getElementById('tlabel').value = u + "'s phone";
            }
        };

        document.getElementById('token-issue-btn').onclick = async function () {
            var scopes = [];
            document.querySelectorAll('#tokenIssueModal input[id^=tscp-]').forEach(function (cb) {
                if (cb.checked) scopes.push(cb.value);
            });
            // Custom field is comma-separated: a mobile-presence token needs TWO
            // per-user scopes (read + write), and a single field can't express that.
            document.getElementById('tcustom').value
                .split(',').map(function (s) { return s.trim(); }).filter(Boolean)
                .forEach(function (s) { if (scopes.indexOf(s) === -1) scopes.push(s); });

            var body = {
                username: document.getElementById('tuser').value,
                label: document.getElementById('tlabel').value.trim(),
                device_id: document.getElementById('tdevice').value.trim() || null,
            };
            if (scopes.length > 0) body.scopes = scopes;
            var exp = parseInt(document.getElementById('texp').value);
            if (!isNaN(exp) && exp > 0) body.expires_in_days = exp;

            var r = await fetch('/api/auth/tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                var e = await r.json().catch(function () { return {}; });
                showErr('token-issue-error', e.detail || 'Failed');
                return;
            }
            var d = await r.json();
            // Show plaintext token ONCE
            var box = document.getElementById('token-issue-result');
            box.style.display = '';
            box.innerHTML =
              '<div class="alert alert-warning"><strong>Token issued — copy it now, it will not be shown again.</strong></div>' +
              '<div class="input-group">' +
                '<input id="token-plain" class="form-control font-monospace" readonly value="' + escape(d.token) + '">' +
                '<button class="btn btn-outline-primary" id="token-copy-btn"><i class="fas fa-copy"></i> Copy</button>' +
              '</div>';
            document.getElementById('token-copy-btn').onclick = function () {
                var inp = document.getElementById('token-plain');
                inp.select();
                document.execCommand('copy');
            };
            document.getElementById('token-issue-btn').disabled = true;
            await refresh();
        };
    }

    function showErr(id, msg) {
        var el = document.getElementById(id);
        if (el) { el.textContent = msg; el.style.display = ''; }
    }

    // ----------------------------------------------------------
    // Public init
    // ----------------------------------------------------------
    window.initAuthSettings = function () {
        if (!window.zmmAuth || !window.zmmAuth.whoami()) {
            // Wait until logged in
            window.zmmAuth.onChange(function (p) {
                if (p) refresh();
            });
            return;
        }
        refresh();
    };
})();