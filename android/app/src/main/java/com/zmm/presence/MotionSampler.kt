package com.zmm.presence

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Inertial sampling for drive mode — how the car is being driven, as opposed
 * to where it is.
 *
 * GPS answers position and speed; it cannot see the things that characterise
 * driving. A hard stop, a fast roundabout and a rough road all happen inside
 * one fix interval and leave no trace in a position track. The accelerometer
 * sees all three, at fifty samples a second.
 *
 * WHY THIS AGGREGATES ON THE PHONE. Raw 50 Hz triaxial data is roughly a
 * megabyte an hour per axis set — for a drive that is already streaming a fix
 * every ten seconds over mobile data, and whose value to the hub is entirely
 * in the summary. So the window between two fixes is reduced here to a handful
 * of numbers plus any discrete events, and only that is sent. Nothing is
 * stored on the phone; a window that is never drained is simply overwritten.
 *
 * THE ORIENTATION PROBLEM, and how it is solved. A phone in a car sits at an
 * arbitrary angle — cradled, in a cupholder, face down on the passenger seat —
 * so its axes mean nothing on their own. Two things fix that:
 *
 *   - Down is known: the gravity vector gives the vertical axis directly, and
 *     splitting acceleration into "along gravity" and "the plane perpendicular
 *     to it" needs no calibration at all. That alone yields road roughness and
 *     the magnitude of every horizontal event.
 *   - Forward is learned: telling braking from acceleration needs the vehicle's
 *     forward axis, and that is estimated from the phone's own data rather than
 *     asked of the user. When GPS says the car clearly gained or lost speed over
 *     a window and the gyro says it was not turning, the mean horizontal
 *     acceleration over that window must have pointed along the car's axis; its
 *     sign is known from the GPS speed change. Successive estimates are blended,
 *     so the axis converges over the first minute of a drive and re-converges if
 *     the phone is moved. Until it has, events are still detected and measured —
 *     they are reported as "harsh" rather than split into brake and accelerate.
 *
 * The gyro contributes the other half of the classification: a horizontal event
 * with significant rotation about the vertical axis is cornering, whatever the
 * forward axis says, because a car that is turning is not braking in a straight
 * line. The barometer, where the phone has one, gives relative altitude an order
 * of magnitude better than GPS does, which is what makes climb worth reporting.
 */
class MotionSampler(context: Context) : SensorEventListener {

    /**
     * One fix interval's worth of motion, in SI units.
     *
     * All of it is derived, none of it is a raw sample: the point of this type
     * is that it is small enough to ride along with a position fix.
     */
    data class Window(
        /** Accelerometer samples behind these numbers; 0 means "no data". */
        val samples: Int,
        /**
         * The most extreme longitudinal acceleration, signed: positive is
         * speeding up, negative is braking. NaN until the forward axis is
         * learned — a null, not a zero, because "not yet known" and "no
         * longitudinal acceleration" are very different claims.
         */
        val longPeakMps2: Float,
        /** Largest lateral (cornering) acceleration, unsigned. NaN uncalibrated. */
        val latPeakMps2: Float,
        /** RMS vertical acceleration — road roughness. Always available. */
        val vertRmsMps2: Float,
        /** Peak rate of change of acceleration; high jerk is abrupt driving. */
        val jerkPeakMps3: Float,
        /** Peak yaw rate about the vertical axis, rad/s. NaN with no gyro. */
        val yawPeakRadS: Float,
        /** Mean barometric pressure over the window, hPa. Null with no barometer. */
        val pressureHpa: Float?,
        /** Whether the forward axis was established for this window. */
        val calibrated: Boolean,
    )

    /**
     * A discrete driving event — the part of the window that is worth a row of
     * its own rather than an average.
     *
     * [kind] is one of brake, accel, corner, or harsh (detected but not
     * classifiable because the forward axis was not yet learned).
     */
    data class Event(
        val tSec: Double,
        val kind: String,
        val peakMps2: Float,
        val durationS: Float,
    )

