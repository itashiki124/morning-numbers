"""Tiny HTTP entrypoint for a Sprite. A request to the Sprite URL wakes it.

  GET  /               the page: latest brief, "Declined so far: N", a question box
  POST /run            fetch -> brief -> Pushover -> archive (GET also accepted)
                       needs the X-Run-Key header (RUN_KEY), dry runs included;
                       at most once per JST day; /run?force=1 sends again
  GET  /run?dry=1      fetch + brief only: nothing is sent, pushed or archived
  GET  /ask?q=...      the archived numbers only; "this week" lists stored daily values.
                       why / will / should questions are declined in one fixed line,
                       logged to archive/declined.md and committed (?dry=1: not logged).
                       Without the run key the log keeps only the matched words and
                       indicators ("should + BTC-USD"), never the typed text.
  GET  /healthz        liveness

/run fails closed: with no RUN_KEY set it refuses every call. Requests are rate
limited (429) so a flood cannot keep the Sprite awake, and idle connections time
out. Nothing here reads or prints the environment.

There is no timer inside: a Sprite sleeps when idle, so the daily run is a
request to /run from outside (the request itself wakes the Sprite).
DRY_RUN=1 in the service env forces every /run to be a dry run.
"""
import hmac
import html
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent import archive, pipeline

REPO_URL = "https://github.com/itashiki124/morning-numbers"
MAX_Q = 300

_lock = threading.Lock()


class RateLimit(object):
    """Sliding one-minute window, overall and per client."""

    def __init__(self, total=30, per_client=8, window=60.0, clock=time.monotonic):
        self.total, self.per_client, self.window, self.clock = total, per_client, window, clock
        self.all, self.by = deque(), {}
        self.mu = threading.Lock()

    def allow(self, client):
        now = self.clock()
        with self.mu:
            while self.all and now - self.all[0] >= self.window:
                self.all.popleft()
            mine = self.by.setdefault(client, deque())
            while mine and now - mine[0] >= self.window:
                mine.popleft()
            if len(self.all) >= self.total or len(mine) >= self.per_client:
                return False
            self.all.append(now)
            mine.append(now)
            if len(self.by) > 1000:  # forget idle clients
                self.by = {k: v for k, v in self.by.items() if v}
            return True


LIMIT = RateLimit()


def key_ok(given):
    key = os.environ.get("RUN_KEY", "")
    return bool(key) and bool(given) and hmac.compare_digest(given.encode(), key.encode())


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morning Numbers: the uncle with no opinions</title>
<style>
 body{{font:16px/1.5 ui-monospace,Menlo,Consolas,monospace;max-width:46rem;margin:2rem auto;
      padding:0 1rem;background:#fbf8f2;color:#222}}
 h1{{font-size:1.6rem;margin:0}} .sub{{color:#555;margin:.2rem 0 1.5rem}}
 pre{{background:#fff;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}}
 form{{display:flex;gap:.5rem}} input{{flex:1;font:inherit;padding:.5rem}}
 button{{font:inherit;padding:.5rem 1rem}} #a{{min-height:4rem}}
 .n{{font-size:1.2rem}} a{{color:#333}}
</style></head><body>
<h1>The uncle with no opinions</h1>
<p class="sub">No generated words. No guesses. Every morning a Fly.io Sprite commits the
numbers to GitHub, and nothing else.</p>
<p class="n">Declined so far: <b id="n">{count}</b></p>
<form id="f"><input id="q" name="q" maxlength="300" autocomplete="off"
 placeholder="Ask it anything. Try: Should I buy Bitcoin?" aria-label="Your question">
<button>Ask</button></form>
<pre id="a" aria-live="polite">It answers with stored numbers only. Ask it "why" and it declines, on the record.</pre>
<h2>Latest brief</h2>
<pre>{brief}</pre>
<p>Every brief and every refusal is a commit:
<a href="{repo}/commits/main">commits</a> ·
<a href="{repo}/blob/main/archive/declined.md">archive/declined.md</a> ·
<a href="{repo}">source</a></p>
<p class="sub">What you type is not stored. A refusal is logged only as the matched words and
the indicators, for example "should + BTC-USD".</p>
<script>
document.getElementById('f').onsubmit = async function (e) {{
  e.preventDefault();
  var q = document.getElementById('q').value, a = document.getElementById('a');
  a.textContent = '...';
  try {{
    var r = await fetch('/ask?q=' + encodeURIComponent(q));
    a.textContent = await r.text();
    var m = a.textContent.match(/Declined so far: (\\d+)/);
    if (m) document.getElementById('n').textContent = m[1];
  }} catch (err) {{ a.textContent = 'No answer (network).'; }}
}};
</script>
</body></html>
"""


def page():
    brief = archive.latest_brief_text() or "No archived brief yet."
    return PAGE.format(count=archive.declined_count(), brief=html.escape(brief),
                       repo=REPO_URL)


class Handler(BaseHTTPRequestHandler):
    server_version = "hacksgiving-agent/0.2"
    timeout = 15  # seconds; an idle open connection must not keep the Sprite awake

    def _send(self, code, body, ctype="application/json", extra=None):
        data = (json.dumps(body, indent=2) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _client(self):
        fwd = self.headers.get("Fly-Client-IP") or self.headers.get("X-Forwarded-For") or ""
        return fwd.split(",")[0].strip() or self.client_address[0]

    def _route(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        owner = key_ok(self.headers.get("X-Run-Key"))
        if not owner and not LIMIT.allow(self._client()):
            return self._send(429, {"error": "too many requests"}, extra={"Retry-After": "60"})
        if u.path == "/":
            return self._send(200, page(), "text/html")
        if u.path == "/run":
            if not owner:
                return self._send(403, {"error": "forbidden"})
            dry = qs.get("dry", ["0"])[0] in ("1", "true") or os.environ.get("DRY_RUN") == "1"
            force = qs.get("force", ["0"])[0] in ("1", "true")
            if not _lock.acquire(blocking=False):
                return self._send(409, {"error": "a run is already in progress"})
            try:
                return self._send(200, pipeline.run(dry_run=dry, force=force))
            finally:
                _lock.release()
        if u.path == "/ask":
            dry = qs.get("dry", ["0"])[0] in ("1", "true") or os.environ.get("DRY_RUN") == "1"
            q = qs.get("q", [""])[0][:MAX_Q]
            return self._send(200, pipeline.ask(q, dry_run=dry, public=not owner) + "\n",
                              "text/plain")
        return self._send(404, {"error": "not found", "routes": ["/", "/ask?q=", "/healthz"]})

    do_GET = _route
    do_POST = _route

    def log_message(self, fmt, *args):  # path only; never log headers or queries
        sys.stderr.write("%s %s\n" % (self.command, urlparse(self.path).path))


def main():
    port = int(os.environ.get("PORT", "8080"))
    print("listening on :%d" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
