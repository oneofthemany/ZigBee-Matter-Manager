package com.zmm.presence.car

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import androidx.car.app.CarContext
import androidx.car.app.Screen
import androidx.car.app.constraints.ConstraintManager
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.CarColor
import androidx.car.app.model.CarLocation
import androidx.car.app.model.Distance
import androidx.car.app.model.DistanceSpan
import androidx.car.app.model.ItemList
import androidx.car.app.model.Metadata
import androidx.car.app.model.Place
import androidx.car.app.model.PlaceListMapTemplate
import androidx.car.app.model.PlaceMarker
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import androidx.lifecycle.lifecycleScope
import com.google.android.gms.location.LocationServices
import com.zmm.presence.HubClient
import com.zmm.presence.Prefs
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Cheapest fuel near the car, as a place list on the head unit map.
 *
 * The screen is deliberately one level deep with no sub-screens. A driver gets
 * a list and a "navigate" tap; anything that needs reading — price history,
 * per-station trends, the other three fuel grades — stays in the hub's web UI,
 * where it can be looked at while stopped.
 */
class FuelScreen(carContext: CarContext) : Screen(carContext) {

    private val prefs = Prefs(carContext)

    private var stations: List<HubClient.Station> = emptyList()
    private var error: String? = null
    private var loading = true

    /** Metres per mile; the hub speaks metric, this tab has always shown miles. */
    private val metresPerMile = 1609.344

    init {
        refresh()
    }

    override fun onGetTemplate(): Template {
        val builder = PlaceListMapTemplate.Builder()
            .setTitle(carContext.getString(
                com.zmm.presence.R.string.car_fuel_title, fuelLabel(prefs.carFuelType)
            ))
            .setHeaderAction(Action.APP_ICON)
            .setActionStrip(
                ActionStrip.Builder()
                    // One tap cycles the grade rather than opening a picker: a
                    // second screen would be another thing to read at a junction,
                    // and the host limits how deep a task may go anyway.
                    .addAction(
                        Action.Builder()
                            .setTitle(prefs.carFuelType)
                            .setOnClickListener { cycleFuel() }
                            .build()
                    )
                    .addAction(
                        Action.Builder()
                            .setTitle(carContext.getString(com.zmm.presence.R.string.car_refresh))
                            .setOnClickListener { refresh() }
                            .build()
                    )
                    .build()
            )

        when {
            loading -> builder.setLoading(true)
            error != null -> builder.setItemList(messageList(error!!))
            stations.isEmpty() -> builder.setItemList(
                messageList(carContext.getString(com.zmm.presence.R.string.car_no_stations))
            )
            else -> builder.setItemList(stationList())
        }
        return builder.build()
    }

    // ----------------------------------------------------------------------
    // List construction
    // ----------------------------------------------------------------------

    private fun stationList(): ItemList {
        // The host decides how many rows a driver may be shown, and it varies by
        // car. Building more than this throws rather than truncating, so ask.
        val limit = carContext
            .getCarService(ConstraintManager::class.java)
            .getContentLimit(ConstraintManager.CONTENT_LIMIT_TYPE_PLACE_LIST)

        val list = ItemList.Builder()
        stations.take(limit).forEach { s ->
            list.addItem(stationRow(s))
        }
        return list.build()
    }

    private fun stationRow(s: HubClient.Station): Row {
        val price = "£%.2f".format(s.price)

        // DistanceSpan rather than a formatted string: the host renders it in
        // the unit the car is set to and keeps it aligned with its own map.
        val distance = SpannableDistance(
            Distance.create(s.distanceKm * 1000 / metresPerMile, Distance.UNIT_MILES)
        )

        return Row.Builder()
            .setTitle("$price  ${s.brand}")
            .addText(distance.spanned(s.postcode.ifEmpty { s.address }))
            .setMetadata(
                Metadata.Builder()
                    .setPlace(
                        Place.Builder(CarLocation.create(s.lat, s.lon))
                            .setMarker(
                                PlaceMarker.Builder()
                                    // Marker labels are capped at three glyphs, so
                                    // the pence figure is all that fits — "134",
                                    // not "£1.34".
                                    .setLabel("%.0f".format(s.price * 100))
                                    .setColor(CarColor.GREEN)
                                    .build()
                            )
                            .build()
                    )
                    .build()
            )
            .setOnClickListener { navigateTo(s) }
            .build()
    }

