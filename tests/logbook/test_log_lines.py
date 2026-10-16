"""
What a device update puts in the live log (modules/logbook.loggable_changes):
only what changed since the last line, without aliases. No database.
"""

from __future__ import annotations

from harness import Checker

# A Kitchen light's report as core sees it: the full light state on every change.
LIGHT = {"state": "ON", "on": True, "brightness": 28, "level": 70, "color_temp": 454,
         "state_1": "ON", "on_1": True, "last_seen": 1}


def run() -> Checker:
    from modules.logbook import loggable_changes
    c = Checker("log_lines")

    c.section("a light's report")
    last: dict = {}
    first = loggable_changes(last, LIGHT)
    c.check("the first is logged without its aliases (on, level, on_1)",
            first == {"state": "ON", "brightness": 28, "color_temp": 454, "state_1": "ON"}, first)
    c.check("the same report again logs nothing", loggable_changes(last, dict(LIGHT, last_seen=2)) == {})
    dim = loggable_changes(last, dict(LIGHT, brightness=40, level=102))
    c.check("a dim logs only the brightness", dim == {"brightness": 40}, dim)
    c.check("and the level it was given is now the one remembered", last["level"] == 102, last)

    c.section("what stays")
    c.check("an alias with no partner in the line is kept",
            loggable_changes({}, {"level": 70}) == {"level": 70})
    last = {}
    loggable_changes(last, {"action": "single", "temperature": 21.36})
    again = loggable_changes(last, {"action": "single", "temperature": 21.36})
    c.check("a repeated press is logged, a repeated temperature is not",
            again == {"action": "single"}, again)
    c.check("link quality never makes a line", loggable_changes({}, {"lqi": 120, "rssi": -60}) == {})
    return c


if __name__ == "__main__":
    run()
