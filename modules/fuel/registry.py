"""
Which fuel source covers which country.

Shaped like `geocode.SOURCES` — a data registry of what exists, separate from
the code that builds one — because the two answer the same kind of question and
the Settings UI that renders a country picker already exists for that one.

A region key is an ISO-3166 alpha-2 country code, or `CC-SUB` where a country
reports per state rather than nationally. Australia is the reason: fuel price
reporting there is state law, so NSW, Queensland and Western Australia each
publish their own feed and there is no national one to prefer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from modules.fuel.base import FallbackProvider, FuelProvider

logger = logging.getLogger("modules.fuel.registry")


def _gb(config: Dict[str, Any]) -> FuelProvider:
    """
    The UK chain: statutory feed first, retailer feeds behind it.

    Both are national bulk snapshots reporting the same four grade codes, so
    the fallback is invisible to a caller — only `source` in the status payload
    says which one answered.
    """
    from modules.fuel.providers.uk_fuel_finder import FuelFinderClient
    from modules.fuel.providers.uk_retailers import UKRetailerFeeds

    fuel_cfg = (config.get("fuel") or {})
    return FallbackProvider(
        region="GB",
        label="United Kingdom",
        children=[
            FuelFinderClient(fuel_cfg.get("finder") or {}),
            UKRetailerFeeds(fuel_cfg.get("retailers") or {}),
        ],
        config=config,
    )


def _de(config: Dict[str, Any]) -> FuelProvider:
    from modules.fuel.providers.de_tankerkoenig import GermanyTankerkoenig
    return GermanyTankerkoenig((config.get("fuel") or {}).get("tankerkoenig") or {})


def _es(config: Dict[str, Any]) -> FuelProvider:
    from modules.fuel.providers.es_minetur import SpainMinetur
    return SpainMinetur((config.get("fuel") or {}).get("minetur") or {})


def _fr(config: Dict[str, Any]) -> FuelProvider:
    from modules.fuel.providers.fr_gouv import FranceGouv
    return FranceGouv((config.get("fuel") or {}).get("fr_gouv") or {})


def _it(config: Dict[str, Any]) -> FuelProvider:
    from modules.fuel.providers.it_mimit import ItalyMimit
    return ItalyMimit((config.get("fuel") or {}).get("mimit") or {})


#: region key -> how to describe and how to build it. `build` is a callable
#: rather than a class so a region can be a chain (GB) or a single client
#: without the caller caring which.
REGIONS: Dict[str, Dict[str, Any]] = {
    "GB": {
        "label": "United Kingdom",
        "country": "GB",
        "build": _gb,
        "needs_credentials": True,
        "station_level": True,
        "note": ("Statutory Fuel Finder feed, falling back to the retailer "
                 "open-data scheme. Needs a Fuel Finder client ID and secret."),
    },
    "DE": {
        "label": "Germany",
        "country": "DE",
        "build": _de,
        "needs_credentials": True,
        "station_level": True,
        "note": ("Tankerkönig, over the Bundeskartellamt's MTS-K data. Needs a "
                 "free API key. Searches are capped at 25 km and one request "
                 "per minute, so results are cached for ten minutes."),
    },
    "ES": {
        "label": "Spain",
        "country": "ES",
        "build": _es,
        "needs_credentials": False,
        "station_level": True,
        "note": "Ministry price register. No key needed; the whole country in one file.",
    },
    "FR": {
        "label": "France",
        "country": "FR",
        "build": _fr,
        "needs_credentials": False,
        "station_level": True,
        "note": ("Flux instantané from data.economie.gouv.fr, refreshed about "
                 "every ten minutes. No key needed. Carries no brand names."),
    },
    "IT": {
        "label": "Italy",
        "country": "IT",
        "build": _it,
        "needs_credentials": False,
        "station_level": True,
        "note": ("Osservaprezzi Carburanti. No key needed. Published once a "
                 "day, and self-service prices are preferred where a station "
                 "reports both."),
    },
}

#: Used when nothing is configured and nothing can be detected. The UK is the
#: honest default here: it is the only region this project has ever supported,
#: so an existing hub that upgrades must land exactly where it already was.
DEFAULT_REGION = "GB"


def known_regions() -> List[Dict[str, Any]]:
    """The registry as the Settings picker wants it — no build callables."""
    return [
        {"region": key, **{k: v for k, v in meta.items() if k != "build"}}
        for key, meta in REGIONS.items()
    ]


def resolve_region(country: str = "", subdivision: str = "") -> str:
    """
    A region key from a country and optional subdivision.

    Falls back from `AU-NSW` to `AU` and then to the default, so a country whose
    states are not all implemented still resolves to something rather than
    failing — and so a subdivision typed in the wrong case still works.
    """
    cc = (country or "").strip().upper()
    sub = (subdivision or "").strip().upper()
    if not cc:
        return DEFAULT_REGION
    if sub and f"{cc}-{sub}" in REGIONS:
        return f"{cc}-{sub}"
    if cc in REGIONS:
        return cc
    # A country present only as subdivisions, with no state chosen: take the
    # first registered one rather than pretending the country is unsupported.
    for key in REGIONS:
        if key.startswith(f"{cc}-"):
            logger.info("No subdivision set for %s — defaulting to %s", cc, key)
            return key
    logger.warning("No fuel provider for country %r — using %s", cc, DEFAULT_REGION)
    return DEFAULT_REGION


def build_provider(region: str, config: Dict[str, Any]) -> FuelProvider:
    """Construct the provider for a region key. Unknown keys fall back."""
    meta = REGIONS.get(region) or REGIONS[DEFAULT_REGION]
    builder: Callable[[Dict[str, Any]], FuelProvider] = meta["build"]
    return builder(config)
