"""
Place search — postcode, ZIP or town name to coordinates.

Why this exists
---------------
Apiary locations are picked by clicking a map. That is right for confirming a
point but slow for reaching one: somewhere two counties away is a lot of
dragging from wherever the map happens to open. Typing a postcode gets the map
there in one step, and the click still does the confirming — so this only has to
be accurate enough to centre a view, not to place a pin.

Local first
-----------
Lookups run against a postal-code dataset held on the hub, downloaded once per
country the household cares about. That makes search instant, keeps working with
no internet, and means a typed search string never leaves the house — which
matters more here than for tiles, because "42 Acacia Avenue" is a sharper fact
than a tile coordinate.

The dataset comes from GeoNames (CC BY 4.0, attribution required and rendered in
the UI). One file per country carries both postal codes and the town each sits
in, so a single download answers "SW1A 1AA" and "Slough" alike. Precision varies
by country — several countries publish only district-level centroids — which is
sufficient here and would not be if this placed the pin itself.

Online fallback
---------------
Street addresses and named businesses are not in a postal dataset, so an
optional Nominatim fallback covers them. It is off unless enabled, and skipped
entirely whenever the local store answers.

    Nominatim's usage policy caps absolutely at one request per second and
    requires an identifying User-Agent. Both are enforced here rather than
    trusted to callers: every outbound call is serialised through one lock, so
    concurrent users queue instead of bursting. Do not remove that to make a
    type-ahead feel snappier — a type-ahead is the pattern the policy forbids,
    which is why the UI searches on submit rather than on keystroke.

Storage
-------
data/geocode.duckdb, reached through one dedicated worker thread that owns the
connection (one DB, one thread — the project convention). It is reference data:
written only when a country is installed or removed, read on every search.

Coordinates typed directly are parsed in the browser and reach neither store.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import shutil
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from modules.config_yaml import update_block

logger = logging.getLogger("modules.geocode")

DB_PATH = Path("./data/geocode.duckdb")

NOMINATIM = "https://nominatim.openstreetmap.org/search"

#: Where postal data comes from. Sources are additive rather than exclusive: a
#: UK household wants Open Postcode Geo for exact postcodes AND GeoNames for
#: town names, because neither carries what the other does.
#:
#: `precision` is what a matched code resolves to, and it is the honest reason
#: to install more than one. GeoNames publishes district centroids for several
#: countries — the UK included, where "SL1 4XY" is simply not in the data — so
#: on its own a full postcode lands on a district. Open Postcode Geo carries
#: every live UK unit postcode but no place names at all.
#:
#: `attribution` is a licence condition for both, not a courtesy.
SOURCES: Dict[str, Dict[str, Any]] = {
    "geonames": {
        "label": "GeoNames",
        "url": "https://download.geonames.org/export/zip/{cc}.zip",
        "countries": None,                      # any published country
        "precision": "district",
        "has_places": True,
        "attribution": "Postal data © GeoNames, CC BY 4.0",
        "note": "Postcodes and town names. Small download, ~90 countries.",
    },
    "open_postcode_geo": {
        "label": "Open Postcode Geo",
        # tar.gz of CSV. The .sql.tar.gz alongside it is a MySQL dump, which
        # DuckDB cannot execute without dialect translation — read_csv ingests
        # this one natively and far faster than any INSERT replay would.
        "url": "https://download.getthedata.com/downloads/open_postcode_geo.csv.tar.gz",
        "countries": ("GB",),
        "precision": "unit",
        "has_places": False,
        "attribution": ("Contains OS data © Crown copyright and database right; "
                        "ONS data © Crown copyright and database right. "
                        "Open Government Licence v3.0"),
        "note": ("Every live UK postcode, to the unit. Large download "
                 "(~100 MB). Carries no town names — install GeoNames too."),
    },
}


def default_source(cc: str) -> str:
    """The most precise source covering a country."""
    for name, s in SOURCES.items():
        if s["countries"] and cc in s["countries"] and s["precision"] == "unit":
            return name
    return "geonames"

#: Identifies this software upstream, as both services' policies require.
USER_AGENT = ("ZigBee-Matter-Manager/1.0 "
              "(self-hosted home automation; apiary location search)")

#: Generous: Open Postcode Geo is ~100 MB, and a hub on domestic broadband
#: doing anything else at the time should still finish rather than fail late.
DOWNLOAD_TIMEOUT_S = 900
FETCH_TIMEOUT_S = 10

#: Nominatim's hard limit is one request per second. Kept slightly above it so
#: clock jitter cannot turn a compliant pace into a violation.
_NOMINATIM_MIN_INTERVAL_S = 1.1

MAX_RESULTS = 8

#: Countries GeoNames publishes postal data for, as a starting list for the UI.
#: Not exhaustive and not authoritative — the API accepts any ISO-3166 alpha-2
#: code, so a country missing from here still installs; it just has to be typed.
COMMON_COUNTRIES = {
    "GB": "United Kingdom", "US": "United States", "IE": "Ireland",
    "CA": "Canada", "AU": "Australia", "NZ": "New Zealand",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
    "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "CH": "Switzerland",
    "PT": "Portugal", "DK": "Denmark", "SE": "Sweden", "NO": "Norway",
    "FI": "Finland", "PL": "Poland", "CZ": "Czechia", "IN": "India",
    "JP": "Japan", "BR": "Brazil", "MX": "Mexico", "ZA": "South Africa",
}

_CC_RE = re.compile(r"^[A-Za-z]{2}$")

#: Anything postcode-shaped: letters, digits, spaces and hyphens only. Used to
#: decide whether to try an exact code match before a town-name match, not to
#: validate — postal formats vary far too much to police centrally.
_CODEISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,11}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postal_codes (
    country     TEXT NOT NULL,
    source      TEXT NOT NULL,
    code        TEXT NOT NULL,
    code_norm   TEXT NOT NULL,
    place       TEXT,
    place_norm  TEXT,
    admin1      TEXT,
    admin2      TEXT,
    lat         DOUBLE NOT NULL,
    lon         DOUBLE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postal_code ON postal_codes (code_norm);
CREATE INDEX IF NOT EXISTS idx_postal_place ON postal_codes (place_norm);
CREATE TABLE IF NOT EXISTS datasets (
    country      TEXT   NOT NULL,
    source       TEXT   NOT NULL,
    row_count    BIGINT NOT NULL,
    installed_at DOUBLE NOT NULL,
    PRIMARY KEY (country, source)
);
"""

