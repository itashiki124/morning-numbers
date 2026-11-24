"""fetch -> build -> notify -> archive, plus the simple /ask answerer."""
import datetime as dt

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


def ask(question):
    """Answer from the latest archived numbers only. No model, no guessing."""
    data = archive.latest()
    if not data:
        return "No archived briefing yet. Call /run first."
    q = (question or "").lower()
    keys = [k for k, words in ALIASES.items() if any(w in q for w in words)] or list(ALIASES)
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
    return "\n".join(out)
