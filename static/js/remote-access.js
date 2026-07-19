/* ============================================================
   ZMM Remote Access — managed Cloudflare Tunnel settings panel
   ============================================================
   Renders into an element with id="remote-access-host"
   (Settings → Security → Remote Access sub-tab).
   Call window.initRemoteAccess() when the tab is shown; the
   module also self-attaches to the sub-tab's shown.bs.tab event.
   ============================================================ */

(function () {
    'use strict';

    var HOST_ID = 'remote-access-host';
    var state = {
        status: null,
        loadFailed: false,
    };

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtUptime(s) {
        if (s == null) return '—';
        s = Math.floor(s);
        var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        if (h > 48) return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
        if (h > 0) return h + 'h ' + m + 'm';
        return m + 'm ' + (s % 60) + 's';
    }

    async function refresh() {
        try {
            var r = await fetch('/api/remote-access/status');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            state.status = await r.json();
            state.loadFailed = false;
        } catch (e) {
            zmmLog('remote-access').error('[remote-access] status fetch failed', e);
            state.loadFailed = true;
        }
        render();
    }

    // ----------------------------------------------------------
    // Rendering
    // ----------------------------------------------------------

    function render() {
        var host = document.getElementById(HOST_ID);
        if (!host) return;

        if (state.loadFailed || !state.status) {
            host.innerHTML =
                '<div class="alert alert-warning">' +
                '<i class="fas fa-exclamation-triangle me-1"></i> ' +
                'Could not load remote access status.</div>';
            return;
        }

        var auth = window.zmmAuth;
        var isAdmin = auth && auth.hasScope('admin');
        var st = state.status;

        // refresh() rebuilds the whole host, so carry the active pane across
        // re-renders (fall back to Status if the pane is gone, e.g. de-admin).
        var paneIds = ['raStatusPane'].concat(isAdmin ? ['raConfigPane'] : []).concat(['raHelpPane']);
        var activeEl = host.querySelector('.tab-pane.active');
        var active = activeEl && paneIds.indexOf(activeEl.id) !== -1 ? activeEl.id : 'raStatusPane';

        function pill(id, icon, label) {
            return '<li class="nav-item">' +
              '<button class="nav-link' + (id === active ? ' active' : '') + '" ' +
                'data-bs-toggle="tab" data-bs-target="#' + id + '">' +
                '<i class="fas ' + icon + ' me-1"></i> <span class="tab-label">' + label + '</span>' +
              '</button></li>';
        }
        function pane(id, html) {
            return '<div class="tab-pane fade' + (id === active ? ' show active' : '') + '" id="' + id + '">' +
                   html + '</div>';
        }

        host.innerHTML =
            '<ul class="nav nav-pills mb-3 zmm-icon-rail" id="raSubNav">' +
              '<li class="nav-item d-md-none rail-toggle-item">' +
                '<button class="nav-link rail-toggle" type="button" title="Toggle tab labels" ' +
                  'aria-label="Toggle tab labels" ' +
                  'onclick="this.closest(\'ul\').classList.toggle(\'labels-expanded\')">' +
                  '<i class="fas fa-text-width"></i></button></li>' +
              pill('raStatusPane', 'fa-globe', 'Status') +
              (isAdmin ? pill('raConfigPane', 'fa-sliders-h', 'Configuration') : '') +
              pill('raHelpPane', 'fa-question-circle', 'Setup Guide') +
            '</ul>' +
            '<div class="tab-content">' +
              pane('raStatusPane',
                   renderStatusCard(st) + (!st.binary_path ? renderInstallCard(st) : '')) +
              (isAdmin ? pane('raConfigPane', renderSettingsCard(st)) : '') +
              pane('raHelpPane', renderHelpCard(st)) +
            '</div>';

        bindActions(isAdmin);
    }

    // ----------------------------------------------------------
    // Install instructions — tailored to where ZMM actually runs
    // ----------------------------------------------------------

    function installCommands(st) {
        var env = st.environment || {};
        var arch = env.arch === 'aarch64' ? 'arm64'
                 : env.arch === 'x86_64' ? 'amd64'
                 : (env.arch || 'amd64');
        var dlBase = 'https://github.com/cloudflare/cloudflared/releases/latest/download/';

        if (env.in_container) {
            return {
                title: 'ZMM is running inside a container',
                note: 'Installing cloudflared on the host does not help — the ' +
                      'container cannot see it. Run this <strong>on the host</strong> to drop the ' +
                      'binary into the running container:',
                cmd: 'sudo podman exec -u root zigbee-matter-manager bash -c \\\n' +
                     '  "curl -fsSL ' + dlBase + 'cloudflared-linux-$(dpkg --print-architecture) \\\n' +
                     '   -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"',
                after: 'This survives restarts but not an image rebuild. Recent versions of ' +
                       'build.sh bake cloudflared into the image, so it becomes permanent on your ' +
                       'next ZMM upgrade/rebuild. (Using docker instead of podman? Swap the command name.)',
            };
        }
        var id = (env.os_id || '') + ' ' + (env.os_like || '');
        if (/fedora|rhel|centos/.test(id)) {
            return {
                title: 'Install cloudflared (' + (env.os_pretty || 'Fedora/RHEL') + ')',
                note: 'Add Cloudflare’s RPM repo so dnf keeps it updated:',
                cmd: 'curl -fsSL https://pkg.cloudflare.com/cloudflared-ascii.repo | sudo tee /etc/yum.repos.d/cloudflared.repo\n' +
                     'sudo dnf install cloudflared',
                after: 'Then click the refresh button above.',
            };
        }
        if (/debian|ubuntu/.test(id)) {
            return {
                title: 'Install cloudflared (' + (env.os_pretty || 'Debian/Ubuntu') + ')',
                note: 'Grab the .deb for this machine (' + escape(arch) + '):',
                cmd: 'curl -fsSL ' + dlBase + 'cloudflared-linux-' + arch + '.deb -o cloudflared.deb\n' +
                     'sudo dpkg -i cloudflared.deb',
                after: 'Then click the refresh button above.',
            };
        }
        return {
            title: 'Install cloudflared',
            note: 'Download the static binary for this machine (' + escape(arch) + '):',
            cmd: 'sudo curl -fsSL ' + dlBase + 'cloudflared-linux-' + arch + ' -o /usr/local/bin/cloudflared\n' +
                 'sudo chmod +x /usr/local/bin/cloudflared',
            after: 'Then click the refresh button above.',
        };
    }

    function renderInstallCard(st) {
        var i = installCommands(st);
        return '' +
        '<div class="card shadow-sm mb-4 border-warning">' +
          '<div class="card-header bg-warning-subtle py-2">' +
            '<span class="fw-bold"><i class="fas fa-download me-1"></i> ' + i.title + '</span>' +
          '</div>' +
          '<div class="card-body small">' +
            '<p>' + i.note + '</p>' +
            '<div class="position-relative">' +
              '<pre class="bg-dark text-light p-3 rounded" style="white-space:pre-wrap;" id="ra-install-cmd">' +
                escape(i.cmd) + '</pre>' +
              '<button class="btn btn-sm btn-outline-light position-absolute top-0 end-0 m-2" id="ra-copy-btn" ' +
                'title="Copy to clipboard"><i class="fas fa-copy"></i></button>' +
            '</div>' +
            '<p class="text-muted mb-0">' + i.after + '</p>' +
          '</div>' +
        '</div>';
    }

    function renderStatusCard(st) {
        var badge = st.running
            ? '<span class="badge bg-success">Running</span>'
            : (st.enabled
                ? '<span class="badge bg-danger">Stopped</span>'
                : '<span class="badge bg-secondary">Disabled</span>');

        var urlRow = st.url
            ? '<tr><th class="text-muted">Public URL</th><td>' +
              '<a href="' + escape(st.url) + '" target="_blank" rel="noopener">' +
              escape(st.url) + ' <i class="fas fa-external-link-alt small"></i></a>' +
              (st.mode === 'quick'
                  ? ' <span class="badge bg-warning text-dark ms-1">ephemeral</span>'
                  : '') +
              '</td></tr>'
            : '';

        var binRow = st.binary_path
            ? '<tr><th class="text-muted">cloudflared</th><td><code>' +
              escape(st.binary_path) + '</code>' +
              (st.binary_version
                  ? ' <small class="text-muted">' + escape(st.binary_version) + '</small>'
                  : '') + '</td></tr>'
            : '<tr><th class="text-muted">cloudflared</th>' +
              '<td><span class="badge bg-danger">not installed</span> ' +
              '<small class="text-muted">install instructions below</small></td></tr>';

        return '' +
        '<div class="card shadow-sm mb-4">' +
          '<div class="card-header bg-light d-flex justify-content-between align-items-center py-2">' +
            '<span class="fw-bold"><i class="fas fa-globe me-1"></i> Remote Access Tunnel ' + badge + '</span>' +
            '<button class="btn btn-outline-secondary btn-sm" id="ra-refresh-btn">' +
              '<i class="fas fa-sync-alt"></i></button>' +
          '</div>' +
          '<div class="card-body py-2">' +
            (st.last_error
                ? '<div class="alert alert-danger py-2 small mb-2">' +
                  '<i class="fas fa-exclamation-circle me-1"></i>' +
                  escape(st.last_error) + '</div>'
                : '') +
            '<table class="table table-sm mb-0"><tbody>' +
              urlRow +
              '<tr><th class="text-muted" style="width:11rem">Mode</th><td>' +
                (st.mode === 'quick'
                    ? 'Quick tunnel (trial — URL changes on every restart)'
                    : 'Cloudflare Tunnel (token)') + '</td></tr>' +
              '<tr><th class="text-muted">Edge connections</th><td>' +
                (st.running ? escape(st.connections) : '—') + '</td></tr>' +
              '<tr><th class="text-muted">Uptime</th><td>' + fmtUptime(st.uptime_s) + '</td></tr>' +
              '<tr><th class="text-muted">Local origin</th><td><code>' +
                escape(st.origin_url) + '</code></td></tr>' +
              binRow +
            '</tbody></table>' +
          '</div>' +
        '</div>';
    }

    function renderSettingsCard(st) {
        return '' +
        '<div class="card shadow-sm mb-4">' +
          '<div class="card-header bg-light py-2">' +
            '<span class="fw-bold"><i class="fas fa-sliders-h me-1"></i> Configuration</span>' +
          '</div>' +
          '<div class="card-body">' +

            '<div class="form-check form-switch fs-5 mb-3">' +
              '<input class="form-check-input" type="checkbox" id="ra-enabled"' +
                (st.enabled ? ' checked' : '') + '>' +
              '<label class="form-check-label" for="ra-enabled">Enable remote access</label>' +
            '</div>' +

            '<div class="mb-3">' +
              '<label class="form-label fw-bold">Mode</label>' +
              '<div class="form-check">' +
                '<input class="form-check-input" type="radio" name="ra-mode" id="ra-mode-token" value="token"' +
                  (st.mode !== 'quick' ? ' checked' : '') + '>' +
                '<label class="form-check-label" for="ra-mode-token">' +
                  '<strong>Cloudflare Tunnel</strong> <span class="badge bg-success">recommended</span>' +
                  '<div class="form-text mt-0">Permanent hostname on your own domain. ' +
                  'Needs a free Cloudflare account and a connector token.</div>' +
                '</label>' +
              '</div>' +
              '<div class="form-check">' +
                '<input class="form-check-input" type="radio" name="ra-mode" id="ra-mode-quick" value="quick"' +
                  (st.mode === 'quick' ? ' checked' : '') + '>' +
                '<label class="form-check-label" for="ra-mode-quick">' +
                  '<strong>Quick tunnel</strong> <span class="badge bg-warning text-dark">testing only</span>' +
                  '<div class="form-text mt-0">No account needed. Random ' +
                  '<code>*.trycloudflare.com</code> URL that changes on every restart.</div>' +
                '</label>' +
              '</div>' +
            '</div>' +

            '<div class="mb-3" id="ra-token-group">' +
              '<label class="form-label fw-bold" for="ra-token">Tunnel token</label>' +
              '<input type="password" class="form-control" id="ra-token" autocomplete="off" ' +
                'placeholder="' + (st.token_set
                    ? '•••••••• (saved — leave blank to keep)'
                    : 'paste connector token from the Cloudflare dashboard') + '">' +
              '<div class="form-text">Zero Trust → Networks → Tunnels → your tunnel → ' +
                'install connector: copy the long token from the command shown.</div>' +
            '</div>' +

            '<div class="mb-3" id="ra-hostname-group">' +
              '<label class="form-label fw-bold" for="ra-hostname">Public hostname</label>' +
              '<input type="text" class="form-control" id="ra-hostname" ' +
                'value="' + escape(st.hostname || '') + '" placeholder="zmm.example.com">' +
              '<div class="form-text">The hostname you mapped to ' +
                '<code>' + escape(st.origin_url) + '</code> in the tunnel’s ' +
                'Public Hostname settings. Used for the status link above.</div>' +
            '</div>' +

            '<div class="mb-3">' +
              '<a class="small text-decoration-none" data-bs-toggle="collapse" href="#ra-advanced">' +
                '<i class="fas fa-caret-right me-1"></i>Advanced</a>' +
              '<div class="collapse" id="ra-advanced">' +
                '<label class="form-label fw-bold mt-2" for="ra-binpath">cloudflared binary path</label>' +
                '<input type="text" class="form-control" id="ra-binpath" ' +
                  'value="' + escape(st.cloudflared_path || '') + '" ' +
                  'placeholder="leave blank to auto-detect from PATH">' +
                '<div class="form-text">Only needed if the binary lives somewhere unusual.</div>' +
              '</div>' +
            '</div>' +

            '<div id="ra-save-error" class="alert alert-danger py-2 small" style="display:none;"></div>' +

            '<div class="d-flex gap-2">' +
              '<button class="btn btn-success" id="ra-save-btn">' +
                '<i class="fas fa-save me-1"></i> Save &amp; Apply</button>' +
              (st.enabled && !st.running
                  ? '<button class="btn btn-outline-primary" id="ra-start-btn">' +
                    '<i class="fas fa-play me-1"></i> Start</button>'
                  : '') +
              (st.running
                  ? '<button class="btn btn-outline-danger" id="ra-stop-btn">' +
                    '<i class="fas fa-stop me-1"></i> Stop</button>'
                  : '') +
            '</div>' +
          '</div>' +
        '</div>';
    }

    function renderHelpCard(st) {
        var origin = escape(st.origin_url);
        var dash = 'https://dash.cloudflare.com/';
        // A click-path breadcrumb (e.g. "Zero Trust → Networks → Connectors").
        function crumb(path) {
            return '<div class="mb-2 ms-1"><i class="fas fa-location-arrow text-primary me-1"></i>' +
                   '<code>' + path + '</code></div>';
        }
        return '' +
        '<div class="card shadow-sm">' +
          '<div class="card-header bg-light py-2">' +
            '<span class="fw-bold">' +
              '<i class="fas fa-question-circle me-1"></i> Setup guide</span>' +
          '</div>' +
          '<div id="ra-help"><div class="card-body small">' +

            '<p>The tunnel dials <em>out</em> to Cloudflare, so it works behind NAT/CGNAT with ' +
            'no port-forwarding. Remote users open your hostname in a browser and log in with ' +
            'their ZMM account. Every step below is done at ' +
            '<a href="' + dash + '" target="_blank" rel="noopener">dash.cloudflare.com</a>; ' +
            'a first-time setup takes about 10 minutes.</p>' +

            // 1. domain
            '<p class="fw-bold mb-1">1. Add your domain to Cloudflare</p>' +
            '<p class="mb-1">Skip if your domain is already on Cloudflare and showing ' +
            '<em>Active</em>. Otherwise add it, then point your registrar’s nameservers at the two ' +
            'Cloudflare gives you and wait for the zone to go Active.</p>' +
            crumb('dash.cloudflare.com → Add a domain') +

            // 2. cloudflared binary
            '<p class="fw-bold mb-1">2. cloudflared binary</p>' +
            '<p class="mb-2">' + (st.binary_path
                ? '<code>cloudflared</code> already ships inside the ZMM image ' +
                  '(<code>' + escape(st.binary_path) + '</code>) and ZMM runs it for you — nothing ' +
                  'to install, and you do <em>not</em> need to run the install command Cloudflare ' +
                  'shows in the next step.'
                : 'Normally <code>cloudflared</code> ships inside the ZMM image, but this host is ' +
                  'missing it — install it with the card above first.') + '</p>' +

            // 3. tunnel + token
            '<p class="fw-bold mb-1">3. Create the tunnel &amp; copy its token</p>' +
            '<p class="mb-1">Create a <em>Cloudflared</em> connector and name it (e.g. ' +
            '<code>zmm</code>). On the “install connector” screen, copy the long token — the value ' +
            'after <code>--token</code> — and paste it into <strong>Tunnel token</strong> above.</p>' +
            crumb('Zero Trust → Networks → Connectors → Create tunnel → Cloudflared') +

            // 4. route + No TLS Verify (the critical step)
            '<p class="fw-bold mb-1">4. Route the tunnel to ZMM ' +
              '<span class="badge bg-danger">important</span></p>' +
            '<p class="mb-1">Add a published-application route that sends your hostname to ZMM’s ' +
            'local origin, and <strong>turn on No TLS Verify</strong>. ZMM serves the origin over ' +
            'HTTPS with a <em>self-signed</em> certificate, so without this cloudflared refuses it ' +
            'with <code>x509: certificate signed by unknown authority</code>. (The connector’s local ' +
            '<code>--no-tls-verify</code> flag is ignored for token tunnels — it must be set here.)</p>' +
            '<ul class="mb-1">' +
              '<li>Set the <strong>Public hostname</strong> (e.g. <code>zmm.example.com</code>) — ' +
                'this auto-creates the DNS record in step 5.</li>' +
              '<li>Set the <strong>Service / URL</strong> to <code>' + origin + '</code>.</li>' +
              '<li>Switch <strong>No TLS Verify</strong> on.</li>' +
            '</ul>' +
            crumb('Zero Trust → Networks → Connectors → your connector → ⋯ → Configure → ' +
                  'Published application routes → ⋯ → Edit → Origin and request → ' +
                  'Connection settings → TLS → No TLS Verify') +

            // 5. DNS
            '<p class="fw-bold mb-1">5. Check the DNS record</p>' +
            '<p class="mb-1">Step 4 normally creates this automatically. Confirm there is a ' +
            '<strong>CNAME</strong> for your hostname pointing at ' +
            '<code>&lt;tunnel-id&gt;.cfargotunnel.com</code>, <strong>Proxied</strong> (orange ' +
            'cloud). If adding the hostname said “<em>A DNS record with this name already ' +
            'exists</em>,” a stale record from an old tunnel is in the way — delete it here, then ' +
            're-add the hostname so it recreates the CNAME against the current tunnel.</p>' +
            crumb('dash.cloudflare.com → your domain → DNS → Records') +

            // 6. Access (optional)
            '<p class="fw-bold mb-1">6. Cloudflare Access — optional login gate</p>' +
            '<p class="mb-1">ZMM already enforces password + TOTP MFA, so Access is optional. If a ' +
            'Cloudflare login page appears — or you see “<em>That account does not have access</em>” ' +
            '— an Access application is gating the hostname and either needs a policy that includes ' +
            'your users, <em>or</em> can be removed entirely.</p>' +
            '<p class="mb-1"><strong>To allow specific users</strong> (add an Allow policy, then ' +
            'enable a login method such as One-time PIN or Google):</p>' +
            crumb('Zero Trust → Access controls → Applications → your app → Access policies → ' +
                  'Add a policy → Action: Allow → Include → Emails → add each user → Save') +
            '<p class="mb-1"><strong>Or remove Access</strong> to land straight on the ZMM login:</p>' +
            crumb('Zero Trust → Access controls → Applications → your app → ⋯ → Delete') +

            // 7. finish in ZMM
            '<p class="fw-bold mb-1">7. Finish in ZMM</p>' +
            '<p class="mb-2">Back on this page: paste the token, enter the same <strong>Public ' +
            'hostname</strong>, tick <strong>Enable remote access</strong>, and <strong>Save &amp; ' +
            'Apply</strong>. The status card should show <span class="badge bg-success">Running</span> ' +
            'with edge connections above zero.</p>' +

            '<div class="alert alert-info py-2 mb-2">' +
              '<i class="fas fa-shield-alt me-1"></i> Accounts and API tokens carrying the ' +
              '<code>network:lan_only</code> scope are blocked from connecting through the tunnel. ' +
              'Enable <strong>MFA</strong> for every account allowed in remotely ' +
              '(Settings → User Accounts).' +
            '</div>' +

            '<p class="fw-bold mb-1"><i class="fas fa-stethoscope me-1"></i>Troubleshooting</p>' +
            '<ul class="mb-2">' +
              '<li><strong>Error 1033 / “tunnel not found”</strong> → the hostname’s CNAME points ' +
                'at a tunnel with no live connector (see step 5).</li>' +
              '<li><strong>Cloudflare login / “That account does not have access”</strong> → an ' +
                'Access policy issue (see step 6).</li>' +
              '<li><strong>502 / origin unreachable</strong>, or ' +
                '<strong>x509: certificate signed by unknown authority</strong> in the logs → ' +
                '<strong>No TLS Verify</strong> is off (step 4), or ZMM is not listening on ' +
                '<code>' + origin + '</code>.</li>' +
            '</ul>' +

            '<p class="mb-0 text-muted">Prefer a private overlay network instead of a public URL? ' +
            'Tailscale/WireGuard works out of the box — see <code>docs/remote_access.md</code>.</p>' +
          '</div></div>' +
        '</div>';
    }

    // ----------------------------------------------------------
    // Actions
    // ----------------------------------------------------------

    function showErr(msg) {
        var el = document.getElementById('ra-save-error');
        if (!el) return;
        el.textContent = msg;
        el.style.display = '';
    }

    async function post(url) {
        var r = await fetch(url, { method: 'POST' });
        if (!r.ok) {
            var e = await r.json().catch(function () { return {}; });
            throw new Error(e.detail || 'Request failed');
        }
    }

    function bindActions(isAdmin) {
        var rb = document.getElementById('ra-refresh-btn');
        if (rb) rb.onclick = refresh;

        var cp = document.getElementById('ra-copy-btn');
        if (cp) cp.onclick = function () {
            var pre = document.getElementById('ra-install-cmd');
            if (!pre) return;
            navigator.clipboard.writeText(pre.textContent).then(function () {
                cp.innerHTML = '<i class="fas fa-check"></i>';
                setTimeout(function () {
                    cp.innerHTML = '<i class="fas fa-copy"></i>';
                }, 1500);
            });
        };

        if (!isAdmin) return;

        // Token/hostname inputs only make sense in token mode
        function syncModeVisibility() {
            var quick = document.getElementById('ra-mode-quick').checked;
            document.getElementById('ra-token-group').style.display = quick ? 'none' : '';
            document.getElementById('ra-hostname-group').style.display = quick ? 'none' : '';
        }
        document.querySelectorAll('input[name=ra-mode]').forEach(function (r) {
            r.onchange = syncModeVisibility;
        });
        syncModeVisibility();

        var save = document.getElementById('ra-save-btn');
        if (save) save.onclick = async function () {
            var body = {
                enabled: document.getElementById('ra-enabled').checked,
                mode: document.getElementById('ra-mode-quick').checked ? 'quick' : 'token',
                hostname: document.getElementById('ra-hostname').value.trim(),
                cloudflared_path: document.getElementById('ra-binpath').value.trim(),
            };
            var tok = document.getElementById('ra-token').value.trim();
            if (tok) body.tunnel_token = tok;   // blank = keep existing
            save.disabled = true;
            try {
                var r = await fetch('/api/remote-access/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!r.ok) {
                    var e = await r.json().catch(function () { return {}; });
                    throw new Error(e.detail || 'Save failed');
                }
                await refresh();
            } catch (err) {
                showErr(err.message);
                save.disabled = false;
            }
        };

        var start = document.getElementById('ra-start-btn');
        if (start) start.onclick = async function () {
            start.disabled = true;
            try { await post('/api/remote-access/start'); }
            catch (err) { showErr(err.message); }
            await refresh();
        };

        var stop = document.getElementById('ra-stop-btn');
        if (stop) stop.onclick = async function () {
            stop.disabled = true;
            try { await post('/api/remote-access/stop'); }
            catch (err) { showErr(err.message); }
            await refresh();
        };
    }

    // ----------------------------------------------------------
    // Wiring
    // ----------------------------------------------------------

    window.initRemoteAccess = refresh;

    document.addEventListener('DOMContentLoaded', function () {
        var trigger = document.querySelector('[data-bs-target="#securityRemoteAccess"]');
        if (trigger) trigger.addEventListener('shown.bs.tab', refresh);
    });
})();