# Ordered so a full code never has to compete with a town sharing its prefix:
#   0  the code, exactly
#   1  the query's first word, exactly — its outward/district part
#   2  a code the query starts with — the district containing what was typed
#   3  codes starting with the query — what was typed is a partial code
#   4  the town, exactly
#   5  towns starting with the query
#
# Tiers 1 and 2 are what make full postcodes work at all. Several countries, the
# UK among them, publish only district-level codes ("SL1", never "SL1 1AA"), so
# matching solely on equality or query-prefix returns nothing for the string a
# user is most likely to type. Falling back to the district lands them in the
# right place — all this has to do, since the click is what sets the point.
#
# Tier 1 exists because normalising away the space loses information that
# disambiguates: "EH1 1AA" becomes EH11AA, which begins with both EH1 and EH11,
# and only the space says which was meant. Where the user typed one, the first
# word is the answer and outranks any prefix reasoning.
#
# Codes and towns are separate arms because they need different shapes of
# answer, and both are collapsed to one row per key with an averaged centroid.
# A town has hundreds of codes and a district has hundreds of units, so listing
# rows individually would bury every other match under one place's postcodes.
_SEARCH_SQL = """
WITH codes AS (
    SELECT MIN(CASE WHEN code_norm = ?           THEN 0
                    WHEN code_norm = ?           THEN 1
                    WHEN ? LIKE code_norm || '%' THEN 2
                    ELSE 3 END)     AS tier,
           any_value(code)          AS label,
           COUNT(*)                 AS n,
           any_value(place)         AS place_detail,
           any_value(admin1)        AS admin1,
           any_value(admin2)        AS admin2,
           any_value(country)       AS country,
           AVG(lat) AS lat, AVG(lon) AS lon
    FROM postal_codes
    WHERE (code_norm = ?
           OR code_norm = ?
           -- length guard: a one-character code would otherwise prefix-match
           -- every query beginning with that letter.
           OR (LENGTH(code_norm) >= 2 AND ? LIKE code_norm || '%')
           OR code_norm LIKE ?) {country_filter}
    GROUP BY code_norm
),
towns AS (
    SELECT CASE WHEN place_norm = ? THEN 4 ELSE 5 END AS tier,
           any_value(place)      AS label,
           COUNT(*)              AS n,
           CAST(NULL AS VARCHAR) AS place_detail,
           admin1,
           any_value(admin2)     AS admin2,
           any_value(country)    AS country,
           AVG(lat) AS lat, AVG(lon) AS lon
    FROM postal_codes
    -- Sources carrying codes but no place names (Open Postcode Geo) leave this
    -- NULL, and without the guard every one of their rows collapses into a
    -- single nameless "town" that matches nothing and outranks real answers.
    WHERE place_norm IS NOT NULL
      AND (place_norm = ? OR place_norm LIKE ?) {country_filter}
    -- admin1 as well as the name, so two identically-named towns in different
    -- regions stay two answers rather than averaging into a point in neither.
    GROUP BY place_norm, admin1
)
SELECT tier, label, n, place_detail, admin1, admin2, country, lat, lon
FROM (SELECT * FROM codes UNION ALL SELECT * FROM towns)
-- Within a district-fallback tier the longest code wins: it is the most
-- specific thing the dataset holds that still contains what was typed. Within
-- a partial-code tier the shortest wins, being closest to what was typed.
ORDER BY tier,
         CASE WHEN tier = 2 THEN -LENGTH(label) ELSE LENGTH(label) END,
         label
LIMIT ?
"""


