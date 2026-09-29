"""
Build sitemap.xml for the streaks-droughts section with honest per-URL <lastmod>.

A URL's lastmod moves only when that page's content changes. The content hash of
every listed page is kept in sitemap-lastmod.json (committed); each run compares:
  new or changed page -> today's date (UTC)
  unchanged page      -> keeps its stored date
  removed page        -> dropped from the sitemap and the state file
First run (no state file) seeds every page with today's date.

"Public" page = any .html under this folder that is not robots-noindex and whose
<link rel="canonical"> points at itself (redirect stubs like lastgame.html and the
legacy city-era team pages are skipped automatically).

Stdlib only, paths relative to this file, so it runs the same locally and in CI.
"""
import os
import re
import sys
import json
import hashlib
import datetime
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
# Must match build_site.SITE_URL (not imported: that module needs pandas/numpy).
SITE_URL = "https://hoopsmatic.com/streaks-droughts/"
SITEMAP_FILE = os.path.join(ROOT, "sitemap.xml")
STATE_FILE = os.path.join(ROOT, "sitemap-lastmod.json")

SKIP_DIRS = {".git", ".github", "data", "node_modules", "__pycache__"}
NOINDEX_RE = re.compile(r'<meta[^>]+name=["\']robots["\'][^>]*noindex', re.I)
CANON_RE = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.I)


def page_url(rel):
    """players/x.html -> SITE_URL + players/x.html ; index.html -> SITE_URL (dir form)."""
    if rel == "index.html":
        return SITE_URL
    if rel.endswith("/index.html"):
        return SITE_URL + rel[: -len("index.html")]
    return SITE_URL + rel


def public_pages():
    """Yield (url, content_hash) for every public HTML page."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith((".", "_")))
        for fn in sorted(filenames):
            # _-prefixed files are local previews/scratch (see .gitignore)
            if not fn.endswith(".html") or fn.startswith("_"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            with open(path, "rb") as f:
                raw = f.read()
            # Normalise line endings so a Windows (CRLF) build and a Linux CI checkout
            # of the same page hash identically.
            raw = raw.replace(b"\r\n", b"\n")
            text = raw.decode("utf-8", errors="replace")
            if NOINDEX_RE.search(text):
                continue
            url = page_url(rel)
            m = CANON_RE.search(text)
            if m and m.group(1).strip() != url:
                continue   # canonicalises elsewhere -> not its own public URL
            yield url, hashlib.sha256(raw).hexdigest()


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}

    new_state, added, changed = {}, 0, 0
    for url, h in public_pages():
        prev = state.get(url)
        if prev and prev.get("hash") == h:
            new_state[url] = prev
        else:
            new_state[url] = {"hash": h, "lastmod": today}
            if prev:
                changed += 1
            else:
                added += 1
    removed = len(set(state) - set(new_state))

    # Homepage first, then alphabetical: stable output, no churn between runs.
    urls = sorted(new_state, key=lambda u: (u != SITE_URL, u))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        lines.append(f"  <url><loc>{escape(u)}</loc><lastmod>{new_state[u]['lastmod']}</lastmod></url>")
    lines.append("</urlset>")

    with open(SITEMAP_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(STATE_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump({u: new_state[u] for u in urls}, f, indent=1, sort_keys=False)
        f.write("\n")

    print(f"sitemap.xml: {len(urls)} URLs ({added} new, {changed} changed, "
          f"{removed} removed, {len(urls) - added - changed} unchanged)")


if __name__ == "__main__":
    sys.exit(main())
