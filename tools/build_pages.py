"""
tools/build_pages.py — the dashboard as a static site for GitHub Pages.

    python tools/build_pages.py [out_dir]            (default: _site)

Copies dashboard/ unchanged, adds pages/replay.json (tools/record_replay.py),
and switches both pages to replay mode: window.SCW_REPLAY is set, and the
backend-rooted URLs (/static/..., /db, /) become relative so the site works
under https://<user>.github.io/<repo>/. The backend-served dashboard is not
affected. .github/workflows/pages.yml runs this on every push to master.
"""
from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dashboard")
REC = os.path.join(ROOT, "pages", "replay.json")
INJECT = '<script>window.SCW_REPLAY = "replay.json";</script>'
REWRITES = {
    "index.html": [('href="/db"', 'href="db.html"')],
    "db.html": [('href="/"', 'href="./"')],
}

out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_site"))
if not os.path.exists(REC):
    sys.exit("pages/replay.json is missing -- run tools/record_replay.py first")
if os.path.exists(out):
    shutil.rmtree(out)
shutil.copytree(SRC, out)
shutil.copy2(REC, os.path.join(out, "replay.json"))
open(os.path.join(out, ".nojekyll"), "w").close()

for name, extra in REWRITES.items():
    path = os.path.join(out, name)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace('"/static/', '"./')
    for old, new in extra:
        html = html.replace(old, new)
    html = html.replace("<head>", "<head>\n" + INJECT, 1)
    left = re.findall(r'(?:href|src)="/[^"]*"|"/static/', html)
    if INJECT not in html or left:
        sys.exit("%s: replay flag not injected or backend-rooted URLs left: %s" % (name, left))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

size = sum(os.path.getsize(os.path.join(d, n)) for d, _, ns in os.walk(out) for n in ns)
print("built %s (%.1f MB)" % (out, size / 1e6))
