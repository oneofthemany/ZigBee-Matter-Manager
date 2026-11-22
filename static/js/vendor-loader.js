/* Heavy vendored libraries load on first use, not with the page: ECharts and
 * Leaflet are ~1.2 MB that most views never open. Classic script so both the
 * classic tabs and the ES modules can reach it before anything draws.
 * One promise per URL, so concurrent callers share one <script>; a failed
 * load is forgotten so the next use retries. */
(function () {
    var pending = {};
    // The editor's Monaco loader installs a global AMD `define`; a UMD bundle
    // that sees it registers there instead of setting its global, so it is
    // hidden while one of ours loads.
    var hidingAmd = 0, savedDefine;

    function hideAmd() {
        if (hidingAmd++ === 0 && window.define && window.define.amd) {
            savedDefine = window.define;
            window.define = undefined;
        }
    }
    function restoreAmd() {
        if (--hidingAmd === 0 && savedDefine) {
            window.define = savedDefine;
            savedDefine = undefined;
        }
    }

    function loadScript(src) {
        if (!pending[src]) {
            pending[src] = new Promise(function (resolve, reject) {
                var s = document.createElement('script');
                s.src = src;
                s.onload = function () { restoreAmd(); resolve(); };
                s.onerror = function () {
                    restoreAmd();
                    delete pending[src];
                    s.remove();
                    reject(new Error('failed to load ' + src));
                };
                hideAmd();
                document.head.appendChild(s);
            });
        }
        return pending[src];
    }

    // Rejects when the script ran but left no global, so a caller that retries
    // on success can't spin on a library that never arrives.
    function library(src, global) {
        return function () {
            if (window[global]) return Promise.resolve(window[global]);
            return loadScript(src).then(function () {
                if (!window[global]) {
                    delete pending[src];
                    throw new Error(src + ' loaded but did not define ' + global);
                }
                return window[global];
            });
        };
    }

    window.zmmLoadScript = loadScript;
    window.zmmLoadECharts = library('/static/js/vendor/echarts.min.js', 'echarts');
    window.zmmLoadLeaflet = library('/static/vendor/leaflet.js', 'L');
    window.zmmLoadHls = library('/static/js/vendor/hls.min.js', 'Hls');
    window.zmmLoadPdfJs = library('/static/js/vendor/pdf.min.js', 'pdfjsLib');
})();
