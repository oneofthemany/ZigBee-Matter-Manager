"""
Blocklist ingest and the domain matcher.

Sources are fetched with the stdlib and cached as editable .domains files; a
compile step unions the enabled ones. A name is blocked when it or any parent is
in the block set unless an allowlist entry covers it, with the user denylist
layered always-on. See docs/beekeeper.md.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("beekeeper.blocklists")

_META_NAME = "_meta.json"

# A hosts line is "<ip> <host>[ <host>...]"; a domain-list line is just a host
# (optionally "*." wildcard-prefixed, e.g. OISD's domainswild format).
_SINK_IPS = {"0.0.0.0", "127.0.0.1", "::", "::1", "255.255.255.255"}
# Hostnames that appear in every /etc/hosts and must never be sinkholed.
_NEVER_BLOCK = {
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters", "ip6-allhosts",
}
# Light validity gate: labels of a-z0-9/hyphen/underscore, at least one dot.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9_-]{1,63}(?:\.(?!-)[a-z0-9_-]{1,63})+$")

_FETCH_HEADERS = {"User-Agent": "ZMM-Beekeeper/0.1 (+https://github.com/oneofthemany)"}
_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB guard against a runaway/wrong URL


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "list"


def _clean_domain(token: str) -> Optional[str]:
    token = token.strip().lower().rstrip(".")
    if token.startswith("*."):        # wildcard-prefixed domain-list entry
        token = token[2:]
    if not token or token in _NEVER_BLOCK:
        return None
    if _looks_like_ip(token):          # a bare IP (e.g. "0.0.0.0 0.0.0.0") is not a domain
        return None
    if not _DOMAIN_RE.match(token):
        return None
    return token


def parse_lines(lines: Iterable[str]) -> Iterable[str]:
    """Yield bare domains from hosts-format or domain-list content.

    Handles: comments (``#``), inline comments, blank lines, sink-IP prefixes
    (``0.0.0.0 host`` / ``127.0.0.1 host``), multiple hosts per line, plain
    domain-only lists, and ``*.`` wildcard prefixes.
    """
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and (parts[0] in _SINK_IPS or _looks_like_ip(parts[0])):
            tokens = parts[1:]        # hosts format: drop the leading IP
        else:
            tokens = parts            # domain-list format: each token is a host
        for tok in tokens:
            dom = _clean_domain(tok)
            if dom:
                yield dom


def _looks_like_ip(token: str) -> bool:
    # Cheap check so a hosts file using a non-sink IP still parses correctly.
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", token)) or ":" in token


# Fetch + persist

def fetch_list(url: str, timeout: float = 20.0) -> str:
    """Download a list body over HTTPS. Raises on transport/HTTP errors."""
    req = urllib.request.Request(url, headers=_FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-configured URL
        data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError(f"list exceeds {_MAX_BYTES} bytes cap")
    return data.decode("utf-8", errors="replace")


@dataclass
class ListMeta:
    name: str
    url: str
    enabled: bool
    slug: str
    count: int = 0
    bytes: int = 0
    fetched_at: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "url": self.url, "enabled": self.enabled,
            "slug": self.slug, "count": self.count, "bytes": self.bytes,
            "fetched_at": self.fetched_at, "error": self.error,
        }


def refresh_lists(lists_dir: Path, sources: List[Dict], timeout: float = 20.0) -> List[ListMeta]:
    """Fetch every enabled source, write ``<slug>.domains``, and persist meta.

    Returns per-source metadata (including errors) for the UI. A failed fetch
    keeps any previously-cached file so the resolver still has data.
    """
    lists_dir.mkdir(parents=True, exist_ok=True)
    metas: List[ListMeta] = []
    for src in sources:
        url = str(src.get("url") or "").strip()
        name = str(src.get("name") or url or "list")
        enabled = bool(src.get("enabled", True))
        slug = slugify(name if name else url)
        meta = ListMeta(name=name, url=url, enabled=enabled, slug=slug)
        target = lists_dir / f"{slug}.domains"
        if not enabled:
            # Keep the cached file (for a quick re-enable) but report it disabled.
            if target.exists():
                meta.count = _count_lines(target)
                meta.bytes = target.stat().st_size
            metas.append(meta)
            continue
        try:
            body = fetch_list(url, timeout=timeout)
            domains = sorted(set(parse_lines(body.splitlines())))
            tmp = target.with_suffix(".domains.tmp")
            tmp.write_text("\n".join(domains) + ("\n" if domains else ""), encoding="utf-8")
            tmp.replace(target)       # atomic swap so the compiler never sees a half file
            meta.count = len(domains)
            meta.bytes = len(body)
            meta.fetched_at = time.time()
            logger.info("refreshed %s: %d domains", name, len(domains))
        except Exception as e:
            meta.error = str(e)
            if target.exists():
                meta.count = _count_lines(target)
                meta.bytes = target.stat().st_size
            logger.warning("failed to refresh %s (%s): %s", name, url, e)
        metas.append(meta)
    _write_meta(lists_dir, metas)
    return metas


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _write_meta(lists_dir: Path, metas: List[ListMeta]) -> None:
    try:
        (lists_dir / _META_NAME).write_text(
            json.dumps([m.to_dict() for m in metas], indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("could not write list meta: %s", e)


def read_meta(lists_dir: Path) -> List[Dict]:
    try:
        return json.loads((lists_dir / _META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


# Matcher

def _read_domain_file(path: Path) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                dom = _clean_domain(line.split("#", 1)[0])
                if dom:
                    out.add(dom)
    except OSError:
        pass
    return out


@dataclass
class Matcher:
    """Immutable-ish snapshot of the compiled block/allow/deny sets.

    Rebuilt on refresh and swapped in atomically by the server, so in-flight
    lookups always see a consistent set.
    """
    blocked: Set[str] = field(default_factory=set)   # from fetched lists
    denied: Set[str] = field(default_factory=set)    # user custom denylist
    allowed: Set[str] = field(default_factory=set)   # user allowlist (wins)

    @property
    def block_count(self) -> int:
        return len(self.blocked | self.denied)

    def is_blocked(self, qname: str) -> Tuple[bool, Optional[str]]:
        """Return (blocked, reason). reason ∈ {"denylist","blocklist"} | None.

        Walks parent suffixes of ``qname``. An allowlist hit at any level short-
        circuits to not-blocked; otherwise a denylist/blocklist hit blocks.
        """
        name = qname.strip(".").lower()
        if not name:
            return False, None
        labels = name.split(".")
        # Check from the most specific suffix upward: a.b.c → a.b.c, b.c, c.
        for i in range(len(labels)):
            suffix = ".".join(labels[i:])
            if suffix in self.allowed:
                return False, None
            if suffix in self.denied:
                return True, "denylist"
            if suffix in self.blocked:
                return True, "blocklist"
        return False, None


def compile_matcher(lists_dir: Path, allowlist_file: Path,
                    denylist_file: Path, enabled_slugs: Optional[Set[str]] = None) -> Matcher:
    """Union the enabled per-list ``.domains`` files and layer allow/deny on top.

    ``enabled_slugs`` limits which cached files count; None → every enabled
    source recorded in meta (falling back to all ``.domains`` files if no meta).
    """
    blocked: Set[str] = set()
    if enabled_slugs is None:
        meta = read_meta(lists_dir)
        if meta:
            enabled_slugs = {m["slug"] for m in meta if m.get("enabled", True)}
    for path in sorted(lists_dir.glob("*.domains")):
        if enabled_slugs is not None and path.stem not in enabled_slugs:
            continue
        blocked |= _read_domain_file(path)
    denied = _read_domain_file(denylist_file)
    allowed = _read_domain_file(allowlist_file)
    logger.info("compiled matcher: %d blocked, %d denied, %d allowed",
                len(blocked), len(denied), len(allowed))
    return Matcher(blocked=blocked, denied=denied, allowed=allowed)
