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

archive/declined.md lists the questions /ask declined (why / will / should ...),
one line each, and is committed through the same connector.
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


def _put_file(rel, local_path, message, dry_run=False):
    """Write one repo file through the GitHub Connector (Contents API)."""
    gw = os.environ.get("GITHUB_GATEWAY_URL", "").rstrip("/")
    repo = os.environ.get("GITHUB_REPO", "")
    if not gw or not repo:
        return {"pushed": False, "reason": "not configured", "path": rel}
    url = "{}/repos/{}/contents/{}".format(gw, repo, rel)
    if dry_run:
        return {"pushed": False, "reason": "dry-run", "method": "PUT", "url": url, "path": rel}
    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    body = {"message": message, "content": content}
    status, existing, err = _gh("GET", url)
    if status is None:
        return {"pushed": False, "status": None, "reason": err, "path": rel}
    if status == 200 and existing.get("sha"):
        body["sha"] = existing["sha"]  # update the existing file
    status, _, err = _gh("PUT", url, body)
    out = {"pushed": status in (200, 201), "status": status, "path": rel}
    if err:
        out["reason"] = err
    return out


def push_github(run_date, dry_run=False):
    md, _ = paths(run_date)
    return _put_file("archive/{}.md".format(run_date), md, "brief {}".format(run_date),
                     dry_run=dry_run)


DECLINED = "declined.md"
DECLINED_MAX_CHARS = 80
# Separate daily caps so strangers on the public page cannot use up the owner's quota.
DECLINED_CAP = {"public": 15, "owner": 10}
DECLINED_PER_DAY = DECLINED_CAP["owner"]
PUBLIC_TAG = "(public) "


def clean_question(q):
    """ASCII letters, digits, spaces and light punctuation only; 80 chars max."""
    kept = "".join(c for c in (q or "") if c.isascii() and (c.isalnum() or c in " ?!.,'&%-/"))
    return " ".join(kept.split())[:DECLINED_MAX_CHARS]


def public_summary(matched, metrics):
    """The fixed form a public refusal is logged as: matched words + indicators only.
    The typed text itself is never stored or committed."""
    words = [clean_question(w) for w in (matched or []) if clean_question(w)][:3] or ["opinion"]
    mets = [clean_question(m) for m in (metrics or []) if clean_question(m)][:4] or ["all"]
    return "{} + {}".format("/".join(words), ", ".join(mets))[:DECLINED_MAX_CHARS]


def declined_lines():
    p = os.path.join(ROOT, DECLINED)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [l for l in f.read().splitlines() if l.startswith("- ")]


def _source(line):
    parts = line.split("  ", 1)
    return "public" if len(parts) == 2 and parts[1].startswith(PUBLIC_TAG) else "owner"


def record_declined(question, dry_run=False, now=None, public=False, matched=None, metrics=None):
    """Append one declined question to archive/declined.md and commit it to GitHub.
    public=True (the open web page): only "matched words + indicators" is written, never
    the typed text. Owner calls (with the run key) may keep the cleaned question.
    Returns the record; never raises. Dry runs and the daily caps write nothing."""
    now = now or dt.datetime.utcnow()
    source = "public" if public else "owner"
    text = public_summary(matched, metrics) if public else (clean_question(question) or "(empty)")
    lines = declined_lines()
    out = {"question": text, "count": len(lines), "source": source}
    if dry_run:
        out.update(recorded=False, reason="dry-run", github={"pushed": False, "reason": "dry-run"})
        return out
    today = now.strftime("%Y-%m-%d")
    used = sum(1 for l in lines if l[2:12] == today and _source(l) == source)
    if used >= DECLINED_CAP[source]:
        out.update(recorded=False, reason="daily cap reached",
                   github={"pushed": False, "reason": "daily cap reached"})
        return out
    p = os.path.join(ROOT, DECLINED)
    entry = (PUBLIC_TAG if public else "") + text
    try:
        os.makedirs(ROOT, exist_ok=True)
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("# Questions Morning Numbers declined\n\n"
                        "It reports stored numbers. It does not explain, predict or advise.\n"
                        "Lines marked (public) came from the open page and keep only the "
                        "matched words and indicators, not what was typed.\n\n")
            f.write("- {}  {}\n".format(now.strftime("%Y-%m-%dT%H:%M:%SZ"), entry))
    except OSError as e:
        out.update(recorded=False, reason="%s: %s" % (type(e).__name__, e),
                   github={"pushed": False, "reason": "local write failed"})
        return out
    out.update(recorded=True, count=len(lines) + 1)
    try:
        out["github"] = _put_file("archive/" + DECLINED, p, "declined: " + entry)
    except Exception as e:  # noqa: BLE001 - the answer must still go out
        out["github"] = {"pushed": False, "reason": "%s: %s" % (type(e).__name__, e)}
    return out


def declined_count():
    return len(declined_lines())


def latest_brief_text():
    """The text of the latest archived brief (last run block of the newest .md), or None."""
    if not os.path.isdir(ROOT):
        return None
    mds = sorted(f for f in os.listdir(ROOT) if f.endswith(".md") and len(f) == 13)
    if not mds:
        return None
    with open(os.path.join(ROOT, mds[-1]), encoding="utf-8") as f:
        body = f.read()
    parts = body.split("```")
    return parts[-2].strip("\n") if len(parts) >= 3 else None


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
    days = _days(before)
    if not days:
        return None
    with open(os.path.join(ROOT, days[-1]), encoding="utf-8") as f:
        return json.load(f)


def _days(before=None):
    return sorted(f for f in os.listdir(ROOT) if f.endswith(".json") and len(f) == 15
                  and (before is None or f[:-5] < before))


def all_days():
    """Every archived day's JSON, oldest first."""
    if not os.path.isdir(ROOT):
        return []
    out = []
    for name in _days():
        try:
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out
