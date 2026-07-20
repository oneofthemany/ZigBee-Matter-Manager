package com.zmm.presence

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Receives ENTER/EXIT from the OS — including when the app is not running — and
 * reports the fix to the hub.
 */
class GeofenceReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return

        if (event.hasError()) {
            Log.w(TAG, "geofence error code=${event.errorCode}")
            return
        }

        val transition = event.geofenceTransition
        val entering = when (transition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> true
            Geofence.GEOFENCE_TRANSITION_EXIT -> false
            else -> {
                Log.d(TAG, "ignoring transition $transition")
                return
            }
        }

        val prefs = Prefs(context)
        if (!prefs.isPaired) { Log.w(TAG, "transition but not paired"); return }

        // triggeringLocation is the OS's fix for the transition. It can be null
        // (rare, but documented) — fall back to the geofence centre, because the
        // hub only cares whether we're inside the radius and an ENTER we drop is
        // an ENTER that never happens.
        val loc = event.triggeringLocation
        val lat = loc?.latitude ?: prefs.homeLat
        val lon = loc?.longitude ?: prefs.homeLon
        val accuracy = loc?.accuracy
        if (lat.isNaN() || lon.isNaN()) { Log.w(TAG, "no usable location"); return }

        // A geofence EXIT must report a position OUTSIDE the radius, or the hub
        // will just decide we're home again. If the OS gave us no fix on exit,
        // reporting the centre would be actively wrong — say nothing instead.
        if (!entering && loc == null) {
            Log.w(TAG, "EXIT with no location; skipping rather than reporting home")
            return
        }

        val ts = (loc?.time?.takeIf { it > 0 } ?: System.currentTimeMillis()) / 1000.0

        // Keep the process alive across the coroutine: onReceive returns
        // immediately and the OS may kill us the moment it does.
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                when (val r = HubClient.postFix(prefs, lat, lon, accuracy, ts)) {
                    is HubClient.Result.Ok ->
                        Log.i(TAG, "reported ${if (entering) "ENTER" else "EXIT"} @ $lat,$lon")
                    is HubClient.Result.Err ->
                        // No retry queue: the next transition corrects it, and a
                        // stale queued fix is worse than none.
                        Log.w(TAG, "report failed: ${r.message}")
                }
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        private const val TAG = "ZmmGeofenceRx"
    }
}
