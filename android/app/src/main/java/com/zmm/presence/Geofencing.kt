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
import com.google.android.gms.location.Priority

/**
 * Arms/disarms the home geofence and any named places.
 *
 * Home decides presence: inside it you are home, outside you are away. Places
 * add nothing to that decision — they exist so the OS wakes this app when you
 * arrive somewhere interesting, at which point it posts a fix and the HUB
 * decides which place that is. Keeping resolution server-side means a place
 * can be added or resized without an app update, and the phone and hub can
 * never disagree about a boundary.
 *
 * Either way this app never streams location; it reports at crossings and on
 * a periodic heartbeat.
 */
object Geofencing {

    const val HOME_ID = "zmm_home"

    /** Request-id prefix for place geofences, so they can be told from home. */
    const val PLACE_PREFIX = "zmm_place_"
    private const val TAG = "ZmmGeofence"

    /**
     * Floor for [maxCacheAgeMs]. Half of the 15-minute responsive heartbeat,
     * so even the most aggressive mode accepts a cache of ordinary age.
     */
    private const val MIN_CACHE_AGE_MS = 7L * 60 * 1000

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
     * One-shot current location, for reporting state immediately rather than
     * waiting for a boundary crossing.
     *
     * INITIAL_TRIGGER_ENTER is supposed to fire on arming if you are already
     * inside, but it only does so once Play Services has a location it trusts.
     * With a cold fix that can take minutes, or not happen at all — leaving the
     * hub showing "unknown" while the user is standing at home, which reads as
     * a broken pairing.
     *
     * Tries a fresh fix, falling back to the last known one. Balanced power:
     * this decides which side of a 100 m circle you are on, so it does not
     * warrant a GPS fix.
     */
    /** Map the hub's mode name onto a Play Services priority constant. */
    private fun priorityOf(name: String): Int = when (name) {
        "high" -> Priority.PRIORITY_HIGH_ACCURACY
        "low" -> Priority.PRIORITY_LOW_POWER
        else -> Priority.PRIORITY_BALANCED_POWER_ACCURACY
    }

    /**
     * How old a cached position may be and still be worth reporting.
     *
     * Half the reporting period: a fix no older than that is no staler than
     * the cadence the hub already expects, while anything older is a position
     * the user may well have left. Floored so the shortest mode still tolerates
     * a normally-aged cache rather than rejecting almost everything.
     */
    private fun maxCacheAgeMs(prefs: Prefs): Long =
        (prefs.heartbeatS * 1000L / 2).coerceAtLeast(MIN_CACHE_AGE_MS)

    /**
     * Age of a fix, measured on the monotonic clock.
     *
     * Not `System.currentTimeMillis() - loc.time`: that is wall clock, and an
     * NTP correction or a timezone-crossing phone can make a fresh fix look
     * hours old (or a stale one look current). elapsedRealtime cannot run
     * backwards, which is the only property this check needs.
     */
    private fun ageMs(loc: android.location.Location): Long =
        (android.os.SystemClock.elapsedRealtimeNanos() - loc.elapsedRealtimeNanos) / 1_000_000L

    @SuppressLint("MissingPermission")   // checked explicitly below
    fun currentFix(ctx: Context, cb: (android.location.Location?) -> Unit) {
        if (!hasForegroundLocation(ctx)) { cb(null); return }
        val client = LocationServices.getFusedLocationProviderClient(ctx)
        val prefs = Prefs(ctx)
        val priority = priorityOf(prefs.priority)
        val maxAge = maxCacheAgeMs(prefs)

        // getCurrentLocation returns null more often than it looks: under
        // LOW_POWER or BALANCED, from a Doze-throttled worker, indoors, it is
        // the common outcome rather than the exception — so this fallback is
        // the heartbeat's usual path, not its edge case.
        //
        // lastLocation is whatever the fused provider happens to still hold,
        // and on a phone that has been sitting still that can be hours old.
        // Reporting it unchecked told the hub the user was at a position they
        // may have left long ago, and it is the only channel still speaking
        // when someone is settled at home: the geofence emits nothing without
        // a crossing, and the passive subscription is filtered at 50 m of
        // displacement at the source. An old position is worse than none —
        // no fix is a gap the next cycle closes, a wrong one is believed.
        fun fallbackToLastKnown() {
            client.lastLocation
                .addOnSuccessListener { loc ->
                    when {
                        loc == null -> cb(null)
                        ageMs(loc) > maxAge -> {
                            Log.i(TAG, "cached fix too old (${ageMs(loc) / 1000}s > " +
                                "${maxAge / 1000}s); reporting no fix")
                            cb(null)
                        }
                        else -> cb(loc)
                    }
                }
                .addOnFailureListener { cb(null) }
        }

        client.getCurrentLocation(priority, null)
            .addOnSuccessListener { loc -> if (loc != null) cb(loc) else fallbackToLastKnown() }
            .addOnFailureListener { fallbackToLastKnown() }
    }

    /**
     * Arm the home geofence, plus one per named place.
     *
     * @param places extra named regions to watch, beyond home. These exist
     *        purely to wake the phone: the fix it then posts is resolved
     *        against the hub's place registry, so the phone never decides
     *        which place someone is in and a radius change on the hub takes
     *        effect without touching the app.
     * @return via [onResult]: null on success, else a human-readable reason.
     */
    @SuppressLint("MissingPermission")   // checked explicitly below
    fun arm(
        ctx: Context,
        lat: Double,
        lon: Double,
        radiusM: Float,
        places: List<HubClient.Place> = emptyList(),
        onResult: (String?) -> Unit,
    ) {
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
            // From the hub's reporting mode. This is a hint, not a contract:
            // the OS batches geofence events for power and will happily be
            // slower than asked, so a low value buys a better chance of a
            // prompt event, never a guarantee of one.
            .setNotificationResponsiveness(Prefs(ctx).responsivenessMs)
            .build()

        val request = GeofencingRequest.Builder()
            // INITIAL_TRIGGER_ENTER: if we're already inside when arming, fire
            // immediately. Otherwise the hub thinks you're away until you next
            // leave and come back, which is a confusing first run.
            .setInitialTrigger(
                GeofencingRequest.INITIAL_TRIGGER_ENTER or GeofencingRequest.INITIAL_TRIGGER_EXIT
            )
            .addGeofence(geofence)
            .apply {
                // Android caps an app at 100 geofences; the hub caps places
                // well below that, so this cannot overflow in practice. A
                // place with a bad radius is skipped rather than failing the
                // whole arm — losing one region beats losing home.
                places.forEach { p ->
                    if (p.radiusM > 0f) {
                        addGeofence(
                            Geofence.Builder()
                                .setRequestId(PLACE_PREFIX + p.id)
                                .setCircularRegion(p.lat, p.lon, p.radiusM)
                                .setExpirationDuration(Geofence.NEVER_EXPIRE)
                                .setTransitionTypes(
                                    Geofence.GEOFENCE_TRANSITION_ENTER or
                                    Geofence.GEOFENCE_TRANSITION_EXIT
                                )
                                .setLoiteringDelay(60_000)
                                .setNotificationResponsiveness(Prefs(ctx).responsivenessMs)
                                .build()
                        )
                    }
                }
            }
            .build()

        LocationServices.getGeofencingClient(ctx)
            .addGeofences(request, pendingIntent(ctx))
            .addOnSuccessListener {
                Log.i(TAG, "armed: home $lat,$lon r=$radiusM + ${places.size} place(s)")
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
