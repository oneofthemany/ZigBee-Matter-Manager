"""Silverblue / rpm-ostree: what the check reports and what apply, upgrade and reboot do."""

from __future__ import annotations

from harness import Checker, Host, bodhi, cached_update, dbdiff, deployment

SEC, BUGFIX = 1, 2   # libdnf advisory kinds
BOOTED = deployment("44.20260930.0", "aaa", booted=True)


def run() -> Checker:
    c = Checker("rpm_ostree")

    c.section("check: an update that can be applied live")
    h = Host()
    h.set(status={"deployments": [BOOTED],
                  "cached-update": cached_update("44.20261004.0", ["firefox", "ImageMagick"],
                                                 [["FEDORA-1", SEC, 3, [], {}], ["FEDORA-2", SEC, 2, [], {}],
                                                  ["FEDORA-3", BUGFIX, 0, [], {}]])})
    d = h.collect()
    c.check("one pending update, from the booted version to the new one",
            d.get("update_count") == 1 and (d.get("packages") or [{}])[0].get("current") == "44.20260930.0"
            and (d.get("packages") or [{}])[0].get("candidate") == "44.20261004.0", d.get("packages"))
    c.check("security count is the security advisories, not lines of output", d.get("security_count") == 2, d.get("security_count"))
    c.check("no kernel or glibc in it, so it can be applied live", d.get("live_applicable") is True and d.get("reboot_packages") == [], d)
    c.check("nothing is waiting for a reboot", d.get("reboot_required") is False and d.get("staged_version") is None, d)
    h.close()

    c.section("check: an update that needs a reboot")
    h = Host()
    h.set(status={"deployments": [BOOTED],
                  "cached-update": cached_update("44.20261004.0", ["firefox", "kernel-core", "glibc", "glibc-devel"])})
    d = h.collect()
    c.check("it can't be applied live", d.get("live_applicable") is False, d)
    c.check("and says which packages need the reboot (not glibc-devel)",
            d.get("reboot_packages") == ["glibc", "kernel-core"], d.get("reboot_packages"))
    h.close()

    c.section("check: an update already staged")
    h = Host()
    h.set(status={"deployments": [deployment("44.20261004.0", "bbb", booted=False, staged=True), BOOTED],
                  "cached-update": cached_update("44.20261004.0", ["kernel-core"])})
    d = h.collect()
    c.check("the staged update isn't offered again (the old apply-twice loop)", d.get("update_count") == 0, d)
    c.check("it's reported as waiting for a reboot, with its version",
            d.get("reboot_required") is True and d.get("staged_version") == "44.20261004.0", d)
    h.close()

    c.section("check: a staged update already applied live")
    h = Host()
    live_booted = deployment("44.20260930.0", "aaa", booted=True, **{"live-replaced": "bbb"})
    h.set(status={"deployments": [deployment("44.20261004.0", "bbb", booted=False, staged=True), live_booted]})
    d = h.collect()
    c.check("it no longer asks for a reboot", d.get("reboot_required") is False, d)
    h.close()

    c.section("check: which Fedora release is really out")
    h = Host()   # Bodhi: 43, 44 current; 45 (branched) and 46 (rawhide) pending
    h.set(status={"deployments": [BOOTED]})
    d = h.collect()
    c.check("branched and rawhide don't count as released (no phantom Fedora 46)", d.get("os_release_available") is None, d)
    h.set(bodhi=bodhi([44, 45], [46]))
    d = h.collect()
    c.check("a released 45 is offered, automated because the branch can be worked out",
            d.get("os_release_available") == "45" and d.get("os_release_automated") is True, d)
    h.set(bodhi=bodhi([44, 45, 46, 47], []))
    d = h.collect()
    c.check("never more than two releases ahead", d.get("os_release_available") == "46", d.get("os_release_available"))
    h.set(bodhi=bodhi([44, 45], []), status={"deployments": [deployment("44.1", "aaa", True, origin="myremote:custom/stream")]})
    d = h.collect()
    c.check("a branch it can't rewrite is shown but left manual",
            d.get("os_release_available") == "45" and d.get("os_release_automated") is False, d)
    h.close()

    c.section("apply: live when safe")
    h = Host()
    h.set(status={"deployments": [BOOTED]},
          status_after_upgrade={"deployments": [deployment("44.20261004.0", "bbb", False, True), BOOTED]},
          dbdiff=dbdiff(["firefox", "ImageMagick"]))
    h.trigger("apply")
    st = h.apply()
    calls = h.calls()
    c.check("stages the update, then applies it live with --allow-replacement",
            "rpm-ostree upgrade" in calls and "rpm-ostree apply-live --allow-replacement" in calls, calls)
    c.check("compares the booted and staged commits", "rpm-ostree db diff --format=json aaa bbb" in calls, calls)
    c.check("reports it applied without a reboot", st.get("state", "") == "done" and "no reboot needed" in (st.get("detail") or ""), st)
    c.check("doesn't reboot", not any(x.startswith("systemctl") for x in calls), calls)
    h.close()

    c.section("apply: staged when it needs a reboot")
    h = Host()
    h.set(status={"deployments": [BOOTED]},
          status_after_upgrade={"deployments": [deployment("44.20261004.0", "bbb", False, True), BOOTED]},
          dbdiff=dbdiff(["firefox", "kernel-core", "glibc"]))
    h.trigger("apply")
    st = h.apply()
    calls = h.calls()
    c.check("doesn't try to apply live", not any("apply-live --allow" in x for x in calls), calls)
    c.check("says it's staged, reboot to apply, and why", "reboot to apply" in st.get("detail", "")
            and "glibc, kernel-core" in st.get("detail", ""), st)
    c.check("doesn't reboot on its own", not any(x.startswith("systemctl") for x in calls), calls)
    h.close()

    c.section("apply: live apply fails")
    h = Host()
    h.set(status={"deployments": [BOOTED]},
          status_after_upgrade={"deployments": [deployment("44.20261004.0", "bbb", False, True), BOOTED]},
          dbdiff=dbdiff(["firefox"]), live_rc="1")
    h.trigger("apply")
    st = h.apply()
    c.check("falls back to staged, reboot to apply", "live apply failed" in st.get("detail", ""), st)
    h.close()

    c.section("release upgrade")
    h = Host()
    h.set(status={"deployments": [BOOTED]})
    h.trigger("release_upgrade", "45")
    st = h.apply()
    calls = h.calls()
    c.check("rebases the booted branch to the new release", "rpm-ostree rebase fedora:fedora/45/x86_64/silverblue" in calls, calls)
    c.check("then reboots", "systemctl reboot" in calls and st.get("state", "") == "rebooting", (calls, st))
    h.close()

    h = Host()
    h.set(status={"deployments": [dict(BOOTED, origin=None,
                                       **{"container-image-reference": "ostree-image-signed:docker://quay.io/fedora/fedora-silverblue:44"})]})
    h.trigger("release_upgrade", "45")
    h.apply()
    c.check("an image-based install moves its tag instead",
            "rpm-ostree rebase ostree-image-signed:docker://quay.io/fedora/fedora-silverblue:45" in h.calls(), h.calls())
    h.close()

    h = Host()
    h.set(status={"deployments": [BOOTED]})
    h.trigger("release_upgrade", "47")
    st = h.apply()
    c.check("refuses a jump of more than two releases, without rebasing or rebooting",
            st.get("state", "") == "failed" and not any("rebase" in x or x.startswith("systemctl") for x in h.calls()), (st, h.calls()))
    h.close()

    c.section("reboot")
    h = Host()
    h.set(status={"deployments": [BOOTED]})
    h.trigger("reboot")
    st = h.apply()
    c.check("reboots the host and says so", "systemctl reboot" in h.calls() and st.get("state", "") == "rebooting", (h.calls(), st))
    h.close()
    return c
