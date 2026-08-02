"""
Live-edit detection — surfaces in-container code changes that an image-based
upgrade would silently discard.

Editor and test-batch writes land in /app, are not in git, and are not carried
into a new image. Best-effort and never raises: the release manifest first, then
git, then an .editor_backups fallback. See docs/upgrades.md.
"""
import datetime
import hashlib
import io
import logging
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

logger = logging.getLogger("modules.live_edits")

# Short cache so the 5s status poll doesn't run `git status` every tick. The
# explicit /live-edits endpoint (and the pre-swap confirm gate) bypass it for
# an authoritative, up-to-the-moment result.
_CACHE_TTL = 15.0
_cache = {"at": 0.0, "result": None}

PROJECT_ROOT = Path("/app")
# Written by build.sh's Containerfile: "<sha256>  <relpath>" per shipped file.
MANIFEST_PATH = PROJECT_ROOT / ".release_manifest"
# Under data/ (host bind mount) since the move to manager-side recovery —
# keep in sync with modules/test_recovery.py and boot_guard.py. The old
# ".editor_backups/" ignore prefix stays for pre-migration checkouts.
BACKUP_DIR = PROJECT_ROOT / "data" / ".editor_backups"

# Runtime state / volume-mounted dirs, never "code edits an upgrade discards" —
# belt-and-suspenders on top of .gitignore. config/ is bind-mounted from the
# data volume (build.sh: --volume DATA_DIR/config:/app/config), so its edits
# PERSIST across an image swap — flagging them as "would be discarded" is wrong.
_IGNORE_PREFIXES = ("data/", "logs/", "config/", ".editor_backups/",
                    "__pycache__/", ".git/")
_IGNORE_SUFFIXES = (".pyc", ".pyo", ".log")
# Exact paths that aren't user edits to preserve: VERSION is replaced by the new
# image by design; Containerfile/.dockerignore are build artifacts;
# .release_manifest is the yardstick itself.
_IGNORE_EXACT = {"VERSION", "Containerfile", ".dockerignore", ".release_manifest"}

# backup name = "<safe>.<YYYYMMDD_HHMMSS>...bak"  →  capture <safe> for grouping.
_BACKUP_TS_RE = re.compile(r"^(.*?)\.\d{8}_\d{6}")

# Tolerance when comparing a file's mtime against the image build reference.
# COPY replays context mtimes, so pristine files land a hair before VERSION;
# a real edit is minutes-to-days newer, so a few seconds of slack is ample.
_MTIME_SLACK = 5.0


def _ignored(path: str) -> bool:
    # Strip only a leading "./" — NOT lstrip("./"), which also eats the leading
    # dot of dotpaths like ".editor_backups/" and defeats the prefix match.
    p = path[2:] if path.startswith("./") else path
    return (
        p in _IGNORE_EXACT
        or any(p.startswith(pre) for pre in _IGNORE_PREFIXES)
        or any(p.endswith(suf) for suf in _IGNORE_SUFFIXES)
    )


