"""Rule storage: validation, ownership, persistence and the browser import."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness import Checker
from modules.notification_rules import NotificationRuleStore, normalise_rule


def _rejects(data) -> bool:
    try:
        normalise_rule(data)
    except ValueError:
        return True
    return False


def run() -> Checker:
    c = Checker("rule_store")

    c.section("validation")
    c.check("an unknown trigger is rejected", _rejects({"trigger": "teleport"}))
    c.check("'selected devices' with none selected is rejected",
            _rejects({"trigger": "smoke", "scope": "devices", "devices": []}))
    c.check("a tab scope without a tab is rejected", _rejects({"trigger": "smoke", "scope": "tab"}))
    c.check("a threshold trigger without a number is rejected",
            _rejects({"trigger": "temp_above", "threshold": "warm"}))
    c.check("half a time window is rejected", _rejects({"trigger": "smoke", "timeFrom": "22:00"}))
    c.check("an out-of-range time is rejected",
            _rejects({"trigger": "smoke", "timeFrom": "25:00", "timeTo": "06:00"}))
    c.check("a cooldown the editor doesn't offer is rejected",
            _rejects({"trigger": "smoke", "cooldownMinutes": 7}))
    r = normalise_rule({"trigger": "temp_above", "threshold": "21.5", "scope": "devices",
                        "devices": ["aa"], "title": "  Hot  ", "timeFrom": "7:5", "timeTo": "09:00"})
    c.check("a threshold sent as text from the form is stored as a number", r["threshold"] == 21.5, r)
    c.check("titles are trimmed and times normalised to HH:MM",
            r["title"] == "Hot" and r["timeFrom"] == "07:05", r)
    c.check("devices are dropped when the scope isn't 'selected'",
            normalise_rule({"trigger": "smoke", "scope": "all", "devices": ["aa"]})["devices"] == [])

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        c.section("ownership")
        store = NotificationRuleStore(path)
        a = store.create("alice", {"trigger": "smoke"})
        store.create("bob", {"trigger": "water_leak"})
        c.check("each user lists only their own rules",
                [r["trigger"] for r in store.for_owner("alice")] == ["smoke"])
        c.check("another user can't edit a rule", store.update("bob", a["id"], {"trigger": "vibration"}) is None)
        c.check("another user can't delete a rule", store.delete("bob", a["id"]) is False)
        c.check("the owner can", store.delete("alice", a["id"]) is True and not store.for_owner("alice"))

        c.section("persistence")
        store.create("alice", {"trigger": "smoke", "title": "Fire"})
        reloaded = NotificationRuleStore(path)
        reloaded.load()
        c.check("rules survive a restart, owners intact",
                sorted((r["owner"], r["trigger"]) for r in reloaded.rules.values())
                == [("alice", "smoke"), ("bob", "water_leak")])
        c.check("no temp file is left behind", not path.with_suffix(".tmp").exists())
        path.write_text("{not json")
        broken = NotificationRuleStore(path)
        broken.load()
        c.check("an unreadable file loads as empty instead of crashing startup", broken.rules == {})

    with tempfile.TemporaryDirectory() as tmp:
        c.section("importing a browser's local rules")
        store = NotificationRuleStore(Path(tmp) / "rules.json")
        legacy = [
            # Shapes the old editor wrote to localStorage.
            {"id": "rule-abc", "enabled": True, "trigger": "contact_opened", "scope": "all",
             "devices": [], "tab": None, "timeFrom": None, "timeTo": None,
             "cooldownMinutes": 5, "title": None, "message": None},
            {"id": "rule-def", "enabled": False, "trigger": "temp_below", "scope": "devices",
             "devices": ["cc"], "threshold": "3", "cooldownMinutes": 15},
            {"id": "rule-bad", "trigger": "no_such_trigger"},
        ]
        imported, errors = store.import_rules("alice", legacy)
        rules = store.for_owner("alice")
        c.check("valid rules are imported for the uploading user", imported == 2 and len(rules) == 2, rules)
        c.check("a disabled rule stays disabled",
                any(r["trigger"] == "temp_below" and r["enabled"] is False for r in rules))
        c.check("an invalid rule is reported, not silently dropped", len(errors) == 1, errors)
        c.check("imported rules get hub ids, so a re-import can't clobber another user's rule",
                all(r["id"] not in ("rule-abc", "rule-def") for r in rules))
        saved = json.loads((Path(tmp) / "rules.json").read_text())
        c.check("the import is on disk", len(saved["rules"]) == 2)
        again, _ = store.import_rules("alice", legacy)
        c.check("a second upload of the same list (two tabs at first load) adds nothing",
                again == 0 and len(store.for_owner("alice")) == 2)
        bobs, _ = store.import_rules("bob", legacy[:1])
        c.check("…but the same rule from another user is theirs to have", bobs == 1)

    return c
