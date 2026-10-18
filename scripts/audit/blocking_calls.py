#!/usr/bin/env python3
"""
Find blocking work reachable from the event loop. Read-only, stdlib only.

A sync function is blocking when it calls a seed (telemetry DB access,
time.sleep, requests, subprocess, ...) or another blocking sync function in
this repo. Reports:

  async   an ``async def`` calls a blocking function directly
  loop    a sync function the loop runs (call_soon/call_later/add_done_callback
          target, or a zigpy listener method) calls a blocking function

Hand-off sites don't count: the callable passed to ``asyncio.to_thread`` or
``run_in_executor`` is a reference, not a call, and lambda bodies are skipped.

    python3 scripts/audit/blocking_calls.py [--json] [--only async|loop] [ROOT]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {"tests", "__pycache__", "android", "frontend", "static", "node_modules",
             ".git", "logs", "data", "zmm_telemetry", "screenshots", "venv", ".venv"}

# Qualified names (after import resolution) that block the calling thread.
SEEDS = {
    "time.sleep": "sleeps",
    "subprocess.run": "runs a subprocess", "subprocess.call": "runs a subprocess",
    "subprocess.check_call": "runs a subprocess",
    "subprocess.check_output": "runs a subprocess",
    "os.system": "runs a subprocess",
    "urllib.request.urlopen": "blocking HTTP",
    "socket.create_connection": "blocking socket",
    "duckdb.connect": "opens DuckDB", "sqlite3.connect": "opens SQLite",
    # Opening or fetching the shared DuckDB connections takes their locks.
    "modules.telemetry_db._get_db": "telemetry DB (_db_lock)",
    "modules.telemetry_db._get_octopus_db": "octopus DB lock",
    "modules.telemetry_db._get_system_db": "system DB lock",
}
# Accepted by design (zigbee_cache.py, FACTS): short reads on the caller's
# thread, writes queued to a worker cursor. Reported only with --all.
LOW_SEEDS = {"modules.zigbee_cache._get_db": "zigbee cache query on the loop"}
SEED_PREFIXES = {"requests.": "blocking HTTP", "httpx.Client": "blocking HTTP"}

# zigpy invokes these synchronously on the loop thread.
LOOP_LISTENER_METHODS = {"attribute_updated", "cluster_command", "device_joined",
                         "device_initialized", "raw_device_initialized", "device_left",
                         "device_removed", "handle_message", "general_command"}
LOOP_SCHEDULERS = {"call_soon", "call_later", "call_at", "call_soon_threadsafe",
                   "add_done_callback"}
HANDOFFS = {"to_thread", "run_in_executor", "run_sync"}
# A function that asks where it runs picks its path at runtime (cache on the
# loop, query off it); static reachability can't see that, so it isn't propagated.
LOOP_PROBES = ("_on_event_loop", "on_event_loop", "get_running_loop")


@dataclass
class Func:
    qual: str
    path: Path
    line: int
    is_async: bool
    cls: str | None
    calls: list[tuple[str, int]] = field(default_factory=list)
    loop_root: str | None = None
    loop_guarded: bool = False


def module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class ModuleScan(ast.NodeVisitor):
    def __init__(self, mod: str, path: Path, is_pkg: bool):
        self.mod, self.path, self.is_pkg = mod, path, is_pkg
        self.aliases: dict[str, str] = {}
        self.funcs: list[Func] = []
        self.stack: list[Func] = []
        self.cls_stack: list[str] = []
        self.scheduled: list[str] = []

    def _resolve_from(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.mod.split(".")
        drop = node.level - (1 if self.is_pkg else 0)
        base = base[:len(base) - drop] if drop else base
        return ".".join(base + ([node.module] if node.module else []))

    # Imports anywhere (function-local imports are common here) feed one alias
    # table per module; shadowing across functions is rare enough to accept.
    def visit_Import(self, node):
        for a in node.names:
            if a.asname:
                self.aliases[a.asname] = a.name
            else:
                self.aliases[a.name.split(".")[0]] = a.name.split(".")[0]

    def visit_ImportFrom(self, node):
        src = self._resolve_from(node)
        for a in node.names:
            self.aliases[a.asname or a.name] = f"{src}.{a.name}" if src else a.name

    def visit_ClassDef(self, node):
        self.cls_stack.append(node.name)
        self.generic_visit(node)
        self.cls_stack.pop()

    def _func(self, node, is_async):
        cls = self.cls_stack[-1] if self.cls_stack and not self.stack else None
        prefix = f"{self.mod}.{cls}" if cls else self.mod
        if self.stack:
            prefix = self.stack[-1].qual + ".<locals>"
        f = Func(f"{prefix}.{node.name}", self.path, node.lineno, is_async, cls)
        if cls and node.name in LOOP_LISTENER_METHODS and not is_async:
            f.loop_root = f"zigpy listener {node.name}()"
        self.funcs.append(f)
        self.stack.append(f)
        saved = self.cls_stack
        self.cls_stack = []
        for stmt in node.body:
            self.visit(stmt)
        self.cls_stack = saved
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._func(node, False)

    def visit_AsyncFunctionDef(self, node):
        self._func(node, True)

    def visit_Lambda(self, node):
        return  # deferred body; usually a hand-off

    def dotted(self, expr) -> str | None:
        if isinstance(expr, ast.Name):
            return self.aliases.get(expr.id, f"{self.mod}.{expr.id}")
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
                cls = self._enclosing_class()
                return f"{self.mod}.{cls}.{expr.attr}" if cls else None
            base = self.dotted(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    def _enclosing_class(self):
        for f in reversed(self.stack):
            if f.cls:
                return f.cls
        return None

    def visit_Call(self, node):
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if attr in HANDOFFS:
            # Arguments are evaluated here, but the callable itself is not run.
            skip = 2 if attr == "run_in_executor" else 1
            for a in node.args[skip:] + [k.value for k in node.keywords]:
                self.visit(a)
            return
        if attr in LOOP_SCHEDULERS:
            idx = 1 if attr in ("call_later", "call_at") else 0
            if len(node.args) > idx:
                target = self.dotted(node.args[idx])
                if target:
                    self.scheduled.append(target)
        if self.stack:
            name = self.dotted(fn)
            if name:
                self.stack[-1].calls.append((name, node.lineno))
                if not self.stack[-1].is_async and name.endswith(LOOP_PROBES):
                    self.stack[-1].loop_guarded = True
        self.generic_visit(node)


def seed_reason(name: str) -> str | None:
    if name in LOW_SEEDS:
        return LOW_SEEDS[name]
    if name in SEEDS:
        return SEEDS[name]
    for p, why in SEED_PREFIXES.items():
        if name.startswith(p):
            return why
    return None


def scan(root: Path):
    funcs: dict[str, Func] = {}
    scheduled: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"skip {rel}: {e}", file=sys.stderr)
            continue
        ms = ModuleScan(module_name(root, path), rel, path.name == "__init__.py")
        ms.visit(tree)
        for f in ms.funcs:
            funcs.setdefault(f.qual, f)
        scheduled.update(ms.scheduled)
    for q in scheduled:
        if q in funcs and not funcs[q].is_async and not funcs[q].loop_root:
            funcs[q].loop_root = "scheduled on the loop"
    return funcs


def blocking_chains(funcs: dict[str, Func]) -> dict[str, list[str]]:
    """Map each blocking sync function to one witness chain ending at a seed."""
    chain: dict[str, list[str]] = {}
    changed = True
    while changed:
        changed = False
        for q, f in funcs.items():
            if f.is_async or f.loop_guarded or q in chain:
                continue
            for name, line in f.calls:
                why = seed_reason(name)
                if why:
                    chain[q] = [f"{name} ({why})"]
                elif name in chain and name != q:
                    chain[q] = [name] + chain[name]
                else:
                    continue
                changed = True
                break
    return chain


def findings(funcs, chain):
    out = []
    for q, f in funcs.items():
        if not (f.is_async or f.loop_root):
            continue
        seen = set()
        for name, line in f.calls:
            why = seed_reason(name)
            path = [f"{name} ({why})"] if why else ([name] + chain[name] if name in chain else None)
            if not path or name in seen:
                continue
            seen.add(name)
            out.append({
                "kind": "async" if f.is_async else "loop",
                "file": str(f.path), "line": line, "function": q,
                "context": "async def" if f.is_async else f.loop_root,
                "chain": path,
                "severity": "low" if any(c.split(" ")[0] in LOW_SEEDS for c in path) else "high",
            })
    return sorted(out, key=lambda d: (d["kind"], d["file"], d["line"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=["async", "loop"])
    ap.add_argument("--all", action="store_true", help="include accepted low-severity paths")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    funcs = scan(root)
    res = [r for r in findings(funcs, blocking_chains(funcs))
           if (not args.only or r["kind"] == args.only)
           and (args.all or r["severity"] == "high")]
    if args.json:
        json.dump(res, sys.stdout, indent=2)
        print()
        return
    for r in res:
        print(f"{r['file']}:{r['line']}  [{r['kind']}] {r['function']}")
        print(f"    {' -> '.join(r['chain'])}")
    print(f"\n{len(res)} finding(s) across {len({r['file'] for r in res})} file(s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
