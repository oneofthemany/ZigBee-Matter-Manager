package com.zmm.presence

import android.content.Context
import android.content.SharedPreferences

/**
 * Pairing details for one hub + one presence user.
 *
 * The bearer token lives here. On a non-rooted device app-private SharedPreferences
 * are readable only by this app, which is the same bar as any other token store on
 * Android; EncryptedSharedPreferences would add key-store wrapping but not change
 * the outcome if the device itself is compromised. The real mitigation is that the
 * token is scoped to one user's presence and revocable per-device from the hub.
 */
class Prefs(context: Context) {

    private val sp: SharedPreferences =
        context.getSharedPreferences("zmm_presence", Context.MODE_PRIVATE)

    var hubUrl: String
        get() = sp.getString(KEY_HUB, "") ?: ""
        set(v) = sp.edit().putString(KEY_HUB, normaliseUrl(v)).apply()

    var userId: String
        get() = sp.getString(KEY_USER, "") ?: ""
        set(v) = sp.edit().putString(KEY_USER, v.trim()).apply()

    var token: String
        get() = sp.getString(KEY_TOKEN, "") ?: ""
        set(v) = sp.edit().putString(KEY_TOKEN, v.trim()).apply()

    /**
     * SHA-256 of the hub's public key, captured at pairing. See [CertPin].
     * Only meaningful when [trustMode] is [TRUST_PIN].
     */
    var certPin: String
        get() = sp.getString(KEY_PIN, "") ?: ""
        set(v) = sp.edit().putString(KEY_PIN, v.trim()).apply()

    /**
     * How to authenticate the hub: [TRUST_SYSTEM], [TRUST_PIN], or "" when not
     * yet paired.
     *
     * Empty is a hard refusal, never a fallback. If the app cannot say how it
     * decided to trust this hub, it must not send the token at all — silently
     * defaulting to CA validation would turn a failed pairing into an
     * unauthenticated one.
     */
    var trustMode: String
        get() = sp.getString(KEY_TRUST, "") ?: ""
        set(v) = sp.edit().putString(KEY_TRUST, v).apply()

    /** Home geofence, cached so BootReceiver can re-arm without a network call. */
    var homeLat: Double
        get() = Double.fromBits(sp.getLong(KEY_LAT, NAN_BITS))
        set(v) = sp.edit().putLong(KEY_LAT, v.toRawBits()).apply()

    var homeLon: Double
        get() = Double.fromBits(sp.getLong(KEY_LON, NAN_BITS))
        set(v) = sp.edit().putLong(KEY_LON, v.toRawBits()).apply()

    var radiusM: Float
        get() = sp.getFloat(KEY_RADIUS, 0f)
        set(v) = sp.edit().putFloat(KEY_RADIUS, v).apply()

    /**
     * Reporting mode, cached from the hub alongside the home geofence.
     *
     * Cached for the same reason the geofence is: BootReceiver re-arms after a
     * restart, and it must not need a network round-trip to do it. A phone that
     * reboots out of signal would otherwise come back untracked.
     */
    var modeName: String
        get() = sp.getString(KEY_MODE, "balanced") ?: "balanced"
        set(v) = sp.edit().putString(KEY_MODE, v).apply()

    var heartbeatS: Long
        get() = sp.getLong(KEY_HEARTBEAT, 1800L)
        set(v) = sp.edit().putLong(KEY_HEARTBEAT, v).apply()

    var responsivenessMs: Int
        get() = sp.getInt(KEY_RESPONSIVENESS, 120_000)
        set(v) = sp.edit().putInt(KEY_RESPONSIVENESS, v).apply()

    var priority: String
        get() = sp.getString(KEY_PRIORITY, "balanced") ?: "balanced"
        set(v) = sp.edit().putString(KEY_PRIORITY, v).apply()

    /**
     * Named places, cached as JSON alongside the home geofence.
     *
     * Same reason home is cached: BootReceiver re-arms without a network call,
     * and a phone that reboots out of signal must come back watching the same
     * regions it was watching before.
     */
    var placesJson: String
        get() = sp.getString(KEY_PLACES, "[]") ?: "[]"
        set(v) = sp.edit().putString(KEY_PLACES, v).apply()

    fun savePlaces(places: List<HubClient.Place>) {
        val arr = org.json.JSONArray()
        places.forEach { p ->
            arr.put(org.json.JSONObject().apply {
                put("id", p.id); put("name", p.name)
                put("lat", p.lat); put("lon", p.lon)
                put("radius_m", p.radiusM.toDouble())
            })
        }
        placesJson = arr.toString()
    }

    fun loadPlaces(): List<HubClient.Place> = try {
        val arr = org.json.JSONArray(placesJson)
        (0 until arr.length()).mapNotNull { i ->
            arr.optJSONObject(i)?.let { o ->
                HubClient.Place(
                    id = o.optString("id"),
                    name = o.optString("name"),
                    lat = o.optDouble("lat", Double.NaN),
                    lon = o.optDouble("lon", Double.NaN),
                    radiusM = o.optDouble("radius_m", 0.0).toFloat(),
                )
            }
        }.filter { it.id.isNotEmpty() && !it.lat.isNaN() && it.radiusM > 0f }
    } catch (e: Exception) {
        emptyList()
    }

