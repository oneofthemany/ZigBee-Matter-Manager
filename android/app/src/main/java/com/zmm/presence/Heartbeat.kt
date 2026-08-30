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

        refreshConfig(prefs)

        // Deliver anything drive mode could not. Doing it here rather than
        // only at the start of the next drive means a journey truncated by a
        // dead spot is repaired within one heartbeat instead of waiting for
        // the car to be driven again — and the hub reopens the trip it had
        // already closed to fold the recovered stretch back in.
        //
        // Before the fresh fix below, for the ordering reason FixSpool
        // documents: the newest position must be the last one the hub sees.
        try {
            FixSpool(applicationContext).drain(prefs)
        } catch (e: Exception) {
            Log.w(TAG, "spool drain failed", e)
        }

        val loc = awaitFix(applicationContext)
        if (loc == null) {
            Log.w(TAG, "no location fix available this cycle")
            // Success, NOT retry. A cycle with no fix is routine indoors, and
            // this is periodic work: the next period is already scheduled, so
            // "nothing to report" needs no rescheduling. Retry here did the
            // opposite of what it looks like — a retry REPLACES the next
            // periodic run with a backed-off one, and the backoff doubles per
            // attempt up to WorkManager's five-hour ceiling. A few no-fix
            // cycles in a row therefore pushed the heartbeat hours past the
            // stale window, so the very condition that most needed the next
            // report on time was the one that delayed it furthest.
            return Result.success()
        }

        val payload = HubClient.fixPayload(
            loc.latitude, loc.longitude,
            if (loc.hasAccuracy()) loc.accuracy else null,
            loc.time / 1000.0,
        )

        return when (val r = HubClient.postRaw(prefs, payload)) {
            is HubClient.Result.Ok -> {
                Log.i(TAG, "heartbeat reported")
                Result.success()
            }
            is HubClient.Result.Err -> {
                // Could be transient (no signal, hub off the network while the
                // user is out) or permanent (revoked token). Spool rather than
                // retry: the drain at the top of the next cycle redelivers it
                // on the schedule already running, which covers the transient
                // case without the backoff compounding described above. A
                // permanent failure still surfaces in the hub as a user going
                // stale, which is the correct visible outcome.
                Log.w(TAG, "heartbeat failed, spooled: ${r.message}")
                try {
                    FixSpool(applicationContext).offer(payload)
                } catch (e: Exception) {
                    Log.w(TAG, "spool write failed", e)
                }
                Result.success()
            }
        }
    }

    private suspend fun awaitFix(ctx: Context): android.location.Location? =
        suspendCancellableCoroutine { cont ->
            Geofencing.currentFix(ctx) { loc -> cont.resume(loc) }
        }

    /**
     * Re-fetch mode, home and places from the hub, applying anything changed.
     *
     * This is the piece that was missing. Before this, `fetchHome` only ran at
     * PAIR time — so changing the reporting mode, radius, home location, or
     * apiary in the web UI had NO effect on an already-paired phone until it
     * was forgotten and re-paired. The "applied next time it contacts the hub"
     * hint in the UI was aspirational, not true: the phone contacts the hub
     * every heartbeat, but never used to ask it "has anything changed?".
     *
     * Runs before the fix is posted, on the same cycle that already talks to
     * the hub, so it costs nothing extra in wake-ups. Best-effort throughout:
     * a failed refresh must never stop the heartbeat from reporting position
     * with whatever config it already has cached.
     */
    private suspend fun refreshConfig(prefs: Prefs) {
        val r = try {
            HubClient.fetchHome(prefs)
        } catch (e: Exception) {
            Log.w(TAG, "config refresh failed", e)
            return
        }
        val home = (r as? HubClient.Result.Ok)?.value ?: run {
            if (r is HubClient.Result.Err) Log.i(TAG, "config refresh: ${r.message}")
            return
        }

        val modeChanged = home.mode.heartbeatS != prefs.heartbeatS ||
            home.mode.name != prefs.modeName
        val geofenceChanged = home.lat != prefs.homeLat ||
            home.lon != prefs.homeLon ||
            home.radiusM != prefs.radiusM

        prefs.homeLat = home.lat
        prefs.homeLon = home.lon
        prefs.radiusM = home.radiusM
        prefs.saveMode(home.mode)
        prefs.saveJourneys(home.journeys)

        // Ok(empty) is "hub confirms no places"; Err is "could not tell" and
        // must leave the cache untouched — see fetchPlaces' doc comment. Using
        // the cached list as the "no change" default on failure means a
        // network blip this cycle neither wipes places nor spuriously re-arms
        // the geofence.
        val cachedPlaces = prefs.loadPlaces()
        val places = when (val pr = HubClient.fetchPlaces(prefs)) {
            is HubClient.Result.Ok -> pr.value
            is HubClient.Result.Err -> {
                Log.i(TAG, "place refresh skipped: ${pr.message}")
                cachedPlaces
            }
        }
        val placesChanged = places.map { it.id to it.radiusM } !=
            cachedPlaces.map { it.id to it.radiusM }
        if (placesChanged) prefs.savePlaces(places)

        if (modeChanged) {
            Log.i(TAG, "mode changed on hub -> ${home.mode.name} " +
                "(${home.mode.heartbeatS}s); rescheduling")
            // Safe to call from inside the worker it reschedules: UPDATE policy
            // replaces the periodic request going forward without touching the
            // run currently in progress.
            schedule(applicationContext, home.mode.heartbeatS)
        }

        if (geofenceChanged || placesChanged) {
            Log.i(TAG, "geofence config changed on hub; re-arming")
            Geofencing.arm(applicationContext, home.lat, home.lon, home.radiusM, places) { err ->
                if (err != null) Log.w(TAG, "re-arm after config refresh failed: $err")
            }
        }
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

            // Flex window: without one, PeriodicWorkRequestBuilder's two-arg
            // form sets flex = the WHOLE interval, meaning the job is
            // "eligible to run at any point in this 15/30/60 minute window" —
            // not "runs every 15/30/60 minutes". The OS is then free to push
            // execution toward the far end of that window to batch it with
            // other work, and under Doze/App Standby that slack compounds
            // across periods. Bounding flex to the tail of each period makes
            // the actual firing time land close to the interval boundary
            // instead of drifting through it.
            //
            // MIN_PERIODIC_FLEX_S is WorkManager's own floor (5 min) — a
            // shorter flex is silently clamped up to it, same as interval.
            val flex = (interval / 3).coerceIn(MIN_PERIODIC_FLEX_S, interval)

            val req = PeriodicWorkRequestBuilder<HeartbeatWorker>(
                interval, TimeUnit.SECONDS,
                flex, TimeUnit.SECONDS,
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

        /** WorkManager's floor for the flex window; shorter values are rounded up. */
        const val MIN_PERIODIC_FLEX_S = 300L
    }
}
