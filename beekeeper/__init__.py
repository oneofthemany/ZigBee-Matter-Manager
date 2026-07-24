"""Beekeeper — ZMM's first-party DNS sinkhole ad/tracker blocker.

A small, fully-owned DNS forwarding resolver that answers queries for the whole
LAN: known ad/tracker domains are sinkholed, everything else is forwarded to an
upstream resolver. Runs as its own always-on sidecar process (see
``beekeeper.__main__``) so restarting or upgrading the main ZMM app never takes
household DNS down.

"Own the source": the DNS engine, matcher, resolver, stats and control API are
all in-repo with no third-party DNS library. Blocklists are ingested as *data*
from public hosts-format sources into ``data/beekeeper/lists/`` where they can
be pruned and overridden — the same approach Pi-hole/AdGuard use for their own
default lists.
"""

__version__ = "0.1.0"
