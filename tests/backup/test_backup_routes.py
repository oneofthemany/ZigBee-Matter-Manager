"""
Backup routes over real FastAPI: a download restores, an encrypted backup
restores with its passphrase, and a hostile zip can't write outside the
manifest. Skipped without FastAPI.
"""

from __future__ import annotations

import io
import stat
import tempfile
import zipfile
from pathlib import Path

from harness import Checker

import test_backup as T


def run() -> Checker:
    c = Checker("backup_routes")
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n  skipped (fastapi not installed)")
        return c

    from modules import backup as B
    from modules.auth import User
    from modules.auth_middleware import Principal
    import routes.backup_routes as R

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        app_dir = T._app(tmp)
        saved = (B.APP_DIR, R.APP_DIR, B.SECRETS_FILE)
        B.APP_DIR = R.APP_DIR = str(app_dir)
        B.SECRETS_FILE = str(tmp / "sched-secrets.yaml")
        sched = B.BackupScheduler(schedule_path=tmp / "sched.json", status_path=tmp / "status.json",
                                  app_dir=str(app_dir))
        B.set_backup_scheduler(sched)
        try:
            app = FastAPI()

            @app.middleware("http")
            async def sign_in(request: Request, call_next):
                user = request.headers.get("X-User")
                if user:
                    scopes = {"admin"} if user == "root" else {"system:read"}
                    request.state.principal = Principal(User(username=user), scopes, auth_method="cookie")
                return await call_next(request)

            R.register_backup_routes(app, lambda: None)
            api = TestClient(app)
            root, alex = {"X-User": "root"}, {"X-User": "alex"}

            c.section("download and restore")
            r = api.get("/api/backup/create?include_telemetry=false", headers=root)
            c.check("a download is a zip with the manifest's files",
                    r.status_code == 200 and "zmm_backup_" in r.headers["content-disposition"]
                    and "config/secrets.yaml" in zipfile.ZipFile(io.BytesIO(r.content)).namelist(), r.headers)
            c.check("the temp copy is removed after sending",
                    not list(Path(tempfile.gettempdir()).glob("zmm-backup-*")))
            (app_dir / "data/alarm.json").unlink()
            (app_dir / "config/secrets.yaml").write_text("changed: true\n")
            rr = api.post("/api/backup/restore", headers=root,
                          files={"file": ("backup.zip", r.content, "application/zip")}).json()
            c.check("restoring it brings the files back", rr["success"] and (app_dir / "data/alarm.json").exists()
                    and "hunter2" in (app_dir / "config/secrets.yaml").read_text(), rr.get("errors"))
            c.check("secrets and PIN hashes are restored owner-only",
                    stat.S_IMODE((app_dir / "config/secrets.yaml").stat().st_mode) == 0o600
                    and stat.S_IMODE((app_dir / "data/alarm.json").stat().st_mode) == 0o600)

            c.section("encrypted backups")
            B.encrypt_file(_bytes_to(tmp / "plain.zip", r.content), tmp / "b.zip.enc", "correct horse")
            enc = (tmp / "b.zip.enc").read_bytes()
            (app_dir / "data/alarm.json").unlink()

            def restore(passphrase=None):
                data = {"passphrase": passphrase} if passphrase else {}
                return api.post("/api/backup/restore", headers=root, data=data,
                                files={"file": ("b.zip.enc", enc, "application/octet-stream")}).json()
            rr = restore()
            c.check("an encrypted backup asks for its passphrase", not rr["success"] and rr["needs_passphrase"], rr)
            rr = restore("wrong")
            c.check("a wrong passphrase restores nothing",
                    not rr["success"] and not (app_dir / "data/alarm.json").exists(), rr)
            rr = restore("correct horse")
            c.check("the right one restores", rr["success"] and (app_dir / "data/alarm.json").exists(), rr)

            c.section("a hostile zip")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("backup_manifest.json", "{}")
                zf.writestr("../escaped.txt", "x")
                zf.writestr("main.py", "print('owned')")
                zf.writestr("data/automations.json", '["ok"]')
            rr = api.post("/api/backup/restore", headers=root,
                          files={"file": ("evil.zip", buf.getvalue(), "application/zip")}).json()
            c.check("only manifest paths are written: no traversal, no code",
                    rr["restored"] == ["data/automations.json"] and not (tmp / "escaped.txt").exists()
                    and not (app_dir / "main.py").exists(), rr["restored"])

            c.section("schedule API")
            c.check("only an admin reads or changes the schedule",
                    api.get("/api/backup/schedule", headers=alex).status_code == 403
                    and api.put("/api/backup/schedule", headers=alex, json={"enabled": True}).status_code == 403)
            r = api.put("/api/backup/schedule", headers=root,
                        json={"enabled": True, "encrypt": True, "passphrase": "s3cret-pass"})
            c.check("an admin saves it and the passphrase doesn't come back",
                    r.status_code == 200 and r.json()["passphrase_set"] and "s3cret-pass" not in r.text, r.text)
            c.check("a bad schedule is a 400",
                    api.put("/api/backup/schedule", headers=root, json={"time": "soon"}).status_code == 400)
            r = api.post("/api/backup/schedule/run", headers=root).json()
            c.check("run now makes a backup and reports it", r["success"] and r["status"]["last_file"].endswith(".zip.enc"), r)
            st = api.get("/api/backup/status", headers=alex).json()
            c.check("anyone with system:read sees the last result, not the keys",
                    st["enabled"] and st["last_file"] and "passphrase" not in st, st)
        finally:
            B.APP_DIR, R.APP_DIR, B.SECRETS_FILE = saved
            B.set_backup_scheduler(None)
    return c


def _bytes_to(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
