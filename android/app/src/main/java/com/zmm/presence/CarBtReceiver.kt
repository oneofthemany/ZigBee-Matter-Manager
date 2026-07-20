package com.zmm.presence

import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

/**
 * Starts/stops drive mode as the car's Bluetooth connects and disconnects.
 *
 * Manifest-registered on ACL_CONNECTED/ACL_DISCONNECTED, which are on the
 * implicit-broadcast exemption list — so this fires with the app dead, which
 * is the whole point: getting in the car must switch reporting up a gear
 * without anyone opening the app.
 *
 * Every broadcast for every Bluetooth device on the phone lands here
 * (earbuds, watches, speakers), so the first thing done is match against the
 * one MAC the user chose as "the car" and drop everything else.
 */
class CarBtReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != BluetoothDevice.ACTION_ACL_CONNECTED &&
            action != BluetoothDevice.ACTION_ACL_DISCONNECTED) return

        val prefs = Prefs(context)
        val wanted = prefs.carBtAddress
        if (wanted.isEmpty()) return

        @Suppress("DEPRECATION")
        val device: BluetoothDevice? =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
            else
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)

        if (device?.address != wanted) return

        when (action) {
            BluetoothDevice.ACTION_ACL_CONNECTED -> {
                if (prefs.isPaired && prefs.armed) {
                    Log.i(TAG, "car connected — starting drive mode")
                    DriveService.start(context)
                } else {
                    Log.i(TAG, "car connected but not paired/armed; ignoring")
                }
            }
            BluetoothDevice.ACTION_ACL_DISCONNECTED -> {
                Log.i(TAG, "car disconnected — stopping drive mode")
                DriveService.stop(context)
            }
        }
    }

    companion object {
        private const val TAG = "ZmmDrive"
    }
}
