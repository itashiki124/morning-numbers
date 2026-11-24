"""Daily archive.

Local: archive/YYYY-MM-DD.md (human-readable, appended per run) and
archive/YYYY-MM-DD.json (machine-readable, latest run of the day; /ask reads it).

GitHub (via the Sprites GitHub Connector): the same .md is written with the
Contents API, PUT {GITHUB_GATEWAY_URL}/repos/{GITHUB_REPO}/contents/archive/<date>.md.
The gateway attaches the OAuth token. Disabled unless GITHUB_GATEWAY_URL and
GITHUB_REPO are set, or when dry_run is on.

Dry runs never write here. Production runs also leave state/YYYY-MM-DD.json
(local only, not pushed) so a second /run on the same JST day sends nothing
unless forced.
"""
import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.request

ROOT = os.environ.get("ARCHIVE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "archive")


def state_dir():
    return os.environ.get("STATE_DIR") or os.path.join(os.path.dirname(ROOT), "state")


def paths(run_date):
    return (os.path.join(ROOT, run_date + ".md"), os.path.join(ROOT, run_date + ".json"))


def write_local(run_date, text, quotes, us_session=None):
    os.makedirs(ROOT, exist_ok=True)
    md, js = paths(run_date)
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    new = not os.path.exists(md)
    with open(md, "a", encoding="utf-8") as f:
        if new:
            f.write("# Market brief {}\n".format(run_date))
        f.write("\n## Run {}\n\n```\n{}\n```\n".format(stamp, text))
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"run_date": run_date, "generated_at": stamp, "us_session": us_session,
                   "quotes": [q.to_dict() for q in quotes]}, f, indent=2)
    return md


def _gh(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return e.code, {}, "HTTP %d" % e.code
    except Exception as e:  # noqa: BLE001 - timeouts, DNS, TLS: report, do not raise
        return None, {}, "%s: %s" % (type(e).__name__, e)


def push_github(run_date, dry_run=False):
    gw = os.environ.get("GITHUB_GATEWAY_URL", "").rstrip("/")
    repo = os.environ.get("GITHUB_REPO", "")
    md, _ = paths(run_date)
    rel = "archive/{}.md".format(run_date)
    if not gw or not repo:
        return {"pushed": False, "reason": "not configured", "path": rel}
    url = "{}/repos/{}/contents/{}".format(gw, repo, rel)
    if dry_run:
        return {"pushed": False, "reason": "dry-run", "method": "PUT", "url": url, "path": rel}
    with open(md, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    body = {"message": "brief {}".format(run_date), "content": content}
    status, existing, err = _gh("GET", url)
    if status is None:
        return {"pushed": False, "status": None, "reason": err, "path": rel}
    if status == 200 and existing.get("sha"):
        body["sha"] = existing["sha"]  # update the file for today
    status, _, err = _gh("PUT", url, body)
    out = {"pushed": status in (200, 201), "status": status, "path": rel}
    if err:
        out["reason"] = err
    return out


def record_delivery(run_date, delivery):
    """Add the delivery outcome (reasons included) to today's local .json."""
    _, js = paths(run_date)
    try:
        with open(js, encoding="utf-8") as f:
            data = json.load(f)
        data["delivery"] = delivery
        with open(js, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except (OSError, ValueError):
        pass


def sent_marker(run_date):
    p = os.path.join(state_dir(), run_date + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def mark_sent(run_date, delivery):
    os.makedirs(state_dir(), exist_ok=True)
    p = os.path.join(state_dir(), run_date + ".json")
    prev = sent_marker(run_date) or {"runs": []}
    prev["runs"].append(dict(delivery, at=dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(prev, f, indent=2)


def latest(before=None):
    """Latest archived day (optionally strictly before an ISO date)."""
    if not os.path.isdir(ROOT):
        return None
    days = sorted(f for f in os.listdir(ROOT) if f.endswith(".json")
                  and (before is None or f[:-5] < before))
    if not days:
        return None
    with open(os.path.join(ROOT, days[-1]), encoding="utf-8") as f:
        return json.load(f)
