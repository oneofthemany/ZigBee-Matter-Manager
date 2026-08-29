"""
Fuel Finder API client — the statutory UK fuel price feed.

Replaces the retailer open-data feeds behind uk-fuel-prices-api with the
government service those feeds now report into. The Motor Fuel Price (Open
Data) Regulations 2025 require prices to be published within 30 minutes of a
change, which is both why this source is worth moving to and why the refresh
interval below is 30 minutes rather than something guessed.

Deliberately duck-types UKFuelPricesApi — `get_prices`, `stations`,
`stationsWithinRadius` — so FuelPriceService can hold either without knowing
which. That is what keeps the fallback path in fuel_prices.py to a few lines
instead of two parallel implementations of every query.

Auth is OAuth 2.0 client credentials (scope fuelfinder.read), tokens last an
hour. See docs/journeys.md and config/config.yaml (fuel.finder).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("modules.fuel_finder")

#: Credentials are read from here, in this order, and never from config.yaml —
#: that file is tracked in git, so a secret pasted into it is a secret pushed
#: to the remote. See _resolve_credentials.
ENV_CLIENT_ID = "ZMM_FUEL_FINDER_CLIENT_ID"
ENV_CLIENT_SECRET = "ZMM_FUEL_FINDER_CLIENT_SECRET"
SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")


def _resolve_credentials(config: Dict[str, Any]) -> tuple[str, str]:
    """
    client_id and client_secret, from the environment or the gitignored
    secrets file.

    Two sources rather than one because the two deployments differ: the
    container gets environment variables, a bare install gets a file it can
    edit. Neither is config.yaml, which is tracked.

    A secret left in config.yaml is read anyway — refusing would just make the
    app look broken — but it is loud about it, because by the time it works the
    secret is already staged for the next commit.
    """
    client_id = os.environ.get(ENV_CLIENT_ID, "").strip()
    client_secret = os.environ.get(ENV_CLIENT_SECRET, "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            secrets = (yaml.safe_load(fh) or {}).get("fuel_finder") or {}
        client_id = client_id or str(secrets.get("client_id") or "").strip()
        client_secret = client_secret or str(secrets.get("client_secret") or "").strip()
    except FileNotFoundError:
        pass
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")

    if client_id and client_secret:
        return client_id, client_secret

    in_config = str(config.get("client_secret") or "").strip()
    if in_config:
        logger.error(
            "Fuel Finder client_secret found in config.yaml, which is TRACKED "
            "IN GIT. Move it to %s or the %s environment variable, then rotate "
            "it in the developer portal — assume the one in the file is burnt.",
            SECRETS_FILE, ENV_CLIENT_SECRET,
        )
        return str(config.get("client_id") or "").strip(), in_config

    return client_id, client_secret

#: Fuel Finder grade -> the code this project already uses everywhere else.
#: B7_Premium maps to SDV because "super diesel" is what the UI has always
#: called it; renaming the internal code would churn history rows and the car
#: screen for no gain. B10 and HVO have no existing code — they are carried
#: through in the per-station price map but are not selectable, so nothing is
#: silently dropped and nothing new appears in the UI uninvited.
GRADE_MAP = {
    "E10": "E10",
    "E5": "E5",
    "B7_Standard": "B7",
    "B7_Premium": "SDV",
}
PASSTHROUGH_GRADES = ("B10", "HVO")

EARTH_RADIUS_KM = 6371.0

#: Tokens last 3600s. Renew early: a token that expires mid-flight surfaces as
#: a 401 on a query the driver is waiting on, and the whole point of the car
#: screen is that it answers at a glance.
TOKEN_REFRESH_MARGIN_S = 300


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Same maths uk-fuel-prices-api did server-side."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def looks_like_pence(prices: List[float]) -> bool:
    """
    Whether a batch of prices is quoted in pence rather than pounds.

    The API field guide documents `price` as "last recorded fuel price" and
    does not state a unit, so it is measured rather than assumed. UK pump
    prices sit around 130-180p, i.e. £1.30-£1.80: the two scales are two orders
    of magnitude apart and nothing plausible falls between them. Median, not
    mean, so a single junk row cannot flip the whole batch.

    Everything downstream — history rows, the car screen, the Drive tab — is in
    pounds, so this is the one place the question gets asked.
    """
    usable = [p for p in prices if isinstance(p, (int, float)) and p > 0]
    if not usable:
        return False
    return statistics.median(usable) > 20.0


def mask(secret: str) -> str:
    """Last four characters only — enough to tell two keys apart, no more."""
    secret = (secret or "").strip()
    if not secret:
        return ""
    return f"{'*' * max(4, len(secret) - 4)}{secret[-4:]}"


def credentials_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    What the settings UI may know: whether credentials exist, where they came
    from, and enough of the id to tell one pair from another. The secret itself
    is never returned — a value the browser can read is a value a compromised
    session can exfiltrate, and it is write-only for a reason.
    """
    client_id, client_secret = _resolve_credentials(config)
    if os.environ.get(ENV_CLIENT_ID) and os.environ.get(ENV_CLIENT_SECRET):
        source = "environment"
    elif client_id or client_secret:
        source = "secrets_file"
    else:
        source = "none"
    return {
        "configured": bool(client_id and client_secret),
        "source": source,
        "client_id": mask(client_id),
        "secret_set": bool(client_secret),
        # Editable here only when the environment isn't supplying them: a POST
        # would write a file that the environment then silently overrides,
        # which reads as "the save didn't work".
        "editable": source != "environment",
        "secrets_file": SECRETS_FILE,
    }


