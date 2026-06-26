/**
 * Shared ECharts layer — every chart in the app goes through here.
 * Location: static/js/chart-utils.js
 *
 * Why this exists:
 *   - One place to register the light/dark themes so charts match the app's
 *     `data-theme` (set by theme-toggle.js) and re-theme live on `themechange`.
 *   - Auto-resize against the container so charts fill Bootstrap cards.
 *   - A stable wrapper so callers never hold a disposed instance after a
 *     theme swap (ECharts can't change theme in place — it must re-init).
 *
 * Usage:
 *   import { createChart } from './chart-utils.js';
 *   const chart = createChart(document.getElementById('my-chart'));
 *   chart.setOption({ ... });           // on every data refresh
 *   chart.dispose();                    // when the view goes away
 *
 * Requires the global `echarts` (vendored in index.html before the modules).
 */

// Transparent backgrounds so charts blend into whatever card they sit in.
echarts.registerTheme('zbm-light', {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'inherit' },
});

echarts.registerTheme('zbm-dark', {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'inherit', color: '#adb5bd' },
    title: { textStyle: { color: '#e9ecef' } },
    legend: { textStyle: { color: '#adb5bd' } },
    tooltip: {
        backgroundColor: 'rgba(33,37,41,0.95)',
        borderColor: '#495057',
        textStyle: { color: '#e9ecef' },
    },
    categoryAxis: {
        axisLine: { lineStyle: { color: '#495057' } },
        axisTick: { lineStyle: { color: '#495057' } },
        axisLabel: { color: '#adb5bd' },
        splitLine: { lineStyle: { color: '#343a40' } },
    },
    valueAxis: {
        axisLine: { lineStyle: { color: '#495057' } },
        axisTick: { lineStyle: { color: '#495057' } },
        axisLabel: { color: '#adb5bd' },
        splitLine: { lineStyle: { color: '#343a40' } },
    },
    timeAxis: {
        axisLine: { lineStyle: { color: '#495057' } },
        axisTick: { lineStyle: { color: '#495057' } },
        axisLabel: { color: '#adb5bd' },
        splitLine: { lineStyle: { color: '#343a40' } },
    },
});

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark'
        ? 'zbm-dark'
        : 'zbm-light';
}

/**
 * Create a managed ECharts chart bound to `el`.
 *
 * Returns a wrapper with stable methods regardless of internal re-inits:
 *   setOption(opt, notMerge?)  store + apply options
 *   resize()                   force a resize
 *   instance()                 the live echarts instance (for getZr, etc.)
 *   dispose()                  tear down listeners + instance
 *
 * @param {HTMLElement} el
 * @param {object} [initOpts]  passed to echarts.init (e.g. { renderer:'svg' })
 */
export function createChart(el, initOpts = {}) {
    if (!el) return null;

    let inst = echarts.init(el, currentTheme(), initOpts);
    let lastOption = null;

    const ro = new ResizeObserver(() => inst.resize());
    ro.observe(el);

    // ECharts has no live theme swap — re-init and replay the last raw option.
    // Replaying the caller's option (not getOption()) keeps the new theme's
    // colours, since the raw option carries no baked-in theme styling.
    const onThemeChange = () => {
        if (!el.isConnected) return;
        inst.dispose();
        inst = echarts.init(el, currentTheme(), initOpts);
        if (lastOption) inst.setOption(lastOption, true);
    };
    document.addEventListener('themechange', onThemeChange);

    return {
        setOption(opt, notMerge = true) {
            lastOption = opt;
            inst.setOption(opt, notMerge);
        },
        resize() { inst.resize(); },
        instance() { return inst; },
        dispose() {
            ro.disconnect();
            document.removeEventListener('themechange', onThemeChange);
            inst.dispose();
        },
    };
}
