"""
Serve the bundled docs/*.md as an in-app wiki.

Rendered server-side with markdown-it-py and raw HTML disabled, so shipped docs
cannot inject markup. Slugs are validated against the enumerated on-disk file
list, so there is no path traversal. Screenshot images are excluded from the
image at build time, so those references will not resolve.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger("routes.wiki")

# Resolved relative to the app working dir (/app in the container).
DOCS_DIR = Path("docs")


def _iter_docs():
    """Yield the top-level markdown files under docs/, sorted by name."""
    if not DOCS_DIR.is_dir():
        return
    for p in sorted(DOCS_DIR.glob("*.md")):
        if p.is_file():
            yield p


def _title_for(path: Path) -> str:
    """First ``# `` heading, else a title-cased filename."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.startswith("# "):
                    return s[2:].strip()
                # Stop at the first non-heading, non-blank line.
                if s and not s.startswith("#"):
                    break
    except Exception:
        pass
    return path.stem.replace("_", " ").replace("-", " ").strip().title()


def _render_markdown(text: str) -> str:
    """Markdown → HTML. Prefers markdown-it-py; degrades to escaped <pre>."""
    try:
        from markdown_it import MarkdownIt
        # commonmark baseline + GFM tables/strikethrough. Deliberately NOT the
        # "gfm-like" preset — that enables linkify, which needs the extra
        # linkify-it-py package we don't ship (it raises ModuleNotFoundError).
        # html=False (default) → raw HTML in the source is escaped, not injected.
        md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
        return md.render(text)
    except Exception:
        import html as _html
        return f"<pre class='wiki-raw'>{_html.escape(text)}</pre>"


def register_wiki_routes(app: FastAPI):
    @app.get("/api/wiki", include_in_schema=False)
    async def list_wiki():
        docs = [{"slug": p.stem, "title": _title_for(p)} for p in _iter_docs()]
        docs.sort(key=lambda d: d["title"].lower())
        return JSONResponse({"docs": docs})

    @app.get("/api/wiki/{slug}", include_in_schema=False)
    async def get_wiki(slug: str):
        # Whitelist: only serve a file we actually enumerated (no traversal).
        by_slug = {p.stem: p for p in _iter_docs()}
        path = by_slug.get(slug)
        if path is None:
            raise HTTPException(status_code=404, detail="doc not found")
        text = path.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({
            "slug": slug,
            "title": _title_for(path),
            "html": _render_markdown(text),
        })

    logger.info("Wiki routes registered (/api/wiki, /api/wiki/{slug})")
