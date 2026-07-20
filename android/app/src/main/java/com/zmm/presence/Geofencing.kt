package com.zmm.presence

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices

/**
 * Arms/disarms the single home geofence.
 *
 * One geofence, one place. If you're inside it you're home; if you're not, you're
 * away. That maps exactly onto what presence_users.py already models, which is why
 * this app doesn't need to stream location at all.
 */
object Geofencing {

    const val HOME_ID = "zmm_home"
    private const val TAG = "ZmmGeofence"

    fun hasForegroundLocation(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    fun hasBackgroundLocation(ctx: Context): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
                PackageManager.PERMISSION_GRANTED
        } else true   // pre-Q, foreground grant covers background

    private fun pendingIntent(ctx: Context): PendingIntent {
        val intent = Intent(ctx, GeofenceReceiver::class.java)
        // MUTABLE: the OS writes the transition details into this intent before
        // delivering it. IMMUTABLE would silently break geofencing on API 31+.
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        return PendingIntent.getBroadcast(ctx, 0, intent, flags)
    }

    /**
     * @return null on success, else a human-readable reason.
     */
    @SuppressLint("MissingPermission")   // checked explicitly below
    fun arm(ctx: Context, lat: Double, lon: Double, radiusM: Float, onResult: (String?) -> Unit) {
        if (!hasForegroundLocation(ctx)) { onResult("Location permission not granted"); return }
        if (!hasBackgroundLocation(ctx)) { onResult("Background location ('Allow all the time') not granted"); return }

        val geofence = Geofence.Builder()
            .setRequestId(HOME_ID)
            .setCircularRegion(lat, lon, radiusM)
            .setExpirationDuration(Geofence.NEVER_EXPIRE)
            .setTransitionTypes(Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT)
            // Debounce the boundary: without this, sitting on the edge (or a poor
            // fix) flaps home/away and hammers the hub. The server has its own
            // hysteresis; this stops the noise before it leaves the phone.
            .setLoiteringDelay(60_000)
            .setNotificationResponsiveness(60_000)
            .build()

        val request = GeofencingRequest.Builder()
            // INITIAL_TRIGGER_ENTER: if we're already inside when arming, fire
            // immediately. Otherwise the hub thinks you're away until you next
            // leave and come back, which is a confusing first run.
            .setInitialTrigger(
                GeofencingRequest.INITIAL_TRIGGER_ENTER or GeofencingRequest.INITIAL_TRIGGER_EXIT
            )
            .addGeofence(geofence)
            .build()

        LocationServices.getGeofencingClient(ctx)
            .addGeofences(request, pendingIntent(ctx))
            .addOnSuccessListener {
                Log.i(TAG, "geofence armed at $lat,$lon r=$radiusM")
                onResult(null)
            }
            .addOnFailureListener { e ->
                Log.w(TAG, "arm failed", e)
                onResult(describe(e))
            }
    }

    fun disarm(ctx: Context, onResult: (String?) -> Unit = {}) {
        LocationServices.getGeofencingClient(ctx)
            .removeGeofences(pendingIntent(ctx))
            .addOnSuccessListener { onResult(null) }
            .addOnFailureListener { e -> onResult(e.message) }
    }

    /** Play Services error codes are opaque integers; translate the ones that matter. */
    private fun describe(e: Exception): String {
        val msg = e.message ?: return "Could not arm the geofence"
        return when {
            msg.contains("1000") -> "Geofencing unavailable — turn on device Location, and set Location accuracy to High"
            msg.contains("1001") -> "Too many geofences registered"
            msg.contains("1002") -> "Too many PendingIntents"
            msg.contains("API_NOT_AVAILABLE", true) ||
                msg.contains("SERVICE_MISSING", true) -> "Google Play Services unavailable — this app needs it for geofencing"
            else -> msg
        }
    }
}
