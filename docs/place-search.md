# Place search

Postal-code and town lookup behind the map location picker, implemented in
`modules/geocode.py`. It rides along with the map tile proxy
(`routes/map_routes.py`) and is authenticated for the same reason: an open
geocoding proxy is the same liability as an open tile proxy.

Its own DuckDB file and worker thread, for the same single-writer reason as
[journeys](journeys.md).

## Sources

Sources are **additive rather than exclusive**. A UK household wants Open
Postcode Geo for exact postcodes *and* GeoNames for town names, because neither
carries what the other does.

`precision` is what a matched code resolves to, and it is the honest reason to
install more than one:

- **GeoNames** publishes district centroids for several countries — the UK
  included, where `SL1 4XY` is simply not in the data. On its own, a full
  postcode lands on a district.
- **Open Postcode Geo** carries every live UK unit postcode, but no place names
  at all.

`attribution` is a licence condition for both, not a courtesy.

## Search ranking

Tiers are ordered so a full code never has to compete with a town sharing its
prefix:

| Tier | Match |
| --- | --- |
| 0 | the code, exactly |
| 1 | the query's first word, exactly — its outward/district part |
| 2 | a code the query starts with — the district containing what was typed |
| 3 | codes starting with the query — what was typed is a partial code |
| 4 | the town, exactly |
| 5 | towns starting with the query |

Tiers 1 and 2 are what make full postcodes work at all. Several countries, the
UK among them, publish only district-level codes (`SL1`, never `SL1 1AA`), so
matching solely on equality or query-prefix returns nothing for the string a
user is most likely to type. Falling back to the district lands them in the
right place — which is all this has to do, since the click is what sets the
point.

Tier 1 exists because normalising away the space loses information that
disambiguates: `EH1 1AA` becomes `EH11AA`, which begins with both `EH1` and
`EH11`, and only the space says which was meant. Where the user typed one, the
first word is the answer and outranks any prefix reasoning.

Codes and towns are separate arms because they need different shapes of answer.
Both are collapsed to one row per key with an averaged centroid: a town has
hundreds of codes and a district hundreds of units, so listing rows
individually would bury every other match under one place's postcodes.

## Open Postcode Geo import

Headerless CSV, one row per postcode ever issued. Column order is fixed by the
publisher: postcode, status, usertype, easting, northing, quality, country,
latitude, longitude, then a series of derived spellings and area/district/sector
splits.

Loaded through `read_csv` rather than row by row. This is 2.6M rows, and an
`executemany` of that is minutes where the engine's own reader is seconds. It is
also why the CSV beats the `.sql` dump published alongside it.

## Why it exists

Apiary locations are picked by clicking a map. That is right for confirming a
point but slow for reaching one: somewhere two counties away is a lot of
dragging from wherever the map happens to open. Typing a postcode gets the map
there in one step, and the click still does the confirming — so this only has to
be accurate enough to centre a view, not to place a pin.

## Local first

Lookups run against a postal-code dataset held on the hub, downloaded once per
country the household cares about. That makes search instant, keeps working with
no internet, and means a typed search string never leaves the house — which
matters more here than for tiles, because "42 Acacia Avenue" is a sharper fact
than a tile coordinate.

The GeoNames dataset is CC BY 4.0: attribution is required and is rendered in
the UI. One file per country carries both postal codes and the town each sits
in, so a single download answers "SW1A 1AA" and "Slough" alike. Precision varies
by country — several publish only district-level centroids — which is sufficient
here and would not be if this placed the pin itself.

## Online fallback

Street addresses and named businesses are not in a postal dataset, so an
optional Nominatim fallback covers them. It is off unless enabled, and skipped
entirely whenever the local store answers.

> Nominatim's usage policy caps absolutely at one request per second and
> requires an identifying User-Agent. Both are enforced in `geocode.py` rather
> than trusted to callers: every outbound call is serialised through one lock,
> so concurrent users queue instead of bursting. **Do not remove that to make a
> type-ahead feel snappier** — a type-ahead is the pattern the policy forbids,
> which is why the UI searches on submit rather than on keystroke.

Coordinates typed directly are parsed in the browser and reach neither store.
