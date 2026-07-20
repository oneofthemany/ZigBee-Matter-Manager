package com.zmm.presence

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Android drops all geofences on reboot (and on app update). Re-arm from the
 * cached home config so presence survives a restart without the user opening
 * the app.
 *
 * Note: BOOT_COMPLETED is only delivered if the app has been launched at least
 * once since install — Android won't start a never-run app. The README says so.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) return

        val prefs = Prefs(context)
        if (!prefs.armed || !prefs.isPaired || !prefs.hasHome) {
            Log.d(TAG, "nothing to re-arm (armed=${prefs.armed} paired=${prefs.isPaired} home=${prefs.hasHome})")
            return
        }
        if (!Geofencing.hasBackgroundLocation(context)) {
            // The user revoked "Allow all the time" while we were off. Don't
            // pretend we're armed.
            Log.w(TAG, "background location revoked; not re-arming")
            prefs.armed = false
            return
        }

        val pending = goAsync()
        Geofencing.arm(
            context, prefs.homeLat, prefs.homeLon, prefs.radiusM, prefs.loadPlaces(),
        ) { err ->
            if (err == null) {
                Log.i(TAG, "geofence re-armed after $action")
                // Periodic work survives reboot on its own, but not an app
                // reinstall — and re-scheduling is idempotent (UPDATE policy),
                // so doing it here costs nothing and closes that gap.
                HeartbeatWorker.schedule(context, prefs.heartbeatS)
            } else {
                Log.w(TAG, "re-arm failed: $err")
                prefs.armed = false
                HeartbeatWorker.cancel(context)
            }
            pending.finish()
        }
    }

    companion object {
        private const val TAG = "ZmmBoot"
    }
}
