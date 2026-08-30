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
     * The trip currently being recorded, and when it last saw a fix.
     *
     * Persisted rather than held in DriveService because the thing this
     * protects against is DriveService not surviving: a head unit that drops
     * and re-establishes the Bluetooth link, or the system killing the
     * service mid-drive. Either restarts it, and a restart that minted a
     * fresh trip id would cut one drive into fragments — fragments short
     * enough that the hub discards most of them as blips, leaving a
     * three-fix stub as the only evidence a drive happened.
     *
     * Resuming instead makes the phone agree with how the hub already
     * segments trips: same drive until there is a real gap.
     */
    var driveTripId: String
        get() = sp.getString(KEY_TRIP_ID, "") ?: ""
        set(v) = sp.edit().putString(KEY_TRIP_ID, v).apply()

    var driveTripLastMs: Long
        get() = sp.getLong(KEY_TRIP_LAST, 0L)
        set(v) = sp.edit().putLong(KEY_TRIP_LAST, v).apply()

    /**
     * Last activity transition seen, and when. Persisted because the receiver
     * that writes it and the service that reads it are separate processes'
     * worth of lifetime apart — either may be dead when the other runs.
     */
    var lastActivity: String
        get() = sp.getString(KEY_ACTIVITY, "") ?: ""
        set(v) = sp.edit().putString(KEY_ACTIVITY, v).apply()

    var lastActivityMs: Long
        get() = sp.getLong(KEY_ACTIVITY_MS, 0L)
        set(v) = sp.edit().putLong(KEY_ACTIVITY_MS, v).apply()

    /** The activity, or null once it is too old to describe the present. */
    val currentActivity: String?
        get() {
            val a = lastActivity
            if (a.isEmpty()) return null
            val age = System.currentTimeMillis() - lastActivityMs
            return if (age in 0..ActivityMonitor.STALE_MS) a else null
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

    /**
     * The grades the hub's region offers, cached from the last lookup.
     *
     * Cached rather than fetched because the head unit cycles grades with a
     * tap: the list has to be in hand before the first request of a drive, or
     * the first tap would either do nothing or offer a UK grade to a driver in
     * Germany. Stored as a JSON array of `{"c": code, "l": label}` so the order
     * the region declared them in survives — a map would not promise that.
     *
     * Empty until the first successful lookup, which is what
     * [carFuelGradeCodes] falling back to the UK four is for.
     */
    var carFuelGrades: String
        get() = sp.getString(KEY_CAR_FUEL_GRADES, "") ?: ""
        set(v) = sp.edit().putString(KEY_CAR_FUEL_GRADES, v).apply()

    /** The cached grade codes in order, or the UK four if none are cached. */
    val carFuelGradeCodes: List<String>
        get() = parseGrades().map { it.first }.ifEmpty { UK_GRADE_CODES }

    /** The label for a grade code, or the code itself when it is not known. */
    fun carFuelGradeLabel(code: String): String =
        parseGrades().firstOrNull { it.first == code }?.second ?: code

    private fun parseGrades(): List<Pair<String, String>> {
        val raw = carFuelGrades
        if (raw.isEmpty()) return emptyList()
        return runCatching {
            val arr = org.json.JSONArray(raw)
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                val code = o.optString("c")
                if (code.isEmpty()) null else code to o.optString("l", code)
            }
        }.getOrDefault(emptyList())
    }

    /** Store a region's grades, in the order it declared them. */
    fun setCarFuelGrades(grades: Map<String, String>) {
        val arr = org.json.JSONArray()
        grades.forEach { (code, label) ->
            arr.put(org.json.JSONObject().put("c", code).put("l", label))
        }
        carFuelGrades = arr.toString()
    }

    var armed: Boolean
        get() = sp.getBoolean(KEY_ARMED, false)
        set(v) = sp.edit().putBoolean(KEY_ARMED, v).apply()

    val isPaired: Boolean
        get() = hubUrl.isNotEmpty() && userId.isNotEmpty() && token.isNotEmpty()

    /**
     * Whether the hub has actually accepted these credentials.
     *
     * [isPaired] only says the three fields are non-empty, which is the right
     * test for "is there anything to try" but a dangerous one to show the user:
     * a mistyped or revoked token still fills the fields, so a screen driven by
     * isPaired alone reports "Paired" while every request comes back 401. Set
     * only by a successful hub round-trip, cleared when a credential is edited
     * or the hub rejects one. A transient network failure deliberately leaves
     * it alone — being briefly offline is not evidence the token went bad.
     */
    var verified: Boolean
        get() = sp.getBoolean(KEY_VERIFIED, false)
        set(v) = sp.edit().putBoolean(KEY_VERIFIED, v).apply()

    val hasHome: Boolean
        get() = !homeLat.isNaN() && !homeLon.isNaN() && radiusM > 0f

    /**
     * Light / dark / follow-the-system, as an AppCompatDelegate.MODE_NIGHT_*
     * constant.
     *
     * Stored as the framework's own constant rather than a private enum so it
     * can go straight to setDefaultNightMode with nothing to translate. The
     * default is MODE_NIGHT_FOLLOW_SYSTEM (-1).
     */
    var themeMode: Int
        get() = sp.getInt(KEY_THEME, -1)   // AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
        set(v) = sp.edit().putInt(KEY_THEME, v).apply()

    /**
     * Wipe the pairing.
     *
     * The chosen theme deliberately survives: "Forget this hub" is about the
     * hub, and silently reverting someone's display preference alongside it
     * would look like a bug in the toggle rather than part of forgetting.
     */
    fun clear() {
        val theme = themeMode
        sp.edit().clear().apply()
        themeMode = theme
    }

    companion object {
        private const val KEY_HUB = "hub_url"
        private const val KEY_USER = "user_id"
        private const val KEY_TOKEN = "token"
        private const val KEY_LAT = "home_lat"
        private const val KEY_LON = "home_lon"
        private const val KEY_RADIUS = "radius_m"
        private const val KEY_ARMED = "armed"
        private const val KEY_VERIFIED = "verified"
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
        private const val KEY_CAR_FUEL_GRADES = "car_fuel_grades"

        /**
         * What the car app offers before it has heard from the hub. The UK's
         * four, because that is the only region this app ever shipped with —
         * an install that upgrades must behave exactly as it did until the
         * first lookup tells it otherwise.
         */
        val UK_GRADE_CODES = listOf("E10", "E5", "B7", "SDV")
        private const val KEY_TRIP_ID = "drive_trip_id"
        private const val KEY_TRIP_LAST = "drive_trip_last_ms"
        private const val KEY_THEME = "theme_mode"
        private const val KEY_ACTIVITY = "last_activity"
        private const val KEY_ACTIVITY_MS = "last_activity_ms"

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

        /**
         * Whether this hub address can be reached from outside the home network.
         *
         * Drive mode reports every minute while the car is connected, which by
         * definition happens away from home. A LAN address cannot answer once
         * the phone leaves the Wi-Fi, so the whole feature would consist of
         * failed requests — and the failure looks like a broken hub rather than
         * an address that was never going to work.
         *
         * Conservative by design: anything not recognisably routable counts as
         * local. A false "local" costs a hub owner one look at Remote Access; a
         * false "public" costs a drive's worth of silently dropped reports.
         */
        fun isPublicUrl(url: String): Boolean {
            val host = try {
                java.net.URI(normaliseUrl(url)).host?.lowercase()
            } catch (e: Exception) {
                null
            } ?: return false

            // mDNS and single-label names resolve on the LAN only.
            if (host == "localhost" || host.endsWith(".local")) return false
            if (!host.contains('.') && !host.contains(':')) return false

            // Bracketless IPv6 still arrives here with colons.
            if (host.contains(':')) {
                val h = host.trim('[', ']')
                if (h == "::1") return false
                // fc00::/7 (unique local) and fe80::/10 (link local).
                if (h.startsWith("fc") || h.startsWith("fd") ||
                    h.startsWith("fe8") || h.startsWith("fe9") ||
                    h.startsWith("fea") || h.startsWith("feb")) return false
                return true
            }

            val octets = host.split('.')
            if (octets.size == 4 && octets.all { it.toIntOrNull() in 0..255 }) {
                val (a, b) = octets[0].toInt() to octets[1].toInt()
                return when {
                    a == 10 -> false                          // 10.0.0.0/8
                    a == 127 -> false                         // loopback
                    a == 172 && b in 16..31 -> false          // 172.16.0.0/12
                    a == 192 && b == 168 -> false             // 192.168.0.0/16
                    a == 169 && b == 254 -> false             // link local
                    a == 100 && b in 64..127 -> false         // CGNAT
                    else -> true
                }
            }

            // A dotted name that isn't an IP literal: a real domain.
            return true
        }
    }
}