def _walk_tree():
    """Yield non-ignored file paths under /app, relative to it."""
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        rel_dir = os.path.relpath(dirpath, PROJECT_ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir
        # Prune ignored dirs in place so os.walk never descends into data/ etc.
        dirnames[:] = [
            d for d in dirnames
            if not _ignored(f"{rel_dir}/{d}/" if rel_dir else f"{d}/")
        ]
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not _ignored(rel):
                yield rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest():
    """Parse .release_manifest → {relpath: sha256}, or None if unusable."""
    try:
        text = MANIFEST_PATH.read_text()
    except Exception:
        return None  # pre-manifest image, or dev checkout

    out = {}
    for line in text.splitlines():
        # sha256sum format: "<hash>  <path>". A leading '\' on the hash means
        # sha256sum escaped a path containing a newline/backslash.
        digest, sep, path = line.partition("  ")
        if not sep:
            continue
        digest = digest.strip().lstrip("\\")
        path = path.strip()
        if digest and path and not _ignored(path):
            out[path] = digest
    return out or None


def _manifest_changes():
    """Return list[{path,status,source}] vs the shipped manifest, or None."""
    manifest = _read_manifest()
    if manifest is None:
        return None

    changes = []
    seen = set()
    for rel in _walk_tree():
        seen.add(rel)
        expected = manifest.get(rel)
        if expected is None:
            # Not in the release at all — a file created in-app.
            changes.append({"path": rel, "status": "A", "source": "manifest"})
            continue
        try:
            if _sha256(PROJECT_ROOT / rel) != expected:
                changes.append({"path": rel, "status": "M", "source": "manifest"})
        except Exception as e:
            logger.warning("hash failed for %s: %s", rel, e)

    for rel in manifest:
        if rel not in seen:
            changes.append({"path": rel, "status": "D", "source": "manifest"})

    changes.sort(key=lambda c: c["path"])
    return changes


def _git_changes():
    """Return list[{path,status,source}] from git, or None if git can't be used."""
    if not (PROJECT_ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None  # git binary not in image
    except Exception as e:
        logger.warning("git status failed: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning("git status returned %s: %s", proc.returncode, proc.stderr.strip())
        return None

    changes = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "?"
        path = line[3:].strip()
        if " -> " in path:                 # renamed: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if _ignored(path):
            continue
        changes.append({"path": path, "status": status, "source": "git"})
    return changes


def _image_build_ref():
    """Best guess at when the running image was built, as an mtime.

    COPY preserves the build context's mtimes, and that context is a fresh
    `git clone` (scripts/upgrade.sh), so every pristine file carries ~clone time.
    VERSION is stamped just after the clone, making it the newest image-origin
    file — anything newer than it was written at runtime, i.e. a live edit.
    """
    for name in ("VERSION", "main.py", "launcher.py"):
        try:
            p = PROJECT_ROOT / name
            if p.is_file():
                return p.stat().st_mtime
        except Exception:
            continue
    return None


def _unmangle_index():
    """Map mangled backup base names back to real paths.

    Backups are named with '/' → '_' (routes/editor_routes.py), which looks
    lossy in the abstract but is resolvable against the actual tree: mangle
    every real path and look the backup name up. Collisions (two real paths
    mangling to the same name) are dropped rather than guessed at.
    """
    index, ambiguous = {}, set()
    for rel in _walk_tree():
        safe = rel.replace("/", "_").replace("\\", "_")
        if index.get(safe, rel) != rel:
            ambiguous.add(safe)
        index[safe] = rel
    for safe in ambiguous:
        index.pop(safe, None)
    return index


def _backup_changes():
    """Last-resort fallback for pre-manifest images.

    A backup only proves an edit happened at some point — and since backups sit
    in the data/ bind mount and are never pruned, they survive the very upgrade
    that discarded the edit. So resolve each to a real path and keep it only if
    that file is STILL divergent (written after the image was built). Without
    this gate the count freezes and the banner can never clear.
    """
    if not BACKUP_DIR.is_dir():
        return []

    bases = set()
    try:
        for f in BACKUP_DIR.iterdir():
            if not f.is_file() or not f.name.endswith(".bak"):
                continue
            m = _BACKUP_TS_RE.match(f.name)
            bases.add(m.group(1) if m else f.name)
    except Exception as e:
        logger.warning("backup scan failed: %s", e)
        return []
    if not bases:
        return []

    index = _unmangle_index()
    build_ref = _image_build_ref()
    changes = []
    for base in sorted(bases):
        rel = index.get(base)
        if rel is None:
            # File is gone, or the name is ambiguous. Can't confirm it's live,
            # and reporting it would be the phantom-count bug all over again.
            logger.debug("backup %r does not resolve to a live file — skipping", base)
            continue
        if build_ref is not None:
            try:
                if (PROJECT_ROOT / rel).stat().st_mtime <= build_ref + _MTIME_SLACK:
                    continue  # came from the image; the edit is already gone
            except Exception:
                continue
        changes.append({"path": rel, "status": "M", "source": "backups"})
    return changes


def detect_live_edits(use_cache: bool = False) -> dict:
    """
    Enumerate live, uncommitted edits an upgrade would discard.

    Args:
      use_cache: serve a recent (<15s) result if available. The status poll
                 sets this; the explicit endpoint / confirm gate leaves it
                 False for an authoritative result.

    Returns:
      {
        "supported": bool,        # could we detect at all?
        "method": "manifest"|"git"|"backups"|"none",
        "count": int,
        "files": [{"path","status","source"}, ...],   # capped for UI
        "exact": bool,            # False only for the backups fallback
      }
    """
    if use_cache and _cache["result"] is not None and (time.time() - _cache["at"]) < _CACHE_TTL:
        return _cache["result"]

    result = _detect_live_edits_uncached()
    _cache["at"] = time.time()
    _cache["result"] = result
    return result


def _detect_live_edits_uncached() -> dict:
    for method, fn in (("manifest", _manifest_changes), ("git", _git_changes)):
        try:
            files = fn()
        except Exception as e:
            logger.warning("%s detection failed: %s", method, e)
            continue
        if files is not None:
            return {
                "supported": True,
                "method": method,
                "exact": True,
                "count": len(files),
                "files": files[:200],
            }

    try:
        fallback = _backup_changes()
    except Exception as e:
        logger.warning("backup detection failed: %s", e)
        fallback = []
    return {
        "supported": True,
        "method": "backups" if fallback else "none",
        "exact": False,
        "count": len(fallback),
        "files": fallback[:200],
    }


def _read_version():
    try:
        return (PROJECT_ROOT / "VERSION").read_text().strip()
    except Exception:
        return None


def build_export_archive():
    """Build a zip of the CURRENT content of live-edited files so the user can
    keep them before an upgrade discards them.

    exact methods (manifest/git) → real paths preserved (unzip into a checkout).
    fallback                     → bundles the raw .editor_backups/ (pre-edit
                  originals), since without an exact method we can't be sure the
                  resolved paths are the full picture.

    Returns (bytes, filename) or (None, None) when there's nothing to export.
    """
    info = detect_live_edits(use_cache=False)
    if not info.get("count"):
        return None, None

    version = _read_version()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"zmm-live-edits-{version}-{ts}.zip" if version else f"zmm-live-edits-{ts}.zip"

    manifest = [
        f"ZMM live-edit export — {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"App version:      {version or 'unknown'}",
        f"Detection method: {info['method']}",
        f"Files:            {info['count']}",
        "",
    ]

    root = PROJECT_ROOT.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if info.get("exact"):
            for entry in info["files"]:
                path = entry.get("path", "")
                status = entry.get("status", "")
                full = PROJECT_ROOT / path
                try:
                    resolved = full.resolve()
                except Exception:
                    manifest.append(f"[skip:badpath] {path}")
                    continue
                if not str(resolved).startswith(str(root)):
                    manifest.append(f"[skip:outside] {path}")
                    continue
                # Deletions / missing files have no content to export.
                if status == "D" or not full.is_file():
                    manifest.append(f"[skip:{status or 'missing'}] {path}")
                    continue
                try:
                    z.write(full, arcname=path)
                    manifest.append(f"[{status or 'M'}] {path}")
                except Exception as e:
                    manifest.append(f"[error:{e}] {path}")
        else:
            manifest.append("no exact detection method — bundling .editor_backups/ (pre-edit originals).")
            if BACKUP_DIR.is_dir():
                for f in sorted(BACKUP_DIR.iterdir()):
                    if f.is_file():
                        try:
                            z.write(f, arcname=f".editor_backups/{f.name}")
                        except Exception:
                            pass

        z.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")

    return buf.getvalue(), fname
