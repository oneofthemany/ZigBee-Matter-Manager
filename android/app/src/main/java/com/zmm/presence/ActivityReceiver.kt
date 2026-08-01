package com.zmm.presence

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.google.android.gms.location.ActivityTransition
import com.google.android.gms.location.ActivityTransitionResult
import com.google.android.gms.location.DetectedActivity

/**
 * Records the phone's current activity, and ends a drive when the car does.
 *
 * One broadcast can carry several transitions; they arrive oldest-first, so
 * the last ENTER in the batch is the current state.
 */
class ActivityReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (!ActivityTransitionResult.hasResult(intent)) return
        val result = ActivityTransitionResult.extractResult(intent) ?: return

        val prefs = Prefs(context)
        var leftVehicle = false

        for (e in result.transitionEvents) {
            val name = ActivityMonitor.nameOf(e.activityType)
            val entering =
                e.transitionType == ActivityTransition.ACTIVITY_TRANSITION_ENTER
            if (entering) {
                prefs.lastActivity = name
                prefs.lastActivityMs = System.currentTimeMillis()
                leftVehicle = false
            } else if (e.activityType == DetectedActivity.IN_VEHICLE) {
                leftVehicle = true
            }
            Log.i(TAG, "activity ${if (entering) "enter" else "exit"} $name")
        }

        // Ending the drive on the vehicle exit rather than waiting for the
        // Bluetooth to drop: head units hold the link while the car sits
        // parked, and every fix until then is the walk away from it.
        if (leftVehicle && DriveService.running) {
            Log.i(TAG, "left the vehicle — stopping drive mode")
            DriveService.stop(context)
        }
    }

    companion object {
        private const val TAG = "ZmmActivity"
    }
}
