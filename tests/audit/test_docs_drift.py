"""docs_drift: routes, citations, paths and section order across docs and code."""

from __future__ import annotations

from harness import Checker, tree

import docs_drift

SRC = {
    "routes/device_routes.py": """
        def register(app):
            @app.post("/api/device/command")
            async def send(): ...
            @app.get("/api/device/{ieee}/history")
            async def history(ieee): ...
        """,
    "modules/x_api.py": """
        from fastapi import APIRouter
        router = APIRouter(prefix="/api/x")
        @router.get("/status")
        async def status(): ...
        def helper(): ...
        # see design.md §2.1, design.md §3.2, design.md §3.4 and gone.md §1
        """,
    "static/js/app.js": """
        fetch('/api/device/command', {method: 'POST'});
        fetch(`/api/device/${ieee}/history`);
        fetch('/api/device/' + ieee + '/history');
        fetch('/api/x/status' + qs);
        fetch(`/api/device/${ieee}/command`, {method: 'POST'});
        """,
    "docs/design.md": """
        # Design
        ## 2. Model
        ### 2.1 Delay
        ## 3. Limits
        1. one
        2. two
        ## 4. Later
        ### 2.9 Misplaced
        Calls `helper()` and `vanished()` and `time.monotonic()`.
        Lives in `modules/x_api.py` and `modules/nope.py`.
        `GET /api/x/status` and `POST /api/x/gone`, and the `/api/device` namespace.
        """,
}


def run() -> Checker:
    c = Checker("docs_drift")
    with tree(SRC) as root:
        res = docs_drift.check(root, undocumented=False)
        details = [r["detail"] for r in res]
        kinds = {}
        for r in res:
            kinds.setdefault(r["kind"], []).append(r["detail"])

        c.section("routes")
        c.check("a frontend call to a path no route serves is reported",
                any("/api/device/${ieee}/command" in d for d in kinds.get("route", [])), kinds)
        c.check("template-literal and concatenated paths match their routes",
                sum("frontend calls" in d for d in kinds.get("route", [])) == 1, kinds.get("route"))
        c.check("an APIRouter prefix is applied to its routes",
                not any("/api/x/status" in d for d in details), details)
        c.check("a doc naming an endpoint that does not exist is reported",
                any("/api/x/gone" in d for d in details), details)
        c.check("a doc naming a namespace is not reported",
                not any(d.startswith("doc names /api/device ") for d in details), details)

        c.section("citations and sections")
        c.check("a cite to an existing heading passes",
                not any("§2.1" in d for d in details), details)
        c.check("a cite to a list item under a numbered heading passes",
                not any("§3.2" in d for d in details), details)
        c.check("a cite to a list item that does not exist is reported",
                any("design.md §3.4" in d for d in details), details)
        c.check("a cite to a missing doc is reported",
                any("gone.md" in d for d in details), details)
        c.check("a subsection placed outside its section is reported",
                any("section 2.9 sits under '4. Later', not section 2" in d
                    for d in kinds.get("doc", [])), kinds.get("doc"))

        c.section("paths and symbols")
        c.check("a doc naming a missing file is reported, an existing one is not",
                kinds.get("path") == ["doc names modules/nope.py, which does not exist"],
                kinds.get("path"))
        c.check("a doc naming an undefined function is reported; stdlib names are not",
                kinds.get("symbol") == ["doc names vanished(), defined nowhere in the repo"],
                kinds.get("symbol"))
    return c
