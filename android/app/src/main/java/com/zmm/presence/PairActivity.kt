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
        prefs = Prefs(this)

        b.hubUrl.setText(prefs.hubUrl)
        b.userId.setText(prefs.userId)
        b.token.setText(prefs.token)

        b.pairBtn.setOnClickListener { pair() }
        b.permsBtn.setOnClickListener { requestPermissions() }
        b.armBtn.setOnClickListener { if (prefs.armed) disarm() else arm() }
        b.forgetBtn.setOnClickListener { forget() }

        b.deviceId.text = getString(R.string.device_id_fmt, deviceId())
        render()
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

        // No pin yet: learn the hub's key and have the user vouch for it before
        // we store it or send the token anywhere.
        if (prefs.certPin.isEmpty()) { confirmPin(); return }

        fetchHome()
    }

    /**
     * Trust-on-first-use. We show the fingerprint and ask the user to compare it
     * with their hub before accepting. This is the one moment the app cannot
     * verify anything for them, so it says so plainly rather than dressing an
     * unverified key up as a routine confirmation.
     */
    private fun confirmPin() {
        status("Fetching hub certificate…")
        b.pairBtn.isEnabled = false
        lifecycleScope.launch {
            val probed = try {
                val u = java.net.URL(prefs.hubUrl)
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                    CertPin.probe(u.host, if (u.port == -1) 443 else u.port, 15_000)
                }
            } catch (e: Exception) {
                status("Could not reach the hub: ${e.message}")
                b.pairBtn.isEnabled = true
                return@launch
            }

            val pin = CertPin.spkiPin(probed)
            b.pairBtn.isEnabled = true
            AlertDialog.Builder(this@PairActivity)
                .setTitle(R.string.pin_title)
                .setMessage(getString(R.string.pin_body, CertPin.readable(pin), probed.subjectDN.name))
                .setPositiveButton(R.string.pin_accept) { _, _ ->
                    prefs.certPin = pin
                    fetchHome()
                }
                .setNegativeButton(R.string.cancel) { _, _ -> status("Pairing cancelled.") }
                .show()
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
                    status("Paired. Home is ${fmt(r.value.lat)}, ${fmt(r.value.lon)} " +
                        "(${r.value.radiusM.toInt()} m).")
                }
                is HubClient.Result.Err -> status("Pairing failed: ${r.message}")
            }
            b.pairBtn.isEnabled = true
            render()
        }
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
        Geofencing.arm(this, prefs.homeLat, prefs.homeLon, prefs.radiusM) { err ->
            runOnUiThread {
                if (err == null) {
                    prefs.armed = true
                    status("Armed. The hub will be told when you arrive or leave.")
                } else {
                    prefs.armed = false
                    status("Could not arm: $err")
                }
                render()
            }
        }
    }

    private fun disarm() {
        Geofencing.disarm(this) { err ->
            runOnUiThread {
                prefs.armed = false
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
