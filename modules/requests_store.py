"""
Requests — asks that need an answer, not just a notification.

A notification is fire-and-forget: it either arrived or it didn't, and nobody
finds out which. A *request* is addressed to a person, must be accepted or
declined, and tells the sender if it goes unanswered:

    Sean reaches the shops  ->  "Get milk?" sent to Alex
    Alex accepts            ->  Sean sees it was accepted
    nobody answers in 20m   ->  Sean is told it lapsed

That last line is the whole point, and it is why this lives on the hub rather
than in the browser. Only something always-running can notice that an answer
never came.

Design notes
------------
State is authoritative here, delivery is not. A request exists and expires the
same way whether or not any notification reached anyone — a phone that was off,
a browser that was closed, and a person who ignored it all end in the same
place, which is what makes the escalation trustworthy.

Expiry is swept, not scheduled per-request. One periodic pass over a small set
survives restarts for free, where a timer per request would have to be rebuilt
and could silently not be.

Storage: data/requests.yaml. Persisted so a hub restart cannot swallow a
pending ask — the sender would otherwise never learn it lapsed.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("modules.requests")

CONFIG_PATH = Path("./data/requests.yaml")

# Long enough that a phone in a pocket gets a fair chance, short enough that the
# sender learns while it still matters. Overridable per request.
DEFAULT_TIMEOUT_S = 20 * 60

# Answered requests are kept briefly so the UI can show what happened, then
# dropped. This is a scratchpad for asks in flight, not a message archive.
KEEP_ANSWERED_S = 24 * 3600

MAX_MESSAGE_LEN = 280
MAX_PENDING_PER_SENDER = 20

STATE_PENDING = "pending"
STATE_ACCEPTED = "accepted"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"

TERMINAL = frozenset({STATE_ACCEPTED, STATE_DECLINED, STATE_EXPIRED})


@dataclass
class Request:
    id: str
    from_user: str
    to_user: str
    message: str
    created_at: float
    expires_at: float
    state: str = STATE_PENDING
    answered_at: Optional[float] = None
    # True once the sender has been told this lapsed. Separate from `state` so
    # a restart mid-sweep cannot double-notify or, worse, skip the escalation.
    escalated: bool = False
    source: str = "manual"          # "manual" | "automation"
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["seconds_remaining"] = max(0, round(self.expires_at - time.time()))
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Request":
        return Request(
            id=str(d["id"]),
            from_user=str(d["from_user"]),
            to_user=str(d["to_user"]),
            message=str(d.get("message") or ""),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
            state=str(d.get("state") or STATE_PENDING),
            answered_at=d.get("answered_at"),
            escalated=bool(d.get("escalated", False)),
            source=str(d.get("source") or "manual"),
            context=dict(d.get("context") or {}),
        )

    @property
    def is_pending(self) -> bool:
        return self.state == STATE_PENDING


class RequestStore:
    """Pending asks, their answers, and the escalation when none comes."""

    def __init__(
            self,
            config_path: Path = CONFIG_PATH,
            notifier: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.requests: Dict[str, Request] = {}
        # Called for every delivery-worthy moment: a new ask, an answer, a
        # lapse. Injected rather than imported so this module has no opinion
        # about how anything is delivered.
        self.notifier = notifier

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.config_path.exists():
            return
        try:
            raw = yaml.safe_load(self.config_path.read_text()) or {}
        except Exception as e:                                  # noqa: BLE001
            logger.error("[requests] could not read %s: %s", self.config_path, e)
            return
        for entry in (raw.get("requests") or []):
            try:
                r = Request.from_dict(entry)
            except Exception as e:                              # noqa: BLE001
                logger.warning("[requests] skipping bad entry: %s", e)
                continue
            self.requests[r.id] = r
        logger.info("[requests] loaded %d", len(self.requests))

    def save(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(
                {"requests": [
                    {k: v for k, v in asdict(r).items()} for r in self.requests.values()
                ]},
                sort_keys=False,
            ))
            tmp.replace(self.config_path)
        except OSError as e:
            logger.error("[requests] save failed: %s", e)

    # -- creation ----------------------------------------------------------

    async def create(
            self,
            from_user: str,
            to_user: str,
            message: str,
            timeout_s: float = DEFAULT_TIMEOUT_S,
            source: str = "manual",
            context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = (message or "").strip()
        if not message:
            return {"success": False, "error": "Message is required"}
        if len(message) > MAX_MESSAGE_LEN:
            return {"success": False, "error": f"Message over {MAX_MESSAGE_LEN} characters"}
        if not to_user:
            return {"success": False, "error": "Recipient is required"}
        if timeout_s <= 0:
            return {"success": False, "error": "Timeout must be positive"}

        # A misfiring automation could otherwise bury someone in identical
        # asks; the sender hits this long before the recipient notices.
        pending = sum(
            1 for r in self.requests.values()
            if r.from_user == from_user and r.is_pending
        )
        if pending >= MAX_PENDING_PER_SENDER:
            return {"success": False,
                    "error": f"{from_user} already has {pending} unanswered requests"}

        now = time.time()
        r = Request(
            id=uuid.uuid4().hex[:12],
            from_user=from_user,
            to_user=to_user,
            message=message,
            created_at=now,
            expires_at=now + timeout_s,
            source=source,
            context=dict(context or {}),
        )
        self.requests[r.id] = r
        self.save()
        await self._notify("request_created", r)
        logger.info("[requests] %s -> %s: %r (%ds)",
                    from_user, to_user, message[:60], int(timeout_s))
        return {"success": True, "request": r.to_dict()}

    # -- answering ---------------------------------------------------------

    async def answer(self, request_id: str, user: str, accept: bool) -> Dict[str, Any]:
        r = self.requests.get(request_id)
        if not r:
            return {"success": False, "error": "Request not found"}
        if r.to_user != user:
            # Only the addressee answers. Anything else would let one person
            # silently clear another's asks.
            return {"success": False, "error": "This request is not addressed to you"}
        if r.state in TERMINAL:
            # Answering a lapsed request is a race, not an error: the sweep
            # may have run while the notification sat on a lock screen. Report
            # the settled state rather than pretending the tap did something.
            return {"success": False,
                    "error": f"Already {r.state}",
                    "request": r.to_dict()}

        r.state = STATE_ACCEPTED if accept else STATE_DECLINED
        r.answered_at = time.time()
        self.save()
        await self._notify(
            "request_accepted" if accept else "request_declined", r,
        )
        logger.info("[requests] %s %s %s", user, r.state, request_id)
        return {"success": True, "request": r.to_dict()}

    # -- expiry ------------------------------------------------------------

    async def sweep(self) -> int:
        """
        Expire lapsed requests and tell their senders. Returns how many lapsed.

        Escalation is marked before the notifier runs and saved regardless of
        whether it succeeds. A notifier that throws must not cause the same
        lapse to be announced again on the next pass.
        """
        now = time.time()
        lapsed: List[Request] = []
        dropped = 0

        for r in list(self.requests.values()):
            if r.is_pending and r.expires_at <= now:
                r.state = STATE_EXPIRED
                r.answered_at = None
                lapsed.append(r)
            elif r.state in TERMINAL:
                settled = r.answered_at or r.expires_at
                if now - settled > KEEP_ANSWERED_S:
                    del self.requests[r.id]
                    dropped += 1

        if lapsed or dropped:
            for r in lapsed:
                r.escalated = True
            self.save()

        for r in lapsed:
            logger.info("[requests] %s lapsed unanswered by %s", r.id, r.to_user)
            await self._notify("request_expired", r)

        return len(lapsed)

    # -- queries -----------------------------------------------------------

    def for_user(self, user: str, include_settled: bool = False) -> List[Dict[str, Any]]:
        """Requests this person should see: addressed to them, or sent by them."""
        out = []
        for r in self.requests.values():
            if r.to_user != user and r.from_user != user:
                continue
            if not include_settled and r.state in TERMINAL:
                continue
            out.append(r.to_dict())
        out.sort(key=lambda d: d["created_at"], reverse=True)
        return out

    def all(self, include_settled: bool = False) -> List[Dict[str, Any]]:
        out = [r.to_dict() for r in self.requests.values()
               if include_settled or r.state not in TERMINAL]
        out.sort(key=lambda d: d["created_at"], reverse=True)
        return out

    async def _notify(self, event: str, r: Request) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier(event, r.to_dict())
        except Exception as e:                                  # noqa: BLE001
            # Delivery failing must never roll back state. The request still
            # exists and still expires; that is what makes escalation reliable.
            logger.warning("[requests] notify %s failed: %s", event, e)


_store: Optional[RequestStore] = None


def get_request_store() -> Optional[RequestStore]:
    return _store


def set_request_store(s: RequestStore) -> None:
    global _store
    _store = s
