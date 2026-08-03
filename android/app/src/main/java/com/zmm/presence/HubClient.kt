package com.zmm.presence

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The hub calls this app makes. Nothing else.
 *
 *   GET  /api/presence/users/<user>       -> home_lat, home_lon, radius_m
 *   POST /api/presence/users/<user>/fix   -> {lat, lon, accuracy, timestamp}
 *   GET  /api/places                      -> wake-up geofence regions
 *   GET  /api/fuel/nearby                 -> cheapest stations, for the car app
 *
 * HttpURLConnection rather than a networking library: a handful of endpoints
 * don't justify a dependency, and every dependency is another thing that could
 * talk to someone who isn't your hub.
 */
object HubClient {

    private const val TAG = "ZmmHub"
    private const val TIMEOUT_MS = 15_000

    data class HomeConfig(
        val lat: Double,
        val lon: Double,
        val radiusM: Float,
        /** Reporting aggressiveness, resolved by the hub. See [ModeParams]. */
        val mode: ModeParams,
        /** Journey recording, resolved by the hub. See [JourneyParams]. */
        val journeys: JourneyParams = JourneyParams(),
    )

    /**
     * Whether drives are recorded as journeys, and how often drive mode
     * should report while they are. Hub-decided for the same reason as
     * [ModeParams]: enabling journeys or retuning the cadence is a hub-side
     * edit, picked up at the next config refresh. Defaults are "off, once a
     * minute" — exactly the pre-journeys behaviour — for hubs predating this.
     */
    data class JourneyParams(
        val enabled: Boolean = false,
        val driveIntervalS: Long = 60,
    )

    /** A named region from the hub, used only to register a wake-up geofence. */
    data class Place(
        val id: String,
        val name: String,
        val lat: Double,
        val lon: Double,
        val radiusM: Float,
    )

    /**
     * How hard to work at tracking, as decided on the hub.
     *
     * The phone deliberately holds no copy of the mode table: the hub sends
     * resolved numbers, so retuning a device is a hub-side edit rather than an
     * app update. Defaults here are the "balanced" values and exist only for a
     * hub too old to send the block.
     */
    data class ModeParams(
        val name: String = "balanced",
        val heartbeatS: Long = 1800,
        val responsivenessMs: Int = 120_000,
        val priority: String = "balanced",
    )

    sealed class Result<out T> {
        data class Ok<T>(val value: T) : Result<T>()
        /**
         * [authFailed] marks the hub having actively rejected the credential
         * (401/403) as opposed to any other failure. Callers need the two apart:
         * a rejected token must drop the paired state, whereas a timeout on a
         * train must not — it says nothing about whether the token is good.
         */
        data class Err(val message: String, val authFailed: Boolean = false) : Result<Nothing>()
    }

