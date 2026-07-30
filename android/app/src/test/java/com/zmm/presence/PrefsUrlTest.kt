package com.zmm.presence

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [Prefs.isPublicUrl] decides whether drive mode is offered at all.
 *
 * Getting it wrong in the permissive direction is the expensive one: a hub
 * misclassified as public means a foreground service holding GPS for a whole
 * journey, posting to an address that cannot answer once the car leaves the
 * driveway. These cases are unreachable from the phone without re-pairing
 * against each address in turn, which is why they live here.
 */
class PrefsUrlTest {

    private fun assertLocal(url: String) =
        assertFalse("$url should be treated as home-network only", Prefs.isPublicUrl(url))

    private fun assertPublic(url: String) =
        assertTrue("$url should be treated as publicly reachable", Prefs.isPublicUrl(url))

    @Test
    fun `real domains are public`() {
        assertPublic("https://zigbee-hive-manager.uk")
        assertPublic("https://hub.example.com:8443")
        assertPublic("zigbee-hive-manager.uk")          // normalised to https
        assertPublic("https://zmm.duckdns.org/")
    }

    @Test
    fun `rfc1918 ranges are local`() {
        assertLocal("https://192.168.1.10:8000")
        assertLocal("https://10.0.0.5")
        assertLocal("https://172.16.0.1")
        assertLocal("https://172.31.255.254")
    }

    @Test
    fun `addresses adjacent to rfc1918 are still public`() {
        // 172.15 and 172.32 sit outside 172.16.0.0/12 — an off-by-one here
        // would wrongly disable drive mode on a routable address.
        assertPublic("https://172.15.0.1")
        assertPublic("https://172.32.0.1")
        assertPublic("https://11.0.0.1")
        assertPublic("https://193.168.1.1")
    }

    @Test
    fun `loopback link-local and cgnat are local`() {
        assertLocal("https://127.0.0.1")
        assertLocal("http://localhost:8000")
        assertLocal("https://169.254.1.1")
        assertLocal("https://100.64.0.1")     // CGNAT: routable-looking, isn't
        assertLocal("https://100.127.255.255")
    }

    @Test
    fun `cgnat boundaries are public`() {
        assertPublic("https://100.63.0.1")
        assertPublic("https://100.128.0.1")
    }

    @Test
    fun `mdns and single-label hosts are local`() {
        assertLocal("https://zmm.local")
        assertLocal("https://zmm.local:8000")
        assertLocal("https://zmm")
        assertLocal("http://hub:8000")
    }

    @Test
    fun `ipv6 private ranges are local`() {
        assertLocal("https://[::1]")
        assertLocal("https://[fd00::1]")
        assertLocal("https://[fe80::1]")
    }

    @Test
    fun `empty and malformed urls are not public`() {
        assertLocal("")
        assertLocal("   ")
        assertLocal("https://")
    }
}
