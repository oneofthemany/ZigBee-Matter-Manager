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
// Colours track the hive palette in css/hive-tokens.css (keep in sync).
echarts.registerTheme('zbm-light', {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'inherit', color: '#53687a' },
    title: { textStyle: { color: '#16283a' } },
    legend: { textStyle: { color: '#53687a' } },
    tooltip: {
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#ded7c6',
        textStyle: { color: '#16283a' },
    },
    categoryAxis: {
        axisLine: { lineStyle: { color: '#ded7c6' } },
        axisTick: { lineStyle: { color: '#ded7c6' } },
        axisLabel: { color: '#53687a' },
        splitLine: { lineStyle: { color: '#eae4d6' } },
    },
    valueAxis: {
        axisLine: { lineStyle: { color: '#ded7c6' } },
        axisTick: { lineStyle: { color: '#ded7c6' } },
        axisLabel: { color: '#53687a' },
        splitLine: { lineStyle: { color: '#eae4d6' } },
    },
    timeAxis: {
        axisLine: { lineStyle: { color: '#ded7c6' } },
        axisTick: { lineStyle: { color: '#ded7c6' } },
        axisLabel: { color: '#53687a' },
        splitLine: { lineStyle: { color: '#eae4d6' } },
    },
});

echarts.registerTheme('zbm-dark', {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'inherit', color: '#7e97a8' },
    title: { textStyle: { color: '#eaf2f4' } },
    legend: { textStyle: { color: '#7e97a8' } },
    tooltip: {
        backgroundColor: 'rgba(22,40,58,0.95)',
        borderColor: '#1e3346',
        textStyle: { color: '#eaf2f4' },
    },
    categoryAxis: {
        axisLine: { lineStyle: { color: '#1e3346' } },
        axisTick: { lineStyle: { color: '#1e3346' } },
        axisLabel: { color: '#7e97a8' },
        splitLine: { lineStyle: { color: '#16283a' } },
    },
    valueAxis: {
        axisLine: { lineStyle: { color: '#1e3346' } },
        axisTick: { lineStyle: { color: '#1e3346' } },
        axisLabel: { color: '#7e97a8' },
        splitLine: { lineStyle: { color: '#16283a' } },
    },
    timeAxis: {
        axisLine: { lineStyle: { color: '#1e3346' } },
        axisTick: { lineStyle: { color: '#1e3346' } },
        axisLabel: { color: '#7e97a8' },
        splitLine: { lineStyle: { color: '#16283a' } },
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
