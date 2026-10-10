"""
ntfy / Telegram / Pushover / email delivery (modules/notify_channels.py).

The network is a recording fake; the manager, validation, storage and the
request each service is sent are real.
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path

from harness import Checker

from modules import notify_channels as N

HUB = {**N.HUB_DEFAULTS, "ntfy_server": "https://ntfy.example", "ntfy_token": "NTFYSECRET",
       "telegram_bot_token": "123:BOTSECRET", "pushover_app_token": "APPSECRET",
       "signal_api_url": "http://signal.lan:8080", "signal_number": "+447700900001",
       "smtp_host": "smtp.example", "smtp_from": "zmm@example.com", "smtp_password": "MAILSECRET"}
PUSHOVER_KEY = "u" * 30


class FakeHttp:
    def __init__(self, replies=None, fail=None):
        self.calls = []
        self.replies = replies or {}
        self.fail = fail or {}

    async def __call__(self, method, url, json_body=None, form=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json_body, "form": form,
                           "headers": headers or {}})
        for needle, exc in self.fail.items():
            if needle in url:
                raise exc
        for needle, reply in self.replies.items():
            if needle in url:
                return reply
        if "telegram" in url:
            return 200, {"ok": True, "result": {}}
        if "pushover" in url:
            return 200, {"status": 1}
        return 200, {}

    def to(self, needle):
        return [c for c in self.calls if needle in c["url"]]


def _mgr(tmp, http=None, mail=None, hub=None):
    sent_mail = mail if mail is not None else []
    m = N.ChannelManager(path=Path(tmp) / "nc.json", http=http or FakeHttp(),
                         smtp=lambda hub, to, subj, body: sent_mail.append((to, subj, body)),
                         hub=dict(hub or HUB))
    return m


def _all_on(m, user="alex"):
    m.update(user, {"ntfy": {"enabled": True, "topic": "zmm-abcdef123456"},
                    "pushover": {"enabled": True, "user_key": PUSHOVER_KEY},
                    "email": {"enabled": True, "address": "alex@example.com"}})
    s = m.settings(user)
    s["telegram"] = {"enabled": True, "chat_id": 42, "chat_label": "Alex"}
    s["signal"] = {"enabled": True, "number": "+447700900123"}


def run() -> Checker:
    c = Checker("notify_channels")
    run_ = asyncio.run

    c.section("user settings")
    with tempfile.TemporaryDirectory() as tmp:
        m = _mgr(tmp)
        for bad, what in (({"ntfy": {"topic": "a/b"}}, "a topic with a slash"),
                          ({"pushover": {"user_key": "short"}}, "a malformed Pushover key"),
                          ({"email": {"address": "a@b.com\r\nBcc: x@y.com"}}, "an address carrying a header"),
                          ({"email": {"address": "nope"}}, "a non-address")):
            try:
                m.update("alex", bad)
                c.check(f"{what} is refused", False)
            except ValueError:
                c.check(f"{what} is refused", True)
        s = m.update("alex", {"ntfy": {"enabled": True, "topic": ""}})
        c.check("a channel can't be on without a destination", s["ntfy"]["enabled"] is False, s)
        s = m.update("alex", {"telegram": {"enabled": True, "chat_id": 999}})
        c.check("a client can't set a Telegram chat — only linking does",
                s["telegram"]["chat_id"] is None and not s["telegram"]["enabled"], s)
        c.check("chat messages are off by default, rules and alarms on",
                s["kinds"] == {"notification_rule": True, "message_created": False, "alarm": True}, s["kinds"])
        f = Path(tmp) / "nc.json"
        c.check("the settings file is 0600", stat.S_IMODE(f.stat().st_mode) == 0o600)

        _all_on(m)
        m.save()
        m2 = _mgr(tmp)
        m2.load()
        c.check("a linked Telegram chat and verified Signal number survive a restart",
                m2.settings("alex")["telegram"]["chat_id"] == 42
                and m2.settings("alex")["signal"]["number"] == "+447700900123", m2.settings("alex"))
        s = m.update("alex", {"signal": {"enabled": True, "number": "+447700900999"}})
        c.check("a client can't set a Signal number — only verifying does",
                s["signal"]["number"] == "+447700900123", s["signal"])

        m.update("alex", {"ntfy": {"topic": "short"}})
        c.check("no warning for a short topic on a private ntfy server", m.warnings("alex") == [])
        m.hub["ntfy_server"] = "https://ntfy.sh"
        c.check("a short topic on public ntfy.sh is warned about", len(m.warnings("alex")) == 1)

    c.section("delivery")
    with tempfile.TemporaryDirectory() as tmp:
        http, mail = FakeHttp(), []
        m = _mgr(tmp, http, mail)
        _all_on(m)
        r = run_(m.send_to_user("alex", {"title": "Back door", "body": "opened", "urgent": True},
                                kind="notification_rule"))
        c.check("a rule goes out on every enabled channel",
                sorted(r) == ["email", "ntfy", "pushover", "signal", "telegram"]
                and all(v["ok"] for v in r.values()), r)
        n = http.to("ntfy.example")[0]
        c.check("ntfy gets topic, title, message and high priority for an urgent rule",
                n["json"]["topic"] == "zmm-abcdef123456" and n["json"]["title"] == "Back door"
                and n["json"]["message"] == "opened" and n["json"]["priority"] == 4, n)
        c.check("ntfy gets the hub's access token", n["headers"].get("Authorization") == "Bearer NTFYSECRET")
        t = http.to("telegram")[0]
        c.check("Telegram sends to the linked chat", t["json"]["chat_id"] == 42
                and t["json"]["text"] == "Back door\nopened", t)
        sg = http.to("signal.lan")[0]
        c.check("Signal sends from the hub's number to the user's",
                sg["url"] == "http://signal.lan:8080/v2/send"
                and sg["json"] == {"message": "Back door\nopened", "number": "+447700900001",
                                   "recipients": ["+447700900123"]}, sg)
        p = http.to("pushover")[0]
        c.check("Pushover gets app token, user key and priority 1",
                p["form"]["token"] == "APPSECRET" and p["form"]["user"] == PUSHOVER_KEY
                and p["form"]["priority"] == 1, p)
        c.check("email goes to the user's address with the title as subject",
                mail == [("alex@example.com", "Back door", "opened")], mail)

        http.calls.clear()
        r = run_(m.send_to_user("alex", {"title": "Sam", "body": "hi"}, kind="message_created"))
        c.check("chat messages stay off the channels until opted in", r == {} and not http.calls, r)
        m.update("alex", {"kinds": {"message_created": True}})
        r = run_(m.send_to_user("alex", {"title": "Sam", "body": "hi"}, kind="message_created"))
        c.check("an opted-in user gets chat messages", len(r) == 5, r)
        c.check("a user with no channels gets nothing", run_(m.send_to_user("nobody", {"title": "x"})) == {})

        m.hub["pushover_app_token"] = ""
        r = run_(m.send_to_user("alex", {"title": "x"}))
        c.check("a channel the hub no longer has set up is skipped", "pushover" not in r, r)

    c.section("failures")
    with tempfile.TemporaryDirectory() as tmp:
        http = FakeHttp(replies={"ntfy": (403, "forbidden")},
                        fail={"telegram": RuntimeError("connect failed: https://api.telegram.org/bot123:BOTSECRET/sendMessage")})
        m = N.ChannelManager(path=Path(tmp) / "nc.json", http=http,
                             smtp=lambda *a: (_ for _ in ()).throw(OSError("auth MAILSECRET rejected")),
                             hub=dict(HUB))
        _all_on(m)
        r = run_(m.send_to_user("alex", {"title": "x"}))
        c.check("one channel failing doesn't stop the others", r["pushover"]["ok"] and not r["ntfy"]["ok"], r)
        c.check("a refused ntfy post reports the status", "403" in r["ntfy"]["error"], r["ntfy"])
        c.check("the bot token never appears in a reported error",
                "BOTSECRET" not in r["telegram"]["error"] and "***" in r["telegram"]["error"], r["telegram"])
        c.check("the SMTP password never appears in a reported error",
                "MAILSECRET" not in r["email"]["error"], r["email"])

        async def hang(*a, **k):
            await asyncio.sleep(3600)
        m._http = hang
        saved = N.SEND_TIMEOUT_S
        N.SEND_TIMEOUT_S = -4.9
        try:
            r = run_(m.send_to_user("alex", {"title": "x"}))
        finally:
            N.SEND_TIMEOUT_S = saved
        c.check("a hung service times out instead of holding delivery",
                not r["ntfy"]["ok"] and "no answer" in r["ntfy"]["error"], r)

    c.section("Telegram linking")
    with tempfile.TemporaryDirectory() as tmp:
        http = FakeHttp(replies={"getMe": (200, {"ok": True, "result": {"username": "zmm_bot"}})})
        m = _mgr(tmp, http)
        link = run_(m.telegram_link_start("alex"))
        c.check("linking hands out a deep link carrying a one-time code",
                link["url"] == f"https://t.me/zmm_bot?start={link['code']}" and len(link["code"]) >= 8, link)
        other = {"message": {"text": "/start deadbeef", "chat": {"id": 7, "first_name": "Mallory"}}}
        http.replies["getUpdates"] = (200, {"ok": True, "result": [other]})
        c.check("someone else's /start doesn't link", run_(m.telegram_link_check("alex")) == {"linked": False})
        mine = {"message": {"text": f"/start {link['code']}", "chat": {"id": 42, "first_name": "Alex"}}}
        http.replies["getUpdates"] = (200, {"ok": True, "result": [other, mine]})
        r = run_(m.telegram_link_check("alex"))
        s = m.settings("alex")["telegram"]
        c.check("the chat that sent the code is linked and switched on",
                r == {"linked": True, "chat_label": "Alex"} and s["chat_id"] == 42 and s["enabled"], (r, s))
        try:
            run_(m.telegram_link_check("alex"))
            c.check("a used code can't link again", False)
        except ValueError:
            c.check("a used code can't link again", True)
        http.replies["getUpdates"] = (409, {"ok": False, "description": "Conflict: webhook is active"})
        run_(m.telegram_link_start("alex"))
        try:
            run_(m.telegram_link_check("alex"))
            c.check("a bot with a webhook says why linking can't work", False)
        except ValueError as e:
            c.check("a bot with a webhook says why linking can't work", "webhook" in str(e), str(e))

    c.section("Signal verification")
    with tempfile.TemporaryDirectory() as tmp:
        http = FakeHttp(replies={"signal.lan": (201, {})})
        m = _mgr(tmp, http)
        try:
            run_(m.signal_verify_start("alex", "07700 900123"))
            c.check("a number not in international form is refused", False)
        except ValueError:
            c.check("a number not in international form is refused", True)
        r = run_(m.signal_verify_start("alex", "+44 7700 900123"))
        sent = http.to("signal.lan")[-1]["json"]
        code = sent["message"].rsplit(" ", 1)[-1]
        c.check("a code goes by Signal to the number given, spaces dropped",
                r["number"] == "+447700900123" and sent["recipients"] == ["+447700900123"]
                and len(code) == 6 and code.isdigit(), sent)
        try:
            m.signal_verify_confirm("alex", "000000" if code != "000000" else "111111")
            c.check("a wrong code doesn't verify", False)
        except ValueError:
            c.check("a wrong code doesn't verify", m.settings("alex")["signal"]["number"] == "")
        try:
            m.signal_verify_confirm("bob", code)
            c.check("another user can't use the code", False)
        except ValueError:
            c.check("another user can't use the code", True)
        r = m.signal_verify_confirm("alex", code)
        sig = m.settings("alex")["signal"]
        c.check("the right code verifies the number and switches Signal on",
                r["verified"] and sig == {"enabled": True, "number": "+447700900123"}, sig)

        run_(m.signal_verify_start("sam", "+447700900555"))
        wrong = "000000" if http.to("signal.lan")[-1]["json"]["message"][-6:] != "000000" else "111111"
        msgs = []
        for _ in range(N.SIGNAL_CODE_ATTEMPTS):
            try:
                m.signal_verify_confirm("sam", wrong)
            except ValueError as e:
                msgs.append(str(e))
        c.check("guessing is cut off after a handful of wrong codes", "Too many" in msgs[-1], msgs)

        for _ in range(N.SIGNAL_CODES_PER_WINDOW - 1):
            run_(m.signal_verify_start("sam", "+447700900555"))
        try:
            run_(m.signal_verify_start("sam", "+447700900555"))
            c.check("code sends are rate-limited", False)
        except ValueError as e:
            c.check("code sends are rate-limited", "Too many" in str(e), str(e))

        http.replies["signal.lan"] = (400, {"error": "User +447700900777 is not registered"})
        try:
            run_(m.signal_verify_start("kim", "+447700900777"))
            c.check("a number Signal can't reach says why", False)
        except ValueError as e:
            c.check("a number Signal can't reach says why", "not registered" in str(e), str(e))

    c.section("hub settings")
    with tempfile.TemporaryDirectory() as tmp:
        saved_file = N.SECRETS_FILE
        N.SECRETS_FILE = str(Path(tmp) / "secrets.yaml")
        Path(N.SECRETS_FILE).write_text("fuel_finder:\n  client_id: keep-me\n")
        try:
            m = _mgr(tmp, hub=N.HUB_DEFAULTS)
            m.save_hub({"telegram_bot_token": "123:BOTSECRET", "smtp_host": "smtp.example",
                        "smtp_from": "zmm@example.com", "ntfy_server": "https://ntfy.example/"})
            text = Path(N.SECRETS_FILE).read_text()
            c.check("hub secrets land in the 0600 secrets file beside what was there",
                    "BOTSECRET" in text and "keep-me" in text
                    and stat.S_IMODE(Path(N.SECRETS_FILE).stat().st_mode) == 0o600)
            view = N.hub_public_view(m.hub)
            c.check("the hub view never carries a secret, only that it is set",
                    "BOTSECRET" not in repr(view) and view["telegram_bot_token_set"], view)
            c.check("configured channels become available",
                    view["available"] == ["ntfy", "telegram", "email"], view["available"])
            m.save_hub({"telegram_bot_token": ""})
            c.check("a blank secret keeps the stored one", m.hub["telegram_bot_token"] == "123:BOTSECRET")
            m.save_hub({"telegram_bot_token": None})
            c.check("null clears a secret", m.hub["telegram_bot_token"] == "")
            c.check("a reload reads the saved hub", N.load_hub()["smtp_host"] == "smtp.example")
            for bad, what in (({"ntfy_server": "file:///etc/passwd"}, "a non-http ntfy server"),
                              ({"smtp_port": 70000}, "an out-of-range port"),
                              ({"smtp_security": "maybe"}, "an unknown SMTP mode"),
                              ({"smtp_from": "zmm@example.com\nBcc: x@y"}, "a header in the from address"),
                              ({"signal_api_url": "ftp://signal"}, "a non-http Signal API"),
                              ({"signal_number": "07700900123"}, "a hub number not in international form")):
                try:
                    m.save_hub(bad)
                    c.check(f"{what} is refused", False)
                except ValueError:
                    c.check(f"{what} is refused", True)
        finally:
            N.SECRETS_FILE = saved_file
    return c
