"""
Release ordering tests.

    python3 tests/upgrade/test_ordering.py

A monthly tag has no day, so by tag alone "09.2026" sorts before
"22.03.09.2026" even when it is published after it. These check that a
published monthly is offered over the dailies and patches it follows, on every
channel that can see it, and that the checked result survives the later
comparisons (the status route, the build request).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker  # noqa: E402

from modules import upgrade_manager as um  # noqa: E402


def rel(tag, published, prerelease=False):
    return {"tag_name": tag, "published_at": published, "prerelease": prerelease,
            "draft": False, "body": "", "html_url": f"https://example/{tag}"}


# Newest first, as GitHub lists them — though the code must not rely on that.
RELEASES = [
    rel("v09.2026", "2026-09-23T09:00:00Z"),
    rel("v22.03.09.2026", "2026-09-22T18:00:00Z"),
    rel("v22.02.09.2026", "2026-09-22T15:00:00Z"),
    rel("v22.09.2026", "2026-09-22T10:00:00Z"),
    rel("v20.09.2026", "2026-09-20T10:00:00Z"),
    rel("v08.2026", "2026-08-31T10:00:00Z"),
]


def isolate():
    """Point version state at a fresh temp dir with no baked VERSION file."""
    d = Path(tempfile.mkdtemp())
    um.STATE_DIR = str(d / "state")
    um.UPGRADE_DIR = str(d / "upgrade")
    um.VERSION_STATE_FILE = str(d / "state" / "version.json")
    um.APP_VERSION_FILE = str(d / "VERSION")


def check_as(current, channel, releases=RELEASES):
    isolate()
    um.save_state({**um.load_state(), "current_version": current, "channel": channel})

    async def fake_fetch(repo):
        return releases

    um.fetch_releases = fake_fetch
    return asyncio.run(um.check_for_updates(force=True))


def run() -> Checker:
    c = Checker("test_ordering")

    c.section("tag order alone gets the monthly wrong — the reason for the fix")
    c.check("09.2026 sorts before 22.03.09.2026 by tag",
            um.compare_versions("09.2026", "22.03.09.2026") < 0)

    c.section("the latest release is the last one published")
    for channel in ("patch", "minor", "major", "prerelease"):
        got = um.pick_latest_release(RELEASES, channel)
        c.check(f"{channel}: picks 09.2026", got and got["version"] == "09.2026", got)
    shuffled = list(reversed(RELEASES))
    c.check("list order doesn't matter",
            um.pick_latest_release(shuffled, "patch")["version"] == "09.2026")

    c.section("a host on today's last patch is offered the monthly")
    for channel in ("patch", "minor", "major"):
        r = check_as("22.03.09.2026", channel)
        c.check(f"{channel}: update to 09.2026",
                r["update_available"] and r["latest_version"] == "09.2026", r)
    r = check_as("22.03.09.2026", "patch")
    state = um.load_state()
    c.check("the status route's test agrees", um.update_pending(state))

    c.section("once on the monthly, nothing older is offered")
    r = check_as("09.2026", "patch")
    c.check("no update", not r["update_available"], r)

    c.section("the next daily after the monthly is offered")
    later = [rel("v24.09.2026", "2026-09-24T10:00:00Z")] + RELEASES
    for channel in ("patch", "minor"):
        r = check_as("09.2026", channel, later)
        c.check(f"{channel}: update to 24.09.2026",
                r["update_available"] and r["latest_version"] == "24.09.2026", r)
    r = check_as("09.2026", "major", later)
    c.check("major: a daily is not offered", not r["update_available"], r)

    c.section("channels still gate on the tag shape")
    no_monthly = RELEASES[1:]
    r = check_as("20.09.2026", "major", no_monthly)
    c.check("major skips dailies and patches", not r["update_available"], r)
    r = check_as("20.09.2026", "minor", no_monthly)
    c.check("minor takes the daily, not the patches",
            r["latest_version"] == "22.09.2026", r)

    c.section("when the installed release is gone from GitHub")
    check_as("22.03.09.2026", "patch", [rel("v22.03.09.2026", "2026-09-22T18:00:00Z")])
    # Pruned: only the monthly remains, but the publish time was remembered.
    r = check_as_keep("22.03.09.2026", "patch", [rel("v09.2026", "2026-09-23T09:00:00Z")])
    c.check("the remembered publish time still orders it",
            r["update_available"] and r["latest_version"] == "09.2026", r)
    isolate()
    c.check("never seen: tag order is the fallback",
            um.is_newer("23.09.2026", "22.03.09.2026"))
    c.check("same version is never newer",
            not um.is_newer("v09.2026", "09.2026", "2026-09-24", "2026-09-23"))

    c.section("legacy semver installs still cross over")
    r = check_as("3.5.0", "major", RELEASES + [rel("v3.5.0", "2026-07-01T00:00:00Z")])
    c.check("3.5.0 -> 09.2026", r["latest_version"] == "09.2026", r)
    r = check_as("3.5.0", "major", RELEASES)
    c.check("even when 3.5.0's release is gone", r["latest_version"] == "09.2026", r)

    return c


def check_as_keep(current, channel, releases):
    """check_as without resetting state, so remembered values carry over."""
    async def fake_fetch(repo):
        return releases

    um.fetch_releases = fake_fetch
    return asyncio.run(um.check_for_updates(force=True))


if __name__ == "__main__":
    checker = run()
    print(f"\n{checker.passed} passed, {len(checker.failures)} failed")
    sys.exit(1 if checker.failures else 0)
