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
from typing import Any, Dict, Optional, Union

logger = logging.getLogger("modules.secrets_file")

_lock = threading.Lock()


def path() -> Path:
    """Read per call, so a test or container can point ZMM_SECRETS_FILE elsewhere."""
    return Path(os.environ.get("ZMM_SECRETS_FILE", "./config/secrets.yaml"))


class SecretsUnreadable(ValueError):
    """The file exists but cannot be parsed, so it must not be overwritten. A
    ValueError so every save route already reports its message to the user."""


def _load(file: Path) -> Dict[str, Any]:
    import yaml
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise SecretsUnreadable(f"{file} does not parse; fix it before saving credentials "
                                f"(nothing was changed): {e}") from e
    if not isinstance(data, dict):
        raise SecretsUnreadable(f"{file} is not a YAML mapping; fix it before saving "
                                f"credentials (nothing was changed)")
    return data


FileArg = Optional[Union[str, os.PathLike]]


def _file(file: FileArg) -> Path:
    # A module may keep its own SECRETS_FILE (tests repoint it); else the env.
    return Path(file) if file is not None else path()


def read_section(name: str, file: FileArg = None) -> Dict[str, Any]:
    """One integration's section, or {} when the file or section is absent or unreadable."""
    try:
        section = _load(_file(file)).get(name) or {}
    except (SecretsUnreadable, OSError) as e:
        logger.warning(f"Could not read secrets: {e}")
        return {}
    return section if isinstance(section, dict) else {}


def write_section(name: str, values: Dict[str, Any], file: FileArg = None) -> None:
    """Replace one section, keeping every other. Raises SecretsUnreadable
    rather than overwrite a file it cannot parse."""
    import yaml
    file = _file(file)
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
