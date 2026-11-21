// Its own module so websocket.js can clear the banner without importing the
// editor, which loads only with its tab.
export function hideTestRecoveryBanner() {
    const banner = document.getElementById('testRecoveryBanner');
    if (banner) {
        if (banner._interval) clearInterval(banner._interval);
        banner.remove();
    }
}
