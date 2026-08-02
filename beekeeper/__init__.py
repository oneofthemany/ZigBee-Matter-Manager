"""
Beekeeper — ZMM's first-party DNS sinkhole ad/tracker blocker.

A small forwarding resolver for the whole LAN, running as its own always-on
sidecar so restarting or upgrading the main app never takes household DNS down.
Own the source: engine, matcher, resolver, stats and control API are all in-repo
with no third-party DNS library. Blocklists are ingested as editable data.
See docs/beekeeper.md.
"""

__version__ = "0.1.0"