#: First tier in _SEARCH_SQL that is a town rather than a code match. Named
#: rather than inlined because the tiers have been renumbered once already, and
#: the two places that agree on the boundary are 150 lines apart.
_FIRST_TOWN_TIER = 4


CONFIG_PATH = Path("./config/config.yaml")


def persist_online_fallback(enabled: bool, path: Path = CONFIG_PATH) -> None:
    """Write geocode.online_fallback back to config.yaml, comments intact."""
    update_block(
        path, "geocode", {"online_fallback": bool(enabled)},
        block_comment="Place search for the apiary location picker.",
        comments={"online_fallback":
                  "Fall back to OpenStreetMap for street addresses and\n"
                  "businesses when the local postal data has no answer.\n"
                  "Sends the typed search off the hub, so it is opt-in."},
    )


def _norm(s: str) -> str:
    """Casefold and strip separators, so 'SW1A 1AA' and 'sw1a1aa' agree."""
    return re.sub(r"[\s\-]", "", str(s or "")).upper()


class Geocoder:
    """
    Local postal-code search with an optional online fallback.

    Every DB touch runs on `_executor` — a single dedicated thread that is the
    only holder of the connection.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="geocode-db")
        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._nominatim_lock = asyncio.Lock()
        self._last_nominatim = 0.0
        self.online_fallback = False
        #: Where the toggle is written back to. Injected so a test can point it
        #: somewhere harmless, and so the module never assumes it owns the
        #: hub's config file.
        self.config_path: Optional[Path] = CONFIG_PATH

    def set_online_fallback(self, enabled: bool) -> bool:
        """
        Change the fallback setting and persist it.

        Persistence failing does not undo the live change: the user asked for
        the setting they now have, and refusing it because a file is read-only
        would be a worse answer than losing it at the next restart. The failure
        is returned so the UI can say which of the two happened.
        """
        self.online_fallback = bool(enabled)
        if not self.config_path:
            return False
        try:
            persist_online_fallback(self.online_fallback, self.config_path)
            return True
        except Exception as e:                            # noqa: BLE001
            logger.error(f"Could not persist geocode.online_fallback: {e}")
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._run(self._open)
        logger.info(f"Geocoder started ({self.db_path})")

    async def stop(self) -> None:
        try:
            await self._run(self._close)
        except Exception:                                 # noqa: BLE001
            pass
        self._executor.shutdown(wait=False)

    def _open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self._con.execute(stmt)

    def _close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def search(self, query: str, limit: int = 5,
                     country: Optional[str] = None) -> Dict[str, Any]:
        """
        Candidate places for a query, best first.

        Never raises for an upstream failure: a search that cannot reach the
        internet should leave the user clicking the map, which still works,
        rather than showing them an error about a service they did not ask for.
        """
        q = " ".join(str(query or "").split())
        if not q or len(q) > 200:
            return {"results": [], "source": None}
        limit = max(1, min(int(limit), MAX_RESULTS))
        cc = country.upper() if country and _CC_RE.match(country) else None

        local, credits = await self._run(self._search_local_credited, q, limit, cc)
        if local or not self.online_fallback:
            return {"results": local, "source": "local",
                    "attribution": "; ".join(credits)}

        try:
            return {"results": await self._nominatim(q, limit),
                    "source": "nominatim",
                    "attribution": "© OpenStreetMap contributors"}
        except Exception as e:                            # noqa: BLE001
            logger.warning(f"geocode fallback failed for {q!r}: {e}")
            return {"results": [], "source": "nominatim"}

    def _search_local_credited(self, q, limit, cc):
        return self._search_local(q, limit, cc), self.attributions()

    def _search_local(self, q: str, limit: int,
                      cc: Optional[str]) -> List[Dict[str, Any]]:
        if self._con is None:
            return []
        n = _norm(q)
        # Prefix matching only for something code-shaped. A partial postcode is
        # a useful search; a partial street name matched as a prefix against
        # every code in the country is noise. The sentinel matches nothing.
        code_like = (n + "%") if _CODEISH_RE.match(q) else "\x00"
        place_like = n + "%"

        # The first word of a multi-word query — the outward part of a postcode
        # someone typed with its space. The sentinel matches nothing when there
        # is only one word, leaving the prefix tiers to do the work.
        head = _norm(q.split()[0]) if len(q.split()) > 1 else "\x00"

        cf = "AND country = ?" if cc else ""
        sql = _SEARCH_SQL.format(country_filter=cf)
        params: List[Any] = [n, head, n, n, head, n, code_like] + ([cc] if cc else [])
        params += [n, n, place_like] + ([cc] if cc else [])
        params.append(limit)

        rows = self._con.execute(sql, params).fetchall()
        out = []
        for tier, label, count, place_detail, admin1, admin2, country, lat, lon in rows:
            # Tiers 0-3 are code matches, 4-5 town matches — see _SEARCH_SQL.
            is_town = tier >= _FIRST_TOWN_TIER
            # The place name only identifies a code that covers one locality.
            # A district spanning several would be labelled with whichever of
            # them the grouping happened to surface, which reads as a fact.
            place = place_detail if (not is_town and count == 1) else None
            spread = (not is_town and count > 1)
            out.append({
                "label": label,
                # Widest-to-narrowest context, skipping whatever the dataset
                # left blank — many countries populate only some admin levels.
                "detail": ", ".join(
                    x for x in (place, admin2, admin1, country) if x),
                "lat": float(lat),
                "lon": float(lon),
                "kind": "town" if is_town else "postcode",
                # Flags a centroid averaged over several localities, so the UI
                # can zoom out to cover it rather than land inside one corner.
                "approximate": spread,
                "source": "local",
            })
        return out

    async def _nominatim(self, query: str, limit: int) -> List[Dict[str, Any]]:
        import aiohttp

        async with self._nominatim_lock:
            wait = _NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - self._last_nominatim)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
                headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
                async with aiohttp.ClientSession(timeout=timeout,
                                                 headers=headers) as sess:
                    async with sess.get(NOMINATIM, params={
                            "q": query, "format": "jsonv2",
                            "limit": str(limit), "addressdetails": "1"}) as resp:
                        data = await resp.json(content_type=None) \
                            if resp.status == 200 else None
            finally:
                # Stamped even on failure: a failed request still consumed the
                # allowance, and retrying immediately is what gets a client
                # blocked rather than rate-limited.
                self._last_nominatim = time.monotonic()

        out: List[Dict[str, Any]] = []
        for item in (data or [])[:limit]:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            # display_name is a long comma-separated path. The head names the
            # place and the tail places it, which is what a chooser needs.
            parts = [p.strip() for p in str(item.get("display_name", "")).split(",")]
            out.append({
                "label": parts[0] if parts else query,
                "detail": ", ".join(parts[1:4]),
                "lat": lat, "lon": lon,
                "kind": item.get("type") or "place",
                "source": "nominatim",
            })
        return out

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------
    async def datasets(self) -> List[Dict[str, Any]]:
        return await self._run(self._datasets)

    def _datasets(self) -> List[Dict[str, Any]]:
        rows = self._con.execute(
            "SELECT country, source, row_count, installed_at FROM datasets "
            "ORDER BY country, source"
        ).fetchall()
        out = []
        for cc, src, n, at in rows:
            meta = SOURCES.get(src, {})
            out.append({
                "country": cc,
                "name": COMMON_COUNTRIES.get(cc, cc),
                "source": src,
                "source_label": meta.get("label", src),
                "precision": meta.get("precision"),
                "attribution": meta.get("attribution"),
                "row_count": n,
                "installed_at": at,
            })
        return out

    def attributions(self) -> List[str]:
        """Licence lines for everything currently installed, deduplicated."""
        rows = self._con.execute("SELECT DISTINCT source FROM datasets").fetchall()
        seen = []
        for (src,) in rows:
            a = SOURCES.get(src, {}).get("attribution")
            if a and a not in seen:
                seen.append(a)
        return seen

    async def install(self, country: str,
                      source: Optional[str] = None) -> Dict[str, Any]:
        """
        Download and load one country's postal data from one source.

        Replaces that country's copy from that source only, leaving any other
        source's rows alone — the two are complementary, not competing.

        The download and parse run off the DB thread so a transfer of a hundred
        megabytes cannot block searches for the minutes it takes.
        """
        cc = str(country or "").upper()
        if not _CC_RE.match(cc):
            raise ValueError("country must be a 2-letter ISO code")
        src = source or default_source(cc)
        meta = SOURCES.get(src)
        if not meta:
            raise ValueError(f"Unknown source {src!r}")
        if meta["countries"] and cc not in meta["countries"]:
            raise ValueError(f"{meta['label']} does not cover {cc}")

        loop = asyncio.get_running_loop()
        tmp = Path(tempfile.mkdtemp(prefix="zmm-geocode-"))
        try:
            archive = tmp / "download"
            url = meta["url"].format(cc=cc)
            await loop.run_in_executor(None, self._download_to, url, archive)

            if src == "geonames":
                rows = await loop.run_in_executor(None, self._parse_geonames,
                                                  archive, cc)
                if not rows:
                    raise ValueError(f"No postal data published for {cc}")
                n = await self._run(self._load_rows, cc, src, rows)
            else:
                csv_path = await loop.run_in_executor(
                    None, self._extract_member, archive, tmp, ".csv")
                n = await self._run(self._load_open_postcode_geo, cc, src, csv_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        logger.info(f"[geocode] installed {cc}/{src}: {n} postal codes")
        return {"country": cc, "source": src, "row_count": n,
                "source_label": meta["label"],
                "name": COMMON_COUNTRIES.get(cc, cc)}

    @staticmethod
    def _download_to(url: str, dest: Path) -> None:
        # urllib rather than a new dependency, in a thread. Streamed to disk:
        # Open Postcode Geo is ~100 MB, and reading that into memory on a hub
        # that may have 1 GB total is how the process gets OOM-killed.
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r, \
                open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 256 * 1024)

    @staticmethod
    def _extract_member(archive: Path, into: Path, suffix: str) -> Path:
        """Pull the first member with `suffix` out of a .tar.gz or .zip."""
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive, "r:*") as t:
                for m in t.getmembers():
                    if m.isfile() and m.name.lower().endswith(suffix):
                        # Flattened to a known name: a member path from an
                        # archive must never be joined onto a local directory.
                        out = into / ("member" + suffix)
                        with t.extractfile(m) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst, 256 * 1024)
                        return out
            raise ValueError(f"No {suffix} inside the archive")

        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if name.lower().endswith(suffix) and "readme" not in name.lower():
                    out = into / ("member" + suffix)
                    with z.open(name) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 256 * 1024)
                    return out
        raise ValueError(f"No {suffix} inside the archive")

    @classmethod
    def _parse_geonames(cls, archive: Path, cc: str) -> List[tuple]:
        """
        GeoNames postal export: one tab-separated <CC>.txt inside the zip.

        Columns are country, code, place, admin1 name, admin1 code, admin2
        name, admin2 code, admin3 name, admin3 code, lat, lon, accuracy.
        """
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith(".txt") and "readme" not in n.lower()]
            preferred = f"{cc}.txt"
            name = preferred if preferred in z.namelist() else (names[0] if names else None)
            if not name:
                return []
            text = z.read(name).decode("utf-8", errors="replace")

        rows: List[tuple] = []
        for line in text.splitlines():
            f = line.split("\t")
            if len(f) < 11:
                continue
            try:
                lat, lon = float(f[9]), float(f[10])
            except ValueError:
                continue
            code, place = f[1].strip(), f[2].strip()
            if not code:
                continue
            rows.append((f[0].strip() or cc, code, _norm(code),
                         place, _norm(place), f[3].strip(), f[5].strip(),
                         lat, lon))
        return rows

    def _load_rows(self, cc: str, src: str, rows: List[tuple]) -> int:
        self._replace(cc, src)
        self._con.executemany(
            "INSERT INTO postal_codes (country, code, code_norm, place, "
            "place_norm, admin1, admin2, lat, lon, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r + (src,) for r in rows])
        self._record(cc, src, len(rows))
        return len(rows)

    # Open Postcode Geo: headerless CSV, one row per postcode ever issued.
    # Column order is fixed by the publisher — postcode, status, usertype,
    # easting, northing, quality, country, latitude, longitude, then a series
    # of derived spellings and area/district/sector splits.
    #
    # Loaded through read_csv rather than row-by-row: this is 2.6M rows, and
    # an executemany of that is minutes where the engine's own reader is
    # seconds. It is also the reason the CSV beats the .sql dump alongside it.
    _OPG_SQL = """
    INSERT INTO postal_codes
        (country, source, code, code_norm, place, place_norm,
         admin1, admin2, lat, lon)
    SELECT ?, ?,
           column00,
           UPPER(REPLACE(REPLACE(column00, ' ', ''), '-', '')),
           NULL, NULL,
           NULLIF(column06, ''),
           NULLIF(column13, ''),
           TRY_CAST(column07 AS DOUBLE),
           TRY_CAST(column08 AS DOUBLE)
    FROM read_csv(?, header = false, all_varchar = true,
                  columns = {cols})
    -- Terminated postcodes are history, not places someone can drive to, and
    -- the publisher parks the ones with no grid reference at latitude 99.
    WHERE column01 = 'live'
      AND TRY_CAST(column07 AS DOUBLE) BETWEEN -90 AND 90
      AND TRY_CAST(column08 AS DOUBLE) BETWEEN -180 AND 180
    """

    def _load_open_postcode_geo(self, cc: str, src: str, csv_path: Path) -> int:
        cols = "{" + ", ".join(f"'column{i:02d}': 'VARCHAR'" for i in range(17)) + "}"
        self._replace(cc, src)
        self._con.execute(self._OPG_SQL.format(cols=cols),
                          [cc, src, str(csv_path)])
        n = self._con.execute(
            "SELECT COUNT(*) FROM postal_codes WHERE country = ? AND source = ?",
            [cc, src]).fetchone()[0]
        # Borrow town names from GeoNames where it is installed: this source
        # carries none, and "SW1A 1AA — England" is a poorer answer than
        # "SW1A 1AA — Westminster" when the other half is already on disk.
        self._con.execute(
            "UPDATE postal_codes AS p SET place = g.place, place_norm = NULL "
            "FROM (SELECT code_norm, any_value(place) AS place FROM postal_codes "
            "      WHERE country = ? AND source = 'geonames' GROUP BY code_norm) AS g "
            "WHERE p.country = ? AND p.source = ? AND p.place IS NULL "
            "  AND g.code_norm = UPPER(REPLACE(p.admin2, ' ', ''))",
            [cc, cc, src])
        self._record(cc, src, n)
        return int(n)

    def _replace(self, cc: str, src: str) -> None:
        # Replace wholesale rather than merge: a re-install is how a stale copy
        # gets corrected, and merging would keep codes the publisher retired.
        self._con.execute(
            "DELETE FROM postal_codes WHERE country = ? AND source = ?", [cc, src])

    def _record(self, cc: str, src: str, n: int) -> None:
        self._con.execute(
            "INSERT INTO datasets (country, source, row_count, installed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (country, source) DO UPDATE SET "
            "row_count = EXCLUDED.row_count, installed_at = EXCLUDED.installed_at",
            [cc, src, n, time.time()])

    async def remove(self, country: str,
                     source: Optional[str] = None) -> bool:
        cc = str(country or "").upper()
        if not _CC_RE.match(cc):
            return False
        return await self._run(self._remove, cc, source)

    def _remove(self, cc: str, src: Optional[str]) -> bool:
        where = "country = ?" + (" AND source = ?" if src else "")
        params: List[Any] = [cc] + ([src] if src else [])
        if not self._con.execute(
                f"SELECT 1 FROM datasets WHERE {where}", params).fetchone():
            return False
        self._con.execute(f"DELETE FROM postal_codes WHERE {where}", params)
        self._con.execute(f"DELETE FROM datasets WHERE {where}", params)
        return True


_geocoder: Optional[Geocoder] = None


def get_geocoder() -> Optional[Geocoder]:
    return _geocoder


def set_geocoder(g: Geocoder) -> None:
    global _geocoder
    _geocoder = g
