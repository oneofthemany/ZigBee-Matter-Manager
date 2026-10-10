"""
The live log's memory, and the trace behind each line. See docs/logbook.md.

Every `log` event broadcast to the Debug ▸ Logs view is stored with an id;
every automation evaluation runs in a chain (modules/automation.py
current_chain) whose trace entries are stored against it; and a command a rule
or a person sends is remembered for a few seconds, so the device change it
produces is attributed to them. Clicking a log line asks trace() to put those
together: what changed, what caused it, which rules looked at it and why each
did or didn't fire, what they did, and what that changed in turn.

data/logbook.duckdb is its own file with one worker thread holding the only
connection — never the telemetry DB, and never written from the event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("logbook")

DB_PATH = Path("./data/logbook.duckdb")
RETENTION_DAYS = 7
FLUSH_S = 1.0
CAUSE_TTL_S = 15.0            # a command's effect arrives within this
LINK_WINDOW_S = 3.0           # a log line and the evaluation it started
MAX_QUEUE = 20000             # a stalled disk drops rows rather than memory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id VARCHAR, ts DOUBLE, level VARCHAR, category VARCHAR, ieee VARCHAR,
    device_name VARCHAR, attribute VARCHAR, value VARCHAR, message VARCHAR,
    chain_id VARCHAR, cause_chain VARCHAR, cause_user VARCHAR);
CREATE TABLE IF NOT EXISTS chains (
    id VARCHAR, parent_id VARCHAR, ts DOUBLE, trigger_ieee VARCHAR,
    trigger_name VARCHAR, changed VARCHAR, cause_chain VARCHAR, cause_user VARCHAR);
CREATE TABLE IF NOT EXISTS trace (
    chain_id VARCHAR, ts DOUBLE, rule_id VARCHAR, rule_name VARCHAR, phase VARCHAR,
    result VARCHAR, message VARCHAR, target_ieee VARCHAR, level VARCHAR);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS chains_id ON chains (id);
CREATE INDEX IF NOT EXISTS trace_chain ON trace (chain_id)
"""

# Engine devices whose changes the Zigbee path doesn't already log. Zigbee
# ieees never contain "::"; virtual::weather and friends are too chatty.
_LOGGED_PREFIXES = ("worker::", "alarm::", "user::", "camera::", "shelly::", "esphome::",
                    "matter_", "nuki_")
_SKIP_KEYS = {"last_seen", "last_update", "linkquality", "lqi", "rssi", "available_since"}

# Trace results worth a line in the live log, and how to say them.
_LIVE = {"FIRING": "fired", "SUCCESS": "", "CMD_FAIL": "", "EXCEPTION": "", "TARGET_ERROR": "",
         "CHAIN_LIMIT": "", "VALUE_ERROR": ""}


def _val(v: Any) -> str:
    try:
        return v if isinstance(v, str) else json.dumps(v, default=str)
    except (TypeError, ValueError):
        return str(v)


