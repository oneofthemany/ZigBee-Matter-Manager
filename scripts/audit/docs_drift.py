#!/usr/bin/env python3
"""
Cross-check what the docs, the frontend and the code each say exists. Read-only.

  route     the frontend or a doc calls an /api path no backend route serves
  cite      a code comment cites ``name.md §N`` and the doc or section is gone
  path      a doc names a repo file (``modules/x.py``) that doesn't exist
  symbol    a doc names ``func()`` or ``Class.method`` defined nowhere
  doc       a numbered heading out of order (a section pasted in the wrong place)
  config    a *_DEFAULTS mirror in routes/config_routes.py disagrees with
            config/config.yaml (key missing on one side, or a different value)

``--undocumented`` also lists backend routes no doc or frontend file mentions.

Stdlib only, except the config check, which needs PyYAML and is skipped
without it.

    python3 scripts/audit/docs_drift.py [--json] [--only KIND] [--undocumented] [ROOT]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", ".git", "logs", "data", "venv", ".venv",
             "screenshots", "android", "tests"}
METHODS = {"get", "post", "put", "delete", "patch", "websocket"}
# A path segment that is a parameter on either side normalises to {}.
PARAM = re.compile(r"\{[^}]*\}|\$\{[^}]*\}|:[A-Za-z_]\w*|<[^>]+>")
# A literal followed by `+` is only the prefix of a path built at runtime.
JS_CALL = re.compile(r"""(?:fetch|api\w*|request|post|get|put|del\w*|WebSocket)\s*\(\s*(['"`])(/(?:api|ws)[^'"`]*)\1(\s*\+)?""")
DOC_ROUTE = re.compile(r"`(?:(GET|POST|PUT|DELETE|PATCH|WS)\s+)?(/api/[^`\s]*)`")
DOC_ROUTE_BARE = re.compile(r"^\s*[-*|]?\s*\**(GET|POST|PUT|DELETE|PATCH)\**\s+`?(/api/[^\s`|)]*)", re.M)
CITE = re.compile(r"([A-Za-z0-9_\-]+\.md)\s*§\s*(\d+(?:\.\d+)*)")
DOC_PATH = re.compile(r"`((?:modules|routes|handlers|core|device|static|scripts|config|zmm_\w+|"
                      r"beekeeper|vision|recorder|manager|docs)/[\w./\-]+\.\w+)(?::\d+)?`")
DOC_SYMBOL = re.compile(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\(\)`")


def files(root: Path, *suffixes):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.suffix in suffixes and p.is_file() and not any(s in SKIP_DIRS for s in rel.parts[:-1]):
            yield p, rel


def norm(path: str) -> str:
    path = path.split("?")[0].split("#")[0].rstrip("/") or "/"
    return PARAM.sub("{}", path)


def route_matches(path: str, routes: set[str]) -> bool:
    """A path ending in a dynamic tail matches any route under that prefix."""
    p = norm(path)
    if p in routes:
        return True
    segs = p.split("/")
    for r in routes:
        rs = r.split("/")
        if len(rs) == len(segs) and all(a == b or "{}" in (a, b) or a.endswith("{}")
                                        or b.endswith("{}") for a, b in zip(rs, segs)):
            return True
        # {path:path} parameters swallow the rest.
        if "{}" in rs and rs[-1] == "{}" and segs[:len(rs) - 1] == rs[:-1]:
            return True
    return False


class Routes(ast.NodeVisitor):
    def __init__(self):
        self.prefix: dict[str, str] = {}
        self.found: list[tuple[str, str, str, int]] = []
        self.path = ""

    def visit_Assign(self, node):
        if (isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "APIRouter"):
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.prefix[t.id] = kw.value.value
        self.generic_visit(node)

    def _deco(self, node):
        for d in node.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in METHODS and d.args):
                continue
            arg = d.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                path = arg.value
            elif isinstance(arg, ast.JoinedStr):
                path = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values)
            else:
                continue
            owner = d.func.value.id if isinstance(d.func.value, ast.Name) else ""
            self.found.append((d.func.attr.upper(), self.prefix.get(owner, "") + path,
                               self.path, d.lineno))

    def visit_FunctionDef(self, node):
        self._deco(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef


def backend(root: Path):
    routes, defs = [], set()
    for p, rel in files(root, ".py"):
        if rel.parts[0] == "scripts":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        v = Routes()
        v.path = str(rel)
        v.visit(tree)
        routes += v.found
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs.add(n.name)
                if isinstance(n, ast.ClassDef):
                    defs.update(f"{n.name}.{m.name}" for m in n.body
                                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))
    # Python isn't the only source of names a doc may cite.
    for p, _ in files(root, ".js", ".kt"):
        text = p.read_text(encoding="utf-8", errors="replace")
        defs.update(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", text))
        defs.update(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", text))
        defs.update(re.findall(r"^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", text, re.M))
        defs.update(re.findall(r"\bfun\s+([A-Za-z_]\w*)", text))
    return routes, defs


def headings(md: str) -> set[str]:
    """Section numbers a doc defines: '## 7.3 Title', '### §7.3', '## 7. Title',
    and the numbered items of a list directly under a numbered heading (§10.7)."""
    out, cur = set(), None
    for line in md.splitlines():
        m = re.match(r"^#+\s*(?:Appendix\s+)?§?\s*(\d+(?:\.\d+)*)\.?\s", line)
        if m:
            cur = m.group(1)
            parts = cur.split(".")
            out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif line.startswith("#"):
            cur = None
        elif cur:
            item = re.match(r"^(\d+)\.\s", line)
            if item:
                out.add(f"{cur}.{item.group(1)}")
    return out


def out_of_order(md: str):
    """Numbered headings that come after a later-numbered one."""
    last, last_line = None, 0
    for lineno, line in enumerate(md.splitlines(), 1):
        m = re.match(r"^#+\s*§?\s*(\d+(?:\.\d+)*)\.?\s", line)
        if not m:
            continue
        num = tuple(int(x) for x in m.group(1).split("."))
        # A heading numbered 1 starts a fresh numbered run (steps in a recipe).
        if last and num < last and num[-1] != 1:
            yield lineno, ".".join(map(str, num)), ".".join(map(str, last)), last_line
        last, last_line = num, lineno


def ignored(root: Path) -> set[str]:
    """Anchored, literal .gitignore entries: files a doc may name that are made at runtime."""
    gi = root / ".gitignore"
    if not gi.exists():
        return set()
    return {l.strip().lstrip("/") for l in gi.read_text().splitlines()
            if l.strip() and not l.startswith("#") and not any(c in l for c in "*?[!")}


# Names a doc may cite that belong to the language or platform, not the repo.
PLATFORM = set(sys.stdlib_module_names) | set(dir(__builtins__)) | {
    "performance", "AudioContext", "Date", "Math", "JSON", "window", "document",
    "navigator", "console", "Promise", "Object", "Array", "fetch", "setTimeout",
    "requestAnimationFrame", "super", "ControllerApplication", "Result"}


def check(root: Path, undocumented: bool):
    out = []
    routes, defs = backend(root)
    served = {norm(p) for _, p, _, _ in routes}
    mentioned: set[str] = set()
    js_calls = 0

    for p, rel in files(root, ".js", ".html"):
        if rel.parts[0] == "docs":
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for _, path, concat in JS_CALL.findall(line):
                js_calls += 1
                mentioned.add(norm(path))
                # The appended part may be a query string or more path.
                tails = [path, path.rstrip("/") + "/{}"] if concat else [path]
                if not any(route_matches(t, served) for t in tails):
                    out.append({"kind": "route", "file": str(rel), "line": lineno,
                                "detail": f"frontend calls {path} — no backend route serves it"})

    # Keyed by name for citations; the shallowest wins, so a nested README.md
    # never shadows the root one.
    docs = {}
    for p, rel in sorted(files(root, ".md"), key=lambda f: (len(f[1].parts), str(f[1]))):
        docs.setdefault(rel.name, (p.read_text(encoding="utf-8", errors="replace"), rel))
    runtime_files = ignored(root)
    for name, (text, rel) in docs.items():
        for lineno, num, prev, prev_line in out_of_order(text):
            out.append({"kind": "doc", "file": str(rel), "line": lineno,
                        "detail": f"section {num} comes after section {prev} (line {prev_line})"})
    for name, (text, rel) in docs.items():
        if rel.parts[0] not in ("docs",) and rel.name not in ("README.md", "AGENTS.md"):
            continue
        in_code = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            seen_here = set()
            for _, path in DOC_ROUTE.findall(line) + DOC_ROUTE_BARE.findall(line):
                if path in seen_here or "*" in path or "..." in path:
                    continue
                seen_here.add(path)
                mentioned.add(norm(path))
                if path.endswith(("…", "...")) or any(r.startswith(norm(path) + "/") for r in served):
                    continue  # a namespace, not an endpoint
                if not route_matches(path, served):
                    out.append({"kind": "route", "file": str(rel), "line": lineno,
                                "detail": f"doc names {path} — no backend route serves it"})
            for ref in DOC_PATH.findall(line):
                if "*" in ref or "<" in ref:
                    continue
                if not (root / ref).exists() and ref not in runtime_files:
                    out.append({"kind": "path", "file": str(rel), "line": lineno,
                                "detail": f"doc names {ref}, which does not exist"})
            if in_code:
                continue
            for sym in DOC_SYMBOL.findall(line):
                if sym.split(".")[0] in PLATFORM:
                    continue
                if sym not in defs and sym.split(".")[-1] not in defs:
                    out.append({"kind": "symbol", "file": str(rel), "line": lineno,
                                "detail": f"doc names {sym}(), defined nowhere in the repo"})

    for p, rel in files(root, ".py", ".js", ".kt", ".sh", ".md"):
        text = p.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for doc, sec in CITE.findall(line):
                if doc not in docs:
                    out.append({"kind": "cite", "file": str(rel), "line": lineno,
                                "detail": f"cites {doc} §{sec} — no such doc"})
                elif sec not in headings(docs[doc][0]):
                    out.append({"kind": "cite", "file": str(rel), "line": lineno,
                                "detail": f"cites {doc} §{sec} — the doc has no section {sec}"})

    out += config_mirror(root)
    print(f"checked {js_calls} frontend call(s) against {len(served)} backend route(s)",
          file=sys.stderr)

    if undocumented:
        for method, path, src, line in sorted(routes, key=lambda r: r[1]):
            n = norm(path)
            if n.startswith("/api") and not any(route_matches(m, {n}) for m in mentioned):
                out.append({"kind": "undocumented", "file": src, "line": line,
                            "detail": f"{method} {path} — no doc or frontend file mentions it"})
    return out


def config_mirror(root: Path):
    try:
        import yaml
    except ImportError:
        print("config check skipped: PyYAML not installed", file=sys.stderr)
        return []
    cfg_path, mirror = root / "config/config.yaml", root / "routes/config_routes.py"
    if not (cfg_path.exists() and mirror.exists()):
        return []
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    tree = ast.parse(mirror.read_text())
    out = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("_DEFAULTS")):
            continue
        name = node.targets[0].id
        section = name[:-len("_DEFAULTS")].lower()
        try:
            defaults = ast.literal_eval(node.value)
        except ValueError:
            continue
        yaml_side = cfg.get(section)
        if not isinstance(yaml_side, dict):
            out.append({"kind": "config", "file": "routes/config_routes.py", "line": node.lineno,
                        "detail": f"{name} mirrors '{section}', which config.yaml lacks"})
            continue
        for path, a, b in diff(defaults, yaml_side, section):
            if a is MISSING:
                d = f"{path}: in config.yaml ({b!r}), missing from {name}"
            elif b is MISSING:
                d = f"{path}: in {name} ({a!r}), missing from config.yaml"
            else:
                d = f"{path}: {name} says {a!r}, config.yaml says {b!r}"
            out.append({"kind": "config", "file": "routes/config_routes.py",
                        "line": node.lineno, "detail": d})
    return out


MISSING = object()


def diff(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            yield from diff(a.get(k, MISSING), b.get(k, MISSING), f"{path}.{k}")
    elif a is MISSING or b is MISSING or a != b:
        # YAML has no tuples, and 1 == 1.0 already compares equal.
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and list(a) == list(b)):
            yield path, a, b


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=["route", "cite", "path", "symbol", "config", "doc",
                                       "undocumented"])
    ap.add_argument("--undocumented", action="store_true")
    args = ap.parse_args()
    res = check(Path(args.root).resolve(), args.undocumented or args.only == "undocumented")
    res = [r for r in res if not args.only or r["kind"] == args.only]
    res.sort(key=lambda r: (r["kind"], r["file"], r["line"]))
    if args.json:
        json.dump(res, sys.stdout, indent=2)
        print()
        return
    for r in res:
        print(f"{r['file']}:{r['line']}  [{r['kind']}] {r['detail']}")
    counts = {}
    for r in res:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    print(f"\n{len(res)} finding(s): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
          file=sys.stderr)


if __name__ == "__main__":
    main()
