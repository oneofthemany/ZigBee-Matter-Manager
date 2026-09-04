"""
Swarm Intelligence.

A semantic layer over the automation engine. Devices describe themselves in one
shared vocabulary of triggers, conditions and actions, so any device can be
wired to any other without a hand-written rule for each combination.

    capabilities.py  the vocabulary — what each capability can contribute
    resolver.py      one device, of any protocol, reduced to its offers
    network.py       the whole swarm, and the ranked wiring between devices
    api.py           read-only HTTP surface

Offers compile to the rule dict AutomationEngine already accepts; this package
adds no second execution path.
"""

from modules.swarm.capabilities import (
    ACTION,
    CAPABILITIES,
    CONDITION,
    PARAMS,
    TRIGGER,
    canonical_capability,
    classify,
    resolve_param,
)
from modules.swarm.network import describe_network, load_rooms, pairings, summarise
from modules.swarm.resolver import describe_device, device_capabilities

__all__ = [
    "ACTION", "CAPABILITIES", "CONDITION", "PARAMS", "TRIGGER",
    "canonical_capability", "classify", "resolve_param",
    "describe_device", "device_capabilities",
    "describe_network", "load_rooms", "pairings", "summarise",
]