    private val sm = context.getSystemService(SensorManager::class.java)

    // Prefer the fused sensors: the platform separates gravity from motion
    // using the gyro, which a high-pass filter over the raw accelerometer
    // cannot do without also attenuating a sustained brake. The raw sensor is
    // the fallback for devices that publish no fusion, and it is filtered
    // slowly enough (see GRAVITY_TAU_S) that a four-second stop survives it.
    private val linear = sm?.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION)
    private val gravity = sm?.getDefaultSensor(Sensor.TYPE_GRAVITY)
    private val fused = linear != null && gravity != null
    private val rawAccel =
        if (fused) null else sm?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val gyro = sm?.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
    private val baro = sm?.getDefaultSensor(Sensor.TYPE_PRESSURE)

    /** Sensor callbacks land here, never on the main thread. */
    private var thread: HandlerThread? = null

    private val lock = Any()

    // --- orientation state -------------------------------------------------
    private val down = FloatArray(3)          // unit vector, phone frame
    private var haveDown = false
    private val gravityEma = FloatArray(3)    // no-fusion fallback only
    private val forward = FloatArray(3)       // unit vector, phone frame
    private var forwardFixes = 0

    // --- per-window accumulators (guarded by lock) -------------------------
    private var n = 0
    private var sumVertSq = 0.0
    private var sumHoriz = FloatArray(3)
    private var longMin = 0f
    private var longMax = 0f
    private var latMax = 0f
    private var jerkMax = 0f
    private var yawMax = 0f
    private var yawNow = 0f
    private var pressureSum = 0.0
    private var pressureN = 0
    private var events = ArrayList<Event>(8)

    private val smoothed = FloatArray(3)      // low-passed horizontal vector
    private val prevAccel = FloatArray(3)
    private var prevNanos = 0L
    private var havePrev = false

    // --- in-progress event -------------------------------------------------
    private var inEvent = false
    private var evPeak = 0f
    private var evPeakLong = 0f
    private var evYaw = 0f
    private var evStartNanos = 0L

    // --- GPS feedback for the forward-axis estimate ------------------------
    private var lastSpeedMps = Float.NaN
    private var lastSpeedNanos = 0L

    /** Start sampling. Safe to call when the device has no usable sensors. */
    fun start() {
        val mgr = sm ?: return
        if (!fused && rawAccel == null) {
            Log.i(TAG, "no accelerometer — motion sampling unavailable")
            return
        }
        val t = HandlerThread("zmm-motion").also { it.start() }
        thread = t
        val h = Handler(t.looper)

        if (fused) {
            mgr.registerListener(this, linear!!, RATE_US, h)
            mgr.registerListener(this, gravity!!, RATE_US, h)
        } else {
            mgr.registerListener(this, rawAccel!!, RATE_US, h)
        }
        gyro?.let { mgr.registerListener(this, it, RATE_US, h) }
        // Pressure changes slowly and only relative change matters; a sample a
        // second is plenty and costs nothing next to the accelerometer.
        baro?.let { mgr.registerListener(this, it, 1_000_000, h) }

        Log.i(TAG, "motion sampling started (fused=$fused gyro=${gyro != null} " +
            "baro=${baro != null})")
    }

    fun stop() {
        sm?.unregisterListener(this)
        thread?.quitSafely()
        thread = null
        Log.i(TAG, "motion sampling stopped")
    }

    /**
     * Hand the sampler the GPS speed that came with a fix.
     *
     * This is the only outside input, and it exists solely to learn the forward
     * axis: GPS is the one source that knows, unambiguously, whether the car
     * sped up or slowed down. Call it before draining the window it belongs to.
     */
    fun noteGpsSpeed(speedMps: Float, nanos: Long = SystemClock.elapsedRealtimeNanos()) {
        synchronized(lock) {
            val prev = lastSpeedMps
            val prevT = lastSpeedNanos
            lastSpeedMps = speedMps
            lastSpeedNanos = nanos

            if (prev.isNaN() || prevT == 0L) return
            val dt = (nanos - prevT) / 1e9f
            // Older than this and the two speeds are not two ends of one
            // manoeuvre — a signal gap, or drive mode restarting.
            if (dt <= 0f || dt > MAX_SPEED_GAP_S) return
            val gpsAccel = (speedMps - prev) / dt
            if (abs(gpsAccel) < CALIB_MIN_ACCEL) return
            // A window with real turning in it says nothing about where forward
            // is: the acceleration was partly centripetal.
            if (yawMax >= CORNER_YAW_RAD_S) return
            if (n < CALIB_MIN_SAMPLES) return

            val mean = floatArrayOf(sumHoriz[0] / n, sumHoriz[1] / n, sumHoriz[2] / n)
            val mag = norm(mean)
            if (mag < CALIB_MIN_MEAN) return

            val sign = if (gpsAccel > 0) 1f else -1f
            for (i in 0..2) mean[i] = mean[i] / mag * sign

            if (forwardFixes == 0) {
                System.arraycopy(mean, 0, forward, 0, 3)
            } else {
                // Blend rather than replace: any single window is a noisy
                // estimate, and a phone that is picked up and put back must
                // pull the axis across rather than snap it.
                for (i in 0..2) forward[i] += CALIB_BLEND * (mean[i] - forward[i])
                val m = norm(forward)
                if (m > 1e-6f) for (i in 0..2) forward[i] /= m
            }
            forwardFixes++
        }
    }

    /**
     * Take the accumulated window and reset, or null when nothing was sampled.
     *
     * Draining is destructive by design: the hub is given each interval exactly
     * once, and a fix that fails to send must not double-count its motion into
     * the next one.
     */
    fun takeWindow(): Window? = synchronized(lock) {
        if (n == 0) return null
        val calibrated = forwardFixes >= CALIB_MIN_FIXES
        val longPeak = if (!calibrated) Float.NaN
            else if (abs(longMin) > abs(longMax)) longMin else longMax
        val w = Window(
            samples = n,
            longPeakMps2 = longPeak,
            latPeakMps2 = if (calibrated) latMax else Float.NaN,
            vertRmsMps2 = sqrt(sumVertSq / n).toFloat(),
            jerkPeakMps3 = jerkMax,
            yawPeakRadS = if (gyro != null) yawMax else Float.NaN,
            pressureHpa = if (pressureN > 0) (pressureSum / pressureN).toFloat() else null,
            calibrated = calibrated,
        )
        n = 0
        sumVertSq = 0.0
        sumHoriz = FloatArray(3)
        longMin = 0f; longMax = 0f; latMax = 0f; jerkMax = 0f; yawMax = 0f
        pressureSum = 0.0; pressureN = 0
        w
    }

    /** Take the events detected since the last drain. Destructive, as above. */
    fun takeEvents(): List<Event> = synchronized(lock) {
        val out = events
        events = ArrayList(8)
        out
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    override fun onSensorChanged(e: SensorEvent) {
        when (e.sensor.type) {
            Sensor.TYPE_GRAVITY -> synchronized(lock) { setDown(e.values) }

            Sensor.TYPE_LINEAR_ACCELERATION ->
                synchronized(lock) { accumulate(e.values, e.timestamp) }

            Sensor.TYPE_ACCELEROMETER -> synchronized(lock) {
                // Fallback path: the slow average of total acceleration IS
                // gravity, over any window long enough that the car's own
                // manoeuvres average out.
                if (!haveDown) {
                    setDown(e.values)
                    gravityEma[0] = e.values[0]
                    gravityEma[1] = e.values[1]
                    gravityEma[2] = e.values[2]
                    return@synchronized
                }
                val dt = if (prevNanos == 0L) 0.02f else (e.timestamp - prevNanos) / 1e9f
                val a = (dt / (GRAVITY_TAU_S + dt)).coerceIn(0f, 1f)
                for (i in 0..2) gravityEma[i] += a * (e.values[i] - gravityEma[i])
                setDown(gravityEma)
                accumulate(
                    floatArrayOf(
                        e.values[0] - gravityEma[0],
                        e.values[1] - gravityEma[1],
                        e.values[2] - gravityEma[2],
                    ),
                    e.timestamp,
                )
            }

            Sensor.TYPE_GYROSCOPE -> synchronized(lock) {
                if (!haveDown) return@synchronized
                // Rotation about the vertical axis is yaw — the car turning.
                // Roll and pitch are the phone being handled, and are ignored.
                yawNow = e.values[0] * down[0] + e.values[1] * down[1] + e.values[2] * down[2]
                val m = abs(yawNow)
                if (m > yawMax) yawMax = m
                if (inEvent && m > evYaw) evYaw = m
            }

            Sensor.TYPE_PRESSURE -> synchronized(lock) {
                pressureSum += e.values[0]
                pressureN++
            }
        }
    }

    private fun setDown(g: FloatArray) {
        val m = norm(g)
        if (m < 1e-3f) return
        down[0] = g[0] / m; down[1] = g[1] / m; down[2] = g[2] / m
        haveDown = true
    }

    /** One accelerometer sample, gravity already removed. */
    private fun accumulate(a: FloatArray, nanos: Long) {
        if (!haveDown) return

        val dt = if (!havePrev) 0f else (nanos - prevNanos) / 1e9f
        // A gap this large means the sensor stalled (suspend, or the listener
        // being re-registered); a jerk computed across it would be fiction.
        val usableDt = dt > 0f && dt < 0.5f

        if (usableDt) {
            var ds = 0f
            for (i in 0..2) {
                val d = a[i] - prevAccel[i]
                ds += d * d
            }
            val jerk = sqrt(ds) / dt
            if (jerk > jerkMax) jerkMax = jerk
        }
        System.arraycopy(a, 0, prevAccel, 0, 3)
        prevNanos = nanos
        havePrev = true

        // Split into "along gravity" and "the horizontal plane". This is the
        // step that needs no calibration and is why roughness and event
        // magnitude are trustworthy from the first second of a drive.
        val vert = a[0] * down[0] + a[1] * down[1] + a[2] * down[2]
        val h = floatArrayOf(
            a[0] - vert * down[0],
            a[1] - vert * down[1],
            a[2] - vert * down[2],
        )

        n++
        sumVertSq += (vert * vert).toDouble()
        for (i in 0..2) sumHoriz[i] += h[i]

        // Low-pass the horizontal vector before looking for events: driving
        // manoeuvres live below a few Hz, while a phone rattling in a cradle
        // is well above it and would otherwise trip every threshold. The
        // vector, not its magnitude, so direction survives for the brake /
        // accelerate split.
        val alpha = if (usableDt) (dt / (SMOOTH_TAU_S + dt)).coerceIn(0f, 1f) else 1f
        for (i in 0..2) smoothed[i] += alpha * (h[i] - smoothed[i])

        val mag = norm(smoothed)

        if (forwardFixes >= CALIB_MIN_FIXES) {
            val lon = smoothed[0] * forward[0] + smoothed[1] * forward[1] +
                smoothed[2] * forward[2]
            if (lon > longMax) longMax = lon
            if (lon < longMin) longMin = lon
            // Everything perpendicular to forward, in the horizontal plane.
            val lat = sqrt((mag * mag - lon * lon).coerceAtLeast(0f))
            if (lat > latMax) latMax = lat
        }

        detectEvent(mag, nanos)
    }

    /**
     * Threshold crossing with hysteresis.
     *
     * Two thresholds rather than one because a single one turns the wobble
     * around it into a burst of events: entering needs a clear excursion,
     * leaving needs the excursion to be genuinely over.
     */
    private fun detectEvent(mag: Float, nanos: Long) {
        if (!inEvent) {
            if (mag < EVENT_ENTER_MPS2) return
            inEvent = true
            evPeak = mag
            evYaw = abs(yawNow)
            evStartNanos = nanos
            evPeakLong = longitudinal()
            return
        }

        if (mag > evPeak) {
            evPeak = mag
            evPeakLong = longitudinal()
        }
        if (mag >= EVENT_EXIT_MPS2) return

        inEvent = false
        val dur = (nanos - evStartNanos) / 1e9f
        // Sub-threshold in duration is a pothole shock or a door slam reaching
        // the horizontal plane, not a manoeuvre a driver made.
        if (dur < EVENT_MIN_S) return
        if (events.size >= MAX_EVENTS_PER_WINDOW) return

        val kind = when {
            evYaw >= CORNER_YAW_RAD_S -> "corner"
            forwardFixes < CALIB_MIN_FIXES -> "harsh"
            evPeakLong < 0f -> "brake"
            else -> "accel"
        }
        events.add(Event(
            tSec = wallClock(nanos),
            kind = kind,
            peakMps2 = evPeak,
            durationS = dur,
        ))
    }

    private fun longitudinal(): Float =
        if (forwardFixes < CALIB_MIN_FIXES) 0f
        else smoothed[0] * forward[0] + smoothed[1] * forward[1] + smoothed[2] * forward[2]

    /**
     * Sensor timestamps are nanoseconds since boot; the hub stores wall clock.
     * Converting through the current offset rather than reading the clock at
     * detection keeps the event on the same timeline as the fix it rides with.
     */
    private fun wallClock(nanos: Long): Double =
        System.currentTimeMillis() / 1000.0 -
            (SystemClock.elapsedRealtimeNanos() - nanos) / 1e9

    private fun norm(v: FloatArray): Float =
        sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])

    companion object {
        private const val TAG = "ZmmMotion"

        /**
         * 20 ms — 50 Hz. Comfortably above the few Hz that driving manoeuvres
         * occupy, and comfortably below the 200 Hz ceiling that would need
         * HIGH_SAMPLING_RATE_SENSORS. This is a hint; the platform delivers at
         * whatever rate it can, which is why every calculation here derives dt
         * from the timestamps rather than assuming the period.
         */
        private const val RATE_US = 20_000

        /** Event detection filter constant; ~0.7 Hz corner frequency. */
        private const val SMOOTH_TAU_S = 0.22f

        /** Gravity separation for the no-fusion fallback. Deliberately slow. */
        private const val GRAVITY_TAU_S = 5.0f

        /**
         * 3.5 m/s² is about 0.36 g — firm braking, a quick lane change, a
         * roundabout taken briskly. Below this is ordinary driving and logging
         * it would drown the events worth seeing.
         */
        private const val EVENT_ENTER_MPS2 = 3.5f
        private const val EVENT_EXIT_MPS2 = 2.2f

        /** Shorter than this is a shock, not a manoeuvre. */
        private const val EVENT_MIN_S = 0.4f

        /**
         * ~11°/s. A motorway curve is well under it, a roundabout or junction
         * turn well over, so this cleanly separates "turning" from "braking in
         * a straight line" without needing to know which way forward is.
         */
        private const val CORNER_YAW_RAD_S = 0.20f

        /** Bounds one fix's payload; a window this eventful is already damning. */
        private const val MAX_EVENTS_PER_WINDOW = 12

        // Forward-axis estimation.
        private const val CALIB_MIN_ACCEL = 0.8f     // m/s², GPS-observed
        private const val CALIB_MIN_MEAN = 0.25f     // m/s², mean horizontal
        private const val CALIB_MIN_SAMPLES = 50
        private const val CALIB_MIN_FIXES = 3
        private const val CALIB_BLEND = 0.3f
        private const val MAX_SPEED_GAP_S = 30f
    }
}
