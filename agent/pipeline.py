"""fetch -> build -> notify -> archive, plus the simple /ask answerer."""
import datetime as dt
import json
import os
import re
import threading
import urllib.request

from . import archive, briefing, notify, sources

JST = dt.timezone(dt.timedelta(hours=9))


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 - one channel failing must not stop the other
        return {"error": "%s: %s" % (type(e).__name__, e)}


def run(dry_run=False, run_date=None, force=False):
    """One pass. Dry runs fetch and build only: nothing is sent, pushed or archived.
    A production run happens at most once per JST day unless force=True."""
    run_date = run_date or dt.datetime.now(JST).date().isoformat()
    if not dry_run and not force:
        done = archive.sent_marker(run_date)
        if done:
            return {"run_date": run_date, "skipped": "already ran today; pass force=1 to send again",
                    "previous_runs": done["runs"]}
    quotes = sources.fetch_all()
    prev = archive.latest(before=run_date)
    prev_session = prev.get("us_session") if prev else None
    session = briefing.us_session(quotes)
    text = briefing.build(quotes, run_date, prev_us_session=prev_session)
    result = {
        "run_date": run_date,
        "dry_run": dry_run,
        "us_session": session,
        "no_new_us_session": bool(session and prev_session == session),
        "stale": [q.label for q in quotes if briefing.is_stale(q, session)],
        "briefing": text,
        "missing": [{"label": q.label, "error": q.error} for q in quotes if not q.ok],
    }
    if dry_run:
        result["notify"] = notify.send(text, dry_run=True)
        result["archive"] = {"local": None, "reason": "dry-run: not written",
                             "github": archive.push_github(run_date, dry_run=True)}
        return result
    md = _safe(archive.write_local, run_date, text, quotes, us_session=session)
    result["notify"] = _safe(notify.send, text)
    gh = (_safe(archive.push_github, run_date) if isinstance(md, str)
          else {"pushed": False, "reason": "local archive failed"})
    result["archive"] = {"local": md if isinstance(md, str) else None,
                         "local_error": None if isinstance(md, str) else md.get("error"),
                         "github": gh}
    delivery = {"notify_sent": bool(result["notify"].get("sent")),
                "notify_reason": result["notify"].get("reason") or result["notify"].get("error"),
                "github_pushed": bool(gh.get("pushed")),
                "github_reason": gh.get("reason") or gh.get("error"),
                "forced": force}
    if isinstance(md, str):
        archive.record_delivery(run_date, delivery)
    if delivery["notify_sent"] or delivery["github_pushed"]:
        archive.mark_sent(run_date, delivery)
    result["delivery"] = delivery
    return result


ALIASES = {
    "vix": ["vix", "volatility", "fear"],
    "btc": ["btc", "bitcoin", "crypto"],
    "spx": ["spx", "s&p", "sp500", "s&p500", "s&p 500", "stocks", "equities"],
    "us10y": ["10y", "10-year", "10 year", "treasury", "yield", "rates", "bond"],
}


ALIAS_LABELS = {"vix": "VIX", "btc": "BTC-USD", "spx": "S&P 500", "us10y": "US 10Y"}


DECLINE_LINE = 'I don\'t do "why", "will" or "should". Here is what I have:'

# Questions asking for a reason, a forecast or advice. Matched as whole words.
OPINION_WORDS = ("why", "will", "should", "predict", "prediction", "reason",
                 "forecast", "expect", "tomorrow", "next week", "going to", "buy", "sell",
                 "hold", "advice", "advise", "recommend", "opinion", "think", "worth", "cause",
                 "caused")
WEEK_WORDS = ("this week", "past week", "last week", "week", "7 days", "seven days", "daily")

_declined_lock = threading.Lock()


def _has(q, words):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", q) for w in words)


def is_opinion(question):
    return _has((question or "").lower(), OPINION_WORDS)


def matched_words(question):
    q = (question or "").lower()
    return [w for w in OPINION_WORDS
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", q)]


# Second judge: TypeSafeAI's Jev, a decision model (it returns a probability, not text),
# reached through the Sprites TypeSafeAI Connector. Off unless TYPESAFE_GATEWAY_URL is set.
# It only decides whether to stay silent; it never writes a word of the answer.
JEV_QUESTION = ("Does this question ask for a reason, an explanation, a prediction, "
                "a forecast or investment advice, rather than for a stored number?")
JEV_TIMEOUT = 6


def jev_enabled():
    return bool(os.environ.get("TYPESAFE_GATEWAY_URL")) and os.environ.get("JEV") != "0"


def jev_threshold():
    try:
        return float(os.environ.get("JEV_THRESHOLD", "0.8"))
    except ValueError:
        return 0.8


def _jev_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        # The gateway attaches the real key; this placeholder is what Fly's guide uses.
        "Authorization": "Bearer sprites"})
    with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def jev_judge(question):
    """Probability (0..1) that the question seeks an opinion, or None when Jev is off,
    unreachable or answers in an unexpected shape. Never raises."""
    if not jev_enabled() or not (question or "").strip():
        return None
    gw = os.environ["TYPESAFE_GATEWAY_URL"].rstrip("/")
    body = {"model": os.environ.get("JEV_MODEL", "jev-latest"),
            "state": (question or "")[:300],
            "questions": {"opinion": {"type": "noul", "instructions": JEV_QUESTION}}}
    try:
        p = _jev_post(gw + "/v1/systemone", body)["answers"]["opinion"]["noul"]
        p = float(p)
        return p if 0.0 <= p <= 1.0 else None
    except Exception:  # noqa: BLE001 - Jev down means: fall back to the word list
        return None


