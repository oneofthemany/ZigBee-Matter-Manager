package com.zmm.presence

import android.util.Base64
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/**
 * Certificate pinning, trust-on-first-use.
 *
 * WHY NOT A CA. Every hub is self-hosted with its own self-signed certificate,
 * so there is no shared authority to validate against. The two conventional
 * options are both bad here:
 *
 *   - Trusting the device's user CA store (`<certificates src="user" />`) means
 *     any CA anyone has installed on the phone can intercept this app's traffic.
 *     It also grants that trust for every host, not just the hub.
 *   - Shipping a hardcoded pin is impossible: the certificate is generated per
 *     install, so no constant in this source could match anyone's hub but one.
 *
 * WHAT WE DO INSTEAD. On pairing we record the SHA-256 of the server's Subject
 * Public Key Info and store it. Every later connection must present that exact
 * key. This is the SSH model: the first connection is taken on faith, and the
 * user is shown the fingerprint to check against their hub before accepting.
 * After that, the pin is the hub's identity — the system and user CA stores are
 * not consulted at all, so a hostile CA proves nothing.
 *
 * SPKI, NOT THE CERTIFICATE. We hash the public key rather than the whole
 * certificate so that renewing the certificate with the same key keeps working.
 * Regenerating with a NEW key deliberately breaks the pin and forces the user
 * to re-pair — that is the warning working as designed, not a bug. A pin
 * mismatch and a genuine attack look identical from here, which is the point.
 */
object CertPin {

    /** Base64 SHA-256 of the certificate's SubjectPublicKeyInfo. */
    fun spkiPin(cert: X509Certificate): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded)
        return Base64.encodeToString(digest, Base64.NO_WRAP)
    }

    /**
     * Human-checkable form of a pin: colon-separated LOWERCASE hex.
     *
     * Lowercase specifically to match `openssl dgst -sha256 -c` byte for byte.
     * Users verify this by eye against the command in BUILDING.md, and a case
     * difference between the two makes an identical fingerprint look wrong —
     * which either trains people to wave through mismatches or scares them off
     * a correct pairing. Neither is acceptable for the one check the app cannot
     * perform for them.
     */
    fun readable(pin: String): String =
        Base64.decode(pin, Base64.NO_WRAP).joinToString(":") { "%02x".format(it) }

    /**
     * A socket factory that accepts exactly one public key and nothing else.
     *
     * Note this rejects on a mismatch *before* any request is written, so a
     * token is never sent to a server that failed the pin check.
     */
    fun socketFactory(expectedPin: String): SSLSocketFactory {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) {
                throw CertificateException("client auth not used")
            }

            override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {
                val leaf = chain?.firstOrNull()
                    ?: throw CertificateException("server presented no certificate")
                val actual = spkiPin(leaf)
                if (actual != expectedPin) {
                    throw CertificateException(
                        "Certificate pin mismatch. The hub is presenting a different key " +
                        "than the one paired with. If you regenerated the hub's " +
                        "certificate, use Forget and pair again. If you did not, stop: " +
                        "something is intercepting this connection."
                    )
                }
            }

            // Empty is correct and required: we validate by pin, so advertising
            // no accepted issuers keeps us from being used as a general trust
            // manager by anything that inspects this.
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }

        return SSLContext.getInstance("TLS").apply {
            init(null, arrayOf<javax.net.ssl.X509TrustManager>(tm), java.security.SecureRandom())
        }.socketFactory
    }

    /**
     * Open a TLS connection purely to learn the server's key, accepting whatever
     * it presents. Used ONCE, at pairing, to show the user a fingerprint to
     * confirm. No credentials are sent on this connection.
     */
    fun probe(host: String, port: Int, timeoutMs: Int): X509Certificate {
        var captured: X509Certificate? = null
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {
                captured = chain?.firstOrNull()
            }
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }
        val ctx = SSLContext.getInstance("TLS").apply {
            init(null, arrayOf<javax.net.ssl.X509TrustManager>(tm), java.security.SecureRandom())
        }
        ctx.socketFactory.createSocket().use { raw ->
            raw.connect(java.net.InetSocketAddress(host, port), timeoutMs)
            (ctx.socketFactory.createSocket(raw, host, port, true)
                    as javax.net.ssl.SSLSocket).use { s ->
                s.soTimeout = timeoutMs
                s.startHandshake()
                if (captured == null) {
                    captured = s.session.peerCertificates
                        .filterIsInstance<X509Certificate>().firstOrNull()
                }
            }
        }
        return captured ?: throw CertificateException("hub presented no certificate")
    }

    /** Apply the pin to a connection. No-op for http:// (which pairing refuses). */
    fun apply(conn: java.net.HttpURLConnection, pin: String) {
        if (conn is HttpsURLConnection && pin.isNotEmpty()) {
            conn.sslSocketFactory = socketFactory(pin)
        }
    }
}
