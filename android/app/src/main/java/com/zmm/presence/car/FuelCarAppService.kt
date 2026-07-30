package com.zmm.presence.car

import android.content.Intent
import androidx.car.app.CarAppService
import androidx.car.app.Session
import androidx.car.app.SessionInfo
import androidx.car.app.Screen
import androidx.car.app.validation.HostValidator
import com.zmm.presence.BuildConfig

/**
 * Entry point for the Android Auto head unit.
 *
 * The car never runs the phone's UI: the template host draws every screen and
 * this service only supplies data. That is why nothing from the hub's Drive
 * tab can be reused here — no WebView, no charts, no custom layout. Only the
 * fuel finder maps onto a template, and it is also the only part of that tab
 * worth reading while moving.
 *
 * Declared with category POI, which is what the fuel list honestly is. The
 * category is not cosmetic: the host allows a POI app the place-list and
 * navigation-handoff templates used here, and refuses anything outside them.
 */
class FuelCarAppService : CarAppService() {

    /**
     * Which hosts may drive this service.
     *
     * Debug builds accept any host so the Desktop Head Unit can connect. Release
     * builds accept only hosts signed by Google (Android Auto, Automotive OS):
     * the session hands out fuel prices derived from the user's position, and an
     * unvetted app binding this service would be reading that location.
     *
     * The allowlist is the library's own array of Gearhead and Automotive host
     * signatures. The "_sample" name is unfortunate but it is the real list —
     * copy it into this app's resources if a future library version drops it.
     */
    override fun createHostValidator(): HostValidator =
        if (BuildConfig.DEBUG) {
            HostValidator.ALLOW_ALL_HOSTS_VALIDATOR
        } else {
            HostValidator.Builder(applicationContext)
                .addAllowedHosts(androidx.car.app.R.array.hosts_allowlist_sample)
                .build()
        }

    override fun onCreateSession(sessionInfo: SessionInfo): Session = FuelSession()
}

private class FuelSession : Session() {
    override fun onCreateScreen(intent: Intent): Screen = FuelScreen(carContext)
}
