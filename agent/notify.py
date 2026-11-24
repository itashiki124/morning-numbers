"""Pushover notification.

Production path: the Sprites Pushover Connector. The Sprite POSTs to
  {PUSHOVER_GATEWAY_URL}/1/messages.json
and the gateway attaches the app token (the Sprite never holds it). The
Pushover *user key* is still sent as `user` on each request, read from env.

Local path (optional, for testing off-Sprite): PUSHOVER_APP_TOKEN +
PUSHOVER_USER_KEY sent straight to api.pushover.net.

If neither is configured, or dry_run is set, nothing is sent: the payload is
returned with secrets masked.
"""
import os
import urllib.error
import urllib.parse
import urllib.request

PUSHOVER_DIRECT = "https://api.pushover.net"
MAX_LEN = 1024  # Pushover message limit


def _mask(v):
    return (v[:3] + "***") if v else None


def build_payload(text, title="Morning numbers"):
    return {
        "user": os.environ.get("PUSHOVER_USER_KEY", ""),
        "title": title,
        "message": text[:MAX_LEN],
        "monospace": "1",
        "priority": "-1",  # quiet: no sound/vibration
    }


def send(text, dry_run=False):
    payload = build_payload(text)
    gateway = os.environ.get("PUSHOVER_GATEWAY_URL", "").rstrip("/")
    token = os.environ.get("PUSHOVER_APP_TOKEN", "")
    if gateway:
        url, mode = gateway + "/1/messages.json", "connector"
    elif token:
        url, mode = PUSHOVER_DIRECT + "/1/messages.json", "direct"
    else:
        url, mode = None, "unconfigured"

    shown = dict(payload, user=_mask(payload["user"]))
    if dry_run or url is None or not payload["user"]:
        return {"sent": False, "mode": mode, "reason": "dry-run" if dry_run else "not configured",
                "url": url, "payload": shown}

    body = dict(payload)
    if mode == "direct":
        body["token"] = token
    req = urllib.request.Request(url, data=urllib.parse.urlencode(body).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        # Do not retry unchanged: Pushover blocks IPs that send repeated 4xx.
        return {"sent": False, "mode": mode, "status": e.code, "reason": "HTTP %d" % e.code,
                "payload": shown}
    except Exception as e:  # noqa: BLE001 - timeouts etc. must not stop the archive step
        return {"sent": False, "mode": mode, "status": None,
                "reason": "%s: %s" % (type(e).__name__, e), "payload": shown}
    return {"sent": 200 <= status < 300, "mode": mode, "status": status, "payload": shown}
