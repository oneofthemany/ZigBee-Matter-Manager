"""
sw.js serves /static cache-first, which is only safe because the /sw.js route
names its cache after a fingerprint of static/: any changed file — a release,
a test deploy, an editor save — must change the fingerprint, so the browser
installs a new worker and drops the old cache.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from harness import Checker

from routes.system_routes import static_asset_hashes, static_fingerprint

REPO = Path(__file__).resolve().parents[2]


def run() -> Checker:
    c = Checker("static fingerprint")
    tmp = Path(tempfile.mkdtemp(prefix="zmm_fp_"))
    try:
        (tmp / "js").mkdir()
        (tmp / "js" / "main.js").write_text("one")
        (tmp / "app.css").write_text("body{}")
        first = static_fingerprint(str(tmp))
        c.check("an unchanged tree gives the same fingerprint", static_fingerprint(str(tmp)) == first)

        (tmp / "js" / "main.js").write_text("two")
        os.utime(tmp / "js" / "main.js", ns=(1, 1))
        edited = static_fingerprint(str(tmp))
        c.check("an edited file changes it", edited != first)

        st = (tmp / "app.css").stat()
        os.utime(tmp / "app.css", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        c.check("a file saved again, same size, changes it", static_fingerprint(str(tmp)) != edited)

        before = static_fingerprint(str(tmp))
        (tmp / "js" / "new.js").write_text("")
        c.check("a new file changes it", static_fingerprint(str(tmp)) != before)
        before = static_fingerprint(str(tmp))
        (tmp / "js" / "new.js").unlink()
        c.check("a removed file changes it", static_fingerprint(str(tmp)) != before)
        c.check("a missing tree is not an error", bool(static_fingerprint(str(tmp / "absent"))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The worker carries a file across deploys only while its content hash holds.
    tmp = Path(tempfile.mkdtemp(prefix="zmm_ah_"))
    try:
        (tmp / "js").mkdir()
        (tmp / "js" / "a.js").write_text("one")
        (tmp / "b.css").write_text("body{}")
        first = static_asset_hashes(str(tmp))
        c.check("every file is listed under its /static path",
                sorted(first) == ["/static/b.css", "/static/js/a.js"], sorted(first))

        st = (tmp / "js" / "a.js").stat()
        os.utime(tmp / "js" / "a.js", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        c.check("a fresh mtime with the same content keeps the hash (a deploy unpack)",
                static_asset_hashes(str(tmp)) == first)

        (tmp / "js" / "a.js").write_text("two")
        after = static_asset_hashes(str(tmp))
        c.check("changed content changes that file's hash", after["/static/js/a.js"] != first["/static/js/a.js"])
        c.check("and only that file's", after["/static/b.css"] == first["/static/b.css"])

        # Same size, mtime put back: only the memo can still answer the old hash.
        st = (tmp / "b.css").stat()
        (tmp / "b.css").write_text("body{x")
        os.utime(tmp / "b.css", ns=(st.st_atime_ns, st.st_mtime_ns))
        c.check("a file whose stat has not moved is not read again",
                static_asset_hashes(str(tmp))["/static/b.css"] == first["/static/b.css"])
        c.check("a missing tree is an empty map", static_asset_hashes(str(tmp / "absent")) == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    c.check("the /sw.js route still finds sw.js's ASSET_HASHES placeholder",
            "var ASSET_HASHES = {};" in (REPO / "static" / "sw.js").read_text()
            and '"var ASSET_HASHES = {};"' in (REPO / "main.py").read_text())

    # main.py's /sw.js route rewrites this line; if it stops matching, every
    # change ships under the old cache name and pages keep the old files.
    sw = (REPO / "static" / "sw.js").read_text()
    route = (REPO / "main.py").read_text()
    pattern = re.search(r're\.sub\(r"([^"]+)"', route)
    c.check("the /sw.js route still finds sw.js's CACHE_NAME",
            pattern is not None and re.search(pattern.group(1), sw) is not None,
            pattern and pattern.group(1))
    return c


if __name__ == "__main__":
    run()
