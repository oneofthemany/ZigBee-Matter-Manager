"""
Backups (modules/backup.py): what goes in, encryption, retention, the three
targets and the nightly schedule. Real zip and crypto; WebDAV and S3 answer
from a mock transport.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from harness import Checker

from modules import backup as B


def _app(tmp: Path) -> Path:
    app = tmp / "app"
    for rel, text in (("config/config.yaml", "mqtt: {}\n"), ("config/secrets.yaml", "blueair: {password: hunter2}\n"),
                      ("data/zigbee.db", "db"), ("data/automations.json", "[]"), ("data/alarm.json", "{}"),
                      ("data/workers.json", "{}"), ("data/vapid.json", "{}"), ("data/telemetry.duckdb", "T" * 100),
                      ("data/certs/cert.pem", "cert"), ("data/backups/old.zip", "must not be backed up"),
                      ("data/logbook.duckdb", "history")):
        f = app / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return app


def run() -> Checker:
    c = Checker("backup")

    c.section("what goes in")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app = _app(tmp)
        meta = B.build_zip(tmp / "b.zip", False, 7, str(app))
        names = zipfile.ZipFile(tmp / "b.zip").namelist()
        c.check("config, the device database and the newer data files are in",
                {"config/config.yaml", "data/zigbee.db", "data/alarm.json", "data/workers.json", "data/vapid.json"} <= set(names), names)
        c.check("integration credentials are in — a restore shouldn't log everything out",
                "config/secrets.yaml" in names)
        c.check("directories come along", "data/certs/cert.pem" in names)
        c.check("history is left out of a config backup", "data/telemetry.duckdb" not in names)
        c.check("earlier backups aren't backed up into the next", not any("backups" in n for n in names))
        c.check("the manifest counts files and names what was missing",
                meta["included"] == len(names) - 1 and "data/names.json" in meta["skipped"] and meta["device_count"] == 7, meta)
        B.build_zip(tmp / "full.zip", True, 0, str(app))
        c.check("a full backup adds the history databases",
                "data/telemetry.duckdb" in zipfile.ZipFile(tmp / "full.zip").namelist())
        c.check("restore accepts manifest paths and refuses anything else",
                B.entry_allowed("data/alarm.json") and B.entry_allowed("data/certs/key.pem")
                and not B.entry_allowed("../etc/passwd") and not B.entry_allowed("/etc/passwd")
                and not B.entry_allowed("main.py") and not B.entry_allowed("data/../main.py"))

        c.section("encryption")
        B.encrypt_file(tmp / "b.zip", tmp / "b.enc", "correct horse")
        enc = (tmp / "b.enc").read_bytes()
        c.check("an encrypted backup is marked, and isn't a readable zip",
                B.is_encrypted(enc) and not zipfile.is_zipfile(tmp / "b.enc") and b"config.yaml" not in enc)
        c.check("it is written owner-only", stat.S_IMODE((tmp / "b.enc").stat().st_mode) == 0o600)
        c.check("the right passphrase gives back the same zip",
                B.decrypt_bytes(enc, "correct horse") == (tmp / "b.zip").read_bytes())
        for label, data, pw in (("a wrong passphrase", enc, "wrong"),
                                ("a damaged file", enc[:-20] + bytes(20), "correct horse"),
                                ("a truncated file", enc[:30], "correct horse")):
            try:
                B.decrypt_bytes(data, pw)
                c.check(f"{label} is refused", False)
            except ValueError:
                c.check(f"{label} is refused", True)
        B.encrypt_file(tmp / "b.zip", tmp / "b2.enc", "correct horse")
        c.check("the same backup encrypts differently each time (fresh salt and nonce)",
                (tmp / "b2.enc").read_bytes() != enc)

    c.section("retention")
    def name(day, hour=3):
        return f"zmm_backup_202610{day:02d}_{hour:02d}3000_config.zip"
    # Oct 2026: 1st is a Thursday. Days 1-20, plus a second run on the 20th.
    names = [name(d) for d in range(1, 21)] + [name(20, 15)]
    gone = set(B.to_delete(names + ["holiday.zip", "zmm_backup_notes.txt"], keep_daily=7, keep_weekly=4))
    kept = sorted(set(names) - gone)
    c.check("the last 7 days each keep their newest backup",
            all(name(d) in kept for d in range(14, 20)) and name(20, 15) in kept and name(20) in gone, kept)
    c.check("older weeks keep one each, the newest of the week",
            name(11) in kept and name(4) in kept and name(10) in gone and name(3) in gone, kept)
    c.check("files it didn't name are never deleted", not {"holiday.zip", "zmm_backup_notes.txt"} & gone)
    c.check("with one backup, it is kept", B.to_delete([name(1)], 7, 4) == [])
    c.check("encrypted and full backups count too",
            B.to_delete(["zmm_backup_20261001_033000_full.zip.enc", "zmm_backup_20261002_033000_full.zip.enc"], 1, 0)
            == ["zmm_backup_20261001_033000_full.zip.enc"])

    c.section("S3 signing")
    now = datetime(2013, 5, 24, tzinfo=timezone.utc)
    ak, sk = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    h = B.sigv4("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                B.S3Target.EMPTY, "us-east-1", ak, sk, now)
    c.check("matches AWS's published signature for GET object",
            h["Authorization"].endswith("f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
    h = B.sigv4("GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", {},
                B.S3Target.EMPTY, "us-east-1", ak, sk, now)
    c.check("and for a listing with a query string",
            h["Authorization"].endswith("34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))

    async def targets():
        c.section("targets")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            f = tmp / "src.zip"
            f.write_bytes(b"zipdata" * 1000)
            local = B.LocalTarget("data/backups", str(tmp))
            await local.upload(f, name(1))
            c.check("a folder target writes the file, owner-only, with no partial left behind",
                    await local.list() == [name(1)]
                    and stat.S_IMODE((tmp / "data/backups" / name(1)).stat().st_mode) == 0o600)
            await local.delete(name(1))
            c.check("and deletes it", await local.list() == [])
            try:
                await B.LocalTarget("/proc/nope/backups").upload(f, name(1))
                c.check("an unwritable folder says so", False)
            except B.TargetError as e:
                c.check("an unwritable folder says so", "can't write" in str(e), str(e))

            try:
                import httpx
            except ImportError:
                print("    skipped WebDAV/S3 (httpx not installed)")
                return
            store, seen = {}, []

            def dav(req: httpx.Request) -> httpx.Response:
                seen.append((req.method, req.url.path, req.headers.get("authorization", "")[:6]))
                key = req.url.path.rsplit("/", 1)[-1]
                if req.headers.get("authorization", "") == "":
                    return httpx.Response(401)
                if req.method == "PUT":
                    store[key] = req.read()
                    return httpx.Response(201)
                if req.method == "PROPFIND":
                    hrefs = "".join(f"<d:response><d:href>/dav/zmm/{k}</d:href></d:response>" for k in store)
                    return httpx.Response(207, text=f'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/zmm/</d:href></d:response>{hrefs}<d:response><d:href>/dav/zmm/photo.jpg</d:href></d:response></d:multistatus>')
                if req.method == "DELETE":
                    return httpx.Response(204 if store.pop(key, None) is not None else 404)
                return httpx.Response(405)
            cx = httpx.AsyncClient(transport=httpx.MockTransport(dav))
            w = B.WebDavTarget("https://nas.example/dav/zmm/", "zmm", "pw", client=cx)
            await w.upload(f, name(2))
            c.check("WebDAV uploads the whole file with the credentials",
                    store[name(2)] == f.read_bytes() and seen[0][2] == "Basic ", seen[0])
            c.check("its listing returns only our backups", await w.list() == [name(2)])
            await w.delete(name(2))
            await w.delete(name(2))
            c.check("deleting something already gone isn't an error", store == {})
            try:
                await B.WebDavTarget("https://nas.example/dav/zmm", client=cx).list()
                c.check("refused credentials say so", False)
            except B.TargetError as e:
                c.check("refused credentials say so", "username or password" in str(e), str(e))
            try:
                B.WebDavTarget("ftp://nas/x")
                c.check("a non-http WebDAV URL is refused", False)
            except B.TargetError:
                c.check("a non-http WebDAV URL is refused", True)

            objects, s3seen = {}, []

            def s3(req: httpx.Request) -> httpx.Response:
                s3seen.append(req)
                if "AWS4-HMAC-SHA256 Credential=AK/" not in req.headers.get("authorization", ""):
                    return httpx.Response(403)
                key = req.url.path[len("/bucket/"):]
                if req.method == "PUT":
                    objects[key] = req.read()
                    return httpx.Response(200)
                if req.method == "GET":
                    keys = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in objects) + "<Contents><Key>hub/other.txt</Key></Contents>"
                    return httpx.Response(200, text=f"<ListBucketResult>{keys}</ListBucketResult>")
                if req.method == "DELETE":
                    objects.pop(key, None)
                    return httpx.Response(204)
                return httpx.Response(405)
            cx3 = httpx.AsyncClient(transport=httpx.MockTransport(s3))
            t3 = B.S3Target("https://s3.example", "bucket", "AK", "SK", "eu-west-2", "hub", client=cx3)
            await t3.upload(f, name(3))
            put = s3seen[0]
            import hashlib
            c.check("S3 uploads under the prefix, signed, with the payload's hash",
                    objects == {f"hub/{name(3)}": f.read_bytes()}
                    and put.headers["x-amz-content-sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
                    and "eu-west-2/s3/aws4_request" in put.headers["authorization"], dict(put.headers))
            c.check("its listing is asked for the prefix and returns only our backups",
                    await t3.list() == [name(3)] and b"prefix=hub%2F" in s3seen[-1].url.query, s3seen[-1].url)
            await t3.delete(name(3))
            c.check("and deletes", objects == {})
            for bad in (dict(endpoint="s3.example", bucket="b"), dict(endpoint="https://s3.example", bucket=""),
                        dict(endpoint="https://s3.example", bucket="b", secret_key="")):
                try:
                    B.S3Target(**{"endpoint": "", "bucket": "", "access_key": "AK", "secret_key": "SK", **bad})
                    c.check(f"a bad S3 target {bad} is refused", False)
                except B.TargetError:
                    c.check(f"a bad S3 target {bad} is refused", True)

    async def schedule():
        c.section("schedule")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            app = _app(tmp)
            saved = B.SECRETS_FILE
            B.SECRETS_FILE = str(tmp / "secrets.yaml")
            clock = {"t": datetime(2026, 10, 10, 2, 0).timestamp()}
            alerts, resolved, prepared = [], [], []

            async def prepare(include):
                prepared.append(include)
                return 12
            s = B.BackupScheduler(prepare=prepare, schedule_path=tmp / "sched.json", status_path=tmp / "status.json",
                                  clock=lambda: clock["t"], app_dir=str(app),
                                  alert=lambda *a, **k: alerts.append((a, k)), resolve=resolved.append)
            try:
                c.check("off by default: nothing is due", not s.due() and s.next_run() is None)
                for bad, what in (({"time": "25:00"}, "a bad time"), ({"keep_daily": 0}, "keeping zero days"),
                                  ({"target": {"type": "ftp"}}, "an unknown target"),
                                  ({"encrypt": True}, "encryption without a passphrase"),
                                  ({"target": {"type": "s3", "endpoint": "https://s3.example", "bucket": "b"}}, "S3 without keys")):
                    try:
                        s.update(bad)
                        c.check(f"{what} is refused", False)
                    except ValueError:
                        c.check(f"{what} is refused", True)
                pub = s.update({"enabled": True, "time": "03:30", "encrypt": True, "passphrase": "correct horse",
                                "keep_daily": 2, "keep_weekly": 0})
                c.check("the passphrase is kept in the secrets file and never returned",
                        pub["passphrase_set"] and "correct horse" not in json.dumps(pub)
                        and "correct horse" not in (tmp / "sched.json").read_text()
                        and "correct horse" in Path(B.SECRETS_FILE).read_text())
                c.check("before 03:30 nothing is due, and the next run is today's", not s.due()
                        and datetime.fromtimestamp(s.next_run()).strftime("%d %H:%M") == "10 03:30")
                clock["t"] = datetime(2026, 10, 10, 9, 15).timestamp()
                c.check("a hub that was off at 03:30 is due when it comes back", s.due())

                st = await s.run()
                files = sorted(f for f in (app / "data/backups").iterdir() if f.name.startswith("zmm_"))
                c.check("a run leaves one encrypted backup in the folder",
                        len(files) == 1 and files[0].name.endswith("_config.zip.enc") and st["last_error"] is None, st)
                inner = zipfile.ZipFile(io.BytesIO(B.decrypt_bytes(files[0].read_bytes(), "correct horse")))
                c.check("which decrypts to a zip with its manifest and the device count",
                        json.loads(inner.read("backup_manifest.json"))["device_count"] == 12 and prepared == [False])
                c.check("the status records when, what, how big and where",
                        st["last_success"] == clock["t"] and st["last_file"] == files[0].name
                        and st["last_size"] == files[0].stat().st_size and "backups" in st["target"], st)
                c.check("and survives a restart", json.loads((tmp / "status.json").read_text())["last_file"] == files[0].name)
                c.check("it isn't due again today, and the next run is tomorrow's", not s.due()
                        and datetime.fromtimestamp(s.next_run()).strftime("%d %H:%M") == "11 03:30")
                c.check("a success clears earlier backup alerts", "backup:failed" in resolved)

                for day in (11, 12):
                    clock["t"] = datetime(2026, 10, day, 3, 31).timestamp()
                    await s.run()
                left = sorted(f.name[11:19] for f in (app / "data/backups").iterdir() if f.name.startswith("zmm_"))
                c.check("retention prunes to the days kept, leaving other files alone",
                        left == ["20261011", "20261012"] and (app / "data/backups/old.zip").exists(), left)

                c.check("the target test writes, lists and removes a probe",
                        (await s.test_target())["success"] and len(list((app / "data/backups").iterdir())) == 3)

                s.config["target"]["path"] = "/proc/nope"
                clock["t"] = datetime(2026, 10, 13, 3, 31).timestamp()
                st = await s.run()
                c.check("a failed run is recorded, keeps the last success, and raises an alert",
                        "can't write" in st["last_error"] and st["last_file"].startswith("zmm_backup_20261012")
                        and alerts and alerts[-1][1]["dedupe_key"] == "backup:failed", (st, alerts[-1:]))
                c.check("and isn't retried all day", not s.due())
                c.check("the temp copy is cleaned up either way",
                        not list(Path(tempfile.gettempdir()).glob("zmm-backup-*")))
                alerts.clear()
                clock["t"] = datetime(2026, 10, 15, 12, 0).timestamp()
                s.check_stale()
                c.check("backups going stale raise a warning", alerts and alerts[0][0][0] == "warning"
                        and alerts[0][1]["dedupe_key"] == "backup:stale", alerts)
            finally:
                B.SECRETS_FILE = saved

    asyncio.run(targets())
    asyncio.run(schedule())
    return c
