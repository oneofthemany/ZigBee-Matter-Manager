"""
Application Alert Center — surfaces problems to the user instead of leaving them
buried in the logs.

raise_alert() for concrete, actionable problems, plus an AlertLogHandler on the
root logger that turns any ERROR into a deduplicated alert. Persisted to
data/app_alerts.json and pushed live as `app_alert` websocket events.
See docs/debugging.md.
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("modules.app_alerts")

ALERTS_FILE = Path("./data/app_alerts.json")
MAX_ALERTS = 200
# Long enough that a loop-stall stack dump (innermost first) keeps ~10 frames —
# the culprit plus its call path — while keeping the persisted JSON bounded.
MAX_MESSAGE_CHARS = 2000

# Re-emit/dedupe window: identical alerts within this period bump `count`
# on the existing alert instead of creating a new one.
DEDUPE_COOLDOWN = 600  # seconds

# Loggers whose ERROR records should NOT become alerts: relayed subprocess
# output and chatty third-party libraries that self-recover.
EXCLUDED_LOGGER_PREFIXES = (
    "modules.app_alerts",   # never alert about ourselves (recursion guard)
    "matter_server",        # relays matter-server subprocess stderr verbatim
    "pychromecast",         # reconnects on its own, very chatty
    "editor.",              # editor test flows report through their own UI
    "uk_fuel_prices_api",   # logs ERROR per retailer feed; several are
                            # chronically down (BP 403s non-browsers, KRL
                            # times out) while the query still succeeds off
                            # the rest. modules/fuel_prices.py surfaces real
                            # total failure through the API instead.
)


class AlertCenter:
    """Thread-safe alert store with WebSocket push."""

    def __init__(self):
        self._alerts: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._emit: Optional[Callable] = None          # async fn(evt, data)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._load()

    # wiring

    def set_emitter(self, emit: Callable, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Attach the WebSocket broadcast coroutine. Call from the event loop."""
        self._emit = emit
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop

    # persistence

    def _load(self):
        try:
            if ALERTS_FILE.exists():
                data = json.loads(ALERTS_FILE.read_text())
                self._alerts = data.get("alerts", [])[-MAX_ALERTS:]
        except Exception as e:
            logger.debug(f"Could not load alerts file: {e}")
            self._alerts = []

    def _save(self):
        try:
            ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ALERTS_FILE.write_text(
                json.dumps({"alerts": self._alerts[-MAX_ALERTS:]}, indent=1)
            )
        except Exception as e:
            logger.debug(f"Could not save alerts file: {e}")

    # core API

    def raise_alert(self, severity: str, source: str, title: str, message: str,
                    dedupe_key: Optional[str] = None,
                    data: Optional[Dict] = None) -> Optional[Dict]:
        """
        Record an alert and push it to the UI. Safe to call from any thread.

        severity: "info" | "warning" | "error"
        source:   short module tag shown in the UI (e.g. "automation")
        dedupe_key: identical keys within DEDUPE_COOLDOWN update the
                    existing alert's count instead of creating a new one.
        """
        key = dedupe_key or f"{source}:{title}"
        now = time.time()

        with self._lock:
            for a in reversed(self._alerts):
                if a.get("dedupe_key") == key and not a.get("dismissed"):
                    a["count"] = a.get("count", 1) + 1
                    a["last_seen"] = now
                    a["message"] = message[:MAX_MESSAGE_CHARS]
                    self._save()
                    # Re-push at most once per cooldown window
                    if now - a.get("last_pushed", 0) > DEDUPE_COOLDOWN:
                        a["last_pushed"] = now
                        self._push(a)
                    return a

            alert = {
                "id": uuid.uuid4().hex[:12],
                "ts": now,
                "last_seen": now,
                "last_pushed": now,
                "severity": severity if severity in ("info", "warning", "error") else "error",
                "source": source,
                "title": title[:150],
                "message": message[:MAX_MESSAGE_CHARS],
                "count": 1,
                "dismissed": False,
                "dedupe_key": key,
                "data": data or {},
            }
            self._alerts.append(alert)
            self._alerts = self._alerts[-MAX_ALERTS:]
            self._save()

        self._push(alert)
        return alert

    def _push(self, alert: Dict):
        if not self._emit or not self._loop or self._loop.is_closed():
            return
        try:
            payload = {k: v for k, v in alert.items() if k != "last_pushed"}
            coro = self._emit("app_alert", payload)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._loop:
                running.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as e:
            logger.debug(f"Alert push failed: {e}")

    # queries / management

    def list_alerts(self, include_dismissed: bool = False) -> List[Dict]:
        with self._lock:
            alerts = [a for a in self._alerts
                      if include_dismissed or not a.get("dismissed")]
            return [
                {k: v for k, v in a.items() if k != "last_pushed"}
                for a in reversed(alerts)
            ]

    def dismiss(self, alert_id: str) -> bool:
        with self._lock:
            for a in self._alerts:
                if a["id"] == alert_id:
                    a["dismissed"] = True
                    self._save()
                    return True
        return False

    def clear_all(self) -> int:
        with self._lock:
            n = sum(1 for a in self._alerts if not a.get("dismissed"))
            for a in self._alerts:
                a["dismissed"] = True
            self._save()
        return n


class AlertLogHandler(logging.Handler):
    """Turns ERROR/CRITICAL log records into user-visible alerts."""

    def __init__(self, center: AlertCenter):
        super().__init__(level=logging.ERROR)
        self._center = center

    def emit(self, record: logging.LogRecord):
        try:
            if record.name.startswith(EXCLUDED_LOGGER_PREFIXES):
                return
            msg = record.getMessage()
            # Dedupe on logger + first chunk of the message so repeats of
            # the same failure collapse into one alert with a counter.
            self._center.raise_alert(
                severity="error",
                source=record.name,
                title=f"Error in {record.name}",
                message=msg,
                dedupe_key=f"log:{record.name}:{msg[:120]}",
            )
        except Exception:
            # Never let alerting break logging
            pass


# module singleton

_center: Optional[AlertCenter] = None
_log_handler_installed = False


def get_alert_center() -> AlertCenter:
    global _center
    if _center is None:
        _center = AlertCenter()
    return _center


def install_log_capture():
    """Attach the ERROR-level capture handler to the root logger (idempotent)."""
    global _log_handler_installed
    if _log_handler_installed:
        return
    logging.getLogger().addHandler(AlertLogHandler(get_alert_center()))
    _log_handler_installed = True
    logger.info("Application alert capture enabled (ERROR-level logs → alerts)")


def raise_alert(severity: str, source: str, title: str, message: str,
                dedupe_key: Optional[str] = None, data: Optional[Dict] = None):
    """Convenience module-level wrapper."""
    return get_alert_center().raise_alert(
        severity, source, title, message, dedupe_key=dedupe_key, data=data
    )
