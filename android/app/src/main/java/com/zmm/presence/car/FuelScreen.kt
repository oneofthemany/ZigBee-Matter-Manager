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
 * per-station trends, the other grades — stays in the hub's web UI, where it
 * can be looked at while stopped.
 *
 * Nothing here knows which country it is in. The hub says what a price is
 * quoted in, which grades exist and whether they belong to forecourts at all;
 * this screen renders whatever it is told. That matters in a car more than in
 * a browser: a driver glancing at "159.9p" when the pump says "1,599 €" has
 * been told something false at exactly the wrong moment.
 */
class FuelScreen(carContext: CarContext) : Screen(carContext) {

    private val prefs = Prefs(carContext)

    private var stations: List<HubClient.Station> = emptyList()
    private var units: HubClient.FuelUnits = HubClient.FuelUnits.UK
    private var stationLevel = true
    private var areaName = ""
    private var asOf = ""
    private var error: String? = null
    private var loading = true

    /** Metres per mile, for regions whose drivers think in them. */
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
            // An area average has no forecourt and nowhere to navigate to, so
            // it is stated rather than drawn on the map. Putting one pin at the
            // centre of a state would invite a driver to steer at it.
            !stationLevel -> builder.setItemList(messageList(averageMessage()))
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
        // Formatted by the region's own rules — "159.9p" in the UK, "€1,719"
        // in Germany, "220.9c" in Australia. See HubClient.FuelUnits.
        val price = units.format(s.price)

        // DistanceSpan rather than a formatted string: the host renders it in
        // the unit the car is set to and keeps it aligned with its own map.
        // Kilometres are what the hub sends; the region says which unit its
        // drivers read.
        val distance = SpannableDistance(
            if (units.distance == "km")
                Distance.create(s.distanceKm, Distance.UNIT_KILOMETERS)
            else
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
                                    // Marker labels are capped at three glyphs.
                                    // See FuelUnits.markerLabel for what fits
                                    // and why it rounds; the row title carries
                                    // the exact figure either way.
                                    .setLabel(units.markerLabel(s.price))
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

    /**
     * Step to the next grade the active region sells.
     *
     * The order is the region's own, cached from the last lookup, because a
     * driver in Germany tapping through E10, E5, B7 and SDV would be offered
     * three grades that do not exist there and get an error for each. A grade
     * the region does not know — one left over from a region change — is
     * treated as "before the first", so one tap lands on a valid grade.
     */
    private fun cycleFuel() {
        val order = prefs.carFuelGradeCodes
        if (order.isEmpty()) return
        val at = order.indexOf(prefs.carFuelType)
        prefs.carFuelType = order[(at + 1) % order.size]
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

            // The head unit is only ever used away from home, so a LAN-only hub
            // can never answer here. Saying so beats a generic network error
            // that a driver would read as "no signal".
            if (!Prefs.isPublicUrl(prefs.hubUrl)) {
                loading = false
                error = carContext.getString(com.zmm.presence.R.string.car_needs_public_short)
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
                is HubClient.Result.Ok -> {
                    val data = r.value
                    stations = data.stations
                    units = data.units
                    stationLevel = data.stationLevel
                    areaName = data.areaName
                    asOf = data.asOf
                    error = null
                    // Cached so the next tap on the grade action has the
                    // region's list in hand before any request is made.
                    if (data.grades.isNotEmpty()) {
                        prefs.setCarFuelGrades(data.grades)
                        // A grade left over from another region would fail on
                        // every lookup until the driver happened to tap past
                        // it. Move to something this region sells instead.
                        if (!data.grades.containsKey(prefs.carFuelType)) {
                            prefs.carFuelType = data.defaultGrade
                                .ifEmpty { data.grades.keys.first() }
                        }
                    }
                }
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

    /**
     * What to call a grade in the screen title.
     *
     * The region's own label, lower-cased so it reads inside "Cheapest %s
     * nearby" — the hub sends "Premium petrol (E5)" and "Gasóleo A", which are
     * headings, not sentence fragments. Falls back to the bare code, which is
     * what is printed on the pump anyway.
     */
    private fun fuelLabel(code: String): String {
        val label = prefs.carFuelGradeLabel(code)
        return if (label == code) code else label.lowercase()
    }

    /**
     * The one line a region without station prices can honestly show.
     *
     * The US publishes a weekly average by state, not forecourt prices. Saying
     * so in place of a list is the point: a driver who is shown a figure with
     * no station attached needs to know there is nothing to drive to, and that
     * the number may be days old.
     */
    private fun averageMessage(): String {
        val price = stations.firstOrNull()?.price
            ?: return carContext.getString(com.zmm.presence.R.string.car_no_stations)
        val where = areaName.ifEmpty { "this area" }
        val dated = if (asOf.isEmpty()) "" else " (week ending $asOf)"
        return carContext.getString(
            com.zmm.presence.R.string.car_fuel_average,
            units.format(price), units.volumeLabel, where, dated,
        )
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
