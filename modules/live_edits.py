"""
Live-edit detection — surfaces in-container code changes that an image-based
upgrade would silently discard.

The web editor and the test-batch ("time machine") write changes straight into
/app. Those changes are NOT in git and are NOT carried into a freshly-built
image, so a swap to a new image throws them away. This module enumerates the
divergence so the upgrade flow can warn the user (and offer to wait) before the
point of no return.

Detection is best-effort and never raises — the upgrade UI must keep working
even if detection fails. Two strategies, in priority order:

  1. git — the image is built from `git clone --depth 1 <tag>` with `.git`
     retained (build.sh does `COPY . .` and there is no .dockerignore), so
     `git status --porcelain` (which honours .gitignore, hiding data/ logs/
     __pycache__/) lists exactly the working-tree changes vs the shipped tag.
     Most accurate; gives real paths.

  2. .editor_backups fallback — if git or the .git dir is unavailable, the
     presence of editor/test-recovery backups tells us files were edited. We
     can't perfectly reverse the backup filename back to a path, so we report
     a best-effort count + name hints rather than guess wrongly.
"""
import datetime
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
BACKUP_DIR = PROJECT_ROOT / ".editor_backups"

# Runtime state, never "code edits" — belt-and-suspenders on top of .gitignore.
_IGNORE_PREFIXES = ("data/", "logs/", ".editor_backups/", "__pycache__/", ".git/")
_IGNORE_SUFFIXES = (".pyc", ".pyo", ".log")

# backup name = "<safe>.<YYYYMMDD_HHMMSS>...bak"  →  capture <safe> for grouping.
_BACKUP_TS_RE = re.compile(r"^(.*?)\.\d{8}_\d{6}")


def _ignored(path: str) -> bool:
    p = path.lstrip("./")
    return (
        any(p.startswith(pre) for pre in _IGNORE_PREFIXES)
        or any(p.endswith(suf) for suf in _IGNORE_SUFFIXES)
    )


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


def _backup_changes():
    """Fallback: infer edited files from backup filenames (count + name hints).

    Backup names mangle '/' → '_' irreversibly, so we don't fabricate paths;
    we report the distinct base names as hints so the warning is honest.
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
    return [{"path": b, "status": "edited", "source": "backups"} for b in sorted(bases)]


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
        "method": "git"|"backups"|"none",
        "count": int,
        "files": [{"path","status","source"}, ...],   # capped for UI
        "exact": bool,            # True for git (real paths), False for fallback
      }
    """
    if use_cache and _cache["result"] is not None and (time.time() - _cache["at"]) < _CACHE_TTL:
        return _cache["result"]

    result = _detect_live_edits_uncached()
    _cache["at"] = time.time()
    _cache["result"] = result
    return result


def _detect_live_edits_uncached() -> dict:
    files = _git_changes()
    if files is not None:
        return {
            "supported": True,
            "method": "git",
            "exact": True,
            "count": len(files),
            "files": files[:200],
        }

    fallback = _backup_changes()
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

    git method  → real paths preserved (unzip straight into a checkout).
    fallback    → bundles the raw .editor_backups/ (pre-edit originals), since
                  exact current paths can't be reconstructed without git.

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
        if info["method"] == "git":
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
            manifest.append("git unavailable — bundling .editor_backups/ (pre-edit originals).")
            if BACKUP_DIR.is_dir():
                for f in sorted(BACKUP_DIR.iterdir()):
                    if f.is_file():
                        try:
                            z.write(f, arcname=f".editor_backups/{f.name}")
                        except Exception:
                            pass

        z.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")

    return buf.getvalue(), fname
