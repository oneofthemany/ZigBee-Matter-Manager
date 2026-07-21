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

        // Guard here, not just in the receiver: the service can be started
        // from the UI too, and a stale start after Forget must not stream
        // location for a hub that no longer exists.
        if (!prefs.isPaired || !prefs.armed || !Geofencing.hasForegroundLocation(this)) {
            Log.i(TAG, "not paired/armed — drive mode refused")
            stopSelf()
            return START_NOT_STICKY
        }

        startInForeground(prefs.carBtName)
        tripId = if (prefs.journeysEnabled)
            java.util.UUID.randomUUID().toString() else null
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

    private fun startInForeground(carName: String) {
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

        ServiceCompat.startForeground(
            this, NOTIF_ID, notif,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0,
        )
    }

    @android.annotation.SuppressLint("MissingPermission")   // checked in onStartCommand
    private fun startUpdates(prefs: Prefs) {
        val cb = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                val loc = result.lastLocation ?: return
                scope.launch {
                    when (val r = HubClient.postFix(
                        prefs, loc.latitude, loc.longitude,
                        if (loc.hasAccuracy()) loc.accuracy else null,
                        loc.time / 1000.0,
                        // GPS doppler speed/bearing: far better than anything
                        // derivable from consecutive fixes, and free — the
                        // receiver computes them anyway.
                        speedMps = if (loc.hasSpeed()) loc.speed else null,
                        bearingDeg = if (loc.hasBearing()) loc.bearing else null,
                        tripId = tripId,
                    )) {
                        is HubClient.Result.Ok -> Log.i(TAG, "drive fix reported")
                        is HubClient.Result.Err ->
                            // Transient gaps (tunnels, no signal) are normal in
                            // a car; the next minute's fix corrects it. Nothing
                            // to escalate.
                            Log.w(TAG, "drive fix failed: ${r.message}")
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
        scope.cancel()
        Log.i(TAG, "drive mode stopped")
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ZmmDrive"
        private const val CHANNEL_ID = "zmm_drive"
        private const val NOTIF_ID = 41
        private const val UPDATE_INTERVAL_MS = 60_000L

        /**
         * Whether drive mode is currently streaming. Read by
         * [PassiveLocationReceiver] to stand down while driving — the two
         * channels would otherwise post the same track twice.
         */
        @Volatile
        var running = false
            private set

        fun start(ctx: Context) {
            ContextCompat.startForegroundService(
                ctx, Intent(ctx, DriveService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, DriveService::class.java))
        }
    }
}
