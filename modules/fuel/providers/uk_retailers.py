"""
The UK retailer open-data feeds, behind uk-fuel-prices-api.

The source this project used before the statutory Fuel Finder service existed:
roughly fifteen retailer JSON feeds (Asda, Tesco, BP, Shell and the rest),
aggregated by the package. It stays as the fallback because a misconfigured or
unreachable government API should degrade to the feeds the retailers publish
anyway, not to an empty Drive tab — see [FallbackProvider].

The package is an optional dependency. Its absence is a configuration fact, not
a crash: `configured` goes False and the chain moves on.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from modules.fuel.base import BulkSnapshotProvider
from modules.fuel.providers.uk_fuel_finder import FUEL_TYPES

logger = logging.getLogger("modules.fuel.providers.uk_retailers")


class UKRetailerFeeds(BulkSnapshotProvider):
    """Wraps uk-fuel-prices-api in the provider contract."""

    region = "GB"
    label = "UK retailer feeds"
    grades = FUEL_TYPES
    default_grade = "E10"
    currency = "GBP"
    currency_symbol = "£"
    volume_unit = "L"
    display_scale = "minor"
    display_decimals = 3
    attribution = "UK retailer open-data fuel price scheme"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._api = None
        self._import_error: Optional[str] = None

    @property
    def source(self) -> str:
        return "retailer_feeds"

    def _client(self):
        """
        The package's client, or None if it isn't installed.

        Imported on first use rather than at module scope so that a hub without
        the optional dependency still imports the fuel package cleanly.
        """
        if self._api is None and self._import_error is None:
            try:
                from uk_fuel_prices_api import UKFuelPricesApi
                self._api = UKFuelPricesApi()
            except ImportError as e:
                self._import_error = f"uk-fuel-prices-api not installed: {e}"
                logger.warning(self._import_error)
        return self._api

    @property
    def configured(self) -> bool:
        return self._client() is not None

    async def _fetch_all(self) -> List[Dict[str, Any]]:
        api = self._client()
        if api is None:
            self._last_error = self._import_error
            return []

        # The package keeps its own cache; force_refresh is passed straight
        # through so our TTL and its TTL do not fight over who decides.
        ok = await api.get_prices(force_refresh=True)
        if not ok:
            self._last_error = "No fuel data returned from any retailer feed"
            return []
        return list(api.stations)
