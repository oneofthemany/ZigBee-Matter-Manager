"""
Messages — person-to-person conversations inside ZMM.

Threads between two users with history and unread counts, delivered over the
websocket and as a web push so it lands on a closed phone. Automations send
through the same store, so a rule's message shares the thread. A conversation
belongs to its two participants and the API never lets a third user read it,
admins included. Its own DuckDB file and worker thread.
See docs/notifications.md.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import duckdb

logger = logging.getLogger("modules.messages")

DB_PATH = Path("./data/messages.duckdb")

MAX_BODY_CHARS = 1000
#: Messages older than this are purged; a household chat is not an archive.
RETENTION_DAYS = 365

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT   PRIMARY KEY,
    thread_id  TEXT   NOT NULL,
    from_user  TEXT   NOT NULL,
    to_user    TEXT   NOT NULL,
    body       TEXT   NOT NULL,
    source     TEXT   NOT NULL DEFAULT 'user',
    created_at DOUBLE NOT NULL,
    read_at    DOUBLE
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages (to_user);
"""

_MSG_COLS = ("id", "thread_id", "from_user", "to_user", "body",
             "source", "created_at", "read_at")


def thread_id(user_a: str, user_b: str) -> str:
    """Canonical id for the pair — same thread whoever sends first."""
    return "|".join(sorted((user_a.lower(), user_b.lower())))


class MessageStore:
    """
    Owns data/messages.duckdb. Single worker thread holds the only
    connection; public methods are async and marshal onto it.

    `notifier(event, payload)` fans a sent message out (websocket + push);
    delivery failing never rolls back state — the message exists and shows
    as unread regardless of whether any notification landed.
    """

    def __init__(
            self,
            notifier: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
            db_path: Path = DB_PATH,
    ):
        self.notifier = notifier
        self.db_path = Path(db_path)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="messages-db")
        self._con: Optional[duckdb.DuckDBPyConnection] = None

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _ensure_open(self) -> duckdb.DuckDBPyConnection:
        """Worker-thread only."""
        if self._con is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA.strip().split(";"):
                if stmt.strip():
                    self._con.execute(stmt)
            self._con.execute(
                "DELETE FROM messages WHERE created_at < ?",
                [time.time() - RETENTION_DAYS * 24 * 3600],
            )
        return self._con

    async def stop(self) -> None:
        def _close():
            if self._con is not None:
                self._con.close()
                self._con = None
        try:
            await self._run(_close)
        except Exception:                            # noqa: BLE001
            pass
        self._executor.shutdown(wait=False)

    # Sending
    async def send(self, from_user: str, to_user: str, body: str,
                   source: str = "user") -> Dict[str, Any]:
        from_user = (from_user or "").strip()
        to_user = (to_user or "").strip()
        body = (body or "").strip()[:MAX_BODY_CHARS]
        if not from_user or not to_user:
            return {"success": False, "error": "from_user and to_user required"}
        if from_user.lower() == to_user.lower():
            return {"success": False, "error": "Cannot message yourself"}
        if not body:
            return {"success": False, "error": "Empty message"}

        msg = {
            "id": uuid.uuid4().hex,
            "thread_id": thread_id(from_user, to_user),
            "from_user": from_user,
            "to_user": to_user,
            "body": body,
            "source": source,
            "created_at": time.time(),
            "read_at": None,
        }
        await self._run(self._insert, msg)
        logger.info("[messages] %s -> %s (%s): %s",
                    from_user, to_user, source, body[:60])

        if self.notifier:
            try:
                await self.notifier("message_created", dict(msg))
            except Exception as e:                   # noqa: BLE001
                logger.warning("[messages] notify failed: %s", e)
        return {"success": True, "message": msg}

    def _insert(self, m: Dict[str, Any]) -> None:
        self._ensure_open().execute(
            f"INSERT INTO messages ({', '.join(_MSG_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _MSG_COLS)})",
            [m[c] for c in _MSG_COLS],
        )

    # Reading
    async def threads_for(self, user: str) -> List[Dict[str, Any]]:
        """Conversations this user is part of: peer, last message, unread."""
        return await self._run(self._threads_for, user)

    def _threads_for(self, user: str) -> List[Dict[str, Any]]:
        rows = self._ensure_open().execute(
            """
            SELECT thread_id,
                   arg_max(from_user, created_at),
                   arg_max(to_user, created_at),
                   arg_max(body, created_at),
                   MAX(created_at),
                   COUNT(*) FILTER (WHERE to_user = ? AND read_at IS NULL)
            FROM messages
            WHERE from_user = ? OR to_user = ?
            GROUP BY thread_id
            ORDER BY MAX(created_at) DESC
            """,
            [user, user, user],
        ).fetchall()
        out = []
        for tid, last_from, last_to, body, ts, unread in rows:
            peer = last_to if last_from == user else last_from
            out.append({
                "thread_id": tid, "peer": peer,
                "last_body": body, "last_from": last_from,
                "last_at": ts, "unread": int(unread or 0),
            })
        return out

    async def conversation(self, user: str, peer: str,
                           limit: int = 50,
                           before: Optional[float] = None) -> List[Dict[str, Any]]:
        """Messages between user and peer, oldest→newest of the last `limit`."""
        return await self._run(self._conversation, user, peer, limit, before)

    def _conversation(self, user, peer, limit, before) -> List[Dict[str, Any]]:
        sql = (f"SELECT {', '.join(_MSG_COLS)} FROM messages "
               "WHERE thread_id = ?")
        params: List[Any] = [thread_id(user, peer)]
        if before is not None:
            sql += " AND created_at < ?"
            params.append(float(before))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._ensure_open().execute(sql, params).fetchall()
        return [dict(zip(_MSG_COLS, r)) for r in reversed(rows)]

    async def unread_total(self, user: str) -> int:
        return await self._run(
            lambda: self._ensure_open().execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE to_user = ? AND read_at IS NULL", [user]
            ).fetchone()[0]
        )

    # Read state
    async def mark_read(self, user: str, peer: str) -> int:
        """Mark everything peer→user as read. Returns rows changed."""
        n = await self._run(self._mark_read, user, peer)
        if n and self.notifier:
            # Tells the SENDER their message was seen, and other open tabs of
            # the reader to clear their badge.
            try:
                await self.notifier("messages_read", {
                    "thread_id": thread_id(user, peer),
                    "reader": user, "peer": peer, "count": n,
                })
            except Exception as e:                   # noqa: BLE001
                logger.debug("[messages] read-notify failed: %s", e)
        return n

    def _mark_read(self, user, peer) -> int:
        con = self._ensure_open()
        before = con.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ? "
            "AND to_user = ? AND read_at IS NULL",
            [thread_id(user, peer), user],
        ).fetchone()[0]
        if before:
            con.execute(
                "UPDATE messages SET read_at = ? WHERE thread_id = ? "
                "AND to_user = ? AND read_at IS NULL",
                [time.time(), thread_id(user, peer), user],
            )
        return int(before)


_store: Optional[MessageStore] = None


def get_message_store() -> Optional[MessageStore]:
    return _store


def set_message_store(store: MessageStore) -> None:
    global _store
    _store = store
