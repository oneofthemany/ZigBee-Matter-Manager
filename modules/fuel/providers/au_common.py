"""
What the three Australian schemes have in common.

Fuel price reporting in Australia is state law, not federal, so there is no
national feed to prefer: New South Wales, Queensland and Western Australia each
run their own scheme, with their own grade codes, their own auth and their own
idea of what a price is. They are three providers, registered as `AU-NSW`,
`AU-QLD` and `AU-WA`.

What they do share is the dialect — Australian dollars, quoted at the pump in
cents per litre — and that is what lives here, so the three cannot drift apart
on it.

A note on scale, because all three differ and all three are wrong by two or
three orders of magnitude if read naively. NSW gives cents (`203.9`), WA gives
cents (`186.7`), Queensland gives *tenths* of a cent (`1679` is 167.9c). Every
provider in this package hands prices out in the major currency unit — dollars
here — so each divides by its own documented factor. None of them guess.
"""

from __future__ import annotations

import re
from typing import Any, Optional

CURRENCY = "AUD"
CURRENCY_SYMBOL = "A$"
VOLUME_UNIT = "L"
DISTANCE_UNIT = "km"

#: Australia quotes pump prices in cents — "one eighty nine nine", never
#: "$1.899" — so the number is stored in dollars and shown in cents.
DISPLAY_SCALE = "minor"
DISPLAY_DECIMALS = 3

#: Cents per litre -> dollars per litre.
CENTS_PER_DOLLAR = 100.0

#: A trailing four-digit postcode, which is how all three schemes end an
#: address line: "36 Henderson Road, Alexandria NSW 2015".
_POSTCODE_RE = re.compile(r"\b(\d{4})\s*$")


def postcode_from_address(address: Any) -> Optional[str]:
    """
    The postcode at the end of an address line, or None.

    NSW publishes no postcode field, only a full address string with the
    postcode on the end. Pulling it out is worth the regex: it is what makes the
    Maps link land on the station's own listing rather than on a pin, and what
    the Drive tab puts on the directions button.
    """
    text = str(address or "").strip()
    if not text:
        return None
    found = _POSTCODE_RE.search(text)
    return found.group(1) if found else None


def price_from_cents(raw: Any, divisor: float = CENTS_PER_DOLLAR) -> Optional[float]:
    """
    A scheme's own price number to dollars per litre. None when unusable.

    Rounded to four places rather than three: a tenth of a cent is the fourth
    decimal of a dollar, and Queensland genuinely reports to that precision.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return round(value / divisor, 4)
