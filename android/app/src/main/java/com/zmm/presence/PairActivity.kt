package com.zmm.presence

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.zmm.presence.databinding.ActivityPairBinding
import kotlinx.coroutines.launch

/**
 * Pair with the hub, grant location, arm the geofence. That's the whole app.
 */
class PairActivity : AppCompatActivity() {

    private lateinit var b: ActivityPairBinding
    private lateinit var prefs: Prefs

    private val requestForeground = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        if (granted[Manifest.permission.ACCESS_FINE_LOCATION] == true) {
            // Android 10+ REFUSES to grant background in the same prompt. It must
            // be a second, separate request, and only after foreground is held.
            // Asking for both at once silently returns denied for background.
            requestBackgroundIfNeeded()
        } else {
            status("Location denied. A geofence can't work without it.")
        }
    }

    private val requestBackground = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) status("Background location granted. You can arm the geofence.")
        else explainBackgroundRefusal()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityPairBinding.inflate(layoutInflater)
        setContentView(b.root)
        applyWindowInsets()
        prefs = Prefs(this)

        b.hubUrl.setText(prefs.hubUrl)
        b.userId.setText(prefs.userId)
        b.token.setText(prefs.token)

        b.pairBtn.setOnClickListener { pair() }
        b.permsBtn.setOnClickListener { requestPermissions() }
        b.armBtn.setOnClickListener { if (prefs.armed) disarm() else arm() }
        b.forgetBtn.setOnClickListener { forget() }
        b.discoverBtn.setOnClickListener { discover() }

        b.deviceId.text = getString(R.string.device_id_fmt, deviceId())
        render()
    }

    /**
     * Keep content clear of the system bars and the app bar.
     *
     * targetSdk 35 (Android 15) enforces edge-to-edge: the window extends
     * behind the status and navigation bars whether or not the layout expects
     * it. Without this the top of the form sits underneath the app bar — the
     * hub URL field is the first casualty — and the bottom buttons hide behind
     * the gesture bar.
     *
     * Padding rather than fitsSystemWindows so the scroll surface still
     * extends to the edges; only the content is inset.
     */
    private fun applyWindowInsets() {
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(b.root) { v, insets ->
            val bars = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                androidx.core.view.WindowInsetsCompat.Type.displayCutout()
            )
            // The app bar occupies the top inset already, so only the sides and
            // bottom need adding; the ScrollView handles the rest.
            v.setPadding(bars.left, v.paddingTop, bars.right, bars.bottom)
            insets
        }
    }

    override fun onResume() {
        super.onResume()
        render()   // permissions may have changed in Settings
    }

    // ---- actions ----

    private fun pair() {
        prefs.hubUrl = b.hubUrl.text.toString()
        prefs.userId = b.userId.text.toString()
        prefs.token = b.token.text.toString()

        if (!prefs.isPaired) { status("Fill in hub URL, user id and token."); return }

        if (!Prefs.isSecure(prefs.hubUrl)) {
            status("Hub URL must be https:// — the token would otherwise travel in clear text.")
            return
        }

        // Trust not yet established: work out how this hub should be
        // authenticated before sending the token anywhere.
        if (prefs.trustMode.isEmpty()) { establishTrust(); return }

        fetchHome()
    }

    /**
     * Decide how to authenticate this hub, and pin only if we must.
     *
     * A hub behind a tunnel presents a publicly-issued certificate: ordinary
     * validation covers it, no fingerprint prompt is warranted, and pinning
     * would actively harm — such certificates rotate, and a pin would later
     * fail claiming interception. A hub on the LAN presents a self-signed
     * certificate that no CA vouches for, so the user must vouch for it
     * instead. Only that second case shows a dialog.
     */
    private fun establishTrust() {
        status("Checking hub certificate…")
        b.pairBtn.isEnabled = false
        lifecycleScope.launch {
            val trust = try {
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                    CertPin.probeTrust(prefs.hubUrl, 15_000)
                }
            } catch (e: Exception) {
                status("Could not reach the hub: ${e.message}")
                b.pairBtn.isEnabled = true
                return@launch
            }

            b.pairBtn.isEnabled = true
            when (trust) {
                is CertPin.Trust.System -> {
                    prefs.trustMode = Prefs.TRUST_SYSTEM
                    prefs.certPin = ""
                    status("Hub certificate is publicly trusted.")
                    fetchHome()
                }
                is CertPin.Trust.SelfSigned -> {
                    val pin = CertPin.spkiPin(trust.cert)
                    AlertDialog.Builder(this@PairActivity)
                        .setTitle(R.string.pin_title)
                        .setMessage(getString(R.string.pin_body,
                            CertPin.readable(pin), trust.cert.subjectDN.name))
                        .setPositiveButton(R.string.pin_accept) { _, _ ->
                            prefs.certPin = pin
                            prefs.trustMode = Prefs.TRUST_PIN
                            fetchHome()
                        }
                        .setNegativeButton(R.string.cancel) { _, _ ->
                            status("Pairing cancelled.")
                        }
                        .show()
                }
            }
        }
    }

    private fun fetchHome() {
        status("Contacting hub…")
        b.pairBtn.isEnabled = false
        lifecycleScope.launch {
            when (val r = HubClient.fetchHome(prefs)) {
                is HubClient.Result.Ok -> {
                    prefs.homeLat = r.value.lat
                    prefs.homeLon = r.value.lon
                    prefs.radiusM = r.value.radiusM
                    // Cache the reporting mode too, so arming and the boot
                    // re-arm can apply it without another round-trip.
                    prefs.saveMode(r.value.mode)
                    // Places are optional; fetchPlaces returns empty rather
                    // than failing if the hub has none or is older.
                    prefs.savePlaces(HubClient.fetchPlaces(prefs))
                    status("Paired. Home is ${fmt(r.value.lat)}, ${fmt(r.value.lon)} " +
                        "(${r.value.radiusM.toInt()} m).")
                }
                is HubClient.Result.Err -> status("Pairing failed: ${r.message}")
            }
            b.pairBtn.isEnabled = true
            render()
        }
    }

    /**
     * Search the local network for a hub and offer its address.
     *
     * Stops on the first hit rather than listing every hub: households have
     * one, and a picker for a list of one is friction. The dialog names what
     * was found so a second hub on the network is still visible as wrong.
     */
    private fun discover() {
        status(getString(R.string.discover_searching))
        b.discoverBtn.isEnabled = false

        var handle: Discovery.Handle? = null
        var settled = false

        fun finish(block: () -> Unit) {
            if (settled) return
            settled = true
            handle?.stop()
            runOnUiThread {
                b.discoverBtn.isEnabled = true
                block()
            }
        }

        handle = Discovery.start(this, onFound = { hub ->
            finish {
                val url = hub.preferredUrl
                AlertDialog.Builder(this)
                    .setTitle(R.string.discover_title)
                    .setMessage(getString(
                        if (hub.hasPublic) R.string.discover_body
                        else R.string.discover_body_local,
                        hub.name, url,
                    ))
                    .setPositiveButton(R.string.discover_use) { _, _ ->
                        b.hubUrl.setText(url)
                        // Trust is bound to a host, so switching address must
                        // invalidate it — otherwise a pin taken from the LAN
                        // certificate would be checked against the tunnel's.
                        prefs.trustMode = ""
                        prefs.certPin = ""
                        status("Address set. Enter your user id and token, then Pair.")
                    }
                    .setNegativeButton(R.string.cancel, null)
                    .show()
            }
        }, onError = { msg ->
            finish { status(msg) }
        })

        // mDNS has no "finished" signal — it browses until stopped. Give it a
        // few seconds, then report nothing found rather than spinning forever.
        b.discoverBtn.postDelayed({
            finish { status(getString(R.string.discover_none)) }
        }, 6000)
    }

    private fun requestPermissions() {
        if (!Geofencing.hasForegroundLocation(this)) {
            requestForeground.launch(arrayOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
            ))
        } else {
            requestBackgroundIfNeeded()
        }
    }

    private fun requestBackgroundIfNeeded() {
        if (Geofencing.hasBackgroundLocation(this)) { status("Location already granted."); render(); return }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) { render(); return }

        // Tell the user what they're about to be asked and why — the system dialog
        // says "Allow all the time" with no context, and it's the one people refuse.
        AlertDialog.Builder(this)
            .setTitle(R.string.bg_title)
            .setMessage(R.string.bg_body)
            .setPositiveButton(R.string.bg_ok) { _, _ ->
                requestBackground.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun explainBackgroundRefusal() {
        // After a second refusal Android stops showing the prompt entirely, so
        // the only route left is the app's settings page. Say that, rather than
        // letting the button appear broken.
        AlertDialog.Builder(this)
            .setTitle(R.string.bg_denied_title)
            .setMessage(R.string.bg_denied_body)
            .setPositiveButton(R.string.open_settings) { _, _ ->
                startActivity(Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null)
                ))
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun arm() {
        if (!prefs.hasHome) { status("Pair first — no home location cached."); return }
        status("Arming…")
        Geofencing.arm(
            this, prefs.homeLat, prefs.homeLon, prefs.radiusM, prefs.loadPlaces(),
        ) { err ->
            runOnUiThread {
                if (err == null) {
                    prefs.armed = true
                    HeartbeatWorker.schedule(this, prefs.heartbeatS)
                    status("Armed. Reporting your position…")
                    reportNow()
                } else {
                    prefs.armed = false
                    status("Could not arm: $err")
                }
                render()
            }
        }
    }

    /**
     * Send the current position now, instead of waiting for a crossing.
     *
     * Without this the hub sits at "unknown" until the user next walks through
     * the boundary — which, if they armed at home, could be hours. An unknown
     * state right after a successful pairing is indistinguishable from a broken
     * one, so we resolve it immediately.
     */
    private fun reportNow() {
        Geofencing.currentFix(this) { loc ->
            if (loc == null) {
                runOnUiThread {
                    status("Armed, but no location fix yet. The hub will update " +
                           "when you next arrive or leave.")
                    render()
                }
                return@currentFix
            }
            lifecycleScope.launch {
                val r = HubClient.postFix(
                    prefs, loc.latitude, loc.longitude,
                    if (loc.hasAccuracy()) loc.accuracy else null,
                    loc.time / 1000.0,
                )
                status(when (r) {
                    is HubClient.Result.Ok ->
                        "Armed. Position reported — the hub knows where you are."
                    is HubClient.Result.Err ->
                        "Armed, but reporting position failed: ${r.message}"
                })
                render()
            }
        }
    }

    private fun disarm() {
        Geofencing.disarm(this) { err ->
            runOnUiThread {
                prefs.armed = false
                // Stop the heartbeat too, or a disarmed phone keeps waking up
                // to report a position nobody asked for.
                HeartbeatWorker.cancel(this)
                status(if (err == null) "Disarmed." else "Disarm reported: $err")
                render()
            }
        }
    }

    private fun forget() {
        AlertDialog.Builder(this)
            .setTitle(R.string.forget_title)
            .setMessage(R.string.forget_body)
            .setPositiveButton(R.string.forget_ok) { _, _ ->
                Geofencing.disarm(this)
                HeartbeatWorker.cancel(this)
                prefs.clear()
                b.hubUrl.setText(""); b.userId.setText(""); b.token.setText("")
                status("Forgotten. Revoke the token on the hub too.")
                render()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    // ---- ui ----

    private fun render() {
        val fg = Geofencing.hasForegroundLocation(this)
        val bg = Geofencing.hasBackgroundLocation(this)

        b.permState.text = when {
            !fg -> getString(R.string.perm_none)
            !bg -> getString(R.string.perm_fg_only)
            else -> getString(R.string.perm_ok)
        }
        b.permsBtn.isEnabled = !fg || !bg
        b.armBtn.isEnabled = prefs.isPaired && prefs.hasHome && fg && bg
        b.armBtn.setText(if (prefs.armed) R.string.disarm else R.string.arm)
        b.homeState.text = if (prefs.hasHome) {
            getString(R.string.home_fmt, fmt(prefs.homeLat), fmt(prefs.homeLon), prefs.radiusM.toInt())
        } else getString(R.string.home_none)
    }

    private fun status(s: String) { b.status.text = s }

    private fun fmt(d: Double) = String.format("%.5f", d)

    /** Matches the `device_id` the hub's token model expects for revocation UX. */
    @Suppress("HardwareIds")
    private fun deviceId(): String =
        Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
}