    private fun messageList(text: String): ItemList =
        ItemList.Builder()
            .setNoItemsMessage(text)
            .build()

    // ----------------------------------------------------------------------
    // Actions
    // ----------------------------------------------------------------------

    /**
     * Hand the station to whatever navigation app the head unit uses.
     *
     * A geo: intent through the host, not an attempt to draw a route here: this
     * is a POI app, and routing is the navigation app's job. The label rides in
     * the URI so the destination reads as "Shell" rather than a coordinate pair.
     */
    private fun navigateTo(s: HubClient.Station) {
        val label = Uri.encode("${s.brand} ${s.postcode}".trim())
        val uri = Uri.parse("geo:${s.lat},${s.lon}?q=${s.lat},${s.lon}($label)")
        carContext.startCarApp(Intent(CarContext.ACTION_NAVIGATE, uri))
    }

    private fun cycleFuel() {
        val order = listOf("E10", "E5", "B7", "SDV")
        val next = order[(order.indexOf(prefs.carFuelType).coerceAtLeast(0) + 1) % order.size]
        prefs.carFuelType = next
        refresh()
    }

    private fun refresh() {
        loading = true
        error = null
        invalidate()

        lifecycleScope.launch {
            if (!prefs.isPaired) {
                loading = false
                error = carContext.getString(com.zmm.presence.R.string.car_not_paired)
                invalidate()
                return@launch
            }

            val centre = currentCentre()
            if (centre == null) {
                loading = false
                error = carContext.getString(com.zmm.presence.R.string.car_no_location)
                invalidate()
                return@launch
            }

            when (val r = HubClient.fetchFuelNearby(
                prefs, centre.first, centre.second, prefs.carFuelType
            )) {
                is HubClient.Result.Ok -> { stations = r.value; error = null }
                is HubClient.Result.Err -> { stations = emptyList(); error = r.message }
            }
            loading = false
            invalidate()
        }
    }

    /**
     * Where to search from: the last known fix, falling back to the cached home.
     *
     * `lastLocation` rather than an active request — a fresh GPS fix can take
     * tens of seconds, and fuel within a few miles does not change with the
     * hundred metres of staleness. Home is the fallback because a phone that
     * has just booted in a garage may hold no fix at all, and stations near
     * home are a better answer than an error.
     */
    @SuppressLint("MissingPermission") // Granted in PairActivity; drive mode needs it too.
    private suspend fun currentCentre(): Pair<Double, Double>? {
        val fix = runCatching {
            suspendCancellableCoroutine { cont ->
                LocationServices.getFusedLocationProviderClient(carContext)
                    .lastLocation
                    .addOnSuccessListener { cont.resume(it) }
                    .addOnFailureListener { cont.resume(null) }
            }
        }.getOrNull()

        if (fix != null) return fix.latitude to fix.longitude
        if (prefs.hasHome) return prefs.homeLat to prefs.homeLon
        return null
    }

    private fun fuelLabel(code: String): String = when (code) {
        "E5" -> "premium petrol"
        "B7" -> "diesel"
        "SDV" -> "super diesel"
        else -> "petrol"
    }
}

/** Wraps a [Distance] into the span the templates expect. */
private class SpannableDistance(private val distance: Distance) {
    fun spanned(suffix: String): CharSequence {
        val sb = android.text.SpannableStringBuilder("  ")
        sb.setSpan(DistanceSpan.create(distance), 0, 1, android.text.Spanned.SPAN_INCLUSIVE_EXCLUSIVE)
        if (suffix.isNotEmpty()) sb.append(" · ").append(suffix)
        return sb
    }
}
