package com.zmm.presence

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.google.android.gms.location.LocationResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Receives the passive subscription's fixes (see [PassiveUpdates]) and
 * forwards the latest to the hub.
 *
 * Movement filtering already happened at the source — Play Services only
 * delivers after >=50 m of displacement — so the only throttle here is a
 * short time gap to collapse batched deliveries, which can hand over several
 * accumulated fixes in one broadcast.
 */
class PassiveLocationReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (!LocationResult.hasResult(intent)) return
        val loc = LocationResult.extractResult(intent)?.lastLocation ?: return

        val prefs = Prefs(context)
        if (!prefs.isPaired || !prefs.armed) return

        // Drive mode already streams every minute on its own subscription;
        // passive fixes during a drive would just double-post the same track.
        if (DriveService.running) return

        val now = System.currentTimeMillis()
        if (now - prefs.passiveLastPostMs < MIN_POST_GAP_MS) return
        // Recorded before the network call, deliberately: if the post fails,
        // losing one passive fix is fine (the heartbeat is the guarantee),
        // but a burst of batched deliveries retrying in parallel is not.
        prefs.passiveLastPostMs = now

        // goAsync buys ~10 s of process lifetime for the network call —
        // without it the process can be killed the moment onReceive returns.
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                when (val r = HubClient.postFix(
                    prefs, loc.latitude, loc.longitude,
                    if (loc.hasAccuracy()) loc.accuracy else null,
                    loc.time / 1000.0,
                )) {
                    is HubClient.Result.Ok -> Log.i(TAG, "passive fix reported")
                    is HubClient.Result.Err -> Log.w(TAG, "passive fix failed: ${r.message}")
                }
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        private const val TAG = "ZmmPassive"
        private const val MIN_POST_GAP_MS = 60_000L
    }
}