    /** Fetch this user's own home geofence. Needs scope presence:read:<user>. */
    suspend fun fetchHome(prefs: Prefs): Result<HomeConfig> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/presence/users/${enc(prefs.userId)}"
        try {
            val conn = open(url, "GET", prefs)
            val code = conn.responseCode
            val body = readBody(conn)
            conn.disconnect()

            if (code == 401) return@withContext Result.Err(
                "Token rejected (401). Re-issue it on the hub — the hub only ever " +
                "shows a token once, so the id in its token list is not the token.",
                authFailed = true
            )
            if (code == 403) return@withContext Result.Err(
                "Token lacks presence:read:${prefs.userId} (403).",
                authFailed = true
            )
            if (code == 404) return@withContext Result.Err(
                "No presence user '${prefs.userId}'. Tick Mobile presence for them on the hub."
            )
            if (code !in 200..299) return@withContext Result.Err("Hub returned $code")

            val o = JSONObject(body)
            // A presence user exists but has no home set yet — a real, common state,
            // and distinct from an error. Say which it is.
            if (o.isNull("home_lat") || o.isNull("home_lon")) {
                return@withContext Result.Err(
                    "No home location set for '${prefs.userId}'. Set it in the hub's Presence tab."
                )
            }
            val radius = o.optDouble("radius_m", 0.0).toFloat()
            if (radius <= 0f) return@withContext Result.Err("Home radius is 0 — set it on the hub.")

            // A hub predating reporting modes simply omits this; the defaults
            // then apply and the app behaves exactly as it did before.
            val mp = o.optJSONObject("mode_params")
            val mode = if (mp == null) ModeParams() else ModeParams(
                name = mp.optString("mode", "balanced"),
                heartbeatS = mp.optLong("heartbeat_s", 1800),
                responsivenessMs = mp.optInt("responsiveness_ms", 120_000),
                priority = mp.optString("priority", "balanced"),
            )

            // Same contract as mode_params: absent on an older hub means the
            // defaults, which are the old behaviour.
            val jo = o.optJSONObject("journeys")
            val journeys = if (jo == null) JourneyParams() else JourneyParams(
                enabled = jo.optBoolean("enabled", false),
                driveIntervalS = jo.optLong("drive_interval_s", 60),
            )

            Result.Ok(HomeConfig(
                o.getDouble("home_lat"), o.getDouble("home_lon"), radius, mode,
                journeys,
            ))
        } catch (e: Exception) {
            Log.w(TAG, "fetchHome failed", e)
            Result.Err(e.message ?: "Could not reach the hub")
        }
    }

    /**
     * Report a fix. Needs scope presence:write:<user>.
     *
     * The drive-mode extras default to null so the heartbeat, geofence and
     * passive callers are untouched; only DriveService fills them in. Speed
     * is GPS doppler speed in m/s — the hub aggregates it into per-trip
     * statistics rather than deriving speed from consecutive positions.
     *
     * [motion] and [events] describe the interval that ended at this fix, not
     * the instant of it: a position is a point, but how the car was driven only
     * exists over time. Both are omitted entirely rather than sent as zeroes
     * when there is nothing to say, so a hub can always tell "no sensors" from
     * "sensors, and the driving was smooth".
     */
    suspend fun postFix(
        prefs: Prefs,
        lat: Double,
        lon: Double,
        accuracy: Float?,
        timestampSec: Double,
        speedMps: Float? = null,
        bearingDeg: Float? = null,
        tripId: String? = null,
        altitudeM: Double? = null,
        motion: MotionSampler.Window? = null,
        events: List<MotionSampler.Event> = emptyList(),
        activity: String? = null,
    ): Result<Unit> = postRaw(prefs, fixPayload(
        lat, lon, accuracy, timestampSec, speedMps, bearingDeg, tripId,
        altitudeM, motion, events, activity,
    ))

    /**
     * Build the body of a fix report, without sending it.
     *
     * Separated from [postRaw] so a fix that cannot be delivered now can be
     * kept and delivered later — see [FixSpool]. Everything the hub needs is
     * inside the payload, timestamp included, so a spooled fix is not a
     * degraded one: it lands with the position and time it was taken, however
     * long the gap between taking it and getting a signal.
     */
    fun fixPayload(
        lat: Double,
        lon: Double,
        accuracy: Float?,
        timestampSec: Double,
        speedMps: Float? = null,
        bearingDeg: Float? = null,
        tripId: String? = null,
        altitudeM: Double? = null,
        motion: MotionSampler.Window? = null,
        events: List<MotionSampler.Event> = emptyList(),
        activity: String? = null,
    ): String = JSONObject().apply {
        put("lat", lat)
        put("lon", lon)
        if (accuracy != null) put("accuracy", accuracy.toDouble())
        put("timestamp", timestampSec)
        if (speedMps != null) put("speed", speedMps.toDouble())
        if (bearingDeg != null) put("bearing", bearingDeg.toDouble())
        if (tripId != null) put("trip_id", tripId)
        if (altitudeM != null) put("altitude", altitudeM)
        // Omitted rather than sent as "unknown": the hub treats an absent
        // activity as "no opinion" and counts the fix, which is the right
        // default for every phone that cannot report one.
        if (activity != null) put("activity", activity)
        // Motion rides only on a tagged drive: without a trip there is
        // nothing on the hub for it to belong to.
        if (tripId != null) {
            motion?.let { put("motion", motionJson(it)) }
            if (events.isNotEmpty()) put("events", eventsJson(events))
        }
    }.toString()

    /** POST an already-built fix payload. */
    suspend fun postRaw(prefs: Prefs, payload: String): Result<Unit> =
        withContext(Dispatchers.IO) {
            val url = "${prefs.hubUrl}/api/presence/users/${enc(prefs.userId)}/fix"
            try {
                val conn = open(url, "POST", prefs)
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(payload.toByteArray()) }

                val code = conn.responseCode
                val body = readBody(conn)
                conn.disconnect()

                if (code in 200..299) Result.Ok(Unit)
                else Result.Err("Hub returned $code: ${body.take(200)}")
            } catch (e: Exception) {
                Log.w(TAG, "postFix failed", e)
                Result.Err(e.message ?: "Could not reach the hub")
            }
        }

    /**
     * A window as JSON, with the uncalibrated fields left out.
     *
     * NaN is how [MotionSampler] says "the forward axis is not learned yet",
     * and it has no JSON representation — putting it on the wire would produce
     * a body no strict parser accepts. Omitting the key says the same thing
     * and says it in a form the hub already understands.
     */
    private fun motionJson(w: MotionSampler.Window): JSONObject = JSONObject().apply {
        put("n", w.samples)
        put("vert_rms", round3(w.vertRmsMps2))
        put("jerk_peak", round3(w.jerkPeakMps3))
        put("horiz_peak", round3(w.horizPeakMps2))
        if (!w.longPeakMps2.isNaN()) put("long_peak", round3(w.longPeakMps2))
        if (!w.latPeakMps2.isNaN()) put("lat_peak", round3(w.latPeakMps2))
        if (!w.yawPeakRadS.isNaN()) put("yaw_peak", round3(w.yawPeakRadS))
        w.pressureHpa?.let { put("pressure", round3(it)) }
    }

    private fun eventsJson(events: List<MotionSampler.Event>) =
        org.json.JSONArray().apply {
            events.forEach { e ->
                put(JSONObject().apply {
                    put("t", e.tSec)
                    put("kind", e.kind)
                    put("peak", round3(e.peakMps2))
                    put("dur", round3(e.durationS))
                })
            }
        }

    /** Millimetre-per-second-squared resolution is already more than the sensor has. */
    private fun round3(v: Float): Double = Math.round(v * 1000.0) / 1000.0

    /**
     * Named places, for wake-up geofences.
     *
     * Returns [Result] rather than a bare list, and that distinction is load
     * bearing: an empty list has two very different causes — "the hub
     * genuinely has no places configured" and "could not reach the hub this
     * cycle" — and a caller that cannot tell them apart cannot tell whether
     * it is safe to overwrite its cache. Collapsing both into `emptyList()`
     * previously meant a transient network blip during a heartbeat would
     * silently wipe every cached place and de-arm those geofences. Ok(empty)
     * means "confirmed none"; Err means "unknown, keep what you had".
     */
    suspend fun fetchPlaces(prefs: Prefs): Result<List<Place>> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/places"
        try {
            val conn = open(url, "GET", prefs)
            val code = conn.responseCode
            val body = readBody(conn)
            conn.disconnect()
            if (code !in 200..299) {
                return@withContext Result.Err("places unavailable (HTTP $code)")
            }
            val arr = JSONObject(body).optJSONArray("places")
                ?: return@withContext Result.Ok(emptyList())
            val out = ArrayList<Place>()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                if (!o.optBoolean("enabled", true)) continue
                val id = o.optString("id")
                if (id.isEmpty()) continue
                out.add(Place(
                    id = id,
                    name = o.optString("name", id),
                    lat = o.optDouble("lat", Double.NaN),
                    lon = o.optDouble("lon", Double.NaN),
                    radiusM = o.optDouble("radius_m", 0.0).toFloat(),
                ))
            }
            Result.Ok(out.filter { !it.lat.isNaN() && !it.lon.isNaN() && it.radiusM > 0f })
        } catch (e: Exception) {
            Log.w(TAG, "fetchPlaces failed", e)
            Result.Err(e.message ?: "fetchPlaces failed")
        }
    }

    /**
     * One fuel station, as the car app needs it.
     *
     * [lat]/[lon] rather than the address the web UI shows: the head unit plots
     * a marker and hands the position to its navigation app, and neither takes
     * a postcode. A station the hub reports without coordinates is dropped by
     * [fetchFuelNearby] — a place with no location cannot be rendered.
     */
    data class Station(
        val siteId: String,
        val brand: String,
        val address: String,
        val postcode: String,
        val lat: Double,
        val lon: Double,
        val distanceKm: Double,
        /** Pence per litre, as the feeds publish it. */
        val price: Double,
    )

    /**
     * Cheapest stations around a point. Needs the same bearer token as
     * everything else; the endpoint is authenticated but needs no extra scope.
     *
     * The centre is passed explicitly rather than letting the hub fall back to
     * the user's home: the whole point in the car is "near where I am now",
     * and the caller is the only one who knows whether it has a usable fix.
     */
    suspend fun fetchFuelNearby(
        prefs: Prefs,
        lat: Double,
        lon: Double,
        fuel: String,
        radiusKm: Double = 8.0,
        limit: Int = 12,
    ): Result<List<Station>> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/fuel/nearby" +
                "?fuel=${enc(fuel)}&lat=$lat&lon=$lon&radius_km=$radiusKm&limit=$limit"
        try {
            val conn = open(url, "GET", prefs)
            val code = conn.responseCode
            val body = readBody(conn)
            conn.disconnect()

            if (code == 401) return@withContext Result.Err("Token rejected — re-pair the app.")
            // The hub says 503 when the upstream feeds are stale or down, which is
            // a normal transient state and not the same as a broken hub. Surface
            // its own wording; it is more specific than anything invented here.
            if (code == 503) return@withContext Result.Err(
                runCatching { JSONObject(body).optString("detail") }.getOrNull()
                    ?.takeIf { it.isNotEmpty() } ?: "Fuel data unavailable"
            )
            if (code !in 200..299) return@withContext Result.Err("Hub returned $code")

            val arr = JSONObject(body).optJSONArray("stations")
                ?: return@withContext Result.Ok(emptyList())
            val out = ArrayList<Station>(arr.length())
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val sLat = o.optDouble("latitude", Double.NaN)
                val sLon = o.optDouble("longitude", Double.NaN)
                if (sLat.isNaN() || sLon.isNaN()) continue
                out.add(Station(
                    siteId = o.optString("site_id"),
                    brand = o.optString("brand").ifEmpty { "Unbranded" },
                    address = o.optString("address"),
                    postcode = o.optString("postcode"),
                    lat = sLat,
                    lon = sLon,
                    distanceKm = o.optDouble("distance_km", Double.NaN),
                    price = o.optDouble("price", Double.NaN),
                ))
            }
            Result.Ok(out.filter { !it.price.isNaN() })
        } catch (e: Exception) {
            Log.w(TAG, "fetchFuelNearby failed", e)
            Result.Err(e.message ?: "Could not reach the hub")
        }
    }

    /**
     * Every request goes out pinned. The pin is installed on the connection
     * before the Authorization header is written and before any body is sent,
     * so a server that fails the pin check never receives the token — the
     * handshake throws first.
     *
     * Redirects stay disabled: following one could move a pinned, authorised
     * request to a host that was never pinned.
     */
    private fun open(url: String, method: String, prefs: Prefs): HttpURLConnection {
        if (!Prefs.isSecure(url)) {
            // Defence in depth. Pairing already refuses http, but this makes it
            // impossible for any later code path to put a token on plaintext.
            throw java.io.IOException("Refusing to send credentials over plain http")
        }
        when (prefs.trustMode) {
            Prefs.TRUST_SYSTEM, Prefs.TRUST_PIN -> Unit
            else -> throw java.io.IOException(
                "Hub trust not established — pair with the hub first"
            )
        }
        if (prefs.trustMode == Prefs.TRUST_PIN && prefs.certPin.isEmpty()) {
            throw java.io.IOException("Pinned mode with no stored pin — re-pair with the hub")
        }
        return (URL(url).openConnection() as HttpURLConnection).apply {
            // In TRUST_SYSTEM we deliberately leave the default SSLSocketFactory
            // in place: the platform validates against system CAs, which is what
            // a tunnel-issued certificate needs and what survives its rotation.
            if (prefs.trustMode == Prefs.TRUST_PIN) {
                CertPin.apply(this, prefs.certPin)
            }
            requestMethod = method
            connectTimeout = TIMEOUT_MS
            readTimeout = TIMEOUT_MS
            setRequestProperty("Authorization", "Bearer ${prefs.token}")
            setRequestProperty("Accept", "application/json")
            instanceFollowRedirects = false
        }
    }

    private fun readBody(conn: HttpURLConnection): String = try {
        val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
        stream?.bufferedReader()?.use { it.readText() } ?: ""
    } catch (e: Exception) {
        ""
    }

    private fun enc(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")
}
