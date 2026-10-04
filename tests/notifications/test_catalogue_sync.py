"""The editor's trigger list and the hub's must name the same triggers the same way."""

from __future__ import annotations

import re

from harness import REPO, Checker
from modules.notification_rules import TRIGGERS


def run() -> Checker:
    c = Checker("catalogue_sync")
    js = (REPO / "static/js/notifications.js").read_text()
    block = js[js.index("const TRIGGERS = {"):js.index("};", js.index("const TRIGGERS = {"))]
    editor = dict(re.findall(r"^\s+(\w+):\s*\{\s*label:\s*'([^']*)'", block, re.M))
    needs = set(re.findall(r"^\s+(\w+):.*needsThreshold:\s*true", block, re.M))

    c.section("editor vs hub")
    c.check("the editor offers every trigger the hub evaluates, and no others",
            set(editor) == set(TRIGGERS), sorted(set(editor) ^ set(TRIGGERS)))
    c.check("labels match, so a rule reads the same in the list and the notification",
            all(editor.get(k) == t.label for k, t in TRIGGERS.items()),
            {k: (editor.get(k), t.label) for k, t in TRIGGERS.items() if editor.get(k) != t.label})
    c.check("the editor asks for a threshold exactly where the hub needs one",
            needs == {k for k, t in TRIGGERS.items() if t.needs_threshold}, needs)
    return c