def save_credentials(client_id: str, client_secret: str) -> None:
    """
    Persist credentials to the gitignored secrets file, 0600.

    Written before the file is populated, not after: a secrets file that is
    briefly world-readable is briefly readable by every process on the box.
    """
    import yaml

    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Both client_id and client_secret are required")

    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = yaml.safe_load(path.read_text()) or {}
        except Exception:                                 # noqa: BLE001
            existing = {}

    existing["fuel_finder"] = {
        "client_id": client_id,
        "client_secret": client_secret,
    }

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)
    logger.info("Fuel Finder credentials saved to %s", path)


class FuelFinderClient:
    """OAuth2 client for the Fuel Finder public API."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.client_id, self.client_secret = _resolve_credentials(config)
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self.token_url = str(config.get("token_url") or "").strip()
        self.scope = str(config.get("scope") or "fuelfinder.read").strip()
        # The regulations put a 30-minute ceiling on staleness at the source,
        # so polling faster than that spends rate limit to learn nothing.
        self.refresh_s = max(60, int(config.get("refresh_minutes") or 30) * 60)

        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self._stations: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._last_error: Optional[str] = None
        self._last_refresh: float = 0.0

    # ---------------------------------------------------------------- config

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.client_id
            and self.client_secret
            and self.base_url
            and self.token_url
        )

    @property
    def stations(self) -> List[Dict[str, Any]]:
        """Duck-types UKFuelPricesApi.stations."""
        return self._stations

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ----------------------------------------------------------------- auth

    async def _access_token(self, sess: aiohttp.ClientSession) -> Optional[str]:
        if self._token and time.time() < self._token_expires - TOKEN_REFRESH_MARGIN_S:
            return self._token

        form = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        async with sess.post(
            self.token_url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                # Credentials are the overwhelmingly likely cause and the only
                # one the operator can act on, so say so rather than printing a
                # bare status.
                self._last_error = (
                    f"Fuel Finder auth failed ({resp.status}). "
                    f"Check fuel.finder client_id/client_secret. {body}"
                )
                logger.error(self._last_error)
                self._token = None
                return None
            data = await resp.json()

        token = data.get("access_token")
        if not token:
            self._last_error = "Fuel Finder auth returned no access_token"
            logger.error(self._last_error)
            return None
        self._token = str(token)
        self._token_expires = time.time() + float(data.get("expires_in") or 3600)
        return self._token

    async def verify(self) -> Dict[str, Any]:
        """
        Exchange the credentials for a token and report the result.

        Only the token leg, deliberately: it is the whole of what the operator
        just typed, it costs one small request, and a failure here names the
        cause precisely. Pulling the national dataset to prove a client_id is
        valid would take a minute and tell you no more.
        """
        if not self.configured:
            missing = [
                name for name, value in (
                    ("client_id", self.client_id),
                    ("client_secret", self.client_secret),
                    ("token_url", self.token_url),
                    ("base_url", self.base_url),
                ) if not value
            ]
            return {"success": False, "error": f"Not configured: {', '.join(missing)}"}

        self._token = None                    # never pass on a cached token
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                token = await self._access_token(sess)
        except Exception as e:                            # noqa: BLE001
            return {"success": False, "error": f"Could not reach the token endpoint: {e}"}

        if not token:
            return {"success": False, "error": self._last_error or "Authentication failed"}
        return {
            "success": True,
            "expires_in_s": max(0, int(self._token_expires - time.time())),
        }

    # ----------------------------------------------------------------- fetch

    async def _get_paged(
        self, sess: aiohttp.ClientSession, token: str, path: str
    ) -> List[Dict[str, Any]]:
        """
        GET every page of a collection.

        The developer guidelines say the API paginates but the public docs do
        not pin the shape, so all three common ones are handled: a bare list, a
        wrapper with an items/data/results array, and either a `next` link or a
        page counter. Unknown shapes stop the loop rather than spinning.
        """
        out: List[Dict[str, Any]] = []
        url: Optional[str] = f"{self.base_url}{path}"
        page = 1
        seen: set[str] = set()

        while url and url not in seen and page <= 200:
            seen.add(url)
            async with sess.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status == 401:
                    self._token = None          # force re-auth on the next pass
                    raise RuntimeError("Fuel Finder rejected the token (401)")
                if resp.status != 200:
                    raise RuntimeError(
                        f"Fuel Finder {path} returned {resp.status}"
                    )
                payload = await resp.json()

            if isinstance(payload, list):
                out.extend(payload)
                break

            if not isinstance(payload, dict):
                break

            batch = None
            for key in ("items", "data", "results", "content"):
                if isinstance(payload.get(key), list):
                    batch = payload[key]
                    break
            if batch is None:
                break
            out.extend(batch)

            nxt = payload.get("next") or payload.get("nextPage") or payload.get("_next")
            if isinstance(nxt, str) and nxt:
                url = nxt if nxt.startswith("http") else f"{self.base_url}{nxt}"
            elif payload.get("hasMore") or payload.get("has_more"):
                page += 1
                sep = "&" if "?" in path else "?"
                url = f"{self.base_url}{path}{sep}page={page}"
            else:
                url = None

        return out

    async def get_prices(self, force_refresh: bool = False) -> bool:
        """
        Refresh the station+price snapshot. Duck-types UKFuelPricesApi.

        Returns True when usable data is in hand — including data from a
        previous refresh, because a feed blip must degrade to "slightly stale"
        rather than to an empty car screen.
        """
        if not self.configured:
            self._last_error = "Fuel Finder not configured (fuel.finder in config.yaml)"
            return False

        async with self._lock:
            fresh_enough = (time.time() - self._last_refresh) < self.refresh_s
            if self._stations and fresh_enough and not force_refresh:
                return True

            try:
                timeout = aiohttp.ClientTimeout(total=90)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    token = await self._access_token(sess)
                    if not token:
                        return bool(self._stations)

                    sites = await self._get_paged(sess, token, "/api/v1/pfs")
                    prices = await self._get_paged(sess, token, "/api/v1/pfs/fuel-prices")

                merged = self._merge(sites, prices)
                if not merged:
                    self._last_error = "Fuel Finder returned no usable stations"
                    logger.warning(self._last_error)
                    return bool(self._stations)

                self._stations = merged
                self._last_refresh = time.time()
                self._last_error = None
                logger.info("Fuel Finder: %d stations loaded", len(merged))
                return True

            except Exception as e:                        # noqa: BLE001
                self._last_error = f"Fuel Finder refresh failed: {e}"
                logger.warning(self._last_error)
                return bool(self._stations)

    # ----------------------------------------------------------------- shape

    def _merge(
        self, sites: List[Dict[str, Any]], prices: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Join the two endpoints on node_id and flatten to the station shape the
        rest of the app already speaks.
        """
        by_node: Dict[str, Dict[str, float]] = {}
        # Indexed, not scanned. The national snapshot is thousands of sites and
        # thousands of price rows; pairing them with a linear search per site is
        # quadratic and turns a refresh into a visible stall.
        rows_by_node: Dict[str, Dict[str, Any]] = {}
        raw_prices: List[float] = []

        for row in prices:
            node = str(row.get("node_id") or "")
            if not node:
                continue
            grades: Dict[str, float] = {}
            for entry in row.get("fuel_prices") or []:
                grade = str(entry.get("fuel_type") or "")
                value = entry.get("price")
                if not isinstance(value, (int, float)) or value <= 0:
                    continue
                code = GRADE_MAP.get(grade, grade if grade in PASSTHROUGH_GRADES else None)
                if not code:
                    continue
                grades[code] = float(value)
                raw_prices.append(float(value))
            if grades:
                by_node[node] = grades
                rows_by_node.setdefault(node, row)

        # One decision for the whole batch, not per row: a station that happens
        # to sell only one grade must not be scaled differently from its
        # neighbours, or the cheapest-first sort becomes nonsense.
        divisor = 100.0 if looks_like_pence(raw_prices) else 1.0

        out: List[Dict[str, Any]] = []
        for site in sites:
            node = str(site.get("node_id") or "")
            grades = by_node.get(node)
            if not grades:
                continue
            # Closed sites still carry a last-known price. Showing one as the
            # cheapest nearby sends a driver to a locked forecourt.
            if site.get("permanent_closure") or site.get("temporary_closure"):
                continue

            loc = site.get("location") or {}
            lat, lon = loc.get("latitude"), loc.get("longitude")
            if lat is None or lon is None:
                continue

            address = ", ".join(
                str(p).strip()
                for p in (loc.get("address_line_1"), loc.get("address_line_2"), loc.get("city"))
                if str(p or "").strip()
            )

            station: Dict[str, Any] = {
                "site_id": node,
                "brand": site.get("brand_name") or site.get("trading_name"),
                "address": address,
                "postcode": loc.get("postcode"),
                "latitude": float(lat),
                "longitude": float(lon),
                "last_updated": self._latest_timestamp(rows_by_node.get(node)),
            }
            for code, value in grades.items():
                station[code] = round(value / divisor, 3)
            out.append(station)

        return out

    @staticmethod
    def _latest_timestamp(row: Optional[Dict[str, Any]]) -> Optional[str]:
        if not row:
            return None
        stamps = [
            str(e.get("price_last_updated"))
            for e in (row.get("fuel_prices") or [])
            if e.get("price_last_updated")
        ]
        return max(stamps) if stamps else None

    # --------------------------------------------------------------- queries

    def stationsWithinRadius(                             # noqa: N802
        self, lat: float, lon: float, radius_km: float
    ) -> List[Dict[str, Any]]:
        """
        Duck-types UKFuelPricesApi.stationsWithinRadius.

        Filtered here rather than by the API: the public endpoints publish the
        whole UK and document no geographic query parameter, so the snapshot is
        national and the radius is applied locally. That is also why the
        snapshot is cached — one national fetch per refresh window serves every
        query in it.
        """
        out = []
        for s in self._stations:
            d = haversine_km(lat, lon, s["latitude"], s["longitude"])
            if d <= radius_km:
                out.append({**s, "dist": round(d, 2)})
        return out
