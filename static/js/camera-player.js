/* Live camera view: go2rtc's MSE stream through ZMM's websocket proxy, with
   snapshots as the fallback where MSE is missing or the stream fails.
   Protocol: send {"type":"mse","value":<codecs we play>}; go2rtc answers with
   the MIME type, then fMP4 fragments as binary messages. docs/cameras.md §Live view. */

const CODECS = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'hvc1.1.6.L153.B0',
                'mp4a.40.2', 'mp4a.40.5', 'flac', 'opus'];
const KEEP_S = 10;            // buffer kept behind the playhead
const MAX_LAG_S = 2;          // further behind live than this, jump
const START_TIMEOUT_MS = 8000; // no codec reply by then: snapshots

function mediaSourceClass() {
    // iOS has ManagedMediaSource (17.1+) and no MediaSource on iPhone.
    return window.ManagedMediaSource || window.MediaSource || null;
}

function supportedCodecs(MS) {
    return CODECS.filter(c => MS.isTypeSupported(`video/mp4; codecs="${c}"`)).join(',');
}

/**
 * Play camera `id` into `host`, which must be positioned (a .ratio child is).
 * Returns { stop() }.
 * opts.snapshotOnly: never open a stream (thumbnails).
 */
export function attachPlayer(host, id, opts = {}) {
    host.innerHTML = '';
    const video = document.createElement('video');
    Object.assign(video, { muted: true, autoplay: true, playsInline: true });
    video.className = 'w-100 h-100 d-block bg-black rounded object-fit-contain';
    video.setAttribute('aria-label', 'Live camera view');
    const img = document.createElement('img');
    img.className = 'w-100 h-100 d-none bg-black rounded object-fit-contain';
    img.alt = 'Camera snapshot';
    const note = document.createElement('div');
    note.className = 'position-absolute bottom-0 start-0 m-1 badge bg-dark bg-opacity-75 small';
    host.append(video, img, note);

    let ws = null, ms = null, sb = null, url = null, queue = [], snapTimer = null, stopped = false;
    let startTimer = null;

    function snapshots(reason) {
        if (stopped) return;
        cleanupStream();
        video.classList.add('d-none');
        img.classList.remove('d-none');
        note.textContent = reason || 'Snapshot';
        const load = () => { img.src = `/api/cameras/${encodeURIComponent(id)}/snapshot?t=${Date.now()}`; };
        img.onerror = () => { note.textContent = 'Camera unavailable'; };
        img.onload = () => { note.textContent = reason || 'Snapshot'; };
        load();
        clearInterval(snapTimer);
        snapTimer = setInterval(() => { if (!document.hidden) load(); }, opts.snapshotEveryMs || 3000);
    }

    function pump() {
        if (!sb || sb.updating || !queue.length) return;
        try {
            sb.appendBuffer(queue.shift());
        } catch (e) {
            if (e.name === 'QuotaExceededError' && video.buffered.length) {
                queue = [];
                sb.remove(0, Math.max(0, video.currentTime - 1));
            } else {
                snapshots('Stream error — snapshots');
            }
        }
    }

    function trim() {
        if (!sb || sb.updating || !video.buffered.length) return;
        const end = video.buffered.end(video.buffered.length - 1);
        if (end - video.currentTime > MAX_LAG_S) video.currentTime = end - 0.2;
        const start = video.buffered.start(0);
        if (video.currentTime - start > KEEP_S * 2) sb.remove(start, video.currentTime - KEEP_S);
    }

    function cleanupStream() {
        clearTimeout(startTimer);
        if (ws) { ws.onclose = null; try { ws.close(); } catch (_) { /* gone */ } ws = null; }
        if (url) { URL.revokeObjectURL(url); url = null; }
        sb = null; ms = null; queue = [];
    }

    function stream() {
        const MS = mediaSourceClass();
        const codecs = MS ? supportedCodecs(MS) : '';
        if (!codecs) return snapshots('Snapshots (no live video in this browser)');
        ms = new MS();
        if (window.ManagedMediaSource && MS === window.ManagedMediaSource) video.disableRemotePlayback = true;
        url = URL.createObjectURL(ms);
        video.src = url;
        note.textContent = 'Connecting…';
        startTimer = setTimeout(() => { if (!sb) snapshots('No live stream — snapshots'); }, START_TIMEOUT_MS);
        ms.addEventListener('sourceopen', () => {
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(`${proto}://${location.host}/api/cameras/${encodeURIComponent(id)}/stream`);
            ws.binaryType = 'arraybuffer';
            ws.onopen = () => ws.send(JSON.stringify({ type: 'mse', value: codecs }));
            ws.onmessage = ev => {
                if (typeof ev.data === 'string') {
                    let msg = {};
                    try { msg = JSON.parse(ev.data); } catch (_) { return; }
                    if (msg.type === 'mse' && ms && !sb) {
                        try {
                            sb = ms.addSourceBuffer(msg.value);
                            sb.mode = 'segments';
                            sb.addEventListener('updateend', () => { pump(); trim(); });
                            clearTimeout(startTimer);
                            note.textContent = 'Live';
                            setTimeout(() => { if (note.textContent === 'Live') note.classList.add('d-none'); }, 2000);
                        } catch (_) {
                            snapshots('Unsupported video format — snapshots');
                        }
                    } else if (msg.type === 'error') {
                        snapshots('Stream unavailable — snapshots');
                    }
                    return;
                }
                queue.push(ev.data);
                pump();
            };
            ws.onclose = () => { if (!stopped) snapshots('Stream ended — snapshots'); };
        }, { once: true });
        video.play().catch(() => { /* autoplay muted; a paused tab resumes on view */ });
    }

    if (opts.snapshotOnly) snapshots(opts.label || '');
    else stream();

    return {
        stop() {
            stopped = true;
            clearInterval(snapTimer);
            cleanupStream();
            video.removeAttribute('src');
            video.load();
        },
    };
}
