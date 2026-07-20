package com.zmm.presence

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority

/**
 * Passive fill: piggyback on location fixes the OS computes anyway.
 *
 * This is the Home Assistant companion app's background-location model. A
 * PendingIntent subscription is registered with the fused provider; Play
 * Services then delivers fixes to [PassiveLocationReceiver] with this app's
 * process dead, and — the part that makes it nearly free — whenever ANY app
 * on the phone computes a location (Maps, weather, a ride app), this
 * subscription is handed the result without the hardware being turned on a
 * second time. Android's background-location limits batch delivery to a few
 * times an hour when nothing else is active, which is fine: this channel
 * exists to catch MOVEMENT between heartbeats, not to be a schedule.
 *
 * Division of labour across the four reporting channels, so none of them is
 * doing another's job:
 *
 *   geofence   -> boundary crossings (home / places), OS-priority
 *   heartbeat  -> the guaranteed time floor the stale window is sized against
 *   drive mode -> per-minute streaming while the car is connected
 *   passive    -> movement in between, at whatever cadence the OS offers
 *
 * The 50 m minimum-displacement filter is applied by Play Services itself,
 * not in our receiver: a stationary phone therefore generates no deliveries
 * at all — the heartbeat already owns "still here", and filtering at the
 * source is what keeps this channel's battery cost at zero rather than
 * merely small.
 */
object PassiveUpdates {

    private const val TAG = "ZmmPassive"
    private const val INTERVAL_MS = 60_000L
    private const val FASTEST_MS = 30_000L
    private const val MIN_DISPLACEMENT_M = 50f

    // Batching: fixes may be held and delivered together up to this much
    // later. Trading latency for wake-ups is the right deal here — the
    // geofence covers anything urgent.
    private const val MAX_DELAY_MS = 10 * 60_000L

    private fun pendingIntent(ctx: Context): PendingIntent {
        val intent = Intent(ctx, PassiveLocationReceiver::class.java)
        // MUTABLE for the same reason as the geofence PendingIntent: Play
        // Services writes the LocationResult into the intent before delivery.
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        return PendingIntent.getBroadcast(ctx, 1, intent, flags)
    }

    @SuppressLint("MissingPermission")   // callers arm only after grants
    fun register(ctx: Context) {
        if (!Geofencing.hasBackgroundLocation(ctx)) {
            Log.i(TAG, "no background location; passive updates not registered")
            return
        }
        val request = LocationRequest.Builder(
            Priority.PRIORITY_BALANCED_POWER_ACCURACY, INTERVAL_MS,
        )
            .setMinUpdateIntervalMillis(FASTEST_MS)
            .setMinUpdateDistanceMeters(MIN_DISPLACEMENT_M)
            .setMaxUpdateDelayMillis(MAX_DELAY_MS)
            .build()

        LocationServices.getFusedLocationProviderClient(ctx)
            .requestLocationUpdates(request, pendingIntent(ctx))
            .addOnSuccessListener { Log.i(TAG, "passive updates registered") }
            .addOnFailureListener { Log.w(TAG, "passive register failed", it) }
    }

    fun unregister(ctx: Context) {
        LocationServices.getFusedLocationProviderClient(ctx)
            .removeLocationUpdates(pendingIntent(ctx))
        Log.i(TAG, "passive updates unregistered")
    }
}
