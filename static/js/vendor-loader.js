/* Heavy vendored libraries load on first use, not with the page: ECharts and
 * Leaflet are ~1.2 MB that most views never open. Classic script so both the
 * classic tabs and the ES modules can reach it before anything draws.
 * One promise per URL, so concurrent callers share one <script>; a failed
 * load is forgotten so the next use retries. */
(function () {
    var pending = {};

    function loadScript(src) {
        if (!pending[src]) {
            pending[src] = new Promise(function (resolve, reject) {
                var s = document.createElement('script');
                s.src = src;
                s.onload = function () { resolve(); };
                s.onerror = function () {
                    delete pending[src];
                    s.remove();
                    reject(new Error('failed to load ' + src));
                };
                document.head.appendChild(s);
            });
        }
        return pending[src];
    }

    function library(src, global) {
        return function () {
            if (window[global]) return Promise.resolve(window[global]);
            return loadScript(src).then(function () { return window[global]; });
        };
    }

    window.zmmLoadScript = loadScript;
    window.zmmLoadECharts = library('/static/js/vendor/echarts.min.js', 'echarts');
    window.zmmLoadLeaflet = library('/static/vendor/leaflet.js', 'L');
})();
