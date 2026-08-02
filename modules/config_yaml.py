"""
Comment-preserving writes into config.yaml.

Why this exists
---------------
The obvious way to persist a setting is safe_load, mutate, dump. It is also
lossy in a way that does not announce itself: PyYAML's emitter has no concept of
comments, so a round-trip silently deletes every one of them. config.yaml is
roughly a third comments — the documentation for every knob in the hub — and
losing that to persist one boolean is not a trade worth making, least of all
when nobody notices until they next go looking for an explanation.

So this edits the file as text: it finds a top-level block, rewrites the values
of the keys it was given, and leaves every other byte — comments, ordering,
blank lines, indentation style, keys it has never heard of — exactly as found.

Scope
-----
Deliberately small. It handles top-level blocks one level deep, which is the
shape of every setting the UI persists. It is not a YAML editor: nested maps,
lists and anchors are out of range, and a caller needing those should be reading
and writing its own file rather than growing this. Anything it cannot place is
appended as a new block rather than guessed at.

A write goes through a temporary file in the same directory and is renamed into
place, so an interrupted save cannot leave the hub with half a config.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("modules.config_yaml")

DEFAULT_INDENT = "  "

#: Strings matching this are emitted bare, as a hand-written file would have
#: them. Everything else is quoted — URLs (the "://" would need thinking about),
#: anything with spaces or punctuation, and anything empty. Conservative on
#: purpose: a needlessly quoted string is ugly, an unquoted one that reparses as
#: something else is a bug.
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")

#: Bare words YAML reads as something other than a string.
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "none", "~"}

#: Trailing comment on a key line, kept when the value is rewritten: it usually
#: documents the setting, not the particular value.
_KEY_RE = r"^(?P<indent>\s+){key}:(?P<rest>\s*)(?P<value>.*?)(?P<comment>\s+#.*)?$"


def _scalar(v: Any) -> str:
    """Render a Python scalar as YAML."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if _PLAIN_SAFE_RE.match(s) and s.lower() not in _RESERVED:
        return s
    return "'" + s.replace("'", "''") + "'"


def _block_bounds(lines, name):
    """(start, end) line indices of a top-level block, or None if absent."""
    head = re.compile(rf"^{re.escape(name)}:\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return None
    end = start + 1
    # An indented or blank line continues the block. Anything else at column
    # zero starts the next top-level key and ends it.
    while end < len(lines) and re.match(r"^(\s+\S|\s*$)", lines[end]):
        end += 1
    # Trailing blank lines belong to the gap before the next key, not to the
    # block — inserting after them would land a key outside its own parent.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def update_block(path: Path, name: str, values: Mapping[str, Any],
                 comments: Optional[Dict[str, str]] = None,
                 block_comment: Optional[str] = None) -> None:
    """
    Write `values` into the top-level `name:` block of a YAML file.

    Keys already present are rewritten in place, keeping their indentation and
    any trailing comment. Keys not present are appended to the block. Keys in
    the file but not in `values` are left alone — this updates, it does not
    replace, so a setting this caller has never heard of survives contact
    with it.

    `comments` supplies a leading comment for keys being added for the first
    time; `block_comment` does the same for the block itself. Neither is
    applied to anything that already exists, since the file's own wording wins.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    comments = comments or {}

    bounds = _block_bounds(lines, name)
    if bounds is None:
        lines = _append_block(lines, name, values, comments, block_comment)
    else:
        start, end = bounds
        indent = _block_indent(lines, start, end)
        for key, value in values.items():
            lines, end = _set_key(lines, start, end, indent, key, value,
                                  comments.get(key))

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)


def _block_indent(lines, start, end) -> str:
    """The block's own indent, taken from its first key rather than assumed."""
    for i in range(start + 1, end):
        m = re.match(r"^(\s+)\S", lines[i])
        if m:
            return m.group(1)
    return DEFAULT_INDENT


def _set_key(lines, start, end, indent, key, value, comment):
    """Rewrite or insert one key inside a block. Returns (lines, new_end)."""
    pattern = re.compile(_KEY_RE.format(key=re.escape(key)))
    for i in range(start + 1, end):
        m = pattern.match(lines[i].rstrip("\n"))
        # Only keys at the block's own depth: a deeper one belongs to a nested
        # map, and rewriting it would move a value into the wrong parent.
        if m and m.group("indent") == indent:
            lines[i] = (f"{indent}{key}:{m.group('rest') or ' '}"
                        f"{_scalar(value)}{m.group('comment') or ''}\n")
            return lines, end

    new = []
    if comment:
        new += [f"{indent}# {c}\n" for c in comment.split("\n")]
    new.append(f"{indent}{key}: {_scalar(value)}\n")
    lines[end:end] = new
    return lines, end + len(new)


def _append_block(lines, name, values, comments, block_comment):
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    out = list(lines)
    if out and out[-1].strip():
        out.append("\n")
    if block_comment:
        out += [f"# {c}\n" for c in block_comment.split("\n")]
    out.append(f"{name}:\n")
    for key, value in values.items():
        c = comments.get(key)
        if c:
            out += [f"{DEFAULT_INDENT}# {line}\n" for line in c.split("\n")]
        out.append(f"{DEFAULT_INDENT}{key}: {_scalar(value)}\n")
    return out
