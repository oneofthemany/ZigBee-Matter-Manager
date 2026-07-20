package com.zmm.presence

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The two hub calls this app makes. Nothing else.
 *
 *   GET  /api/presence/users/<user>       -> home_lat, home_lon, radius_m
 *   POST /api/presence/users/<user>/fix   -> {lat, lon, accuracy, timestamp}
 *
 * HttpURLConnection rather than a networking library: two endpoints don't justify
 * a dependency, and every dependency is another thing that could talk to someone
 * who isn't your hub.
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
        data class Err(val message: String) : Result<Nothing>()
    }

    /** Fetch this user's own home geofence. Needs scope presence:read:<user>. */
    suspend fun fetchHome(prefs: Prefs): Result<HomeConfig> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/presence/users/${enc(prefs.userId)}"
        try {
            val conn = open(url, "GET", prefs)
            val code = conn.responseCode
            val body = readBody(conn)
            conn.disconnect()

            if (code == 401) return@withContext Result.Err("Token rejected (401). Re-issue it on the hub.")
            if (code == 403) return@withContext Result.Err(
                "Token lacks presence:read:${prefs.userId} (403)."
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

            Result.Ok(HomeConfig(
                o.getDouble("home_lat"), o.getDouble("home_lon"), radius, mode,
            ))
        } catch (e: Exception) {
            Log.w(TAG, "fetchHome failed", e)
            Result.Err(e.message ?: "Could not reach the hub")
        }
    }

    /** Report a fix. Needs scope presence:write:<user>. */
    suspend fun postFix(
        prefs: Prefs,
        lat: Double,
        lon: Double,
        accuracy: Float?,
        timestampSec: Double,
    ): Result<Unit> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/presence/users/${enc(prefs.userId)}/fix"
        try {
            val payload = JSONObject().apply {
                put("lat", lat)
                put("lon", lon)
                if (accuracy != null) put("accuracy", accuracy.toDouble())
                put("timestamp", timestampSec)
            }.toString()

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
     * Named places, for wake-up geofences. Failure is not fatal: places are an
     * enhancement, and a hub that predates them (or a token without
     * presence:read) should still leave home tracking fully working.
     */
    suspend fun fetchPlaces(prefs: Prefs): List<Place> = withContext(Dispatchers.IO) {
        val url = "${prefs.hubUrl}/api/places"
        try {
            val conn = open(url, "GET", prefs)
            val code = conn.responseCode
            val body = readBody(conn)
            conn.disconnect()
            if (code !in 200..299) {
                Log.i(TAG, "places unavailable (HTTP $code) — home only")
                return@withContext emptyList()
            }
            val arr = JSONObject(body).optJSONArray("places") ?: return@withContext emptyList()
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
            out.filter { !it.lat.isNaN() && !it.lon.isNaN() && it.radiusM > 0f }
        } catch (e: Exception) {
            Log.w(TAG, "fetchPlaces failed", e)
            emptyList()
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
