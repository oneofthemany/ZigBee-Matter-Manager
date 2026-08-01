package com.zmm.presence

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * Per-minute position reporting while connected to the car — the Home
 * Assistant "high accuracy mode" pattern, triggered the same way (the car's
 * Bluetooth connecting).
 *
 * WHY A FOREGROUND SERVICE, when the rest of this app deliberately avoids
 * one: the 15-minute WorkManager floor is fine for a phone sitting in a
 * pocket, but useless in a car — you can drive from the motorway to your
 * driveway inside one heartbeat, and the hub learns you're home only when
 * the geofence ENTER happens to fire. A foreground service is the one
 * Android mechanism allowed to stream location on a fixed cadence, and the
 * usual objection to it — battery — does not apply here: the phone is
 * charging, Android Auto's navigation already has the GPS hot (so these
 * fixes are nearly free), and the service lives exactly as long as the
 * drive.
 *
 * Starting from the background is legal on two independent grounds: a
 * BLUETOOTH_CONNECT broadcast is on Android's background-FGS-start exemption
 * list, and the battery-optimization exemption the app requests is another.
 * With ACCESS_BACKGROUND_LOCATION held, the while-in-use location
 * restriction on background-started services does not apply either.
 */
class DriveService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var callback: LocationCallback? = null

    /**
     * Inertial sampling, running only when journeys are on.
     *
     * Null otherwise, and deliberately so: motion data exists to describe a
     * recorded drive, and with journey recording off the hub discards the fix
     * anyway. Sampling three sensors at 50 Hz to build a summary nobody stores
     * would be pure cost.
     */
    private var motion: MotionSampler? = null

    /**
     * Held for the life of the service.
     *
     * The location callback survives Doze on its own — Play Services wakes the
     * device to deliver it — but sensor listeners do not: when the AP suspends,
     * a non-wakeup sensor simply stops reporting, and every brake and corner in
     * that gap is lost. A drive with the screen off and the phone in a pocket
     * would silently record position but no behaviour. The usual objection to a
     * wake lock does not apply for the same reason it does not apply to this
     * being a foreground service at all: the phone is in a car, on charge, for
     * exactly as long as the drive lasts.
     */
    private var wakeLock: android.os.PowerManager.WakeLock? = null

    /** Fixes the hub refused or never received. See [FixSpool]. */
    private val spool by lazy { FixSpool(this) }

    /**
     * One drive, one id. Generated when the service starts (the car
     * connecting) and dies with it, so the hub can group this drive's fixes
     * into a journey without guessing at gaps. Null when the user hasn't
     * opted in to journey recording — the fixes then carry no trip tag and
     * the hub stores nothing.
     */
    private var tripId: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = Prefs(this)

        // Checked here, not just in the receiver: the service can be started
        // from the UI too, and a stale start after Forget must not stream
        // location for a hub that no longer exists.
        // isPublicUrl is part of the gate, not just a UI affordance: drive mode
        // runs away from home by definition, so a LAN-only hub yields a
        // foreground service burning GPS for requests that cannot arrive. A
        // pairing made before Remote Access was set up reaches here otherwise.
        val allowed = prefs.isPaired && prefs.armed &&
            Prefs.isPublicUrl(prefs.hubUrl) &&
            Geofencing.hasForegroundLocation(this)

        // Foreground status is claimed BEFORE deciding whether to run

        if (!startInForeground(prefs.carBtName, withLocation = allowed)) {
            stopSelf()
            return START_NOT_STICKY
        }

        if (!allowed) {
            Log.i(TAG, "not paired/armed/public — drive mode refused")
            stopSelf()
            return START_NOT_STICKY
        }

        // A second ACL_CONNECTED for the same car (they arrive per-profile on
        // some head units) must not stack a second location callback, sensor
        // registration and wake lock on top of the running ones — nor split
        // one drive into two trips.
        if (running) {
            Log.i(TAG, "drive mode already running; ignoring duplicate start")
            return START_NOT_STICKY
        }

        tripId = if (prefs.journeysEnabled) resumeOrStartTrip(prefs) else null
        if (tripId != null) startMotion()
        startUpdates(prefs)
        running = true
        Log.i(TAG, "drive mode started (${prefs.carBtName})")

        // NOT_STICKY: if the system kills us mid-drive there is no reliable
        // way to know on restart whether the car is still connected, and a
        // resurrected service with the car gone would stream forever. The
        // next BT connect restarts it; meanwhile the geofence still catches
        // arrival, which is the outcome that actually matters.
        return START_NOT_STICKY
    }

    /**
     * Claim foreground status. Returns false if the system refused it.
     *
     * A refusal is not a crash to propagate: it means this service may not run
     * right now — the background-start exemption did not apply, or the
     * location permission behind the service type is gone — and the caller's
     * correct response is to stop cleanly rather than take the app down. The
     * geofence and heartbeat channels still cover presence either way.
     */
    private fun startInForeground(carName: String, withLocation: Boolean): Boolean {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_ID,
            getString(R.string.drive_channel),
            // LOW: visible in the shade (Android requires the user can see a
            // location FGS is running) but never sounds or pops.
            NotificationManager.IMPORTANCE_LOW,
        ))

        val open = PendingIntent.getActivity(
            this, 0, Intent(this, PairActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notif: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle(getString(R.string.drive_notif_title))
            .setContentText(getString(R.string.drive_notif_body,
                carName.ifEmpty { getString(R.string.drive_car_generic) }))
            .setOngoing(true)
            .setContentIntent(open)
            .build()

        return try {
            ServiceCompat.startForeground(
                this, NOTIF_ID, notif,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && withLocation)
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0,
            )
            true
        } catch (e: Exception) {
            // Deliberately broad. The refusals live in different Android
            // versions and different exception types —
            // ForegroundServiceStartNotAllowedException from 12,
            // SecurityException and MissingForegroundServiceTypeException
            // from 14 — and catching them by name would mean compiling
            // against classes that do not exist on older platforms while
            // still missing whatever a future release adds. They all mean the
            // same thing here.
            Log.w(TAG, "foreground start refused", e)
            false
        }
    }

    /**
     * Continue the trip already in progress, or begin a new one.
     *
     * A drive is not the same thing as one run of this service. The service
     * ends whenever the car's Bluetooth link drops — which head units do
     * spuriously, mid-drive, as profiles renegotiate — and whenever the system
     * decides to reclaim it. Minting a fresh id on every start turns one
     * journey into a run of fragments, and the hub discards any fragment under
     * MIN_TRIP_FIXES as a blip, so most of them vanish entirely and the
     * survivor is a stub.
     *
     * The resume window matches the hub's TRIP_CLOSE_GAP_S deliberately: below
     * it the hub would have treated the fixes as one trip regardless, so
     * reusing the id tells it the truth rather than overriding its judgement.
     * Above it the drive really did end, and a new id is correct.
     */
    private fun resumeOrStartTrip(prefs: Prefs): String {
        val gap = System.currentTimeMillis() - prefs.driveTripLastMs
        val previous = prefs.driveTripId
        // A negative gap means the clock moved backwards (NTP correction, a
        // timezone-less RTC catching up at boot). Trusting it could resume a
        // trip from any point in the past.
        val resumable = previous.isNotEmpty() && gap in 0..TRIP_RESUME_WINDOW_MS

        val id = if (resumable) previous else java.util.UUID.randomUUID().toString()
        if (resumable) {
            Log.i(TAG, "resuming trip $id (${gap / 1000}s gap)")
        } else {
            Log.i(TAG, "starting trip $id")
        }
        prefs.driveTripId = id
        prefs.driveTripLastMs = System.currentTimeMillis()
        return id
    }

    private fun startMotion() {
        wakeLock = getSystemService(android.os.PowerManager::class.java)
            ?.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, WAKE_TAG)
            ?.apply {
                setReferenceCounted(false)
                // No timeout: the lifetime that bounds this is the service's,
                // and onDestroy releases it on every path out — including the
                // system killing the service, which tears down the process.
                acquire()
            }
        motion = MotionSampler(this).also { it.start() }
    }

    @android.annotation.SuppressLint("MissingPermission")   // checked in onStartCommand
    private fun startUpdates(prefs: Prefs) {
        val cb = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                val loc = result.lastLocation ?: return

                // Drain before the coroutine, not inside it: the window belongs
                // to the interval that just ended, and posting is asynchronous.
                // Draining after an await would fold part of the next interval
                // into this fix.
                // Keep the trip alive for a restart. Written per fix so the
                // window is measured from the last thing that actually
                // happened, not from when the service started.
                if (tripId != null) prefs.driveTripLastMs = System.currentTimeMillis()

                val m = motion
                if (m != null && loc.hasSpeed()) m.noteGpsSpeed(loc.speed)
                val window = m?.takeWindow()
                val events = m?.takeEvents() ?: emptyList()

                val payload = HubClient.fixPayload(
                    loc.latitude, loc.longitude,
                    if (loc.hasAccuracy()) loc.accuracy else null,
                    loc.time / 1000.0,
                    // GPS doppler speed/bearing: far better than anything
                    // derivable from consecutive fixes, and free — the
                    // receiver computes them anyway.
                    speedMps = if (loc.hasSpeed()) loc.speed else null,
                    bearingDeg = if (loc.hasBearing()) loc.bearing else null,
                    tripId = tripId,
                    // GNSS altitude is coarse (tens of metres), but summed
                    // over a drive with a deadband it still separates a
                    // climb over a pass from a flat run. Where the phone
                    // has a barometer the hub prefers that instead.
                    altitudeM = if (loc.hasAltitude()) loc.altitude else null,
                    motion = window,
                    events = events,
                    activity = prefs.currentActivity,
                )

                scope.launch {
                    // Backlog first, live fix second. A drain that ran after
                    // the live post would leave the hub's last-write-wins
                    // presence sitting on an old position; this way the
                    // newest fix is always the last one written.
                    spool.drain(prefs)

                    when (val r = HubClient.postRaw(prefs, payload)) {
                        is HubClient.Result.Ok -> Log.i(TAG, "drive fix reported")
                        is HubClient.Result.Err -> {
                            // Tunnels and dead spots are routine in a car, and
                            // the fix is not replaceable — the car will not be
                            // on this stretch of road again. Keep it.
                            Log.w(TAG, "drive fix failed, spooled: ${r.message}")
                            spool.offer(payload)
                        }
                    }
                }
            }
        }
        callback = cb

        // HIGH_ACCURACY is the right call here and only here: Android Auto's
        // navigation keeps the GPS lit for the whole drive, so these fixes
        // piggyback on hardware that is already on. Everywhere else this app
        // uses balanced power for exactly the opposite reason.
        //
        // Cadence: hub-decided when journeys are on (default 10 s, so miles
        // and the speed distribution are honestly sampled); the pre-journeys
        // once-a-minute otherwise, since presence alone needs no more.
        val intervalMs = if (prefs.journeysEnabled)
            prefs.driveIntervalS.coerceAtLeast(1L) * 1000L
        else
            UPDATE_INTERVAL_MS
        val request = LocationRequest.Builder(
            Priority.PRIORITY_HIGH_ACCURACY, intervalMs,
        ).build()

        LocationServices.getFusedLocationProviderClient(this)
            .requestLocationUpdates(request, cb, mainLooper)
    }

    override fun onDestroy() {
        running = false
        callback?.let {
            LocationServices.getFusedLocationProviderClient(this)
                .removeLocationUpdates(it)
        }
        // Whatever the sampler still holds covers the seconds since the last
        // fix and is dropped with it. Deliberately: a network call from
        // onDestroy is not reliable enough to build on — the same reason the
        // hub closes trips on an idle timeout rather than waiting to be told —
        // and what is lost is the tail of a car that has already stopped.
        motion?.stop()
        motion = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        scope.cancel()
        Log.i(TAG, "drive mode stopped")
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ZmmDrive"
        private const val CHANNEL_ID = "zmm_drive"
        private const val NOTIF_ID = 41
        private const val UPDATE_INTERVAL_MS = 60_000L
        private const val WAKE_TAG = "zmm:drive-motion"

        /**
         * How long a restart may still count as the same drive. Mirrors the
         * hub's TRIP_CLOSE_GAP_S (journeys.py); keep the two in step.
         */
        private const val TRIP_RESUME_WINDOW_MS = 300_000L

        /**
         * Whether drive mode is currently streaming. Read by
         * [PassiveLocationReceiver] to stand down while driving — the two
         * channels would otherwise post the same track twice.
         */
        @Volatile
        var running = false
            private set

        fun start(ctx: Context) {
            // The other half of the same problem the service guards against,
            // and it lands in the CALLER's process: from Android 12 a
            // background startForegroundService throws unless an exemption
            // applies. Two are expected to (the Bluetooth broadcast, and the
            // battery-optimisation exemption the app asks for), but "expected
            // to" is not "does" across every OEM — and an uncaught throw here
            // means the app crashing at the moment the user gets into the car.
            // Drive mode is an upgrade to reporting, never its foundation;
            // failing to start one is a log line, not a crash.
            try {
                ContextCompat.startForegroundService(
                    ctx, Intent(ctx, DriveService::class.java))
            } catch (e: Exception) {
                Log.w(TAG, "could not start drive mode", e)
            }
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, DriveService::class.java))
        }
    }
}
