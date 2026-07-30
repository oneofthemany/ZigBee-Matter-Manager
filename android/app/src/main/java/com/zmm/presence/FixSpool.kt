package com.zmm.presence

import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.io.File

/**
 * Fixes the hub did not accept, kept until it does.
 *
 * WHY THIS EXISTS. A drive is exactly the situation where the network is
 * least reliable and the data is least replaceable. Position at rest can wait
 * for the next heartbeat, because the phone will still be in the same place;
 * position while moving cannot, because in fifteen minutes the car is
 * somewhere else and that stretch of road is gone for good. Without a spool a
 * tunnel, a valley, or a minute of congested cell in a car park does not
 * degrade a recorded journey — it truncates it, and everything after the gap
 * is attributed to a trip the hub already gave up on and closed.
 *
 * Storage is one JSON object per line in the app's private files directory.
 * A file rather than SharedPreferences because this is an append-mostly log
 * measured in hundreds of entries, and prefs would rewrite the whole blob on
 * every fix.
 *
 * ORDER MATTERS, and not only for tidiness. The hub's presence state is
 * last-write-wins, so replaying an old fix after the current one would park
 * the user where they were half an hour ago. Every caller therefore drains
 * oldest-first BEFORE posting its own fresh fix, which leaves the newest
 * position written last and presence correct at the end of every cycle.
 */
class FixSpool(context: Context) {

    private val file = File(context.filesDir, FILE_NAME)

    /** Keep a failed fix for later. Never throws — spooling is best-effort. */
    @Synchronized
    fun offer(payload: String) {
        try {
            // The format is one object per line, so an embedded newline would
            // split one fix into two unparseable halves. JSONObject.toString
            // does not emit any, but the payload arrives as a plain string and
            // this costs nothing.
            file.appendText(payload.replace("\n", " ") + "\n")
            trimLocked()
        } catch (e: Exception) {
            Log.w(TAG, "spool write failed", e)
        }
    }

    @Synchronized
    fun readAll(): List<String> = try {
        if (file.exists()) file.readLines().filter { it.isNotBlank() } else emptyList()
    } catch (e: Exception) {
        Log.w(TAG, "spool read failed", e)
        emptyList()
    }

    @Synchronized
    fun replace(lines: List<String>) {
        try {
            if (lines.isEmpty()) file.delete()
            else file.writeText(lines.joinToString("\n") + "\n")
        } catch (e: Exception) {
            Log.w(TAG, "spool rewrite failed", e)
        }
    }

    /**
     * Try to deliver spooled fixes, oldest first.
     *
     * Stops at the first failure rather than working through the rest: one
     * failure means the hub is still unreachable, and the remaining attempts
     * would each cost a fifteen-second timeout while the caller is trying to
     * report a live position. Returns how many were accepted.
     *
     * Not synchronized, unlike the accessors — a monitor cannot be held across
     * a suspension point. The callers (drive mode and the heartbeat) each
     * drain from a single coroutine, and a drain that overlapped another would
     * at worst re-send a fix the hub is content to receive twice.
     */
    suspend fun drain(prefs: Prefs, max: Int = MAX_PER_DRAIN): Int {
        val all = readAll()
        if (all.isEmpty()) return 0

        // Anything this old the hub rejects outright as stale, so carrying it
        // forever would mean a permanently blocked queue head.
        val cutoff = System.currentTimeMillis() / 1000.0 - MAX_AGE_S
        val fresh = all.filter { timestampOf(it)?.let { t -> t >= cutoff } ?: true }
        if (fresh.size != all.size) {
            Log.i(TAG, "dropped ${all.size - fresh.size} spooled fixes past the hub's stale window")
            replace(fresh)
        }
        if (fresh.isEmpty()) return 0

        var sent = 0
        for (payload in fresh.take(max)) {
            when (val r = HubClient.postRaw(prefs, payload)) {
                is HubClient.Result.Ok -> sent++
                is HubClient.Result.Err -> {
                    Log.i(TAG, "spool drain stopped after $sent: ${r.message}")
                    break
                }
            }
        }
        if (sent > 0) {
            replace(fresh.drop(sent))
            Log.i(TAG, "delivered $sent spooled fixes (${fresh.size - sent} left)")
        }
        return sent
    }

    /**
     * Drop the oldest entries once the spool passes its ceiling.
     *
     * Oldest rather than newest: if the queue is overflowing the hub has been
     * unreachable for hours, and the recent stretch of road is the part still
     * worth recovering.
     */
    private fun trimLocked() {
        val lines = try {
            if (file.exists()) file.readLines().filter { it.isNotBlank() } else return
        } catch (e: Exception) {
            return
        }
        if (lines.size <= MAX_ENTRIES) return
        try {
            val kept = lines.takeLast(MAX_ENTRIES)
            file.writeText(kept.joinToString("\n") + "\n")
            Log.i(TAG, "spool trimmed to $MAX_ENTRIES")
        } catch (e: Exception) {
            Log.w(TAG, "spool trim failed", e)
        }
    }

    private fun timestampOf(payload: String): Double? = try {
        JSONObject(payload).optDouble("timestamp").takeIf { !it.isNaN() }
    } catch (e: Exception) {
        // Unparseable: treat as fresh so it reaches the hub, which will
        // reject it properly and let it fall off the queue.
        null
    }

    companion object {
        private const val TAG = "ZmmSpool"
        private const val FILE_NAME = "fix_spool.jsonl"

        /** Two hours of drive-mode fixes at the 10 s cadence. */
        private const val MAX_ENTRIES = 720

        /** Matches the hub's own "fix too old" rejection. */
        private const val MAX_AGE_S = 6 * 3600

        /**
         * Per-cycle catch-up limit. Enough to clear a several-minute gap in
         * one go without the drain crowding out the live fix behind it.
         */
        private const val MAX_PER_DRAIN = 25
    }
}
