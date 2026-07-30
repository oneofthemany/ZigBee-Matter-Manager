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
import androidx.appcompat.app.AppCompatDelegate
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

    private val requestBluetooth = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) showCarPicker()
        else status(getString(R.string.car_bt_denied))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        // Before super.onCreate: setDefaultNightMode recreates any started
        // activity to apply, so setting it after would build the whole screen
        // once in the wrong theme and immediately throw it away — visible as a
        // flash of the other palette on every launch.
        AppCompatDelegate.setDefaultNightMode(Prefs(this).themeMode)

        super.onCreate(savedInstanceState)
        b = ActivityPairBinding.inflate(layoutInflater)
        setContentView(b.root)
        applyBackdrop()
        applyWindowInsets()
        prefs = Prefs(this)

        b.hubUrl.setText(prefs.hubUrl)
        b.userId.setText(prefs.userId)
        b.token.setText(prefs.token)

        b.pairBtn.setOnClickListener { pair() }
        b.permsBtn.setOnClickListener { requestPermissions() }
        b.batteryBtn.setOnClickListener { requestBatteryExemption() }
        b.carBtn.setOnClickListener { chooseCar() }
        b.armBtn.setOnClickListener { if (prefs.armed) disarm() else arm() }
        b.forgetBtn.setOnClickListener { forget() }
        b.discoverBtn.setOnClickListener { discover() }
        b.themeBtn.setOnClickListener { cycleTheme() }
        renderThemeButton()

        b.deviceId.text = getString(R.string.device_id_fmt, deviceId())
        b.versionLabel.text = getString(
            R.string.version_fmt, BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE)
        render()
    }

    /**
     * The honeycomb backdrop, on the scroll container so it stays put while the
     * content moves over it.
     *
     * Built here rather than as a background resource because the comb has to
     * tile, and tiling on Android means a BitmapDrawable — a density ladder of
     * PNGs for what is six lines to a stroke. See [HoneycombDrawable].
     */
    private fun applyBackdrop() {
        val d = resources.displayMetrics.density
        b.root.background = HoneycombDrawable(
            baseColor = getColor(R.color.bg_body),
            strokeColor = getColor(R.color.comb_stroke),
            glowColor = getColor(R.color.comb_glow),
            cellRadius = 30f * d,
            strokeWidthPx = 1f * d,
        )
    }

    /**
     * Keep content clear of the system bars.
     *
     * targetSdk 35 (Android 15) forces edge-to-edge: the window starts at y=0,
     * so without this the hero sits under the status bar and the last button
     * under the navigation bar.
     *
     * The theme is NoActionBar — the hero strip is the title — so the system
     * inset is the whole story here.
     *
     * Padding goes on the ScrollView and clipToPadding stays at its default of
     * true, so scrolled content is clipped at the status bar instead of sliding
     * under the clock. The honeycomb still reaches the screen edges regardless:
     * a view's background is drawn across its whole box, padding included.
     */
    private fun applyWindowInsets() {
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(b.root) { v, insets ->
            val bars = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                androidx.core.view.WindowInsetsCompat.Type.displayCutout()
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
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
                    prefs.saveJourneys(r.value.journeys)
                    // Places are optional; a hub predating them (or a token
                    // without presence:read) simply pairs with none rather
                    // than failing pairing outright.
                    when (val pr = HubClient.fetchPlaces(prefs)) {
                        is HubClient.Result.Ok -> prefs.savePlaces(pr.value)
                        is HubClient.Result.Err -> prefs.savePlaces(emptyList())
                    }
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

    /**
     * True once the OS has stopped throttling this app's background work.
     *
     * This is the actual cause of a phone drifting to "unknown" over time.
     * WorkManager's periodic heartbeat (Heartbeat.kt) is registered correctly
     * regardless of this setting — the OS just defers WHEN it is allowed to
     * run, more aggressively the less often the app is opened. Geofence
     * ENTER/EXIT events are delivered at a higher priority and mostly escape
     * this, which is why crossings can keep working while the heartbeat that
     * fills the gaps between them quietly stops.
     */
    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(android.os.PowerManager::class.java) ?: return true
        return pm.isIgnoringBatteryOptimizations(packageName)
    }

    private fun requestBatteryExemption() {
        if (isIgnoringBatteryOptimizations()) { render(); return }

        AlertDialog.Builder(this)
            .setTitle(R.string.battery_title)
            .setMessage(R.string.battery_body)
            .setPositiveButton(R.string.battery_ok_btn) { _, _ ->
                // No activity-result callback needed: onResume() already
                // re-renders on return from Settings, which picks up whatever
                // the user chose in the system dialog — the same pattern
                // requestBackgroundIfNeeded() relies on for its own fallback.
                try {
                    startActivity(Intent(
                        Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:$packageName"),
                    ))
                } catch (e: Exception) {
                    // A handful of OEM builds strip this intent from the ROM.
                    // Say so rather than leaving the button looking broken.
                    status(getString(R.string.battery_unsupported))
                }
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /**
     * Pick which bonded Bluetooth device is "the car".
     *
     * Bonded devices, not nearby scan: the car is something the phone is
     * already paired with (calls, Android Auto), a scan needs location-tinged
     * permissions and a UI for transient strangers, and the failure mode of
     * bonded-only is a clear message telling the user to pair with the car
     * first — which they will have done already in practice.
     */
    private fun chooseCar() {
        // Guarded here as well as by the disabled button: the button can be
        // tapped in the moment between editing the URL and render() running,
        // and picking a car that can never report is worse than saying why.
        if (!Prefs.isPublicUrl(prefs.hubUrl)) {
            status(getString(R.string.car_needs_public))
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) !=
                android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestBluetooth.launch(Manifest.permission.BLUETOOTH_CONNECT)
            return
        }
        showCarPicker()
    }

    @android.annotation.SuppressLint("MissingPermission")   // checked in chooseCar()
    private fun showCarPicker() {
        val adapter = getSystemService(android.bluetooth.BluetoothManager::class.java)?.adapter
        val bonded = try {
            adapter?.bondedDevices?.toList() ?: emptyList()
        } catch (e: SecurityException) {
            emptyList()
        }
        if (bonded.isEmpty()) {
            status(getString(R.string.car_no_bonded))
            return
        }

        val labels = bonded.map { d ->
            val name = d.name ?: d.address
            if (d.address == prefs.carBtAddress) "$name  ✓" else name
        } + getString(R.string.car_pick_none)

        AlertDialog.Builder(this)
            .setTitle(R.string.car_pick_title)
            .setItems(labels.toTypedArray()) { _, which ->
                if (which == bonded.size) {
                    // "None" — turn drive mode off, and stop it if running.
                    prefs.carBtAddress = ""
                    prefs.carBtName = ""
                    DriveService.stop(this)
                    status(getString(R.string.car_cleared))
                } else {
                    val d = bonded[which]
                    prefs.carBtAddress = d.address
                    prefs.carBtName = d.name ?: d.address
                    status(getString(R.string.car_saved, prefs.carBtName))
                }
                render()
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
                    PassiveUpdates.register(this)
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
                // to report a position nobody asked for. Same for drive mode:
                // disarmed means "stop reporting", full stop.
                HeartbeatWorker.cancel(this)
                DriveService.stop(this)
                PassiveUpdates.unregister(this)
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
                DriveService.stop(this)
                PassiveUpdates.unregister(this)
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

        val batteryOk = isIgnoringBatteryOptimizations()
        b.batteryState.text = getString(if (batteryOk) R.string.battery_ok else R.string.battery_none)
        b.batteryBtn.isEnabled = !batteryOk

        // Drive mode needs a hub reachable from outside the home network: it
        // reports every minute while the car is connected, which is precisely
        // when the phone is off the home Wi-Fi. See Prefs.isPublicUrl.
        val publicHub = Prefs.isPublicUrl(prefs.hubUrl)
        b.carBtn.isEnabled = publicHub
        b.carState.text = when {
            !publicHub -> getString(R.string.car_needs_public)
            prefs.carBtAddress.isEmpty() -> getString(R.string.car_none)
            else -> getString(R.string.car_fmt, prefs.carBtName.ifEmpty { prefs.carBtAddress })
        }

        b.armBtn.isEnabled = prefs.isPaired && prefs.hasHome && fg && bg
        b.armBtn.setText(if (prefs.armed) R.string.disarm else R.string.arm)
        b.homeState.text = if (prefs.hasHome) {
            getString(R.string.home_fmt, fmt(prefs.homeLat), fmt(prefs.homeLon), prefs.radiusM.toInt())
        } else getString(R.string.home_none)

        // Pills restate the section's state in one word, so the whole screen
        // can be read down the right-hand edge without parsing the sentences.
        pill(b.hubPill,
            if (prefs.isPaired) R.string.pill_paired else R.string.pill_unpaired,
            if (prefs.isPaired) Tone.OK else Tone.IDLE)

        pill(b.geoPill,
            if (prefs.armed) R.string.pill_armed else R.string.pill_disarmed,
            if (prefs.armed) Tone.OK else Tone.IDLE)

        // Foreground-only is called out separately: it looks like a working
        // permission right up until the app is closed, which is when the
        // geofence actually matters.
        pill(b.permPill,
            when {
                fg && bg -> R.string.pill_ok
                fg -> R.string.pill_partial
                else -> R.string.pill_action
            },
            when {
                fg && bg -> Tone.OK
                fg -> Tone.WARN
                else -> Tone.BAD
            })

        val driveOn = publicHub && prefs.carBtAddress.isNotEmpty()
        pill(b.drivePill,
            when {
                !publicHub -> R.string.pill_unavailable
                driveOn -> R.string.pill_on
                else -> R.string.pill_off
            },
            when {
                !publicHub -> Tone.WARN
                driveOn -> Tone.OK
                else -> Tone.IDLE
            })
    }

    // ---- theme ----

    /**
     * Cycle system -> light -> dark -> system.
     *
     * A cycle rather than a dialog: three states is few enough to tap through,
     * and the icon shows which one is current. "System" is in the cycle, not
     * just the default, so a deliberate choice can be handed back.
     */
    private fun cycleTheme() {
        prefs.themeMode = when (prefs.themeMode) {
            AppCompatDelegate.MODE_NIGHT_NO -> AppCompatDelegate.MODE_NIGHT_YES
            AppCompatDelegate.MODE_NIGHT_YES -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
            else -> AppCompatDelegate.MODE_NIGHT_NO
        }
        renderThemeButton()
        // Recreates the activity. The backdrop is rebuilt from the newly
        // resolved colours on the way through, so nothing else has to react.
        AppCompatDelegate.setDefaultNightMode(prefs.themeMode)
    }

    private fun renderThemeButton() {
        b.themeBtn.setIconResource(
            when (prefs.themeMode) {
                AppCompatDelegate.MODE_NIGHT_NO -> R.drawable.ic_theme_light
                AppCompatDelegate.MODE_NIGHT_YES -> R.drawable.ic_theme_dark
                else -> R.drawable.ic_theme_system
            }
        )
        // The icon alone says which mode is active; the description says it out
        // loud for TalkBack, where an icon swap is invisible.
        b.themeBtn.contentDescription = getString(
            when (prefs.themeMode) {
                AppCompatDelegate.MODE_NIGHT_NO -> R.string.theme_light
                AppCompatDelegate.MODE_NIGHT_YES -> R.string.theme_dark
                else -> R.string.theme_system
            }
        )
    }

    /** Pill colouring. Maps to the dashboard's status token set. */
    private enum class Tone { OK, WARN, BAD, IDLE }

    private fun pill(view: android.widget.TextView, textRes: Int, tone: Tone) {
        val color = getColor(
            when (tone) {
                Tone.OK -> R.color.status_ok
                Tone.WARN -> R.color.status_warn
                Tone.BAD -> R.color.status_bad
                Tone.IDLE -> R.color.text_muted
            }
        )
        view.setText(textRes)
        view.setTextColor(color)
        // Same hue as the text at low alpha, which is how the dashboard builds
        // its *-subtle fills. One colour per state instead of two to keep in
        // step.
        view.backgroundTintList = android.content.res.ColorStateList.valueOf(
            (color and 0x00FFFFFF) or (0x24 shl 24))
    }

    private fun status(s: String) { b.status.text = s }

    private fun fmt(d: Double) = String.format("%.5f", d)

    /** Matches the `device_id` the hub's token model expects for revocation UX. */
    @Suppress("HardwareIds")
    private fun deviceId(): String =
        Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
}
