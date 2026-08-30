"""
Fuel prices, by region.

`service` holds the query layer, `registry` says which source covers which
country, and `base` defines what a source has to provide. Providers live in
`modules/fuel/providers/`, one file per region.

The names below are re-exported because they were importable from
`modules.fuel_prices` before this became a package, and the routes and the
Android endpoint contract are written against them.
"""

from modules.fuel.base import FuelProvider, haversine_km
from modules.fuel.registry import DEFAULT_REGION, known_regions, resolve_region
from modules.fuel.service import (
    DEFAULT_RADIUS_KM,
    FUEL_TYPES,
    MAX_RADIUS_KM,
    FuelPriceService,
    get_fuel_service,
    maps_url,
    reset_fuel_service,
)

__all__ = [
    "FuelProvider", "haversine_km",
    "DEFAULT_REGION", "known_regions", "resolve_region",
    "FUEL_TYPES", "DEFAULT_RADIUS_KM", "MAX_RADIUS_KM",
    "FuelPriceService", "get_fuel_service", "reset_fuel_service", "maps_url",
]
