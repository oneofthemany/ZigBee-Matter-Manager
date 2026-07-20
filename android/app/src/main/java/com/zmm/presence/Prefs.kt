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

    fun saveMode(m: HubClient.ModeParams) {
        modeName = m.name
        heartbeatS = m.heartbeatS
        responsivenessMs = m.responsivenessMs
        priority = m.priority
    }

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
