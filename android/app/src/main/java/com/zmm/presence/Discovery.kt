package com.zmm.presence

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.util.Log

/**
 * Finds the hub on the local network via mDNS.
 *
 * The address worth having is the PUBLIC one. A geofence reports when you
 * leave home — precisely when a LAN address stops resolving — so the app has
 * to be paired against the tunnel URL. That URL is also the one thing a user
 * cannot guess and will mistype.
 *
 * So discovery runs on the home network but hands back the address to use
 * everywhere else. Being on the LAN is what makes the answer trustworthy
 * enough to offer; it is not what the answer is for.
 *
 * Discovery never authenticates anything. Pairing still needs a token, and a
 * self-signed certificate still needs its fingerprint confirmed by hand. This
 * removes typing, not verification.
 */
object Discovery {

    private const val TAG = "ZmmDiscovery"
    private const val SERVICE_TYPE = "_zmm._tcp."

    data class Hub(
        val name: String,
        val localUrl: String,
        val publicUrl: String,
    ) {
        /** What to pair with: the tunnel address when the hub advertises one. */
        val preferredUrl: String get() = if (publicUrl.isNotEmpty()) publicUrl else localUrl

        val hasPublic: Boolean get() = publicUrl.isNotEmpty()
    }

    /**
     * Browse for hubs, calling [onFound] for each as it resolves.
     *
     * Returns a handle that MUST be stopped — NsdManager keeps a multicast
     * listener alive otherwise, which drains battery and outlives the screen
     * the user opened it from.
     */
    fun start(
        ctx: Context,
        onFound: (Hub) -> Unit,
        onError: (String) -> Unit,
    ): Handle {
        val nsd = ctx.getSystemService(Context.NSD_SERVICE) as? NsdManager
            ?: run { onError("Network discovery unavailable"); return Handle(null, null) }

        val seen = mutableSetOf<String>()

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(type: String) {
                Log.i(TAG, "discovery started")
            }

            override fun onServiceFound(info: NsdServiceInfo) {
                // A found service carries no TXT records; it has to be
                // resolved to learn the URLs.
                resolve(nsd, info, seen, onFound)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                seen.remove(info.serviceName)
            }

            override fun onDiscoveryStopped(type: String) {}

            override fun onStartDiscoveryFailed(type: String, code: Int) {
                onError("Could not search the network (code $code)")
            }

            override fun onStopDiscoveryFailed(type: String, code: Int) {}
        }

        return try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
            Handle(nsd, listener)
        } catch (e: Exception) {
            onError(e.message ?: "Discovery failed to start")
            Handle(null, null)
        }
    }

    @Suppress("DEPRECATION")
    private fun resolve(
        nsd: NsdManager,
        info: NsdServiceInfo,
        seen: MutableSet<String>,
        onFound: (Hub) -> Unit,
    ) {
        // resolveService is deprecated on API 34+ in favour of a callback-based
        // resolver, but the replacement does not exist below it. minSdk here is
        // 26, so the deprecated path is still the only one that covers every
        // supported device.
        val listener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(i: NsdServiceInfo, code: Int) {
                Log.w(TAG, "resolve failed for ${i.serviceName}: $code")
            }

            override fun onServiceResolved(i: NsdServiceInfo) {
                if (!seen.add(i.serviceName)) return   // already reported

                val local = txt(i, "local_url")
                val public = txt(i, "public_url")

                // Fall back to the resolved socket address when the hub is too
                // old to advertise local_url. Scheme is assumed https: the app
                // refuses plaintext anyway, so http here would only produce a
                // confusing failure later.
                val fallback = i.host?.hostAddress?.let { "https://$it:${i.port}" } ?: ""

                val hub = Hub(
                    name = i.serviceName,
                    localUrl = local.ifEmpty { fallback },
                    publicUrl = public,
                )
                if (hub.preferredUrl.isNotEmpty()) onFound(hub)
            }
        }
        try {
            nsd.resolveService(info, listener)
        } catch (e: Exception) {
            Log.w(TAG, "resolveService threw", e)
        }
    }

    private fun txt(info: NsdServiceInfo, key: String): String {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP) return ""
        val raw = info.attributes[key] ?: return ""
        return try {
            String(raw, Charsets.UTF_8).trim()
        } catch (e: Exception) {
            ""
        }
    }

    class Handle(
        private val nsd: NsdManager?,
        private val listener: NsdManager.DiscoveryListener?,
    ) {
        fun stop() {
            try {
                if (nsd != null && listener != null) nsd.stopServiceDiscovery(listener)
            } catch (e: Exception) {
                // Already stopped, or never started. Nothing useful to do.
            }
        }
    }
}
