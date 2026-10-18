"""
config/secrets.yaml: credentials kept out of the git-tracked config.yaml.

One top-level section per integration. Where the environment overrides a
value, the owning module decides that; this module only reads and writes the
file. A write replaces one section, atomically, with the file 0600 before any
byte lands, and refuses to touch a file that does not parse: rewriting it from
nothing would drop every other integration's secrets.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("modules.secrets_file")

_lock = threading.Lock()


def path() -> Path:
    """Read per call, so a test or container can point ZMM_SECRETS_FILE elsewhere."""
    return Path(os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml"))


class SecretsUnreadable(Exception):
    """The file exists but cannot be parsed; it must not be overwritten."""


def _load(file: Path) -> Dict[str, Any]:
    import yaml
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise SecretsUnreadable(f"{file} does not parse: {e}") from e
    if not isinstance(data, dict):
        raise SecretsUnreadable(f"{file} is not a mapping")
    return data


def read_section(name: str) -> Dict[str, Any]:
    """One integration's section, or {} when the file or section is absent or unreadable."""
    try:
        section = _load(path()).get(name) or {}
    except (SecretsUnreadable, OSError) as e:
        logger.warning(f"Could not read secrets: {e}")
        return {}
    return section if isinstance(section, dict) else {}


def write_section(name: str, values: Dict[str, Any]) -> None:
    """Replace one section, keeping every other. Raises SecretsUnreadable
    rather than overwrite a file it cannot parse."""
    import yaml
    file = path()
    with _lock:
        data = _load(file)
        data[name] = values
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".secrets.", suffix=".tmp")
        try:
            # mkstemp creates 0600, so the secret is never briefly world-readable.
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
            os.replace(tmp, file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
