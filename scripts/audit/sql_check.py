#!/usr/bin/env python3
"""
Bind every SQL string in the repo against the schema the repo itself creates.

Each file holding CREATE TABLE statements is a schema group: its DDL (tables,
ALTER ... ADD COLUMN migrations, indexes) runs on an in-memory DuckDB, or
SQLite for a file that imports sqlite3 and not duckdb. Every other SQL literal
is planned with EXPLAIN in a group that defines the tables it names, with
parameters bound as NULL. A binder error is a query that fails at runtime: a
missing column, a renamed table, a type the schema can't hold.

``--db snapshot.duckdb`` adds a real database as a group, read-only, so queries
bind against the schema deployed rather than the one the code declares.

Holes in f-strings and ``.format`` templates are tried with several fillers;
an error that survives all of them is reported as low confidence.

Needs duckdb (the locked version): run with a venv built from requirements.lock.

    python scripts/audit/sql_check.py [--all] [--json] [--db FILE]... [ROOT]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

SKIP_DIRS = {"tests", "__pycache__", "android", "frontend", "static", "node_modules",
             ".git", "logs", "data", "zmm_telemetry", "screenshots", "venv", ".venv"}
# Upper-case keyword and a following clause, so log text like "create foo" or
# "Update check failed" isn't taken for SQL.
SQL_START = re.compile(
    r"^\s*(SELECT\b.*\bFROM\b|INSERT\s+INTO\b|UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b|WITH\b.*\bAS\s*\("
    r"|CREATE\s+(OR\s+REPLACE\s+|UNIQUE\s+|TEMP(ORARY)?\s+)*(TABLE|INDEX|VIEW|SEQUENCE|MACRO|TYPE)\b"
    r"|ALTER\s+TABLE\b|DROP\s+(TABLE|INDEX|VIEW)\b)", re.S)
DDL_START = re.compile(r"^\s*(CREATE|ALTER)\b")
TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|(?<!DO )UPDATE|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)\s+([A-Za-z_][\w.]*)", re.I)
CTE_NAME = re.compile(r"(?:\bWITH|,)\s*(?:RECURSIVE\s+)?([A-Za-z_]\w*)\s+AS\s*\(", re.I)
FORMAT_FIELD = re.compile(r"\{[A-Za-z_]\w*(?:![rsa])?(?::[^{}]*)?\}")
# System catalogs and table functions are not the repo's tables.
CATALOG = ("information_schema", "duckdb_", "sqlite_", "pragma_", "read_", "generate_series",
           "range", "unnest", "glob")
HOLE = "__HOLE__"
# Tried in turn for templated SQL; a hole is a clause, a value or a list.
HOLE_FILLS = ("1", "", "(NULL)", "NULL", "id")


@dataclass
class Sql:
    path: str
    line: int
    text: str
    templated: bool


def literal(node, env=None) -> tuple[str, bool] | None:
    """Static text of a str expression, with holes for anything computed.
    ``env`` binds loop variables to known strings."""
    env = env or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if FORMAT_FIELD.search(node.value):
            return FORMAT_FIELD.sub(HOLE, node.value), True
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        out, templated = [], False
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v.value, ast.Name) and v.value.id in env:
                out.append(env[v.value.id])
            else:
                out.append(HOLE)
                templated = True
        return "".join(out), templated
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left, env), literal(node.right, env)
        if left is None and right is None:
            return None
        lt, lf = left or (HOLE, True)
        rt, rf = right or (HOLE, True)
        return lt + rt, lf or rf
    return None


def module_constants(tree) -> dict[str, object]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError, TypeError):
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = value
    return out


def loop_expansions(tree, consts) -> dict[int, list[dict[str, str]]]:
    """For ``for a, b in CONST:`` over a literal, the bindings each SQL
    expression in its body sees, keyed by the expression's id."""
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
                and node.iter.id in consts):
            continue
        names = ([e.id for e in node.target.elts if isinstance(e, ast.Name)]
                 if isinstance(node.target, ast.Tuple) else
                 [node.target.id] if isinstance(node.target, ast.Name) else [])
        rows = consts[node.iter.id]
        if not names or not isinstance(rows, (list, tuple)):
            continue
        envs = []
        for row in rows:
            vals = row if isinstance(row, (list, tuple)) else (row,)
            if len(vals) == len(names):
                envs.append({n: str(v) for n, v in zip(names, vals)})
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.JoinedStr):
                    out[id(sub)] = envs
    return out


