package com.zmm.presence

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import com.google.android.gms.location.ActivityRecognition
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionRequest
import com.google.android.gms.location.DetectedActivity

/**
 * What the phone is doing — riding in a vehicle, walking, cycling, still.
 *
 * WHY THIS EXISTS. Drive mode is triggered by the car's Bluetooth, which
 * answers "is the phone near the car" and not "is the car moving". Sitting in
 * a parked car with the radio on connects the same way a drive does, so the
 * hub records a journey of a few hundred stationary metres of GPS drift. And
 * once parked, the walk from the space to the door keeps posting fixes under
 * the trip id until the closer's idle timeout catches up — that walk is inside
 * the recorded distance.
 *
 * Play Services already fuses the sensors for this and hands out transitions;
 * doing it here from raw accelerometer data would be a worse version of a
 * thing the platform gives away.
 *
 * WHAT IT DELIBERATELY DOES NOT DO. It cannot tell a driver from a passenger —
 * both are IN_VEHICLE — so it is not a defence against someone else driving
 * your car. It separates vehicle travel from everything that is not vehicle
 * travel, which is the part that is actually corrupting the numbers.
 */
object ActivityMonitor {

    /**
     * Transitions worth waking for.
     *
     * IN_VEHICLE bounds the drive. The on-foot pair is what identifies fixes
     * that must not count toward it. STILL is left out on purpose: a car at
     * a red light reports STILL, and treating that as "not driving" would
     * delete exactly the idling the hub is trying to measure.
     */
    private val WATCHED = listOf(
        DetectedActivity.IN_VEHICLE,
        DetectedActivity.ON_BICYCLE,
        DetectedActivity.WALKING,
        DetectedActivity.RUNNING,
    )

    /** The runtime permission, which only exists from Android 10. */
    fun hasPermission(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
            ContextCompat.checkSelfPermission(
                ctx, Manifest.permission.ACTIVITY_RECOGNITION
            ) == PackageManager.PERMISSION_GRANTED

    /**
     * Subscribe to transitions. Safe to call repeatedly — the same PendingIntent
     * replaces its own registration rather than stacking a second one.
     */
    @android.annotation.SuppressLint("MissingPermission")   // checked below
    fun start(ctx: Context) {
        if (!hasPermission(ctx)) {
            Log.i(TAG, "no activity-recognition permission; not subscribing")
            return
        }
        val transitions = WATCHED.flatMap { act ->
            listOf(
                ActivityTransition.Builder()
                    .setActivityType(act)
                    .setActivityTransition(ActivityTransition.ACTIVITY_TRANSITION_ENTER)
                    .build(),
                ActivityTransition.Builder()
                    .setActivityType(act)
                    .setActivityTransition(ActivityTransition.ACTIVITY_TRANSITION_EXIT)
                    .build(),
            )
        }
        ActivityRecognition.getClient(ctx)
            .requestActivityTransitionUpdates(
                ActivityTransitionRequest(transitions), pendingIntent(ctx)
            )
            .addOnSuccessListener { Log.i(TAG, "activity transitions subscribed") }
            .addOnFailureListener { Log.w(TAG, "activity subscribe failed", it) }
    }

    @android.annotation.SuppressLint("MissingPermission")
    fun stop(ctx: Context) {
        if (!hasPermission(ctx)) return
        ActivityRecognition.getClient(ctx)
            .removeActivityTransitionUpdates(pendingIntent(ctx))
            .addOnFailureListener { Log.w(TAG, "activity unsubscribe failed", it) }
    }

    /**
     * FLAG_MUTABLE is required, not a slip: Play Services fills the transition
     * result into this intent, and an immutable one arrives empty. Explicit
     * component, so only our receiver can be reached through it.
     */
    private fun pendingIntent(ctx: Context): PendingIntent {
        val intent = Intent(ctx, ActivityReceiver::class.java)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                PendingIntent.FLAG_MUTABLE else 0
        return PendingIntent.getBroadcast(ctx, REQUEST_CODE, intent, flags)
    }

    /** Wire name for a detected activity; matches the hub's accepted set. */
    fun nameOf(type: Int): String = when (type) {
        DetectedActivity.IN_VEHICLE -> "in_vehicle"
        DetectedActivity.ON_BICYCLE -> "on_bicycle"
        DetectedActivity.WALKING -> "walking"
        DetectedActivity.RUNNING -> "running"
        DetectedActivity.ON_FOOT -> "on_foot"
        DetectedActivity.STILL -> "still"
        DetectedActivity.TILTING -> "tilting"
        else -> "unknown"
    }

    private const val TAG = "ZmmActivity"
    private const val REQUEST_CODE = 71

    /**
     * How long a transition is taken as still describing the present (ms).
     *
     * Transitions are edges, not a poll: with no movement change there is no
     * new one, so the last edge stands. Past this the phone has been somewhere
     * it never reported leaving — the subscription was dropped, or Play
     * Services was restarted — and the honest answer is that we do not know.
     */
    const val STALE_MS = 30 * 60 * 1000L
}