    /**
     * The car's Bluetooth MAC, or "" when drive mode is off.
     *
     * Chosen by the user from the phone's bonded devices. The MAC (not the
     * name) is the identity: names collide ("Car Audio") and can be renamed,
     * but CarBtReceiver must decide from a broadcast, with the app dead,
     * whether THIS device is the car.
     */
    var carBtAddress: String
        get() = sp.getString(KEY_CAR_ADDR, "") ?: ""
        set(v) = sp.edit().putString(KEY_CAR_ADDR, v.trim()).apply()

    /** Display name for the chosen car, purely for the UI and notification. */
    var carBtName: String
        get() = sp.getString(KEY_CAR_NAME, "") ?: ""
        set(v) = sp.edit().putString(KEY_CAR_NAME, v).apply()

    /**
     * When the passive channel last posted, for collapsing batched deliveries.
     * In Prefs rather than a field because the receiver runs in a process that
     * may have been born for that one broadcast.
     */
    var passiveLastPostMs: Long
        get() = sp.getLong(KEY_PASSIVE_POST, 0L)
        set(v) = sp.edit().putLong(KEY_PASSIVE_POST, v).apply()

    fun saveMode(m: HubClient.ModeParams) {
        modeName = m.name
        heartbeatS = m.heartbeatS
        responsivenessMs = m.responsivenessMs
        priority = m.priority
    }

    /**
     * Journey recording, cached from the hub like the mode: DriveService is
     * started by a Bluetooth broadcast with the app possibly dead, and must
     * decide its cadence and whether to tag a trip without a network call.
     */
    var journeysEnabled: Boolean
        get() = sp.getBoolean(KEY_JOURNEYS, false)
        set(v) = sp.edit().putBoolean(KEY_JOURNEYS, v).apply()

    var driveIntervalS: Long
        get() = sp.getLong(KEY_DRIVE_INTERVAL, 60L)
        set(v) = sp.edit().putLong(KEY_DRIVE_INTERVAL, v).apply()

    fun saveJourneys(j: HubClient.JourneyParams) {
        journeysEnabled = j.enabled
        driveIntervalS = j.driveIntervalS
    }

    /**
     * Fuel grade the car app searches for, cycled from the head unit.
     *
     * Phone-side and not hub-decided, unlike the mode and journey settings:
     * this is a per-driver preference about what to show, not a tracking
     * policy, and it must be changeable from the car without a round-trip.
     */
    var carFuelType: String
        get() = sp.getString(KEY_CAR_FUEL, "E10") ?: "E10"
        set(v) = sp.edit().putString(KEY_CAR_FUEL, v).apply()

    var armed: Boolean
        get() = sp.getBoolean(KEY_ARMED, false)
        set(v) = sp.edit().putBoolean(KEY_ARMED, v).apply()

    val isPaired: Boolean
        get() = hubUrl.isNotEmpty() && userId.isNotEmpty() && token.isNotEmpty()

    val hasHome: Boolean
        get() = !homeLat.isNaN() && !homeLon.isNaN() && radiusM > 0f

    fun clear() = sp.edit().clear().apply()

    companion object {
        private const val KEY_HUB = "hub_url"
        private const val KEY_USER = "user_id"
        private const val KEY_TOKEN = "token"
        private const val KEY_LAT = "home_lat"
        private const val KEY_LON = "home_lon"
        private const val KEY_RADIUS = "radius_m"
        private const val KEY_ARMED = "armed"
        private const val KEY_PIN = "cert_pin"
        private const val KEY_TRUST = "trust_mode"
        private const val KEY_MODE = "mode_name"
        private const val KEY_HEARTBEAT = "heartbeat_s"
        private const val KEY_RESPONSIVENESS = "responsiveness_ms"
        private const val KEY_PRIORITY = "priority"
        private const val KEY_PLACES = "places_json"
        private const val KEY_CAR_ADDR = "car_bt_addr"
        private const val KEY_CAR_NAME = "car_bt_name"
        private const val KEY_JOURNEYS = "journeys_enabled"
        private const val KEY_DRIVE_INTERVAL = "drive_interval_s"
        private const val KEY_PASSIVE_POST = "passive_last_post_ms"
        private const val KEY_CAR_FUEL = "car_fuel_type"

        /** Hub cert chains to a system CA — ordinary validation, no pin. */
        const val TRUST_SYSTEM = "system"

        /** Hub cert is self-signed — authenticated solely by [Prefs.certPin]. */
        const val TRUST_PIN = "pin"
        private val NAN_BITS = Double.NaN.toRawBits()

        /**
         * Tolerate "hub:8000", "https://hub:8000/", "https://hub/".
         *
         * A missing scheme becomes https, NEVER http. The bearer token rides on
         * every request; defaulting a bare "192.168.1.1:8000" to plaintext would
         * silently put that token on the wire in the clear, and the user who
         * typed no scheme is exactly the user who would not notice.
         *
         * An explicit "http://" is preserved rather than rewritten — pairing
         * rejects it outright (see [isSecure]), which is a clearer failure than
         * quietly connecting somewhere the user did not ask for.
         */
        fun normaliseUrl(raw: String): String {
            var s = raw.trim().trimEnd('/')
            if (s.isEmpty()) return s
            if (!s.startsWith("http://") && !s.startsWith("https://")) s = "https://$s"
            return s
        }

        fun isSecure(url: String): Boolean = url.startsWith("https://")
    }
}