def split_statements(sql: str) -> list[str]:
    parts, buf, quote = [], [], None
    for ch in sql:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def collect(root: Path) -> tuple[list[Sql], set[str]]:
    found, sqlite_files = [], set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts[:-1]) or rel.parts[0] == "scripts":
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if re.search(r"^\s*import sqlite3", src, re.M) and "duckdb" not in src:
            sqlite_files.add(str(rel))
        docstrings = {id(n.value) for n in ast.walk(tree)
                      if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        expand = loop_expansions(tree, module_constants(tree))
        seen = set()
        for node in ast.walk(tree):
            if id(node) in docstrings or id(node) in seen:
                continue
            first = literal(node)
            if not first or not SQL_START.match(first[0]):
                continue
            # Count only the outermost expression of a concatenation.
            for sub in ast.walk(node):
                seen.add(id(sub))
            envs = expand.get(id(node))
            texts = [literal(node, e) for e in envs] if envs else [first]
            for text, templated in texts:
                stmts = split_statements(text) if DDL_START.match(text) else [text]
                found += [Sql(str(rel), node.lineno, t, templated) for t in stmts]
    return found, sqlite_files


def fill(sql: str, hole: str = "1") -> str:
    """Bind ? / $n as NULL and holes as ``hole``, outside quotes and comments."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            j = sql.find(ch, i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        out.append("NULL" if ch == "?" else ch)
        i += 1
    s = re.sub(r"\$\d+\b", "NULL", "".join(out))
    return s.replace(HOLE, f" {hole} " if hole else " ")


def tables_in(sql: str) -> set[str]:
    ctes = {c.lower() for c in CTE_NAME.findall(sql)}
    out = set()
    for ref in TABLE_REF.findall(fill(sql, HOLE)):
        if ref.lower().startswith(CATALOG) or HOLE in ref:
            continue
        name = ref.split(".")[-1].lower()
        if name not in ctes and not name.startswith(CATALOG):
            out.add(name)
    return out


class Group:
    def __init__(self, name: str, dialect: str, con=None):
        self.name, self.dialect = name, dialect
        self.con = con or (sqlite3.connect(":memory:") if dialect == "sqlite"
                           else duckdb.connect(":memory:"))
        self.errors = (sqlite3.Error,) if dialect == "sqlite" else (duckdb.Error,)

    def run(self, sql: str):
        return self.con.execute(sql)

    def tables(self) -> set[str]:
        q = ("SELECT name FROM sqlite_master WHERE type='table'" if self.dialect == "sqlite"
             else "SELECT table_name FROM information_schema.tables")
        return {r[0].lower() for r in self.run(q).fetchall()}


def build_groups(sqls: list[Sql], sqlite_files: set[str], dbs: list[str]):
    groups: dict[str, Group] = {}
    ddl_errors = []
    by_file: dict[str, list[Sql]] = {}
    for s in sqls:
        if DDL_START.match(s.text):
            by_file.setdefault(s.path, []).append(s)
    for path, ddl in by_file.items():
        if not any(re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?TABLE", d.text) for d in ddl):
            continue
        g = Group(path, "sqlite" if path in sqlite_files else "duckdb")
        pending = list(ddl)
        # Retry until no progress: sequences, tables and indexes in any order.
        while pending:
            failed = []
            for d in pending:
                try:
                    g.run(fill(d.text))
                except g.errors as e:
                    failed.append((d, e))
            if len(failed) == len(pending):
                for d, e in failed:
                    # Re-adding a column is the idempotent migration pattern.
                    if "already exists" not in str(e) and "duplicate column" not in str(e):
                        ddl_errors.append(report(d, "ddl", e))
                break
            pending = [d for d, _ in failed]
        groups[path] = g
    for db in dbs:
        groups[f"db:{db}"] = Group(f"db:{db}", "duckdb", duckdb.connect(db, read_only=True))
    owners: dict[str, list[str]] = {}
    for name, g in groups.items():
        for t in g.tables():
            owners.setdefault(t, []).append(name)
    return groups, owners, ddl_errors


def report(s: Sql, kind: str, err, group: str | None = None) -> dict:
    msg = str(err).strip().splitlines()[0][:300]
    return {"file": s.path, "line": s.line, "kind": kind, "group": group,
            "confidence": "low" if s.templated else "high", "error": msg,
            "sql": " ".join(s.text.split())[:200]}


def check(root: Path, dbs: list[str]) -> list[dict]:
    sqls, sqlite_files = collect(root)
    groups, owners, out = build_groups(sqls, sqlite_files, dbs)
    templated_ddl = {s.path for s in sqls if s.templated
                     and re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?TABLE", s.text)}
    for s in sqls:
        if DDL_START.match(s.text):
            continue
        tabs = tables_in(s.text)
        if not tabs:
            continue
        missing = sorted(t for t in tabs if t not in owners)
        if missing:
            r = report(s, "table", f"no CREATE for {', '.join(missing)}")
            # The file builds a table from a template this tool can't expand.
            if s.path in templated_ddl:
                r["confidence"] = "low"
            out.append(r)
            continue
        fits = [g for g in groups.values() if all(g.name in owners[t] for t in tabs)]
        if not fits:
            out.append(report(s, "table", f"tables {sorted(tabs)} span schema groups"))
            continue
        # The file's own dialect, then a real snapshot, then the file's own schema.
        dialect = "sqlite" if s.path in sqlite_files else "duckdb"
        fits.sort(key=lambda g: (g.dialect != dialect, not g.name.startswith("db:"),
                                 g.name != s.path))
        g = fits[0]
        err = None
        for hole in (HOLE_FILLS if s.templated else ("1",)):
            try:
                g.run("EXPLAIN " + fill(s.text, hole))
                err = None
                break
            except g.errors as e:
                err = err or e
        if err is not None:
            out.append(report(s, "bind", err, g.name))
    return sorted(out, key=lambda r: (r["confidence"] != "high", r["file"], r["line"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include low-confidence templated SQL")
    ap.add_argument("--db", action="append", default=[], help="snapshot DB to bind against")
    args = ap.parse_args()
    res = [r for r in check(Path(args.root).resolve(), args.db)
           if args.all or r["confidence"] != "low"]
    if args.json:
        json.dump(res, sys.stdout, indent=2)
        print()
        return
    for r in res:
        where = f" in {r['group']}" if r.get("group") else ""
        print(f"{r['file']}:{r['line']}  [{r['kind']}/{r['confidence']}{where}] {r['error']}")
        if r["sql"]:
            print(f"    {r['sql']}")
    print(f"\n{len(res)} finding(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