def wants_week(question):
    return _has((question or "").lower(), WEEK_WORDS)


def _keys(q):
    return [k for k, words in ALIASES.items() if any(w in q for w in words)] or list(ALIASES)


def _fmt(key, v):
    return "{:,.2f}{}".format(v, "%" if key == "us10y" else "")


def _latest_lines(data, keys):
    out = []
    for item in data["quotes"]:
        if item["key"] not in keys:
            continue
        if item["error"] or item["change_pct"] is None:
            out.append("{}: no data in the {} archive.".format(item["label"], data["run_date"]))
            continue
        direction = "up" if item["value"] > item["prev_value"] else (
            "down" if item["value"] < item["prev_value"] else "flat")
        unit = "%" if item["key"] == "us10y" else ""
        if item["key"] == "us10y":
            chg = "{:+d} bp".format(round((item["value"] - item["prev_value"]) * 100))
        else:
            chg = "{:+.2f}%".format(item["change_pct"])
        s = "{label} was {v:,.2f}{u} on {d}, {dir} from {pv:,.2f}{u} on {pd} ({chg})".format(
            label=item["label"], v=item["value"], u=unit, d=item["date"], dir=direction,
            pv=item["prev_value"], pd=item["prev_date"], chg=chg)
        sess = data.get("us_session")
        if item["key"] in briefing.US_KEYS and sess and item["date"] < sess:
            s += " [not updated: US latest was {}]".format(sess)
        if item.get("check"):
            s += " [{}]".format(item["check"])
        out.append(s + ". Source: {}.".format(item["source"]))
    return out


def _week_lines(days, keys):
    """Stored daily values of the last 7 days, per indicator, from every archived brief."""
    series, labels = {}, {}
    for data in days:  # oldest first, so a later brief wins on the same date
        for item in data.get("quotes", []):
            if item["key"] not in keys or item.get("error"):
                continue
            labels[item["key"]] = item["label"]
            pts = series.setdefault(item["key"], {})
            for d, v in ((item.get("prev_date"), item.get("prev_value")),
                         (item.get("date"), item.get("value"))):
                if d and v is not None:
                    pts[d] = v
    out = []
    for key in ALIASES:
        if key not in keys:
            continue
        pts = series.get(key)
        if not pts:
            out.append("{}: nothing stored this week.".format(labels.get(key, key)))
            continue
        last = dt.date.fromisoformat(max(pts))
        start = (last - dt.timedelta(days=6)).isoformat()
        row = ["{} {}".format(d[5:].replace("-", "/"), _fmt(key, pts[d]))
               for d in sorted(pts) if d >= start]
        out.append("{} this week: {}.".format(labels[key], ", ".join(row)))
    out.append("Stored daily values from {} archived brief(s); dates are the source's own "
               "session dates (BTC: UTC close).".format(len(days)))
    return out


def ask(question, dry_run=False, public=False):
    """Answer from the archived numbers only. No generated words, no guessing.
    Why / will / should questions are declined in one fixed line, logged to
    archive/declined.md (committed to GitHub), and answered with the numbers.
    The word list decides first; if it lets a question through and Jev is configured,
    Jev decides whether the question is opinion-seeking.
    public=True: the log keeps only the matched words and indicators."""
    q = (question or "").lower()
    keys = _keys(q)
    matched = matched_words(q)
    opinion = bool(matched)
    p = None
    if not opinion:
        p = jev_judge(question)
        if p is not None and p >= jev_threshold():
            opinion = True
            matched = ["Jev {:.2f}".format(p)]
    out = []
    rec = None
    if opinion:
        out.append(DECLINE_LINE)
        if p is not None:
            out.append("Declined (Jev: opinion-seeking, p={:.2f}).".format(p))
        metrics = [ALIAS_LABELS[k] for k in keys] if len(keys) < len(ALIASES) else ["all"]
        with _declined_lock:
            rec = archive.record_declined(question, dry_run=dry_run, public=public,
                                          matched=matched, metrics=metrics)
    data = archive.latest()
    if not data:
        out.append("No archived briefing yet. Call /run first.")
    elif wants_week(q):
        out += _week_lines(archive.all_days(), keys)
    else:
        out += _latest_lines(data, keys)
    if rec is not None:
        tail = "Declined so far: {}.".format(rec["count"])
        if not rec.get("recorded"):
            tail += " (not recorded: {})".format(rec.get("reason"))
        out.append(tail)
    return "\n".join(out)
