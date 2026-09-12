"""
Rules, the admin view, and the end of the compatibility shim.

An automation rule fires with no request behind it, so the Tidal account it
plays on cannot be looked up when it runs — it is stamped on the step when the
rule is saved. Step 5 of docs/plans/tidal-per-user-auth.md.

The stamping helper is imported directly; the routes that call it need FastAPI,
so their wiring is checked by AST as in the other route tests.
"""

from __future__ import annotations

import ast

from harness import Checker, REPO, TempSessions

from modules.media.sources import tidal as T
from modules.media.sources.tidal import TidalSource, UNASSIGNED


def _rule(*steps) -> dict:
    return {"then_sequence": list(steps), "else_sequence": []}


def _stamp():
    """Imported lazily: modules.automation_api needs FastAPI at module scope,
    so the helper is read out of the source instead of imported."""
    src = (REPO / "modules" / "automation_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_stamp_tidal_owner")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<stamp>", "exec"), ns)
    return ns["_stamp_tidal_owner"]


def _stamping(c: Checker) -> None:
    c.section("a rule remembers whose account it plays on")
    stamp = _stamp()

    data = _rule({"media_action": "play_tidal", "tidal_kind": "album",
                  "tidal_id": "7"})
    stamp(data, "alice", overwrite=True)
    c.check("creating stamps the author",
            data["then_sequence"][0]["tidal_owner"] == "alice", data)

    # Editing someone else's rule must not quietly move the account it plays on.
    stamp(data, "bob", overwrite=False)
    c.check("updating leaves an existing owner alone",
            data["then_sequence"][0]["tidal_owner"] == "alice", data)

    legacy = _rule({"media_action": "play_tidal", "tidal_kind": "album",
                    "tidal_id": "7"})
    stamp(legacy, "bob", overwrite=False)
    c.check("but fills one in where a rule has none",
            legacy["then_sequence"][0]["tidal_owner"] == "bob", legacy)

    # A client could put tidal_owner on any step; only Tidal steps keep one,
    # and a create overwrites whatever was sent.
    hostile = _rule({"media_action": "play_tidal", "tidal_id": "7",
                     "tidal_owner": "alice"},
                    {"media_action": "announce", "text": "hi",
                     "tidal_owner": "alice"})
    stamp(hostile, "bob", overwrite=True)
    c.check("a client cannot name someone else's account on create",
            hostile["then_sequence"][0]["tidal_owner"] == "bob", hostile)
    c.check("and a non-Tidal step carries no owner at all",
            "tidal_owner" not in hostile["then_sequence"][1], hostile)

    both = {"then_sequence": [{"media_action": "play_tidal", "tidal_id": "1"}],
            "else_sequence": [{"media_action": "play_tidal", "tidal_id": "2"}]}
    stamp(both, "alice", overwrite=True)
    c.check("the else branch is stamped too",
            both["else_sequence"][0]["tidal_owner"] == "alice", both)

    anon = _rule({"media_action": "play_tidal", "tidal_id": "7"})
    stamp(anon, "", overwrite=True)
    c.check("no caller means no owner invented",
            "tidal_owner" not in anon["then_sequence"][0], anon)

    odd = {"then_sequence": ["not a dict", None], "else_sequence": None}
    stamp(odd, "alice", overwrite=True)
    c.check("a malformed sequence does not raise", True)


def _execution(c: Checker) -> None:
    c.section("and plays on it")
    src = (REPO / "modules" / "automation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") in ("play_tidal", "start_zone")]

    play = [n for n in calls if getattr(n.func, "attr", "") == "play_tidal"]
    c.check("the rule engine calls play_tidal", len(play) == 1, len(play))
    c.check("passing the step's stored owner",
            any("tidal_owner" in ast.unparse(a) for a in play[0].args), 
            ast.unparse(play[0]))

    zone = [n for n in calls if getattr(n.func, "attr", "") == "start_zone"
            and "tidal" in ast.unparse(n)]
    c.check("the zone variant passes it too",
            zone and any("tidal_owner" in ast.unparse(k) for k in zone[0].keywords),
            [ast.unparse(n) for n in zone])


def _admin(c: Checker) -> None:
    c.section("the admin view")
    with TempSessions(T) as tmp:
        src = TidalSource(enabled=True)
        src._available = True
        src._ensure("alice")._session = object()
        rows = src.accounts()
        c.check("lists who is linked", rows == [{"username": "alice",
                                                 "linked": True}], rows)
        c.check("the sentinel is reachable off the class",
                TidalSource.UNASSIGNED == UNASSIGNED)

    tree = ast.parse((REPO / "routes" / "media_routes.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "tidal_accounts"), None)
    if c.check("the endpoint exists", fn is not None):
        src_txt = ast.unparse(fn)
        c.check("it is admin-only",
                'require_scope(' in src_txt and "'admin'" in src_txt, src_txt[:120])
        c.check("and returns no token or session",
                "access_token" not in src_txt and "_session" not in src_txt)


def _no_shim(c: Checker) -> None:
    c.section("the compatibility shim is gone")
    text = (REPO / "modules" / "media" / "sources" / "tidal.py").read_text(
        encoding="utf-8")
    tree = ast.parse(text)
    registry = next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "TidalSource")
    methods = {m.name for m in registry.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    c.check("TidalSource defines no __getattr__", "__getattr__" not in methods)
    c.check("callers that name no user say so explicitly",
            "default_account" in methods)

    service = (REPO / "modules" / "media" / "service.py").read_text(encoding="utf-8")
    c.check("the service reaches the default account by name",
            "registry.default_account()" in service)


def run() -> Checker:
    c = Checker("tidal-automation")
    for part in (_stamping, _execution, _admin, _no_shim):
        part(c)
    return c


if __name__ == "__main__":
    import sys
    sys.exit(1 if run().failures else 0)