class Logbook:
    def __init__(self, db_path: Path = DB_PATH,
                 broadcast: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
                 get_names: Callable[[], Dict[str, str]] = lambda: {},
                 current_chain: Callable[[], Tuple[Optional[str], Optional[str]]] = lambda: (None, None),
                 clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self._broadcast = broadcast
        self._get_names = get_names
        self._current_chain = current_chain
        self._clock = clock
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="logbook-db")
        self._con = None
        self._rows: Dict[str, List[tuple]] = {"events": [], "chains": [], "trace": []}
        self._chains_seen: Dict[str, float] = {}        # chain id -> first seen (stored)
        self._chain_info: Dict[str, Dict[str, Any]] = {}  # pending, not yet stored
        self._causes: Dict[str, Tuple[float, Optional[str], Optional[str]]] = {}
        self._task: Optional[asyncio.Task] = None
        self.dropped = 0

    # Worker thread
    def _ensure_open(self):
        if self._con is None:
            import duckdb
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA.strip().split(";"):
                if stmt.strip():
                    self._con.execute(stmt)
        return self._con

    def _write(self, rows: Dict[str, List[tuple]]) -> None:
        con = self._ensure_open()
        cols = {"events": 12, "chains": 8, "trace": 9}
        for table, batch in rows.items():
            if batch:
                con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * cols[table])})", batch)

    def _prune(self, before: float) -> None:
        con = self._ensure_open()
        for table in ("events", "chains", "trace"):
            con.execute(f"DELETE FROM {table} WHERE ts < ?", [before])
        con.execute("CHECKPOINT")

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    # Lifecycle
    async def start(self) -> None:
        await self._run(self._ensure_open)
        await self._run(self._prune, self._clock() - RETENTION_DAYS * 86400)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await self.flush()
        def _close():
            if self._con is not None:
                self._con.close()
                self._con = None
        try:
            await self._run(_close)
        except Exception:                                 # noqa: BLE001
            pass
        self._executor.shutdown(wait=False)

    async def _loop(self) -> None:
        last_prune = self._clock()
        while True:
            try:
                await asyncio.sleep(FLUSH_S)
                await self.flush()
                if self._clock() - last_prune > 3600:
                    last_prune = self._clock()
                    await self._run(self._prune, last_prune - RETENTION_DAYS * 86400)
                    self._forget_old()
            except asyncio.CancelledError:
                break
            except Exception as e:                        # noqa: BLE001
                logger.warning("[logbook] write failed: %s", e)

    async def flush(self) -> None:
        rows, self._rows = self._rows, {"events": [], "chains": [], "trace": []}
        if any(rows.values()):
            await self._run(self._write, rows)

    def _queue(self, table: str, row: tuple) -> None:
        if sum(len(v) for v in self._rows.values()) >= MAX_QUEUE:
            self.dropped += 1
            return
        self._rows[table].append(row)

    def _forget_old(self) -> None:
        cutoff = self._clock() - 3600
        self._chains_seen = {k: t for k, t in self._chains_seen.items() if t > cutoff}
        self._chain_info = {k: v for k, v in self._chain_info.items() if v["ts"] > cutoff}
        now = self._clock()
        self._causes = {k: v for k, v in self._causes.items() if v[0] > now}

    # Causes
    def note_user_command(self, ieee: str, user: str) -> None:
        self._causes[ieee] = (self._clock() + CAUSE_TTL_S, None, user)

    def _cause_for(self, ieee: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        c = self._causes.get(ieee or "")
        if c and c[0] > self._clock():
            return c[1], c[2]
        return None, None

    # Chains
    def _store_chain(self, chain_id: str) -> None:
        """Persist a chain the first time it has anything worth keeping."""
        if chain_id in self._chains_seen:
            return
        info = self._chain_info.pop(chain_id, None)
        if info is None:
            return
        self._chains_seen[chain_id] = info["ts"]
        self._queue("chains", (chain_id, info["parent"], info["ts"], info["ieee"], info["name"],
                               _val(info["changed"]), info["cause_chain"], info["cause_user"]))

    # Engine hooks (synchronous, called on the loop; must not block)
    def observe(self, ieee: str, changed: Dict[str, Any]) -> None:
        """State listener: note the chain this change started, and log the
        changes of devices the Zigbee path doesn't already log."""
        chain_id, parent = self._current_chain()
        cause_chain, cause_user = self._cause_for(ieee)
        name = self._get_names().get(ieee, ieee)
        if chain_id and chain_id not in self._chains_seen and chain_id not in self._chain_info:
            self._chain_info[chain_id] = {"ts": self._clock(), "parent": parent, "ieee": ieee, "name": name,
                                          "changed": changed, "cause_chain": cause_chain,
                                          "cause_user": cause_user}
            if cause_chain or cause_user or parent:
                self._store_chain(chain_id)
        if not ieee.startswith(_LOGGED_PREFIXES):
            return
        keys = [k for k in changed if k not in _SKIP_KEYS]
        if not keys:
            return
        if chain_id:
            self._store_chain(chain_id)
        for k in keys:
            self._emit({"level": "INFO", "category": "attribute_update", "ieee": ieee,
                        "device_name": name, "attribute": k, "value": changed[k],
                        "message": f"[{ieee}] ({name}) {k}={_val(changed[k])}",
                        "chain_id": chain_id})

    def on_trace(self, entry: Dict[str, Any]) -> None:
        """Trace listener: store the entry against its chain; remember what a
        rule sent; put firings and command results into the live log."""
        chain_id = entry.get("chain_id")
        result = str(entry.get("result") or "")
        if result == "SENDING" and entry.get("target_ieee"):
            self._causes[entry["target_ieee"]] = (self._clock() + CAUSE_TTL_S, chain_id, None)
        if not chain_id:
            return
        self._store_chain(chain_id)
        self._queue("trace", (chain_id, entry.get("timestamp") or self._clock(), entry.get("rule_id"),
                              entry.get("rule_name"), entry.get("phase"), result,
                              str(entry.get("message") or "")[:500], entry.get("target_ieee"),
                              entry.get("level")))
        kind = "FIRING" if result.endswith("_FIRING") else result
        if kind in _LIVE:
            lvl = entry.get("level") or "INFO"
            msg = str(entry.get("message") or "")
            if kind == "FIRING":
                msg = f"Rule '{entry.get('rule_name') or entry.get('rule_id')}' fired ({result[:-7].lower()})"
            self._emit({"level": "INFO" if lvl == "DEBUG" else lvl, "category": "automation",
                        "rule_id": entry.get("rule_id"), "message": msg, "chain_id": chain_id,
                        "ieee": entry.get("target_ieee")})

    def _emit(self, payload: Dict[str, Any]) -> None:
        if self._broadcast is None:
            self.record_log(payload)
            return
        try:
            asyncio.get_running_loop().create_task(self._broadcast("log", payload))
        except RuntimeError:
            self.record_log(payload)

    # Every live-log line passes through here (routes/websocket_routes.broadcast_event).
    def record_log(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Give a log line an id and keep it; returns the payload to send."""
        if payload.get("event_id"):
            return payload
        now = self._clock()
        out = dict(payload)
        out["event_id"] = secrets.token_hex(6)
        out["ts"] = now
        cause_chain, cause_user = (None, None)
        if out.get("category") == "attribute_update":
            cause_chain, cause_user = self._cause_for(out.get("ieee"))
        out["cause_user"] = cause_user
        self._queue("events", (out["event_id"], now, str(out.get("level") or "INFO"),
                               out.get("category"), out.get("ieee"), out.get("device_name"),
                               out.get("attribute"), None if out.get("value") is None else _val(out["value"]),
                               str(out.get("message") or "")[:1000], out.get("chain_id"),
                               cause_chain, cause_user))
        return out

    # Reads (worker thread)
    def _q(self, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        con = self._ensure_open()
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _events(self, before: Optional[float], limit: int, ieee: Optional[str], q: Optional[str]):
        sql, params = "SELECT * FROM events WHERE 1=1", []
        if before:
            sql += " AND ts < ?"
            params.append(before)
        if ieee:
            sql += " AND ieee = ?"
            params.append(ieee)
        if q:
            sql += " AND lower(message) LIKE ?"
            params.append(f"%{q.lower()}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return self._q(sql, params)

    async def events(self, before: Optional[float] = None, limit: int = 200,
                     ieee: Optional[str] = None, q: Optional[str] = None) -> List[Dict[str, Any]]:
        await self.flush()
        return await self._run(self._events, before, max(1, min(int(limit), 1000)), ieee, q)

    def _chain_summary(self, chain_id: str, depth: int = 0) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM chains WHERE id = ? LIMIT 1", [chain_id])
        if not rows:
            return None
        ch = rows[0]
        trace = self._q("SELECT * FROM trace WHERE chain_id = ? ORDER BY ts", [chain_id])
        rules: Dict[str, Dict[str, Any]] = {}
        for t in trace:
            rid = t["rule_id"] or "-"
            if rid == "-":
                continue
            r = rules.setdefault(rid, {"rule_id": rid, "rule_name": t["rule_name"], "fired": False,
                                       "outcome": None, "entries": []})
            r["rule_name"] = r["rule_name"] or t["rule_name"]
            r["entries"].append({k: t[k] for k in ("ts", "phase", "result", "message", "target_ieee", "level")})
            if t["result"].endswith("_FIRING"):
                r["fired"] = True
            if t["phase"] in ("evaluate", "prerequisite", "cooldown", "transition") and not r["fired"]:
                r["outcome"] = t["result"]
        out = {"id": ch["id"], "ts": ch["ts"], "trigger_ieee": ch["trigger_ieee"],
               "trigger_name": ch["trigger_name"], "changed": _load(ch["changed"]),
               "rules": list(rules.values()),
               "notes": [{k: t[k] for k in ("ts", "result", "message", "level")}
                         for t in trace if (t["rule_id"] or "-") == "-"],
               "cause": self._cause(ch["cause_chain"], ch["cause_user"], depth)}
        if depth == 0:
            out["effects"] = self._q("SELECT * FROM events WHERE cause_chain = ? ORDER BY ts", [chain_id])
            kids = self._q("SELECT id FROM chains WHERE parent_id = ? OR cause_chain = ? ORDER BY ts",
                           [chain_id, chain_id])
            out["then"] = [s for s in (self._chain_summary(k["id"], 1) for k in kids) if s]
        return out

    def _cause(self, cause_chain: Optional[str], cause_user: Optional[str], depth: int):
        if cause_user:
            return {"kind": "user", "user": cause_user}
        if cause_chain and depth < 2:
            up = self._chain_summary(cause_chain, depth + 1)
            if up:
                fired = [r for r in up["rules"] if r["fired"]]
                return {"kind": "rule", "chain": up, "rules": [r["rule_name"] or r["rule_id"] for r in fired]}
        return None

    def _trace(self, event_id: str) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM events WHERE id = ? LIMIT 1", [event_id])
        if not rows:
            return None
        ev = rows[0]
        chain_id = ev["chain_id"]
        if not chain_id and ev["ieee"] and ev["category"] == "attribute_update":
            near = self._q("SELECT id, changed, ts FROM chains WHERE trigger_ieee = ? AND ts BETWEEN ? AND ? "
                           "ORDER BY abs(ts - ?) LIMIT 5",
                           [ev["ieee"], ev["ts"] - LINK_WINDOW_S, ev["ts"] + LINK_WINDOW_S, ev["ts"]])
            for c in near:
                if not ev["attribute"] or ev["attribute"] in (_load(c["changed"]) or {}):
                    chain_id = c["id"]
                    break
        chain = self._chain_summary(chain_id) if chain_id else None
        cause = self._cause(ev["cause_chain"], ev["cause_user"], 0)
        return {"event": ev, "chain": chain, "cause": cause or (chain or {}).get("cause")}

    async def trace(self, event_id: str) -> Optional[Dict[str, Any]]:
        await self.flush()
        return await self._run(self._trace, event_id)


def _load(s: Optional[str]) -> Any:
    try:
        return json.loads(s) if s else None
    except ValueError:
        return s


_logbook: Optional[Logbook] = None


def get_logbook() -> Optional[Logbook]:
    return _logbook


def set_logbook(lb: Optional[Logbook]) -> None:
    global _logbook
    _logbook = lb
