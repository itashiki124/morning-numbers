"""Tiny HTTP entrypoint for a Sprite. A request to the Sprite URL wakes it.

  POST /run            fetch -> brief -> Pushover -> archive (GET also accepted)
                       at most once per JST day; /run?force=1 sends again
  GET  /run?dry=1      fetch + brief only: nothing is sent, pushed or archived
  GET  /ask?q=...      explain the latest day-over-day moves from the archive
  GET  /healthz        liveness

Keep the Sprite URL on the default token auth (`sprite url update --auth sprite`)
so strangers cannot trigger notifications. Optionally set RUN_KEY and pass it as
the X-Run-Key header for /run as a second lock.

There is no timer inside: a Sprite sleeps when idle, so the daily run is a
request to /run from outside (the request itself wakes the Sprite).
DRY_RUN=1 in the service env forces every /run to be a dry run.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent import pipeline

_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "hacksgiving-agent/0.1"

    def _send(self, code, body, ctype="application/json"):
        data = (json.dumps(body, indent=2) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        if u.path == "/run":
            key = os.environ.get("RUN_KEY")
            if key and self.headers.get("X-Run-Key") != key:
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
            return self._send(200, pipeline.ask(qs.get("q", [""])[0]) + "\n", "text/plain")
        return self._send(404, {"error": "not found", "routes": ["/run", "/ask?q=", "/healthz"]})

    do_GET = _route
    do_POST = _route

    def log_message(self, fmt, *args):  # path only; never log headers
        sys.stderr.write("%s %s\n" % (self.command, urlparse(self.path).path))


def main():
    port = int(os.environ.get("PORT", "8080"))
    print("listening on :%d" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
