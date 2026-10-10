"""
Notification channels beyond Web Push: ntfy, Telegram, Signal, Pushover and email.
Web Push needs a trusted HTTPS origin; these reach a person from a hub that is
only on the LAN. See docs/notifications.md §Other channels.

Hub settings (servers, bot/app tokens, SMTP) live in config/secrets.yaml under
`notify_channels`, never config.yaml, which is tracked. Each user's own
destinations live in data/notify_channels.json (0600). Unlike Web Push these
services read the text, so chat messages go out only when the user opts in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("notify_channels")

SECRETS_FILE = os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml")
USERS_PATH = Path("./data/notify_channels.json")

CHANNELS = ("ntfy", "telegram", "signal", "pushover", "email")
# What a user's channels carry unless they choose otherwise.
KIND_DEFAULTS = {"notification_rule": True, "message_created": False}
SEND_TIMEOUT_S = 15
LINK_TTL_S = 600
# Each code costs a message from the hub's number to a stranger's, if mistyped.
SIGNAL_CODES_PER_WINDOW = 3
SIGNAL_CODE_ATTEMPTS = 5

HUB_DEFAULTS: Dict[str, Any] = {
    "ntfy_server": "https://ntfy.sh", "ntfy_token": "",
    "telegram_bot_token": "",
    "signal_api_url": "", "signal_number": "",
    "pushover_app_token": "",
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
    "smtp_username": "", "smtp_password": "", "smtp_from": "",
}
HUB_SECRETS = ("ntfy_token", "telegram_bot_token", "pushover_app_token", "smtp_password")
SMTP_SECURITY = ("starttls", "ssl", "none")

TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")       # E.164
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
# A public ntfy.sh topic is readable by anyone who guesses it.
MIN_PUBLIC_TOPIC = 12

# (method, url, json, form, headers) -> (status, parsed body)
HttpFn = Callable[..., Awaitable[Tuple[int, Any]]]
SmtpFn = Callable[[Dict[str, Any], str, str, str], None]


def _no_newlines(name: str, value: str) -> str:
    # Header injection: every one of these ends up in a header or URL.
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must be a single line")
    return value


# Hub settings

def _read_secrets() -> Dict[str, Any]:
    try:
        import yaml
        with open(SECRETS_FILE, "r") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                # noqa: BLE001
        logger.warning(f"Could not read {SECRETS_FILE}: {e}")
        return {}


def load_hub() -> Dict[str, Any]:
    raw = _read_secrets().get("notify_channels") or {}
    return {k: raw.get(k, v) if raw.get(k) is not None else v for k, v in HUB_DEFAULTS.items()}


def _write_hub(hub: Dict[str, Any]) -> None:
    """Opened 0600 before anything is written, as Blueair's credentials are."""
    import yaml
    path = Path(SECRETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_secrets()
    existing["notify_channels"] = hub
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.dump(existing, fh, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)


def normalise_hub(changes: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """A blank secret keeps the stored one; None clears it."""
    hub = dict(current)
    for k in HUB_DEFAULTS:
        if k not in changes:
            continue
        v = changes[k]
        if k in HUB_SECRETS:
            if v is None:
                hub[k] = ""
            elif str(v).strip():
                hub[k] = _no_newlines(k, str(v).strip())
            continue
        if k == "smtp_port":
            port = int(v or 0)
            if not 1 <= port <= 65535:
                raise ValueError("smtp_port must be 1-65535")
            hub[k] = port
            continue
        hub[k] = _no_newlines(k, str(v or "").strip())

    for key, label in (("ntfy_server", "ntfy server"), ("signal_api_url", "Signal API URL")):
        if hub[key]:
            u = urlparse(hub[key])
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError(f"{label} must be an http(s) URL")
            hub[key] = hub[key].rstrip("/")
    hub["signal_number"] = hub["signal_number"].replace(" ", "")
    if hub["signal_number"] and not PHONE_RE.match(hub["signal_number"]):
        raise ValueError("Signal number must be in international form, e.g. +447700900123")
    if hub["smtp_security"] not in SMTP_SECURITY:
        raise ValueError(f"smtp_security must be one of {list(SMTP_SECURITY)}")
    if hub["smtp_from"] and not EMAIL_RE.match(hub["smtp_from"]):
        raise ValueError("smtp_from must be an email address")
    return hub


def available(hub: Dict[str, Any]) -> List[str]:
    """Channels this hub can send on."""
    out = []
    if hub.get("ntfy_server"):
        out.append("ntfy")
    if hub.get("telegram_bot_token"):
        out.append("telegram")
    if hub.get("signal_api_url") and hub.get("signal_number"):
        out.append("signal")
    if hub.get("pushover_app_token"):
        out.append("pushover")
    if hub.get("smtp_host") and hub.get("smtp_from"):
        out.append("email")
    return out


def hub_public_view(hub: Dict[str, Any]) -> Dict[str, Any]:
    """What the admin settings may show: secrets become `<name>_set`."""
    view = {k: v for k, v in hub.items() if k not in HUB_SECRETS}
    view.update({f"{k}_set": bool(hub.get(k)) for k in HUB_SECRETS})
    view["available"] = available(hub)
    return view


# Per-user settings

def _blank_user() -> Dict[str, Any]:
    return {
        "ntfy": {"enabled": False, "topic": ""},
        "telegram": {"enabled": False, "chat_id": None, "chat_label": ""},
        "signal": {"enabled": False, "number": ""},
        "pushover": {"enabled": False, "user_key": ""},
        "email": {"enabled": False, "address": ""},
        "kinds": dict(KIND_DEFAULTS),
    }


def normalise_user(data: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a partial update. A Telegram chat or Signal number is set only by
    linking or verifying, so a client can't point its notifications at someone
    else's."""
    out = json.loads(json.dumps(current))
    for ch in CHANNELS:
        if isinstance(data.get(ch), dict) and "enabled" in data[ch]:
            out[ch]["enabled"] = bool(data[ch]["enabled"])

    ntfy = data.get("ntfy") or {}
    if "topic" in ntfy:
        topic = str(ntfy["topic"] or "").strip()
        if topic and not TOPIC_RE.match(topic):
            raise ValueError("ntfy topic: letters, digits, '-' and '_' only, up to 64")
        out["ntfy"]["topic"] = topic
    push = data.get("pushover") or {}
    if "user_key" in push:
        key = str(push["user_key"] or "").strip()
        if key and not PUSHOVER_KEY_RE.match(key):
            raise ValueError("Pushover user key is 30 letters and digits")
        out["pushover"]["user_key"] = key
    mail = data.get("email") or {}
    if "address" in mail:
        addr = _no_newlines("address", str(mail["address"] or "").strip())
        if addr and (len(addr) > 254 or not EMAIL_RE.match(addr)):
            raise ValueError("Not an email address")
        out["email"]["address"] = addr
    if (data.get("telegram") or {}).get("unlink"):
        out["telegram"] = {"enabled": False, "chat_id": None, "chat_label": ""}
    if (data.get("signal") or {}).get("unlink"):
        out["signal"] = {"enabled": False, "number": ""}

    kinds = data.get("kinds") or {}
    for k in KIND_DEFAULTS:
        if k in kinds:
            out["kinds"][k] = bool(kinds[k])

    # On means a destination exists to send to.
    for ch, field in (("ntfy", "topic"), ("telegram", "chat_id"), ("signal", "number"),
                      ("pushover", "user_key"), ("email", "address")):
        if out[ch]["enabled"] and not out[ch][field]:
            out[ch]["enabled"] = False
    return out


# Senders

async def _httpx(method: str, url: str, json_body=None, form=None, headers=None) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
        r = await client.request(method, url, json=json_body, data=form, headers=headers)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def _smtplib_send(hub: Dict[str, Any], to: str, subject: str, body: str) -> None:
    import smtplib
    import ssl
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = hub["smtp_from"], to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    host, port = hub["smtp_host"], int(hub["smtp_port"])
    if hub["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_S, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_S)
    with server:
        if hub["smtp_security"] == "starttls":
            server.starttls(context=ctx)
        if hub["smtp_username"]:
            server.login(hub["smtp_username"], hub["smtp_password"])
        server.send_message(msg)


class ChannelManager:
    def __init__(self, path: Path = USERS_PATH, http: Optional[HttpFn] = None,
                 smtp: Optional[SmtpFn] = None, hub: Optional[Dict[str, Any]] = None):
        self.path = path
        self._http = http or _httpx
        self._smtp = smtp or _smtplib_send
        self.hub = hub if hub is not None else load_hub()
        self.users: Dict[str, Dict[str, Any]] = {}
        self._links: Dict[str, Tuple[str, float]] = {}   # user -> (code, expires)
        self._signal_pending: Dict[str, Dict[str, Any]] = {}
        self._signal_sent: Dict[str, List[float]] = {}

    # Storage
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.error("[notify_channels] unreadable %s: %s", self.path, e)
            return
        for user, data in (raw.get("users") or {}).items():
            self.users[user] = normalise_user(data, _blank_user())
            # normalise_user never sets a chat or number; restore the verified ones.
            tg = (data.get("telegram") or {})
            if tg.get("chat_id"):
                self.users[user]["telegram"] = {"enabled": bool(tg.get("enabled")),
                                                "chat_id": tg["chat_id"],
                                                "chat_label": str(tg.get("chat_label") or "")}
            sig = (data.get("signal") or {})
            if PHONE_RE.match(str(sig.get("number") or "")):
                self.users[user]["signal"] = {"enabled": bool(sig.get("enabled")),
                                              "number": sig["number"]}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"users": self.users}, fh, indent=1, ensure_ascii=False)
        tmp.replace(self.path)

    def settings(self, user: str) -> Dict[str, Any]:
        return self.users.get(user) or _blank_user()

    def update(self, user: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self.users[user] = normalise_user(data, self.settings(user))
        self.save()
        return self.users[user]

    def save_hub(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        self.hub = normalise_hub(changes, self.hub)
        _write_hub(self.hub)
        return self.hub

    def warnings(self, user: str) -> List[str]:
        s = self.settings(user)
        out = []
        topic = s["ntfy"]["topic"]
        if (topic and len(topic) < MIN_PUBLIC_TOPIC
                and urlparse(self.hub.get("ntfy_server") or "").hostname == "ntfy.sh"):
            out.append("Your ntfy topic is short. On the public ntfy.sh anyone who "
                       "guesses it can read your notifications; use Generate.")
        return out

    # Delivery
    def _scrub(self, text: str) -> str:
        # httpx puts the URL in its errors, and Telegram's URL carries the token.
        for k in HUB_SECRETS:
            v = self.hub.get(k)
            if v:
                text = text.replace(str(v), "***")
        return text

    async def _ntfy(self, s, title, body, urgent):
        headers = {}
        if self.hub.get("ntfy_token"):
            headers["Authorization"] = f"Bearer {self.hub['ntfy_token']}"
        status, resp = await self._http(
            "POST", self.hub["ntfy_server"],
            json_body={"topic": s["ntfy"]["topic"], "title": title, "message": body,
                       "priority": 4 if urgent else 3, "tags": ["house"]},
            headers=headers)
        if not 200 <= status < 300:
            raise RuntimeError(f"ntfy answered {status}: {resp}")

    async def _telegram(self, s, title, body, urgent):
        status, resp = await self._http(
            "POST", f"https://api.telegram.org/bot{self.hub['telegram_bot_token']}/sendMessage",
            json_body={"chat_id": s["telegram"]["chat_id"],
                       "text": f"{title}\n{body}" if body else title})
        if status != 200 or not (isinstance(resp, dict) and resp.get("ok")):
            desc = resp.get("description") if isinstance(resp, dict) else resp
            raise RuntimeError(f"Telegram answered {status}: {desc}")

    async def _signal_send(self, number: str, text: str) -> None:
        status, resp = await self._http(
            "POST", f"{self.hub['signal_api_url']}/v2/send",
            json_body={"message": text, "number": self.hub["signal_number"],
                       "recipients": [number]})
        if not 200 <= status < 300:
            err = resp.get("error") if isinstance(resp, dict) else resp
            raise RuntimeError(f"Signal API answered {status}: {err}")

    async def _signal(self, s, title, body, urgent):
        await self._signal_send(s["signal"]["number"], f"{title}\n{body}" if body else title)

    async def _pushover(self, s, title, body, urgent):
        status, resp = await self._http(
            "POST", "https://api.pushover.net/1/messages.json",
            form={"token": self.hub["pushover_app_token"], "user": s["pushover"]["user_key"],
                  "title": title, "message": body or title, "priority": 1 if urgent else 0})
        if status != 200 or not (isinstance(resp, dict) and resp.get("status") == 1):
            errs = resp.get("errors") if isinstance(resp, dict) else resp
            raise RuntimeError(f"Pushover answered {status}: {errs}")

    async def _email(self, s, title, body, urgent):
        await asyncio.to_thread(self._smtp, self.hub, s["email"]["address"],
                                _no_newlines("subject", " ".join(title.split())), body or title)

    async def _one(self, ch: str, s, title, body, urgent) -> Dict[str, Any]:
        try:
            await asyncio.wait_for(getattr(self, f"_{ch}")(s, title, body, urgent),
                                   SEND_TIMEOUT_S + 5)
            return {"ok": True}
        except asyncio.TimeoutError:
            logger.warning("[notify_channels] %s timed out", ch)
            return {"ok": False, "error": f"no answer within {SEND_TIMEOUT_S + 5:.0f}s"}
        except Exception as e:                            # noqa: BLE001
            err = self._scrub(str(e) or type(e).__name__)
            logger.warning("[notify_channels] %s failed: %s", ch, err)
            return {"ok": False, "error": err}

    async def send_to_user(self, user: str, payload: Dict[str, Any],
                           kind: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Send on each of the user's enabled channels that carry `kind`
        (None, for a test, means every enabled channel). Never raises."""
        s = self.users.get(user)
        if not s or (kind is not None and not s["kinds"].get(kind, False)):
            return {}
        chans = [ch for ch in available(self.hub) if s[ch]["enabled"]]
        if not chans:
            return {}
        title = str(payload.get("title") or "ZMM")[:250]
        body = str(payload.get("body") or "")[:2000]
        urgent = bool(payload.get("urgent"))
        results = await asyncio.gather(*(self._one(ch, s, title, body, urgent) for ch in chans))
        return dict(zip(chans, results))

    # Telegram linking: the user sends the bot a one-time code, so the chat is
    # one they control.
    async def telegram_link_start(self, user: str) -> Dict[str, Any]:
        if "telegram" not in available(self.hub):
            raise ValueError("Telegram is not set up on this hub")
        status, resp = await self._http(
            "GET", f"https://api.telegram.org/bot{self.hub['telegram_bot_token']}/getMe")
        if status != 200 or not (isinstance(resp, dict) and resp.get("ok")):
            raise ValueError("The hub's Telegram bot token was refused")
        bot = resp["result"].get("username") or ""
        code = secrets.token_hex(4)
        self._links[user] = (code, time.monotonic() + LINK_TTL_S)
        return {"bot": bot, "code": code, "url": f"https://t.me/{bot}?start={code}"}

    async def telegram_link_check(self, user: str) -> Dict[str, Any]:
        pending = self._links.get(user)
        if not pending or pending[1] < time.monotonic():
            self._links.pop(user, None)
            raise ValueError("No link in progress, or it expired; start again")
        code = pending[0]
        try:
            status, resp = await self._http(
                "GET", f"https://api.telegram.org/bot{self.hub['telegram_bot_token']}"
                       f"/getUpdates?offset=-100&allowed_updates=%5B%22message%22%5D")
        except Exception as e:                            # noqa: BLE001
            raise ValueError(self._scrub(str(e))) from e
        if status != 200 or not (isinstance(resp, dict) and resp.get("ok")):
            # 409: the bot has a webhook, so updates never reach getUpdates.
            raise ValueError(f"Telegram answered {status}: "
                             f"{resp.get('description') if isinstance(resp, dict) else resp}")
        for upd in reversed(resp.get("result") or []):
            msg = upd.get("message") or {}
            if (msg.get("text") or "").strip() in (f"/start {code}", code):
                chat = msg.get("chat") or {}
                label = (chat.get("title") or chat.get("username")
                         or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))))
                s = self.settings(user)
                s["telegram"] = {"enabled": True, "chat_id": chat.get("id"), "chat_label": label}
                self.users[user] = s
                self.save()
                self._links.pop(user, None)
                return {"linked": True, "chat_label": label}
        return {"linked": False}


    # Signal verification: a code sent to the number proves the user holds it.
    async def signal_verify_start(self, user: str, number: str) -> Dict[str, Any]:
        if "signal" not in available(self.hub):
            raise ValueError("Signal is not set up on this hub")
        number = str(number or "").replace(" ", "")
        if not PHONE_RE.match(number):
            raise ValueError("Enter the number in international form, e.g. +447700900123")
        now = time.monotonic()
        recent = [t for t in self._signal_sent.get(user, []) if now - t < LINK_TTL_S]
        if len(recent) >= SIGNAL_CODES_PER_WINDOW:
            raise ValueError("Too many codes sent; wait a few minutes")
        code = f"{secrets.randbelow(10**6):06d}"
        try:
            await self._signal_send(number, f"ZMM verification code: {code}")
        except Exception as e:                            # noqa: BLE001
            raise ValueError(self._scrub(str(e))) from e
        self._signal_sent[user] = recent + [now]
        self._signal_pending[user] = {"number": number, "code": code,
                                      "expires": now + LINK_TTL_S, "attempts": 0}
        return {"sent": True, "number": number}

    def signal_verify_confirm(self, user: str, code: str) -> Dict[str, Any]:
        p = self._signal_pending.get(user)
        if not p or p["expires"] < time.monotonic():
            self._signal_pending.pop(user, None)
            raise ValueError("No code pending, or it expired; send a new one")
        p["attempts"] += 1
        if not secrets.compare_digest(str(code or "").strip(), p["code"]):
            if p["attempts"] >= SIGNAL_CODE_ATTEMPTS:
                self._signal_pending.pop(user, None)
                raise ValueError("Too many wrong codes; send a new one")
            raise ValueError("That code doesn't match")
        s = self.settings(user)
        s["signal"] = {"enabled": True, "number": p["number"]}
        self.users[user] = s
        self.save()
        self._signal_pending.pop(user, None)
        return {"verified": True, "number": p["number"]}


_manager: Optional[ChannelManager] = None


def get_channel_manager() -> Optional[ChannelManager]:
    return _manager


def set_channel_manager(m: ChannelManager) -> None:
    global _manager
    _manager = m
