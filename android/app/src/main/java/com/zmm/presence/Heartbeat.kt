package com.zmm.presence

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * Periodic position report.
 *
 * Geofence crossings alone are not enough. A phone that settles anywhere stops
 * emitting events entirely, and the hub's `stale_after_s` then flips the user
 * to "unknown" while they are sitting at home — indistinguishable, from the
 * hub's side, from a phone that has been switched off. The heartbeat is what
 * makes "still here" observable.
 *
 * Interval comes from the hub's reporting mode. WorkManager enforces a 15
 * minute floor on periodic work and will silently round anything shorter up to
 * it, so the modes are defined at or above that rather than pretending
 * otherwise. WorkManager (not AlarmManager) because these reports should yield
 * to Doze: a missed heartbeat is cheap, and the stale window is sized to
 * tolerate two of them.
 */
class HeartbeatWorker(
    ctx: Context,
    params: WorkerParameters,
) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val prefs = Prefs(applicationContext)

        // Unpaired or disarmed: cancel rather than fail. Returning failure
        // would leave the work registered and retrying forever.
        if (!prefs.isPaired || !prefs.armed) {
            Log.i(TAG, "not paired/armed — cancelling heartbeat")
            cancel(applicationContext)
            return Result.success()
        }

        val loc = awaitFix(applicationContext)
        if (loc == null) {
            Log.w(TAG, "no location fix available this cycle")
            // Retry rather than fail: a single cycle with no fix is routine
            // indoors, and WorkManager backs off instead of hammering.
            return Result.retry()
        }

        return when (val r = HubClient.postFix(
            prefs, loc.latitude, loc.longitude,
            if (loc.hasAccuracy()) loc.accuracy else null,
            loc.time / 1000.0,
        )) {
            is HubClient.Result.Ok -> {
                Log.i(TAG, "heartbeat reported")
                Result.success()
            }
            is HubClient.Result.Err -> {
                // Could be transient (no signal) or permanent (revoked token).
                // Retry covers the first; the second surfaces in the hub as a
                // user going stale, which is the correct visible outcome.
                Log.w(TAG, "heartbeat failed: ${r.message}")
                Result.retry()
            }
        }
    }

    private suspend fun awaitFix(ctx: Context): android.location.Location? =
        suspendCancellableCoroutine { cont ->
            Geofencing.currentFix(ctx) { loc -> cont.resume(loc) }
        }

    companion object {
        private const val TAG = "ZmmHub"
        private const val WORK_NAME = "zmm_heartbeat"

        /**
         * (Re)schedule the heartbeat at the mode's interval.
         *
         * UPDATE policy so a mode change on the hub retunes the existing
         * schedule instead of stacking a second one alongside it.
         */
        fun schedule(ctx: Context, intervalS: Long) {
            val interval = intervalS.coerceAtLeast(MIN_INTERVAL_S)
            val req = PeriodicWorkRequestBuilder<HeartbeatWorker>(
                interval, TimeUnit.SECONDS,
            )
                .setConstraints(
                    Constraints.Builder()
                        // No point waking to report with nowhere to send it.
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 60, TimeUnit.SECONDS)
                .build()

            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, req,
            )
            Log.i(TAG, "heartbeat scheduled every ${interval}s")
        }

        fun cancel(ctx: Context) {
            WorkManager.getInstance(ctx).cancelUniqueWork(WORK_NAME)
            Log.i(TAG, "heartbeat cancelled")
        }

        /** WorkManager's floor for periodic work; shorter requests are rounded up. */
        const val MIN_INTERVAL_S = 900L
    }
}
